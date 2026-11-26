import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

from novel_reader.book import Book
from novel_reader.speech import Pacing, prepare_passage, spoken_text
from novel_reader.segments import Passage, next_passage, choose_cut


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

    def test_rebalance_and_keep_closing_quote(self):
        text = "甲" * 30 + "。”" + "乙" * 15 + "，" + "丙" * 20 + "。"
        self.assertEqual(choose_cut(text, False), (32, False))
        self.assertEqual(choose_cut(text[32:], False), (len(text) - 32, False))

    def test_turns_are_independent_of_buffer_and_length(self):
        turns = ["梅振衣：“仙童怎么只说我？这可是明月的主意。”", "清风：“明月无心，你是有意。”",
                 "知焰：“" + "随先生亲口说有能耐就试试，" * 5 + "求雷得雷又何怨？”"]
        for target in (30, 45, 60):
            text = "".join(turns)
            for turn in turns:
                end, added = choose_cut(text, target=target)
                self.assertEqual((text[:end], added), (turn, False))
                text = text[end:]

    def test_quote_variants_and_attribution(self):
        for turn in ['明月说：「他答『好。』你听见了吗？」', '清风说："明日再说。"','“好！”', '清风：明月无心，你是有意。']:
            text = turn + "知焰：无心也罢。"
            self.assertEqual(choose_cut(text), (len(turn), False))
        text = "夜已经深了。明月说：“我们走吧。”"
        self.assertEqual(choose_cut(text)[0], text.index("明月"))

    def test_short_opening_and_numeric_punctuation(self):
        text = "16岁时，所以大家都没再提起礼学堂的事情。"
        self.assertEqual(choose_cut(text, target=30), (len(text), False))
        for number in ("1,234.50", "１，２３４：５６"):
            text = "甲" * 26 + number + "乙" * 28 + "。"
            self.assertEqual(choose_cut(text), (len(text), False))

    def test_long_dialogue_splits_within_one_speaker(self):
        clauses = ["清风：“" + "甲" * 35 + "？", "乙" * 34 + "，", "丙" * 34 + "！”"]
        text = "".join(clauses) + "明月说：“好。”"
        self.assertEqual(choose_cut(text, target=30), (len(clauses[0]), False))
        self.assertEqual(choose_cut(text[len(clauses[0]):], target=30),
                         (len("".join(clauses[1:])), False))

    def test_punctuation_cluster_and_soft_maximum(self):
        prefix = "甲" * 30 + "……？！”"
        self.assertEqual(choose_cut(prefix + "乙" * 32), (len(prefix), False))
        self.assertEqual(choose_cut("甲" * 67 + "。"), (68, False))

    def test_word_fallback_and_book_end(self):
        text = "所以大家都没再提起礼学堂的事情" * 12
        end, added = choose_cut(text)
        self.assertTrue(added)
        self.assertIn(end % 15, (0, 2, 4, 5, 6, 7, 9, 12, 13))
        self.assertEqual(choose_cut("完。"), (2, False))

    def test_paragraphs_and_chapters_preserve_source_offsets(self):
        docs = ["短标题", "", "转眼到了午后。\n  风吹过树林。" + "远处传来了声音。" * 45, "最后一句。"]
        book = SimpleNamespace(docs=docs, document=lambda i: (docs[i], {}))
        position = (0, 0)
        pieces = [[] for _ in docs]
        while window := next_passage(book, position):
            span, _ = prepare_passage(window, None)
            self.assertNotIn("\n", span.text)
            self.assertGreater(span.next_position, position)
            self.assertEqual(docs[span.doc][span.start:span.end], span.text)
            pieces[span.doc].append(span.text)
            position = span.next_position
        self.assertEqual(["".join(p) for p in pieces],
                         [s.replace("\n", "").replace(" ", "") for s in docs])

    def test_pacing_changes_real_narrative_cuts(self):
        text = ("他们沿着小河慢慢地往前走去。" * 12)
        pacing = Pacing()
        pacing.observe(50, 10, 5)
        short = pacing.target_length(2)
        full = pacing.target_length(9)
        self.assertLess(choose_cut(text, target=short)[0], choose_cut(text, target=full)[0])
        fast = full
        for _ in range(5):
            pacing.observe(50, 10, 14)
        self.assertLess(pacing.target_length(9), fast)
        self.assertEqual(pacing.target_length(0, opening=True), 30)

    def test_start_delay_accounts_for_next_job(self):
        pacing = Pacing()
        pacing.observe(20, 4, 4)
        self.assertEqual(pacing.opening_delay(4, SimpleNamespace(text="很短。")), 0)
        self.assertGreater(pacing.opening_delay(4, SimpleNamespace(text="字" * 24)), 0)
        self.assertEqual(pacing.opening_delay(.8, SimpleNamespace(text="标题后的正文。")), float("inf"))

    def test_numeric_chapter_and_dates(self):
        self.assertEqual(spoken_text("第101章，2026年。第12回，003号。"), "第一百零一章，二零二六年。第十二回，零零三号。")
