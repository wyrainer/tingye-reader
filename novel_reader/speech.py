import os
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from queue import SimpleQueue
from threading import Lock


RATE = 24000
MODEL = "zipvoice-distill-fp32"

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
        if end - start >= minimum:
            penalty = 0 if text[match.start()] in _STRONG else 5
            candidates.append((abs(end - start - target) + penalty, end, penalty == 0))
    if newline >= 0 or stop == len(text):
        candidates.append((abs(stop - start - target), stop, True))
    if candidates:
        _, end, strong = min(candidates)
        if strong:
            return end
        # Include a tiny tail when its natural boundary is only a few letters away.
        return max((point for _, point, terminal in candidates if terminal and end < point <= end + 5), default=end)

    # A long clause with no punctuation: use whitespace where available.
    end = start + target
    spaces = [i for i in range(start + minimum, stop) if text[i].isspace()]
    if spaces:
        return min(spaces, key=lambda i: abs(i - end))
    while end < stop and text[end - 1].isascii() and text[end - 1].isalnum() and text[end].isascii() and text[end].isalnum():
        end += 1
    return end


def next_passage(book, position, limit, first=False):
    doc, start = position
    while doc < len(book.docs):
        text, _ = book.document(doc)
        while start < len(text) and not text[start].isalnum():
            start += 1
        if start >= len(text):
            doc, start = doc + 1, 0
            continue
        end = passage_end(text, start, limit, first)
        return Passage(doc, start, end, text[start:end])
    return None


def synthesis_worker(model_dir, requests, replies, epoch):
    try:
        import numpy as np
        import sherpa_onnx as sherpa
        import soundfile as sf

        os.chdir(model_dir)
        files = Path(MODEL)
        required = [files / name for name in ("text_encoder.onnx", "fm_decoder.onnx", "tokens.txt", "lexicon.txt")]
        reference = Path("reader-reference-distill-zh.wav")
        for path in [*required, Path("vocos_24khz.onnx"), Path("lexicon-en-us.txt"), reference]:
            if not path.is_file():
                raise FileNotFoundError(f"缺少离线文件 {path}")
        model = sherpa.OfflineTtsZipvoiceModelConfig(
            encoder=str(required[0]), decoder=str(required[1]), tokens=str(required[2]),
            lexicon=f"{required[3]},lexicon-en-us.txt", vocoder="vocos_24khz.onnx", guidance_scale=3.0,
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
        voice.extra = {"min_words_in_sentence": "22", "max_words_in_sentence": "100", "step_progress": "1"}
        replies.put(("ready",))
    except Exception as exc:
        replies.put(("fatal", str(exc)))
        return
    while True:
        request = requests.get()
        if request is None:
            break
        generation, span, steps = request
        if generation != epoch.value:
            continue
        try:
            voice.num_steps = steps
            started = time.perf_counter()
            result = engine.generate(spoken_text(span.text), voice, lambda samples, progress: int(epoch.value == generation))
            if epoch.value != generation:
                continue
            samples = np.asarray(result.samples, dtype=np.float32)
            if not len(samples):
                replies.put(("unvoiced", generation))
                continue
            if result.sample_rate != RATE or not np.isfinite(samples).all():
                raise ValueError("该片段未生成有效语音。")
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
