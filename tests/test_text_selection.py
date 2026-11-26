import multiprocessing as mp
import sqlite3
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from contextlib import closing
from pathlib import Path
from queue import Queue
from unittest.mock import patch
from zipfile import ZipFile

from novel_reader.app import Reader
from novel_reader.segments import Passage
from novel_reader.speech import VoiceSettings, sentence_start


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
        with (patch.object(mp.get_context("spawn").Process, "start"),
              patch("novel_reader.app.locate_models", return_value=Path(tmp.name))):
            self.reader = Reader(root, Path(tmp.name) / "progress.sqlite3")
        root.after_cancel(self.reader.timer)
        self.addCleanup(self.close)
        self.text = "第一句。明月走到河边。她看见了小船。最后一句。"
        self.fixture_dir = Path(tmp.name)
        self.open_fixture()
        self.body = self.reader.body
        self.event_time = 1000
        root.update()

    def open_fixture(self, long_chapter=False):
        book = self.fixture_dir / ("分页.epub" if long_chapter else "小船.epub")
        with ZipFile(book, "w") as z:
            z.writestr("META-INF/container.xml", '<container><rootfile full-path="book.opf"/></container>')
            z.writestr("book.opf", '<package><metadata><title>小船</title></metadata><manifest>'
                       '<item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>'
                       '<item id="b" href="b.xhtml" media-type="application/xhtml+xml"/></manifest>'
                       '<spine><itemref idref="a"/><itemref idref="b"/></spine></package>')
            extra = "<p>" + "沿着小河往前走。" * 180 + "</p><p>到了。</p>" if long_chapter else ""
            z.writestr("a.xhtml", f"<html><body><p>{self.text}</p>{extra}</body></html>")
            z.writestr("b.xhtml", "<html><body><p>又是一天。</p></body></html>")
        self.reader.open_book(book)
        self.root.update_idletasks()

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

    def test_recent_menu_reorders_limits_and_handles_missing_files(self):
        app = self.reader
        menu = app.recent_menu
        source = self.fixture_dir / "小船.epub"
        contents = source.read_bytes()
        files = []
        for i in range(11):
            path = self.fixture_dir / f"测试{i}.epub"
            path.write_bytes(contents)
            app.open_book(path)
            files.append(path)
        app.populate_recent_menu()
        self.assertEqual([menu.entrycget(i, "label") for i in range(menu.index("end") + 1)],
                         [p.name for p in files[-1:0:-1]])
        menu.invoke(3)
        self.assertEqual(app.book.path, files[7])
        app.populate_recent_menu()
        self.assertEqual(menu.entrycget(0, "label"), files[7].name)
        self.assertEqual(menu.index("end"), 9)
        with closing(sqlite3.connect(self.fixture_dir / "progress.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT path FROM recent_files ORDER BY id DESC LIMIT 1").fetchone(),
                             (str(files[7]),))
        files[10].unlink()
        app.start((0, 0))
        with patch("novel_reader.app.messagebox.showwarning") as notice:
            menu.invoke(1)
        self.assertIn(str(files[10]), notice.call_args.args[1])
        self.assertEqual(app.book.path, files[7])
        self.assertTrue(app.active)

    def test_voice_panel_centers_again_after_reader_moves(self):
        for x, y in ((30, 35), (120, 95)):
            self.root.geometry(f"720x500+{x}+{y}")
            self.root.update()
            self.reader.open_voice_panel()
            self.root.update_idletasks()
            dialog = self.reader.voice_panel
            for origin, extent in (("winfo_rootx", "winfo_width"), ("winfo_rooty", "winfo_height")):
                def center(widget):
                    return getattr(widget, origin)() + getattr(widget, extent)() / 2
                self.assertAlmostEqual(center(dialog), center(self.root), delta=3)
            self.reader.close_voice_panel()

    def test_comboboxes_keep_visible_values_after_popup_selection(self):
        app = self.reader
        style = ttk.Style(self.root)
        for _ in range(2):
            app.open_voice_panel()
            panel = app.voice_panel
            self.root.update()
            def select(box, index):
                box.tk.call("ttk::combobox::Post", box)
                lb = f"{box}.popdown.f.l"
                box.tk.call(lb, "selection", "clear", 0, "end")
                box.tk.call(lb, "selection", "set", index)
                box.tk.call("ttk::combobox::LBSelected", lb)
                self.root.update()
            select(panel.provider_box, 1)
            select(panel.voice_box, 3)
            select(panel.provider_box, 0)
            select(panel.provider_box, 1)
            self.assertEqual(panel.values().mac_voice, "female_1")
            self.assertEqual(panel.provider_box.get(), "Mac mini · CosyVoice 3")
            self.assertEqual(panel.voice_box.get(), "龙白芷（女）")
            for box in (panel.voice_box, panel.provider_box):
                self.assertTrue(box.winfo_ismapped())
                palette = panel.colors
                for state in (("readonly",), ("readonly", "focus")):
                    self.assertEqual(style.lookup("TCombobox", "foreground", state), palette.text)
                    self.assertEqual(style.lookup("TCombobox", "fieldbackground", state), palette.control)
            app.close_voice_panel()
            app.toggle_appearance()

    def test_volume_drag_changes_queued_pcm_and_keeps_playback_position(self):
        from array import array
        from types import SimpleNamespace

        app = self.reader
        slider = app.volume_slider
        span = Passage(0, 0, 1, "声")
        app.player.blocks.append((span, array("h", [12000, -12000] * 3).tobytes()))
        app.player.remaining = 12
        clock = SimpleNamespace(outputBufferDacTime=0, currentTime=0)
        for fraction, percent, expected in ((.5, 50, 3000), (0, 0, 0), (1, 100, 12000)):
            x = round(12 + (slider.winfo_width() - 24) * fraction)
            slider.event_generate("<ButtonPress-1>", x=x, y=12)
            slider.event_generate("<B1-Motion>", x=x, y=12)
            self.root.update()
            self.assertEqual(app.volume_text.get(), f"{percent}%")
            out = bytearray(4)
            app.player.render(out, 2, clock, None)
            self.assertEqual(list(memoryview(out).cast("h")), [expected, -expected])
        self.assertEqual(app.player.remaining, 0)
        self.assertEqual(app.player.events.qsize(), 1)

    def test_audio_without_a_reading_session_does_not_touch_book_or_bookmark(self):
        app = self.reader
        with patch.object(app, "book", None), patch.object(app, "identity", None):
            app.player.events.put((0, Passage(-1, 0, 1, "声")))
            app.poll()
        self.assertIsNone(app.audible)
        self.assertEqual(app.db.execute("SELECT count(*) FROM progress").fetchone()[0], 0)

    def test_theme_switch_preserves_reading_and_step_controls(self):
        app = self.reader
        header = app.theme_button.master
        self.assertNotIn("TCombobox", [w.winfo_class() for w in header.winfo_children()])
        voice_button = next(w for w in header.winfo_children()
                            if isinstance(w, ttk.Button) and w.cget("text") == "语音调节…")
        self.assertLess(app.theme_button.winfo_x(), voice_button.winfo_x())
        app.start((0, 0))
        app.highlight(Passage(0, 4, 11, self.text[4:11]))
        self.drag(6, 9)
        initial = self.body.cget("background")
        epoch = app.epoch.value
        for theme in ("dark", "light"):
            app.theme_button.invoke()
            self.root.update_idletasks()
            self.assertEqual(self.body.cget("background") == initial, theme == "light")
            self.assertEqual(app.db.execute("SELECT value FROM preferences WHERE name='appearance'").fetchone(), (theme,))
            self.assertEqual((app.active, app.follow, app.epoch.value), (True, False, epoch))
            self.assertEqual(self.body.get("sel.first", "sel.last"), self.text[6:9])
            self.assertEqual(self.body.get("playing.first", "playing.last"), self.text[4:11])
            app.open_voice_panel()
            self.assertEqual(app.voice_panel.cget("background"), self.body.cget("background"))
            self.assertEqual(app.voice_panel.steps.get(), 4)
            app.close_voice_panel()

    def test_button_heights_and_play_colors_in_both_themes(self):
        app = self.reader

        def buttons(parent):
            for widget in parent.winfo_children():
                if isinstance(widget, (ttk.Button, ttk.Menubutton, ttk.Radiobutton)):
                    yield widget
                yield from buttons(widget)

        for background, foreground in (("#efefef", "#171717"), ("#303030", "#ececec")):
            app.open_voice_panel()
            self.root.update()
            heights = {w.cget("text"): w.winfo_height() for w in buttons(self.root)}
            self.assertGreaterEqual(len(heights), 14)
            self.assertEqual(set(heights.values()), {app.play_button.winfo_height()}, heights)
            style = ttk.Style(self.root)
            play_style = app.play_button.cget("style") or app.play_button.winfo_class()
            self.assertEqual(style.lookup(play_style, "background"), background)
            self.assertEqual(style.lookup(play_style, "foreground"), foreground)
            app.close_voice_panel()
            app.theme_button.invoke()

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
        self.assertEqual(self.menu_play(self.text.index("后")), (0, self.text.index("最")))
        self.assertTrue(self.reader.follow)
        self.reader.highlight(Passage(1, 0, 4, "又是一天"))
        self.assertEqual(self.reader.doc, 1)
        self.assertEqual(self.body.get("playing.first", "playing.last"), "又是一天")

    def test_button_and_menu_restart_from_same_sentence_and_refollow(self):
        app = self.reader
        self.open_fixture(long_chapter=True)
        self.mouse("<ButtonPress-1>", self.text.index("边"))
        self.mouse("<ButtonRelease-1>", self.text.index("边"))
        for selected in (False, True):
            hit = self.text.index("边")
            if selected:
                self.drag(self.text.index("船"), self.text.index("走"))
                hit = self.text.index("看")
            for use_menu in (False, True):
                with self.subTest(selected=selected, menu=use_menu):
                    self.body.event_generate("<MouseWheel>", delta=-120)
                    self.assertFalse(app.follow)
                    if use_menu:
                        self.body.see(f"1.0+{hit}c")
                        self.root.update_idletasks()
                        self.menu_play(hit)
                    else:
                        app.play_button.invoke()
                    self.assertTrue(app.follow)
                    self.assertEqual(app.play_origin, (0, self.text.index("明")))
                    app.ready = True
                    app.poll()
                    _, span, _, _target = app.requests.get(timeout=1)
                    self.assertTrue(span.text.startswith("明月走到河边。"))
                    app.highlight(span)
                    self.assertIsNotNone(self.body.bbox("playing.first"))
                    if selected:
                        self.assertEqual(self.body.get("sel.first", "sel.last"), "走到河边。她看见了小")

    def test_plain_right_click_and_forward_drag(self):
        self.assertEqual(self.menu_play(self.text.index("边")), (0, self.text.index("明")))
        self.drag(self.text.index("走"), self.text.index("河"))
        self.assertEqual(self.body.get("sel.first", "sel.last"), "走到")
        # The right edge of a selection is exclusive.
        self.drag(self.text.index("走"), self.text.index("她"))
        self.assertEqual(self.body.index("sel.last"), self.body.index(f"1.0+{self.text.index('她')}c"))
        self.assertEqual(self.menu_play(self.text.index("她")), (0, self.text.index("她")))

    def test_follow_visible_clipped_wrapped_and_final_lines(self):
        self.open_fixture(long_chapter=True)
        app, body = self.reader, self.body
        text, _ = app.book.document(0)
        app.start((0, 0))
        view = body.yview()
        app.highlight(Passage(0, 1, 6, text[1:6]))
        self.root.update_idletasks()
        self.assertEqual(body.yview(), view)

        # Put just part of the last visual line in view. The start of the
        # passage is visible, and even its last glyph has a nonempty bbox.
        height = app.text_font.metrics("linespace")
        for _ in range(height + 2):
            last = body.index(f"@5,{body.winfo_height() - 6}")
            box = body.bbox(last)
            if box and 0 < box[3] < height:
                break
            body.yview_scroll(1, "pixels")
            self.root.update_idletasks()
        self.assertLess(box[3], height)
        first = body.index(f"{last} -1 display lines")
        offset = len(body.get("1.0", first))
        end = len(body.get("1.0", last)) + 1
        app.highlight(Passage(0, offset, end, text[offset:end]))
        self.root.update_idletasks()
        self.assertEqual(body.index("@3,5"), body.index(f"{first} display linestart"))
        self.assertEqual(body.bbox(last)[3], height)
        app.highlight(Passage(0, len(text)-3, len(text), text[-3:]))
        self.root.update_idletasks()
        self.assertEqual(body.index("@3,5"), body.index("end-2c linestart"))

    def test_manual_browsing_and_jump_to_audible_chapter(self):
        self.open_fixture(long_chapter=True)
        app, body = self.reader, self.body
        app.start((0, 0))
        app.highlight(Passage(0, 0, 4, self.text[:4]))
        scrollbar = next(w for w in body.master.winfo_children() if w.winfo_class() == "TScrollbar")
        for gesture in ("wheel", "scrollbar", "Next", "Prior", "Down", "Up"):
            with self.subTest(gesture=gesture):
                app.jump_button.invoke()
                body.focus_force()
                if gesture == "wheel":
                    body.event_generate("<MouseWheel>", delta=-120)
                elif gesture == "scrollbar":
                    body.tk.call(scrollbar.cget("command"), "moveto", .5)
                else:
                    body.event_generate(f"<KeyPress-{gesture}>")
                self.root.update()
                self.assertFalse(app.follow)
                view = body.yview()
                if gesture in ("wheel", "scrollbar", "Next", "Down"):
                    self.assertGreater(view[0], 0)
                app.highlight(Passage(1, 0, 5, "又是一天。"))
                self.assertEqual((app.doc, body.yview()), (0, view))
                app.highlight(Passage(0, 0, 4, self.text[:4]))
        app.highlight(Passage(1, 0, 5, "又是一天。"))
        app.toggle()
        generation = app.epoch.value
        app.jump_button.invoke()
        self.assertEqual((app.doc, app.epoch.value, app.paused, app.follow), (1, generation, True, True))
        self.assertEqual(body.get("playing.first", "playing.last"), "又是一天。")
        app.invalidate()
        self.assertTrue(app.jump_button.instate(["disabled"]))

    def test_changing_quality_restarts_at_audible_passage_and_preserves_pause(self):
        app = self.reader
        app.start((0, 0))
        app.highlight(Passage(0, 4, 11, self.text[4:11]))
        app.toggle()
        app.open_voice_panel()
        app.voice_panel.steps.set(8)
        app.close_voice_panel(save=True)
        self.assertTrue(app.paused)
        self.assertEqual(app.position, (0, 4))
        app.ready = True
        app.poll()
        generation, span, settings, _target = app.requests.get(timeout=1)
        self.assertEqual((generation, span.start, settings.steps), (app.epoch.value, 4, 8))
        self.assertEqual(app.db.execute("SELECT value FROM preferences WHERE name='voice_mode'").fetchone()[0], "精细 · 8步")

    def test_preview_from_paused_book_can_be_replaced_and_cancelled(self):
        app = self.reader
        app.start((0, 0))
        app.highlight(Passage(0, 4, 11, self.text[4:11]))
        app.toggle()
        app.open_voice_panel()
        panel = app.voice_panel
        panel.speed.set(.85)
        panel.preview()
        old, _, draft, _target = app.requests.get(timeout=1)
        self.assertEqual(draft.speed, .85)
        panel.guidance.set(2.5)
        panel.preview()
        current, sample, draft, _target = app.requests.get(timeout=1)
        with patch.object(app, "replies", Queue()), patch.object(app.player, "append") as append:
            app.replies.put(("audio", old, sample, bytes(480), .1))
            app.replies.put(("audio", current, sample, bytes(480), .1))
            app.poll()
            append.assert_called_once()
        app.close_voice_panel()
        self.assertEqual(app.settings, VoiceSettings())
        self.assertEqual(app.position, (0, 4))
        self.assertTrue(app.paused)
        self.assertEqual(app.db.execute("SELECT offset FROM progress").fetchone(), (4,))

    def test_voice_panel_saves_and_displays_current_values(self):
        app = self.reader
        app.open_voice_panel()
        draft = VoiceSettings(8, 1.15, 2.5, 35, False)
        app.voice_panel.set_values(draft)
        app.close_voice_panel(save=True)
        app.open_voice_panel()
        self.assertEqual(app.voice_panel.values(), draft)
        self.assertEqual(app.settings.steps, 8)
        app.voice_panel.set_values(VoiceSettings())
        app.close_voice_panel()
        self.assertEqual(app.settings, draft)
        self.assertEqual(dict(app.db.execute("SELECT name,value FROM preferences"))["softness"], "35")
        self.assertEqual(dict(app.db.execute("SELECT name,value FROM preferences"))["use_prosody"], "0")

    def test_segmentation_warning_waits_for_queued_audio(self):
        app = self.reader
        app.start((0, 0))
        app.ready = app.busy = True
        app.opening_audio = (Passage(0, 0, 4, self.text[:4]), bytes(48000))
        with (patch.object(app, "replies", Queue()), patch.object(app.player, "append") as play,
              patch.object(app.player, "buffered", side_effect=[1.0, 0.0]),
              patch("novel_reader.app.messagebox.showwarning") as warn):
            app.replies.put(("segment_error", app.epoch.value, "无法断句"))
            app.poll()
            play.assert_called_once()
            warn.assert_not_called()
            app.poll()
            warn.assert_called_once()
            self.assertFalse(app.active)

    def test_mac_short_turn_prefetches_before_playback_and_preserves_seek(self):
        app = self.reader
        app.settings = VoiceSettings(provider="mac")
        app.start((0, 0))
        turns = ['清风：“明月无心，你是有意。”', '梅振衣：“这可是明月的主意。”', '明月：“走吧。”']
        with (patch.object(app.book, "document", return_value=("".join(turns), {})),
              patch.object(app, "replies", Queue()), patch.object(app.mac_client, "submit") as send,
              patch.object(app.player, "append") as play):
            app.poll()
            generation, span, _ = send.call_args.args
            self.assertEqual(span.text, turns[0])
            app.replies.put(("audio", generation, span, bytes(48000), .5))
            app.poll()
            play.assert_not_called()
            _, second, _ = send.call_args.args
            self.assertEqual(second.text, turns[1])
            self.assertEqual(second.start, span.end)
            app.replies.put(("audio", generation, second, bytes(96000), 1.0))
            app.poll()
            self.assertEqual([c.args[0].text for c in play.call_args_list], turns[:2])
            _, third, _ = send.call_args.args
            self.assertEqual(third.text, turns[2])
            app.start((0, span.end))
            app.replies.put(("audio", generation, third, bytes(48000), .5))
            app.poll()
            self.assertEqual(play.call_count, 2)
            self.assertEqual(send.call_args.args[1].text, turns[1])

    def test_short_opening_waits_for_second_and_seek_discards_it(self):
        app = self.reader
        self.open_fixture(long_chapter=True)
        app.ready = True
        with patch.object(app, "replies", Queue()), patch.object(app.player, "append") as play:
            app.start((0, 0))
            app.poll()
            gen, first, _, _target = app.requests.get(timeout=1)
            first = first.prefix(45)
            short = bytes(48000)
            app.replies.put(("audio", gen, first, short, 1.2))
            app.poll()
            _, second, _, _target = app.requests.get(timeout=1)
            second = second.prefix(45)
            play.assert_not_called()
            app.replies.put(("audio", gen, second, short, 1.2))
            app.poll()
            self.assertEqual([call.args[0] for call in play.call_args_list], [first, second])
            app.requests.get(timeout=1)  # Previously queued lookahead, invalidated by the new seek.

            app.start((0, 0))
            app.poll()
            gen, first, _, _target = app.requests.get(timeout=1)
            first = first.prefix(45)
            app.replies.put(("audio", gen, first, short, 1.2))
            app.poll()
            self.assertIsNotNone(app.opening_audio)
            _, stale, _, _target = app.requests.get(timeout=1)
            app.start((1, 0))
            app.replies.put(("audio", gen, stale, short, 1.2))
            app.poll()
            gen, last, _, _target = app.requests.get(timeout=1)
            app.replies.put(("audio", gen, last, short, 1.2))
            app.poll()
            self.assertEqual(play.call_args.args[0], last)
            self.assertEqual(play.call_count, 3)
