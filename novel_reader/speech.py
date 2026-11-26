import os
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from queue import SimpleQueue
from threading import Lock

from .model_paths import MODEL, missing_files
from .mac_speech import PROVIDERS, VOICES
from .prosody import ChineseProsody, HAN, WETTS_PACK
from .segments import DEFAULT_TARGET, MAX_TARGET, MIN_TARGET, SegmentError, choose_cut, spoken_count


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
class VoiceSettings:
    steps: int = 4
    speed: float = 1.0
    guidance: float = 3.0
    softness: int = 0
    use_prosody: bool = True
    provider: str = "zipvoice"
    mac_voice: str = "male_1"

    def __post_init__(self):
        if (self.steps not in (4, 8) or not .75 <= self.speed <= 1.25
                or not 1 <= self.guidance <= 4 or not 0 <= self.softness <= 100):
            raise ValueError("语音参数超出可调范围。")
        if self.provider not in PROVIDERS or self.mac_voice not in VOICES:
            raise ValueError("朗读服务或音色无效。")


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

    def target_length(self, buffered, *, opening=False):
        # Leave a little runway for scheduling and audio output. A cold start
        # gets a moderate first sentence even though its buffer is empty.
        if opening:
            return MIN_TARGET
        affordable = max(0, buffered - .4) * self.characters_per_second / max(.05, self.compute_ratio)
        return max(MIN_TARGET, min(MAX_TARGET, round(.85 * affordable)))

    def opening_delay(self, prepared_seconds, upcoming, target=DEFAULT_TARGET):
        if prepared_seconds < 1.8:
            return float("inf")
        end, _ = choose_cut(upcoming.text, getattr(upcoming, "more", False), target)
        estimate = spoken_count(upcoming.text[:end]) / self.characters_per_second * self.compute_ratio
        return max(0.0, 1.2 * estimate + .35 - prepared_seconds)


def prepare_passage(window, frontend, target=DEFAULT_TARGET):
    predicted = None

    def predict(text):
        nonlocal predicted
        predicted = frontend.boundaries(text)
        return predicted

    end, added = choose_cut(window.text, window.more, target, predict=predict if frontend else None)
    span = window.prefix(end)
    text = frontend.prepare(span.text, predicted) if frontend is not None else span.text
    return span, text + ("，" if added else "")


def synthesis_worker(model_dir, requests, replies, epoch):
    try:
        import numpy as np
        import sherpa_onnx as sherpa
        import soundfile as sf

        missing = missing_files(Path(model_dir))
        if missing:
            raise FileNotFoundError(f"模型文件缺失：{missing[0]}")
        frontend_dir = Path(model_dir).resolve().parent / WETTS_PACK
        frontend = None
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
        generation, span, settings, target = request
        if generation != epoch.value:
            continue
        try:
            voice.num_steps = settings.steps
            voice.speed = settings.speed
            started = time.perf_counter()
            if settings.use_prosody and HAN.search(span.text):
                if frontend is None:
                    try:
                        frontend = ChineseProsody(frontend_dir)
                    except Exception as exc:
                        raise RuntimeError(f"WeTTS 加载失败。请检查 {frontend_dir}，"
                                           f"或在语音调节中关闭自动断句。{exc}") from exc
            span, text = prepare_passage(span, frontend if settings.use_prosody else None, target)
            if epoch.value != generation:
                continue
            utterance = spoken_text(text)
            # Keep each app-selected passage together through the engine's
            # punctuation split/merge, including its closing quotation marks.
            unit_limit = str(min(160, len(utterance) + 1))
            voice.extra = {"min_words_in_sentence": unit_limit, "max_words_in_sentence": unit_limit,
                           "step_progress": "1", "guidance_scale": str(settings.guidance)}
            result = engine.generate(utterance, voice, lambda samples, progress: int(epoch.value == generation))
            if epoch.value != generation:
                continue
            samples = np.asarray(result.samples, dtype=np.float32)
            if not len(samples):
                replies.put(("unvoiced", generation, span))
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
        except SegmentError as exc:
            replies.put(("segment_error", generation, str(exc)))
        except Exception as exc:
            replies.put(("error", generation, str(exc)))


class AudioPlayer:
    def __init__(self):
        self.volume = 100
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
                volume = self.volume
                if volume == 0:
                    out[:written] = bytes(written)
                elif volume < 100:
                    samples = memoryview(out)[:written].cast("h")
                    gain = (volume / 100) ** 2
                    for i, sample in enumerate(samples):
                        samples[i] = round(sample * gain)

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
