import multiprocessing as mp
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import patch
from zipfile import ZipFile

from novel_reader.app import Reader
from novel_reader.speech import Passage, sentence_start


class SentenceTests(unittest.TestCase):
    def test_dialogue_and_paragraphs(self):
        text = "第一句。  “你听见了吗？！”她点头。\n  山下有灯，风很轻。"
        for word, beginning in [("句", "第一"), ("听", "“"), ("！”", "“"), ("”", "“"),
                                ("点", "她"), ("很", "山")]:
            with self.subTest(word=word):
                self.assertEqual(sentence_start(text, text.index(word)), text.index(beginning))

    def test_english_decimal_and_end_of_document(self):
        text = "It was 3.14 degrees. Next sentence."
        self.assertEqual(sentence_start(text, text.index("degrees")), 0)
        self.assertEqual(sentence_start(text, len(text)), text.index("Next"))
        self.assertEqual(sentence_start("", 0), 0)


class TextSelectionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tk.Tk()
        self.root = root
        with patch.object(mp.get_context("spawn").Process, "start"):
            self.reader = Reader(root, Path(tmp.name) / "progress.sqlite3")
        root.after_cancel(self.reader.timer)
        self.addCleanup(self.close)
        self.text = "第一句。明月走到河边。她看见了小船。最后一句。"
        book = Path(tmp.name) / "小船.epub"
        with ZipFile(book, "w") as z:
            z.writestr("META-INF/container.xml", '<container><rootfile full-path="book.opf"/></container>')
            z.writestr("book.opf", '<package><metadata><title>小船</title></metadata><manifest>'
                       '<item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>'
                       '<item id="b" href="b.xhtml" media-type="application/xhtml+xml"/></manifest>'
                       '<spine><itemref idref="a"/><itemref idref="b"/></spine></package>')
            z.writestr("a.xhtml", "<html><body><p>" + self.text + "</p></body></html>")
            z.writestr("b.xhtml", "<html><body><p>又是一天。</p></body></html>")
        self.reader.open_book(book)
        self.body = self.reader.body
        self.event_time = 1000
        root.update()

    def close(self):
        with patch.object(self.reader.worker, "join"):
            self.reader.close()
        self.reader.requests.close()
        self.reader.replies.close()

    def mouse(self, action, offset, **kw):
        x, y, width, height = self.body.bbox(f"1.0+{offset}c")
        self.event_time += 20
        self.body.event_generate(action, x=x + 1, y=y + height // 2, time=self.event_time, **kw)
        self.root.update()

    def drag(self, a, b):
        self.event_time += 1000
        self.mouse("<ButtonPress-1>", a)
        self.mouse("<B1-Motion>", b)
        self.mouse("<ButtonRelease-1>", b)

    def menu_play(self, offset):
        with patch.object(self.reader.context_menu, "tk_popup") as popup:
            self.mouse("<ButtonPress-3>", offset)
            popup.assert_called_once()
        self.reader.context_menu.invoke(0)
        self.assertTrue(self.reader.active)
        return self.reader.position

    def test_reverse_selection_menu_and_playback_highlight(self):
        start = self.text.index("走")
        end = self.text.index("船")
        self.drag(end, start)
        selected = self.text[start:end]
        self.assertEqual(self.body.get("sel.first", "sel.last"), selected)
        self.assertEqual(self.menu_play(self.text.index("看")), (0, self.text.index("明")))
        self.reader.highlight(Passage(0, 4, 11, self.text[4:11]))
        self.assertEqual(self.body.get("sel.first", "sel.last"), selected)
        self.assertEqual(self.body.get("playing.first", "playing.last"), self.text[4:11])
        tags = self.body.tag_names("sel.first")
        self.assertGreater(tags.index("sel"), tags.index("playing"))
        self.reader.highlight(Passage(1, 0, 4, "又是一天"))
        self.assertEqual(self.body.get("sel.first", "sel.last"), selected)
        self.assertEqual(self.menu_play(self.text.index("后")), (0, self.text.index("最")))

    def test_plain_right_click_and_forward_drag(self):
        self.assertEqual(self.menu_play(self.text.index("边")), (0, self.text.index("明")))
        self.drag(self.text.index("走"), self.text.index("河"))
        self.assertEqual(self.body.get("sel.first", "sel.last"), "走到")
        # The right edge of a selection is exclusive.
        self.drag(self.text.index("走"), self.text.index("她"))
        self.assertEqual(self.body.index("sel.last"), self.body.index(f"1.0+{self.text.index('她')}c"))
        self.assertEqual(self.menu_play(self.text.index("她")), (0, self.text.index("她")))

    def test_changing_quality_restarts_at_audible_passage_and_preserves_pause(self):
        app = self.reader
        app.start((0, 0))
        app.highlight(Passage(0, 4, 11, self.text[4:11]))
        app.toggle()
        app.voice_mode.set("精细 · 8步")
        app.change_voice_mode()
        self.assertTrue(app.paused)
        self.assertEqual(app.position, (0, 4))
        app.ready = True
        app.poll()
        generation, span, steps = app.requests.get(timeout=1)
        self.assertEqual((generation, span.start, steps), (app.epoch.value, 4, 8))
        self.assertEqual(app.db.execute("SELECT value FROM preferences WHERE name='voice_mode'").fetchone()[0], "精细 · 8步")

    def test_short_opening_waits_for_second_and_seek_discards_it(self):
        app = self.reader
        app.ready = True
        with patch.object(app, "replies", Queue()), patch.object(app.player, "append") as play:
            app.start((0, 0))
            app.poll()
            gen, first, _ = app.requests.get(timeout=1)
            short = bytes(48000)
            app.replies.put(("audio", gen, first, short, 1.2))
            app.poll()
            _, second, _ = app.requests.get(timeout=1)
            play.assert_not_called()
            app.replies.put(("audio", gen, second, short, 1.2))
            app.poll()
            self.assertEqual([call.args[0] for call in play.call_args_list], [first, second])

            app.start((0, 0))
            app.poll()
            gen, first, _ = app.requests.get(timeout=1)
            app.replies.put(("audio", gen, first, short, 1.2))
            app.poll()
            self.assertIsNotNone(app.opening_audio)
            _, stale, _ = app.requests.get(timeout=1)
            app.start((1, 0))
            app.replies.put(("audio", gen, stale, short, 1.2))
            app.poll()
            gen, last, _ = app.requests.get(timeout=1)
            app.replies.put(("audio", gen, last, short, 1.2))
            app.poll()
            self.assertEqual(play.call_args.args[0], last)
            self.assertEqual(play.call_count, 3)
