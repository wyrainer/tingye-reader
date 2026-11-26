import unittest
from unittest.mock import patch

from novel_reader.prosody import ChineseProsody
from novel_reader.segments import Passage
from novel_reader.speech import prepare_passage


class ProsodyTests(unittest.TestCase):
    def test_original_offsets_and_punctuation(self):
        frontend = ChineseProsody.__new__(ChineseProsody)
        text = "公羊沐中午，3.14 PM。"
        with patch.object(frontend, "boundaries", return_value={3: 2, 4: 1}):
            self.assertEqual(frontend.prepare(text), "公羊沐，中午，3.14 PM。")

    def test_predict_once_for_long_unpunctuated_passage(self):
        text = "甲" * 90
        frontend = ChineseProsody.__new__(ChineseProsody)
        with patch.object(frontend, "boundaries", return_value={3: 2, 45: 3}) as predict:
            span, marked = prepare_passage(Passage(0, 0, len(text), text), frontend)
            predict.assert_called_once_with(text)
        self.assertEqual(span.end, 45)
        self.assertEqual(marked, "甲" * 3 + "，" + "甲" * 42 + "，")
