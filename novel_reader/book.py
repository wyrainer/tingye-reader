import codecs
import posixpath
import re
from collections import OrderedDict
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET
from zipfile import ZipFile


def local(tag):
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()


def target(source, href):
    url = urlsplit(href)
    if url.scheme or url.netloc:
        return "", ""
    path = posixpath.normpath(posixpath.join(posixpath.dirname(source), unquote(url.path))) if url.path else source
    return path, unquote(url.fragment)


@dataclass(frozen=True)
class Entry:
    title: str
    doc: int
    anchor: str = ""


class BodyParser(HTMLParser):
    blocks = set("p div section article aside blockquote h1 h2 h3 h4 h5 h6 li tr dt dd br hr".split())
    ignore = {"head", "script", "style", "nav", "rt", "rp", "svg"}
    void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.anchors = {}
        self.length = 0
        self.hidden = []
        self.pending = ""

    def handle_starttag(self, tag, attrs):
        tag = local(tag)
        attrs = dict(attrs)
        if self.hidden or tag in self.ignore or "hidden" in attrs or attrs.get("aria-hidden") == "true":
            if tag not in self.void:
                self.hidden.append(tag)
            return
        if tag in self.blocks:
            self.pending = "\n"
        for key in ("id", "xml:id", "name"):
            if attrs.get(key):
                self.anchors.setdefault(attrs[key], self.length)

    def handle_endtag(self, tag):
        tag = local(tag)
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        elif tag in self.blocks:
            self.pending = "\n"

    def handle_data(self, data):
        if self.hidden:
            return
        data = re.sub(r"[\s\u200b\ufeff]+", " ", data)
        content = data.strip()
        if content:
            separator = self.pending or (" " if data.startswith(" ") else "")
            piece = (separator if self.length else "") + content
            self.parts.append(piece)
            self.length += len(piece)
            self.pending = ""
        if data.endswith(" ") and not self.pending:
            self.pending = " "


def decode_html(raw):
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    match = re.search(rb'(?:encoding|charset)\s*=\s*["\x27]?([\w.-]+)', raw[:512], re.I)
    encodings = [match[1].decode("ascii")] if match else []
    for name in [*encodings, "utf-8-sig", "gb18030"]:
        try:
            return raw.decode(name)
        except (UnicodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


class Book:
    def __init__(self, path):
        self.path = path
        self.archive = ZipFile(path)
        self.cache = OrderedDict()
        try:
            container = self.xml("META-INF/container.xml")
            root = next((n.get("full-path") for n in container.iter() if local(n.tag) == "rootfile"), None)
            if not root:
                raise ValueError("EPUB 缺少书籍索引。")
            package = self.xml(root)
            self.title = next(("".join(n.itertext()).strip() for n in package.iter() if local(n.tag) == "title"), "") or path.stem
            manifest = {n.get("id"): n for n in package.iter() if local(n.tag) == "item"}
            spine = next((n for n in package.iter() if local(n.tag) == "spine"), None)
            if spine is None:
                raise ValueError("EPUB 缺少阅读顺序。")
            self.docs = []
            for ref in spine:
                item = manifest.get(ref.get("idref"))
                if item is not None and ref.get("linear", "yes") != "no" and "html" in item.get("media-type", ""):
                    name, _ = target(root, item.get("href", ""))
                    if name:
                        self.docs.append(name)
            if not self.docs:
                raise ValueError("这本书没有可朗读的文字章节。")
            self.doc_names = set(self.docs)
            if "META-INF/encryption.xml" in self.archive.namelist():
                for node in self.xml("META-INF/encryption.xml").iter():
                    if local(node.tag) == "cipherreference" and unquote(node.get("URI", "")).lstrip("/") in self.doc_names:
                        raise ValueError("这本书的正文已加密。")
            labels = self.read_toc(root, manifest, spine.get("toc"))
            self.entries = []
            for i, name in enumerate(self.docs):
                sections = labels.get(name, {})
                self.entries.extend(Entry(label, i, anchor) for anchor, label in sections.items())
                if not sections:
                    self.entries.append(Entry(f"章节 {i + 1}", i))
        except Exception:
            self.close()
            raise

    def read(self, name):
        info = self.archive.getinfo(name)
        if info.file_size > 64 * 1024 * 1024:
            raise ValueError("EPUB 单个正文文件超过 64 MB。")
        return self.archive.read(info)

    def xml(self, name):
        return ET.fromstring(self.read(name))

    def read_toc(self, root, manifest, ncx_id):
        sources = [n for n in manifest.values() if "nav" in n.get("properties", "").split()]
        ncx = manifest.get(ncx_id)
        if ncx is None:
            ncx = next((n for n in manifest.values() if n.get("media-type") == "application/x-dtbncx+xml"), None)
        if ncx is not None:
            sources.append(ncx)
        for item in sources:
            path, _ = target(root, item.get("href", ""))
            labels = {}
            try:
                tree = self.xml(path)
                if item is ncx:
                    links = []
                    for point in tree.iter():
                        if local(point.tag) == "navpoint":
                            label = point.find("{*}navLabel")
                            content = point.find("{*}content")
                            if label is not None and content is not None:
                                links.append((content.get("src", ""), "".join(label.itertext())))
                else:
                    nav = next((n for n in tree.iter() if local(n.tag) == "nav" and
                                ("toc" in n.get("{http://www.idpf.org/2007/ops}type", "").split() or n.get("role") == "doc-toc")), None)
                    links = [(n.get("href", ""), "".join(n.itertext())) for n in nav.iter() if local(n.tag) == "a"] if nav is not None else []
                for href, label in links:
                    doc, anchor = target(path, href)
                    if doc in self.doc_names and label.strip():
                        labels.setdefault(doc, {}).setdefault(anchor, " ".join(label.split()))
            except (KeyError, ET.ParseError):
                continue
            if labels:
                return labels
        return {}

    def document(self, index):
        if index not in self.cache:
            parser = BodyParser()
            raw = self.read(self.docs[index])
            parser.feed(decode_html(raw))
            parser.close()
            self.cache[index] = ("".join(parser.parts), parser.anchors)
            if len(self.cache) > 4:
                self.cache.popitem(last=False)
        self.cache.move_to_end(index)
        return self.cache[index]

    def close(self):
        self.archive.close()
