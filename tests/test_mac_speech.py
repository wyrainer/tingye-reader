import io
import json
import unittest
import wave
from queue import Queue
from threading import Event
from unittest.mock import patch

from novel_reader.mac_speech import MacSpeechClient, VOICES, decode_wav
from novel_reader.segments import Passage


def make_wav(rate=24000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", ""))
        wav.writeframes(b"\x02\x00" * 48)
    return buffer.getvalue()


class MacSpeechTests(unittest.TestCase):
    def setUp(self):
        self.replies = Queue()
        self.client = MacSpeechClient(self.replies)
        self.span = Passage(0, 0, 7, "公羊沐中午，")
        self.connection = self.enterContext(patch("novel_reader.mac_speech.http.client.HTTPConnection"))
        self.conn = self.connection.return_value
        self.response = self.conn.getresponse.return_value.__enter__.return_value
        self.response.status = 200
        self.response.headers.get_content_type.return_value = "audio/wav"
        self.response.read.return_value = make_wav()
        self.addCleanup(self.client.close)

    def test_six_voices_use_original_text_and_one_lan_path(self):
        expected = {"龙修（男）": "male_1", "龙逸尘（男）": "male_2", "龙飞（男）": "male_3",
                    "龙白芷（女）": "female_1", "龙安温（女）": "female_2", "龙妙（女）": "female_3"}
        self.assertEqual({name: key for key, name in VOICES.items()}, expected)
        for voice in expected.values():
            self.client.submit(4, self.span, voice)
            kind, epoch, span, pcm, _ = self.replies.get(timeout=3)
            self.assertEqual((kind, epoch, span, pcm), ("audio", 4, self.span, b"\x02\x00" * 48))
            self.connection.assert_called_with("192.168.3.125", 9880, timeout=4)
            method, path, body, headers = self.conn.request.call_args.args
            self.assertEqual((method, path), ("POST", "/v1/audio/speech"))
            self.assertEqual(json.loads(body.decode("utf-8")), {"input": self.span.text, "voice": voice})
            self.assertEqual(headers, {"Content-Type": "application/json; charset=utf-8"})

    def test_redirect_or_server_error_is_not_followed(self):
        for status in (307, 500):
            self.response.status = status
            self.client.submit(1, self.span, "male_1")
            kind, _, message = self.replies.get(timeout=3)
            self.assertEqual(kind, "error")
            self.assertIn(str(status), message)
            self.assertNotIn(self.span.text, message)
        self.assertEqual(self.conn.request.call_count, 2)
        self.response.read.assert_not_called()

    def test_invalid_audio_reports_error(self):
        self.response.read.return_value = b"RIFF"
        self.client.submit(1, self.span, "male_1")
        self.assertEqual(self.replies.get(timeout=3)[0], "error")
        for data in (make_wav(48000), make_wav()[:-8]):
            with self.assertRaises(ValueError):
                decode_wav(data)

    def test_completed_cancelled_audio_is_discarded(self):
        reading, release = Event(), Event()
        def blocked_read(limit):
            reading.set()
            release.wait(3)
            return make_wav()
        self.response.read.side_effect = blocked_read
        self.client.submit(1, self.span, "male_1")
        self.assertTrue(reading.wait(3))
        self.client.cancel()
        self.conn.sock.shutdown.assert_called_once()
        self.client.submit(2, self.span, "female_1")
        release.set()
        self.client.future.result(timeout=3)
        self.assertEqual(self.replies.get_nowait()[:2], ("audio", 2))
        self.assertTrue(self.replies.empty())
