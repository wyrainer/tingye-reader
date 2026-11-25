import os
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from queue import SimpleQueue
from threading import Lock

from .model_paths import MODEL, missing_files


RATE = 24000

# Closing quotes and brackets belong to the sentence they close. A period
# inside a number or an English word is not a sentence terminator.
_SENTENCE_END = re.compile(
    r"""(?:[。！？!?]+|(?:\.+|…+)(?=[\s”’」』）》】)\]"']|$))[”’」』）》】)\]"']*"""
)


def sentence_start(text, offset):
    if not text:
        return 0
    offset = max(0, min(offset, len(text) - 1))
    start = text.rfind("\n", 0, offset) + 1
    for match in _SENTENCE_END.finditer(text, start):
        if match.end() > offset:
            break
        start = match.end()
        if start == offset:
            break
    while start < len(text) and text[start].isspace():
        start += 1
    return start


def spoken_text(text):
    digits = "零一二三四五六七八九"

    def number(match):
        raw = match[0]
        tail = text[match.end():match.end() + 1]
        if len(raw) > 4 or raw.startswith("0") or tail == "年" or "." in raw:
            return "".join("点" if ch == "." else digits[int(ch)] for ch in raw)
        words = ""
        for index, ch in enumerate(raw):
            place = len(raw) - index - 1
            if ch != "0":
                words += digits[int(ch)] + ("", "十", "百", "千")[place]
            elif not words.endswith("零"):
                words += "零"
        return re.sub("^一十", "十", words.rstrip("零"))

    return re.sub(r"\d+(?:\.\d+)?", number, text)


@dataclass(frozen=True)
class Passage:
    doc: int
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class VoiceSettings:
    steps: int = 4
    speed: float = 1.0
    guidance: float = 3.0
    softness: int = 0

    def __post_init__(self):
        if (self.steps not in (4, 8) or not .75 <= self.speed <= 1.25
                or not 1 <= self.guidance <= 4 or not 0 <= self.softness <= 100):
            raise ValueError("语音参数超出可调范围。")


class Pacing:
    def __init__(self):
        self.characters_per_second = 5.0
        self.compute_ratio = 0.7
        self.measured = False

    def observe(self, count, audio_seconds, compute_seconds):
        weight = .35 if self.measured else 1.0
        self.characters_per_second += weight * (count / audio_seconds - self.characters_per_second)
        self.compute_ratio += weight * (compute_seconds / audio_seconds - self.compute_ratio)
        self.measured = True

    @property
    def buffer_goal(self):
        return min(25, max(10, 16 * self.compute_ratio))

    def limit(self, first, buffered):
        if first:
            return max(16, min(26, round(self.characters_per_second * 4.2)))
        # With little audio left, prefer quick results. With sufficient runway,
        # amortize model overhead across a longer phrase.
        seconds = max(2.8, min(10, .76 * buffered / max(.15, self.compute_ratio)))
        return max(14, min(65, round(self.characters_per_second * seconds)))

    def opening_delay(self, prepared_seconds, upcoming):
        # A stand-alone heading provides almost no runway. Start it as soon
        # as the following audio arrives. EOF always releases it immediately.
        if prepared_seconds < 1.8:
            return float("inf")
        estimate = len(upcoming.text) / self.characters_per_second * self.compute_ratio
        return max(0.0, 1.2 * estimate + .35 - prepared_seconds)


_PAUSE = re.compile(r"""(?:[。！？!?；;]+|…+|\.+(?=\s|$|[”’」』）》"'])|[，、：]|(?<!\d)[,:]|[,:](?!\d))[”’」』）》】)\]"']*""")
_STRONG = "。！？!?；;.…"
_DIALOGUE = re.compile(r"""[ \t\u200b\ufeff]*[“‘「『"]""")
_ASCII_WORD = re.compile(r"[A-Za-z0-9]+(?:[.'’,-][A-Za-z0-9]+)*")


def nearest_word_end(text, start, target):
    import logging
    import jieba

    # Tokenize only the local context. The offsets refer to the original EPUB.
    jieba.setLogLevel(logging.WARNING)
    window = text[start:start + target + 32]
    for token in _ASCII_WORD.finditer(text, start, start + target):
        if token.end() == start + target:
            full = _ASCII_WORD.match(text, token.start())
            return full.end() if token.start() == start else min(
                (full.end(), token.start()), key=lambda i: abs(i - start - target))
    for _, left, right in jieba.tokenize(window):
        if right >= target:
            return start + (left if left and target - left < right - target else right)
    return start + len(window)


def passage_end(text, start, target, first):
    # Search beyond the target for a natural boundary before splitting a word.
    ceiling = min(len(text), start + target + (18 if first else 24))
    newline = text.find("\n", start, ceiling + 1)
    stop = newline if newline >= 0 else ceiling
    minimum = min(14, max(9, round(target * .6)))
    candidates = []
    for match in _PAUSE.finditer(text, start, min(len(text), stop + 8)):
        if match.start() >= stop:
            break
        end = match.end()
        if text[match.start()] in "：:" and _DIALOGUE.match(text, match.start() + 1):
            continue
        if end - start >= minimum:
            penalty = 0 if text[match.start()] in _STRONG else (9 if first else 17)
            candidates.append((abs(end - start - target) + penalty, end, penalty == 0))
    if newline >= 0 or stop == len(text):
        candidates.append((abs(stop - start - target), stop, True))
    if candidates:
        _, end, strong = min(candidates)
        if strong:
            return end
        # Include a tiny tail when its natural boundary is only a few letters away.
        return max((point for _, point, terminal in candidates if terminal and end < point <= end + 5), default=end)

    # Keep explicit spaces, dictionary words and Latin tokens intact on long clauses.
    spaces = [i for i in range(start + minimum, stop) if text[i].isspace()]
    if spaces:
        return min(spaces, key=lambda i: abs(i - (start + target)))
    return nearest_word_end(text, start, target)


