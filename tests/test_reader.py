import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

from novel_reader.book import Book
from novel_reader.speech import Pacing, next_passage, spoken_text


class ReaderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "测试书.epub"
        with ZipFile(path, "w") as z:
            z.writestr("META-INF/container.xml", '<container><rootfile full-path="OPS/package.opf"/></container>')
            z.writestr("OPS/package.opf", """<package><metadata><title>试读</title></metadata><manifest>
                <item id="a" href="first.xhtml" media-type="application/xhtml+xml"/>
                <item id="b" href="second%20part.xhtml" media-type="application/xhtml+xml"/>
                <item id="n" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest>
                <spine toc="n"><itemref idref="b"/><itemref idref="a"/></spine></package>""")
            z.writestr("OPS/toc.ncx", """<ncx><navMap><navPoint><navLabel><text>归途</text></navLabel>
                <content src="second%20part.xhtml#part"/></navPoint></navMap></ncx>""")
            z.writestr("OPS/first.xhtml", "<html><body><p>最后一句。</p></body></html>")
            z.writestr("OPS/second part.xhtml", """<html><head><title>页头</title></head>
                <body><nav>目录</nav><p>出发。向前走。继续。</p><p id="part">明月说：“再走一段就到了。”</p></body></html>""")
        self.book = Book(path)
        self.addCleanup(self.book.close)

    def test_ncx_anchor_and_spine(self):
        self.assertEqual(self.book.entries[0].title, "归途")
        text, anchors = self.book.document(0)
        self.assertNotIn("目录", text)
        self.assertTrue(text[anchors["part"]:].lstrip().startswith("明月说"))
        self.assertEqual(self.book.docs[1], "OPS/first.xhtml")

    def test_seek_and_continuity_from_every_character(self):
        text, _ = self.book.document(0)
        last, _ = self.book.document(1)
        for offset in range(len(text) + 1):
            position = (0, offset)
            pieces = []
            while span := next_passage(self.book, position, 12, first=not pieces):
                pieces.append(span.text)
                position = span.doc, span.end
            spoken = lambda s: "".join(c for c in s if c.isalnum())
            self.assertEqual(spoken("".join(pieces)), spoken(text[offset:] + last))

    def test_short_first_then_adaptive_lengths(self):
        self.assertEqual(next_passage(self.book, (0, 0), 12, True).text, "出发。向前走。继续。")
        self.assertEqual(next_passage(self.book, (0, 0), 24).text, "出发。向前走。继续。")
        pacing = Pacing()
        self.assertLess(pacing.limit(False, 0), pacing.limit(False, 15))
        previous = pacing.limit(False, 5)
        pacing.observe(35, 7, 14)
        self.assertLess(pacing.limit(False, 5), previous)

    def test_short_intro_and_punctuation_beyond_target(self):
        intro = "16岁时，他第一次走进这所学堂。"
        sentence = "所以大家都没再提起礼学堂的事情。"
        text = intro + sentence + "下课以后，大家照常回家。"
        book = SimpleNamespace(docs=["a"], document=lambda _: (text, {}))
        self.assertEqual(next_passage(book, (0, 0), Pacing().limit(True, 0), True).text, intro)
        for target in range(12, 20):
            self.assertEqual(next_passage(book, (0, len(intro)), target).text, sentence)
        # Numeric separators are not clause boundaries, and a closing quote
        # stays with the statement even if the length target falls before it.
        quoted = '他说：“价格是1,234.50元。”'
        book.document = lambda _: (quoted + "\n下一段。", {})
        self.assertEqual(next_passage(book, (0, 0), 14).text, quoted)

    def test_start_delay_accounts_for_next_job(self):
        pacing = Pacing()
        pacing.observe(20, 4, 4)
        self.assertEqual(pacing.opening_delay(4, SimpleNamespace(text="很短。")), 0)
        self.assertGreater(pacing.opening_delay(4, SimpleNamespace(text="字" * 24)), 0)
        self.assertEqual(pacing.opening_delay(.8, SimpleNamespace(text="标题后的正文。")), float("inf"))

    def test_numeric_chapter_and_dates(self):
        self.assertEqual(spoken_text("第101章，2026年。第12回，003号。"), "第一百零一章，二零二六年。第十二回，零零三号。")
