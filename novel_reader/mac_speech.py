import http.client
import io
import json
import socket
import struct
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from threading import Lock


PROVIDERS = {"zipvoice": "本机 · ZipVoice", "mac": "Mac mini · CosyVoice 3"}
VOICES = {"male_1": "龙修（男）", "male_2": "龙逸尘（男）", "male_3": "龙飞（男）",
          "female_1": "龙白芷（女）", "female_2": "龙安温（女）", "female_3": "龙妙（女）"}
MAX_WAV_BYTES = 16 * 1024 * 1024


def decode_wav(data):
    with wave.open(io.BytesIO(data), "rb") as wav:
        if wav.getparams()[:3] != (1, 2, 24000) or wav.getcomptype() != "NONE":
            raise ValueError("Mac 返回的 WAV 必须为 24 kHz、16 位、单声道 PCM。")
        size = wav.getnframes() * 2
        if not 0 < size <= MAX_WAV_BYTES:
            raise ValueError("Mac 返回的音频长度无效。")
        pcm = wav.readframes(wav.getnframes())
        if len(pcm) != size:
            raise ValueError("Mac 返回的 WAV 不完整，请重新朗读。")
        return pcm


class MacSpeechClient:
    def __init__(self, replies):
        self.replies = replies
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="MacSpeech")
        self.lock = Lock()
        self.token = None
        self.socket = None
        self.future = None

    def submit(self, generation, span, voice):
        if voice not in VOICES:
            raise ValueError("未知的 Mac 音色。")
        token = object()
        with self.lock:
            self.token = token
            self.future = self.executor.submit(self._fetch, token, generation, span, voice)

    def _fetch(self, token, generation, span, voice):
        started = time.perf_counter()
        # Direct connection to this LAN peer. No proxy, redirects or alternate host.
        conn = http.client.HTTPConnection("192.168.3.125", 9880, timeout=4)
        try:
            with self.lock:
                if self.token is not token:
                    return
            conn.connect()
            with self.lock:
                if self.token is not token:
                    return
                self.socket = conn.sock
                conn.sock.settimeout(90)
            body = json.dumps({"input": span.text, "voice": voice}, ensure_ascii=False).encode("utf-8")
            conn.request("POST", "/v1/audio/speech", body, {"Content-Type": "application/json; charset=utf-8"})
            with conn.getresponse() as response:
                if response.status != 200:
                    raise ValueError(f"Mac 语音服务返回 HTTP {response.status}，请检查服务后重试。")
                if response.headers.get_content_type() != "audio/wav":
                    raise ValueError("Mac 语音服务没有返回 WAV 音频。")
                data = response.read(MAX_WAV_BYTES + 1)
                if len(data) > MAX_WAV_BYTES:
                    raise ValueError("Mac 返回的音频过大。")
            pcm = decode_wav(data)
            reply = ("audio", generation, span, pcm, time.perf_counter() - started)
        except ValueError as exc:
            reply = ("error", generation, str(exc))
        except (wave.Error, EOFError, struct.error):
            reply = ("error", generation, "Mac 返回的 WAV 无法解码，请重新朗读。")
        except PermissionError:
            reply = ("error", generation, "Windows 拒绝了听页对 192.168.3.125:9880 的网络访问。请检查局域网访问权限，再点朗读重试。")
        except (OSError, http.client.HTTPException):
            reply = ("error", generation, "无法完成 Mac 语音请求。请检查 192.168.3.125:9880 的局域网连接和服务，再点朗读重试。")
        finally:
            with self.lock:
                self.socket = None
            conn.close()
        with self.lock:
            if self.token is token:
                self.replies.put(reply)

    def cancel(self):
        with self.lock:
            self.token = None
            if self.future is not None:
                self.future.cancel()
            if self.socket is not None:
                try:
                    self.socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def close(self):
        self.cancel()
        self.executor.shutdown(wait=False, cancel_futures=True)