def next_passage(book, position, limit, first=False):
    doc, start = position
    while doc < len(book.docs):
        text, _ = book.document(doc)
        while start < len(text) and not text[start].isalnum() and text[start] not in "“‘「『\"":
            start += 1
        if start >= len(text):
            doc, start = doc + 1, 0
            continue
        end = passage_end(text, start, limit, first)
        if not any(c.isalnum() for c in text[start:end]):
            start = end
            continue
        return Passage(doc, start, end, text[start:end])
    return None


def synthesis_worker(model_dir, requests, replies, epoch):
    try:
        import numpy as np
        import sherpa_onnx as sherpa
        import soundfile as sf

        missing = missing_files(Path(model_dir))
        if missing:
            raise FileNotFoundError(f"模型文件缺失：{missing[0]}")
        os.chdir(model_dir)
        files = Path(MODEL)
        reference = Path("reader-reference-distill-zh.wav")
        model = sherpa.OfflineTtsZipvoiceModelConfig(
            encoder=str(files / "text_encoder.onnx"), decoder=str(files / "fm_decoder.onnx"),
            tokens=str(files / "tokens.txt"), lexicon=f"{files / 'lexicon.txt'},lexicon-en-us.txt",
            vocoder="vocos_24khz.onnx", guidance_scale=3.0,
        )
        config = sherpa.OfflineTtsConfig(model=sherpa.OfflineTtsModelConfig(
            zipvoice=model, num_threads=4, provider="cpu"))
        if not config.validate():
            raise ValueError("本地模型配置无效。")
        engine = sherpa.OfflineTts(config)
        prompt, rate = sf.read(reference, dtype="float32")
        voice = sherpa.GenerationConfig()
        voice.reference_audio, voice.reference_sample_rate = prompt, rate
        voice.reference_text = "乍看之下，一切的困扰仿佛都围绕在我身边。"
        voice.silence_scale = 1.0
        replies.put(("ready",))
    except Exception as exc:
        replies.put(("fatal", str(exc)))
        return
    while True:
        request = requests.get()
        if request is None:
            break
        generation, span, settings = request
        if generation != epoch.value:
            continue
        try:
            voice.num_steps = settings.steps
            voice.speed = settings.speed
            utterance = spoken_text(span.text)
            # Keep each app-selected passage together through the engine's
            # punctuation split/merge, including its closing quotation marks.
            unit_limit = str(min(160, len(utterance) + 1))
            voice.extra = {"min_words_in_sentence": unit_limit, "max_words_in_sentence": unit_limit,
                           "step_progress": "1", "guidance_scale": str(settings.guidance)}
            started = time.perf_counter()
            result = engine.generate(utterance, voice, lambda samples, progress: int(epoch.value == generation))
            if epoch.value != generation:
                continue
            samples = np.asarray(result.samples, dtype=np.float32)
            if not len(samples):
                replies.put(("unvoiced", generation))
                continue
            if result.sample_rate != RATE or not np.isfinite(samples).all():
                raise ValueError("该片段未生成有效语音。")
            if settings.softness:
                # Blend with a symmetric low-pass filter, preserving length and timing.
                padded = np.pad(samples, 1, mode="edge")
                amount = settings.softness / 400
                samples = (1 - 2 * amount) * samples + amount * (padded[:-2] + padded[2:])
            # Preserve peak shape and leave headroom when converting Vocos output to PCM.
            gain = .94 / max(1.0, float(np.max(np.abs(samples))))
            pcm = (samples * (gain * 32767)).astype("<i2").tobytes()
            replies.put(("audio", generation, span, pcm, time.perf_counter() - started))
        except Exception as exc:
            replies.put(("error", generation, str(exc)))


class AudioPlayer:
    def __init__(self):
        self.stream = None
        self.lock = Lock()
        self.blocks = deque()
        self.events = SimpleQueue()
        self.offset = 0
        self.remaining = 0
        self.last_dac = 0.0
        self.paused = False

    def append(self, span, data):
        if self.stream is None:
            import sounddevice
            self.stream = sounddevice.RawOutputStream(
                samplerate=RATE, channels=1, dtype="int16", blocksize=480,
                latency="low", callback=self.render)
            self.stream.start()
        with self.lock:
            self.blocks.append((span, data))
            self.remaining += len(data)

    def render(self, out, frames, clock, status):
        out[:] = bytes(len(out))
        dac = time.monotonic() + max(0, clock.outputBufferDacTime - clock.currentTime)
        written = 0
        with self.lock:
            while self.blocks and not self.paused and written < len(out):
                span, data = self.blocks[0]
                if self.offset == 0:
                    self.events.put((dac + written / (2 * RATE), span))
                count = min(len(out) - written, len(data) - self.offset)
                out[written:written + count] = data[self.offset:self.offset + count]
                self.offset += count
                written += count
                self.remaining -= count
                if self.offset == len(data):
                    self.blocks.popleft()
                    self.offset = 0
            if written:
                self.last_dac = dac + written / (2 * RATE)

    def buffered(self):
        with self.lock:
            return self.remaining / (2 * RATE) + max(0, self.last_dac - time.monotonic())

    def reset(self):
        if self.stream is not None:
            try:
                self.stream.abort()
            finally:
                self.stream.close()
                self.stream = None
        with self.lock:
            self.blocks.clear()
            self.offset = self.remaining = 0
            self.last_dac = 0.0
            self.paused = False
            self.events = SimpleQueue()
