import multiprocessing as mp
import os
import sqlite3
import sys
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from queue import Empty
from tkinter import filedialog, messagebox, ttk

from .book import Book
from .speech import AudioPlayer, Pacing, next_passage, sentence_start, synthesis_worker


ROOT = Path(__file__).resolve().parents[1]
INK, MUTED, ACCENT = "#243933", "#536961", "#206553"
PAPER, SIDE, LINE = "#fffdf7", "#eeeee6", "#d8dace"
UI = "Microsoft YaHei UI"
VOICE_MODES = {"流畅 · 4步": 4, "精细 · 8步": 8}


class WorkerUnavailable(Exception):
    pass


class Reader:
    def __init__(self, root, state_path=None):
        self.root = root
        root.title("听页 · 本地 EPUB 朗读器")
        root.geometry("1040x700")
        root.minsize(720, 480)
        root.configure(bg=PAPER)
        self.book = self.identity = None
        self.doc = -1
        self.cursor = self.position = (0, 0)
        self.follow = True
        self.active = self.paused = self.busy = self.eof = False
        self.first = True
        self.opening_audio = None
        self.opening_deadline = 0.0
        self.unvoiced = 0
        self.audible = None
        self.pending_marks = deque()
        self.pacing = Pacing()
        self.player = AudioPlayer()
        self.ready = False
        self.failure = ""
        self.closed = False
        state_path = state_path or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "TingYe" / "progress.sqlite3"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(state_path)
        self.db.execute("CREATE TABLE IF NOT EXISTS progress (book TEXT PRIMARY KEY, doc INTEGER, offset INTEGER)")
        self.db.execute("CREATE TABLE IF NOT EXISTS preferences (name TEXT PRIMARY KEY, value TEXT)")
        saved_mode = self.db.execute("SELECT value FROM preferences WHERE name='voice_mode'").fetchone()
        self.voice_mode = tk.StringVar(value=saved_mode[0] if saved_mode and saved_mode[0] in VOICE_MODES else next(iter(VOICE_MODES)))
        context = mp.get_context("spawn")
        self.epoch = context.Value("L", 0)
        self.requests, self.replies = context.Queue(), context.Queue()
        self.worker = context.Process(target=synthesis_worker, daemon=True,
                                      args=(str(ROOT / "models"), self.requests, self.replies, self.epoch))
        self.layout()
        self.worker.start()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Control-o>", lambda e: self.choose())
        root.bind("<Control-Return>", lambda e: self.start())
        root.bind("<space>", self.space)
        self.timer = root.after(35, self.tick)

    def layout(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TScrollbar", background=SIDE, troughcolor=PAPER, borderwidth=0, arrowsize=12)
        header = tk.Frame(self.root, bg=INK, padx=24, pady=13)
        header.pack(fill="x")
        tk.Label(header, text="听页", bg=INK, fg=PAPER, font=(UI, 18, "bold")).pack(side="left")
        tk.Label(header, text="本地 EPUB 朗读器", bg=INK, fg="#d2e2d8", font=(UI, 10), padx=16).pack(side="left")
        self.button(header, "打开 EPUB…", self.choose).pack(side="right")
        mode = ttk.Combobox(header, textvariable=self.voice_mode, values=list(VOICE_MODES), state="readonly", width=11)
        mode.pack(side="right", padx=12)
        mode.bind("<<ComboboxSelected>>", self.change_voice_mode)

        footer = tk.Frame(self.root, bg=SIDE, padx=20, pady=12)
        footer.pack(side="bottom", fill="x")
        self.play_button = self.button(footer, "从此处朗读", self.start, primary=True)
        self.play_button.pack(side="left")
        self.pause_button = self.button(footer, "暂停", self.toggle)
        self.pause_button.pack(side="left", padx=10)
        self.play_button.config(state="disabled")
        self.pause_button.config(state="disabled")
        self.status = tk.StringVar(value="正在准备本地语音模型…")
        tk.Label(footer, textvariable=self.status, bg=SIDE, fg=MUTED, font=(UI, 10), anchor="e").pack(side="right")

        area = tk.PanedWindow(self.root, orient="horizontal", bg=LINE, sashwidth=2, bd=0)
        area.pack(fill="both", expand=True)
        side = tk.Frame(area, bg=SIDE)
        area.add(side, width=235, minsize=135, stretch="never")
        tk.Label(side, text="章节目录", bg=SIDE, fg=INK, font=(UI, 12, "bold"), anchor="w",
                 padx=17, pady=15).pack(fill="x")
        self.toc = tk.Listbox(side, bg=SIDE, fg=INK, selectbackground="#cfdfd3", selectforeground=INK,
                              bd=0, highlightthickness=0, activestyle="none", exportselection=False,
                              font=(UI, 11))
        sidebar_scroll = ttk.Scrollbar(side, command=self.toc.yview)
        sidebar_scroll.pack(side="right", fill="y")
        self.toc.config(yscrollcommand=sidebar_scroll.set)
        self.toc.pack(fill="both", expand=True, padx=(12, 0), pady=(0, 14))
        self.toc.bind("<<ListboxSelect>>", self.browse)
        main = tk.Frame(area, bg=PAPER, padx=22, pady=15)
        area.add(main, minsize=360, stretch="always")
        self.heading = tk.StringVar(value="打开一本 EPUB 开始阅读")
        tk.Label(main, textvariable=self.heading, bg=PAPER, fg=INK, anchor="w", font=(UI, 17, "bold")).pack(fill="x")
        self.location = tk.StringVar(value="点选正文中的文字，即可从那里开始朗读。")
        tk.Label(main, textvariable=self.location, bg=PAPER, fg=MUTED, anchor="w",
                 font=(UI, 10), pady=5).pack(fill="x")
        tk.Frame(main, bg=LINE, height=1).pack(fill="x", pady=(5, 10))
        scroll = ttk.Scrollbar(main)
        scroll.pack(side="right", fill="y")
        self.body = tk.Text(main, wrap="word", bg=PAPER, fg=INK, font=("Microsoft YaHei", 14),
                            relief="flat", highlightthickness=0, padx=3, pady=5, spacing1=4, spacing3=8,
                            cursor="xterm", state="disabled", yscrollcommand=scroll.set, exportselection=False,
                            selectbackground="#315f96", selectforeground="white", inactiveselectbackground="#315f96")
        scroll.config(command=self.body.yview)
        self.body.pack(fill="both", expand=True)
        self.body.tag_configure("playing", background="#d9e9da")
        self.body.tag_configure("picked", underline=True, foreground="#00654c", background="#f2e8c2")
        self.body.tag_raise("sel")
        self.context_menu = tk.Menu(self.body, tearoff=False)
        self.context_menu.add_command(label="从此处播放", command=self.play_context)
        self.context_target = None
        self.body.bind("<Button-1>", self.pick)
        self.body.bind("<ButtonRelease-1>", self.finish_pick)
        self.body.bind("<Button-3>", self.show_context)
        for key, delta in (("Left", -1), ("Right", 1)):
            self.body.bind(f"<{key}>", lambda e, d=delta: self.move(d))

    def button(self, parent, text, action, primary=False):
        button = tk.Button(parent, text=text, command=action, font=(UI, 10, "bold"), relief="flat",
                         padx=16, pady=5, bd=0, bg=ACCENT if primary else "#fbfaf4",
                         fg="white" if primary else INK, activebackground="#c8dccc", cursor="hand2")
        button.bind("<space>", self.space)
        return button

    def choose(self):
        name = filedialog.askopenfilename(filetypes=[("EPUB 电子书", "*.epub"), ("所有文件", "*.*")])
        if name:
            self.open_book(Path(name))

    def open_book(self, path):
        new = None
        try:
            path = Path(path).resolve()
            new = Book(path)
            stat = path.stat()
            key = f"{path}|{stat.st_size}|{stat.st_mtime_ns}"
            saved = self.db.execute("SELECT doc,offset FROM progress WHERE book=?", (key,)).fetchone()
            doc, offset = saved or (0, 0)
            doc = min(max(doc, 0), len(new.docs) - 1)
            content, _ = new.document(doc)
            while not content.strip() and doc + 1 < len(new.docs):
                doc += 1
                offset = 0
                content, _ = new.document(doc)
            offset = min(max(offset, 0), len(content))
        except Exception as exc:
            if new:
                new.close()
            messagebox.showerror("无法打开 EPUB", f"请使用含文字正文、无 DRM 加密的 EPUB。\n\n{exc}", parent=self.root)
            return
        self.invalidate()
        if self.book:
            self.book.close()
        self.book, self.identity, self.doc = new, key, -1
        self.toc.delete(0, "end")
        self.entry_indices = {}
        for i, entry in enumerate(new.entries):
            self.entry_indices.setdefault(entry.doc, []).append(i)
            self.toc.insert("end", entry.title)
        self.heading.set(new.title)
        self.root.title(f"{new.title} · 听页")
        self.display(doc)
        self.choose_offset(offset, scroll=True)
        self.play_button.config(state="normal" if not self.failure else "disabled")
        self.status.set(self.failure or ("可从上次位置续听" if saved else "点选正文后开始朗读"))

    def display(self, doc):
        if self.doc == doc:
            return
        content, _ = self.book.document(doc)
        self.body.config(state="normal")
        self.body.delete("1.0", "end")
        self.body.insert("1.0", content)
        self.body.config(state="disabled")
        self.doc = doc

    def mark_entry(self, offset):
        _, anchors = self.book.document(self.doc)
        indices = self.entry_indices[self.doc]
        index = max((i for i in indices if anchors.get(self.book.entries[i].anchor, 0) <= offset),
                    key=lambda i: (anchors.get(self.book.entries[i].anchor, 0), -i), default=indices[0])
        self.toc.selection_clear(0, "end")
        self.toc.selection_set(index)
        self.toc.see(index)
        self.location.set(f"{self.book.entries[index].title}    ·    正文第 {offset + 1} 字")

    def choose_offset(self, offset, scroll=False):
        self.cursor = (self.doc, offset)
        start = f"1.0+{offset}c"
        self.body.tag_remove("picked", "1.0", "end")
        self.body.tag_add("picked", start, f"{start}+1c")
        if scroll:
            self.body.see(start)
        self.mark_entry(offset)

    def browse(self, event=None):
        selected = self.toc.curselection()
        if not selected or not self.book:
            return
        entry = self.book.entries[selected[0]]
        try:
            _, anchors = self.book.document(entry.doc)
            self.follow = False
            self.display(entry.doc)
            self.choose_offset(anchors.get(entry.anchor, 0), scroll=True)
        except Exception as exc:
            messagebox.showerror("无法读取章节", str(exc), parent=self.root)

    def pick(self, event):
        if self.book:
            index = self.body.index(f"@{event.x},{event.y}")
            offset = len(self.body.get("1.0", index))
            self.follow = False
            self.body.focus_set()
            self.choose_offset(offset)
        # Allow the native Text binding to set its drag anchor and select mode.

    def finish_pick(self, event):
        selected = self.body.tag_ranges("sel")
        if self.book and selected:
            self.follow = False
            offset = len(self.body.get("1.0", selected[0]))
            self.cursor = (self.doc, offset)
            self.mark_entry(offset)
            self.body.tag_remove("picked", "1.0", "end")

    def show_context(self, event):
        if not self.book:
            return "break"
        hit = self.body.index(f"@{event.x},{event.y}")
        selected = self.body.tag_ranges("sel")
        if selected and self.body.compare(selected[0], "<=", hit) and self.body.compare(hit, "<", selected[1]):
            hit = selected[0]
        text, _ = self.book.document(self.doc)
        offset = sentence_start(text, len(self.body.get("1.0", hit)))
        self.context_target = self.book, self.doc, offset
        self.context_menu.entryconfigure(0, state="disabled" if self.failure or not text.strip() else "normal")
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()
        return "break"

    def play_context(self):
        if self.context_target and self.context_target[0] is self.book:
            _, doc, offset = self.context_target
            self.start((doc, offset))

    def move(self, delta):
        if self.book:
            self.follow = False
            text, _ = self.book.document(self.doc)
            self.choose_offset(min(len(text), max(0, self.cursor[1] + delta)), scroll=True)
        return "break"

    def invalidate(self):
        self.epoch.value += 1
        self.player.reset()
        self.pending_marks.clear()
        self.opening_audio = None
        self.opening_deadline = 0.0
        self.active = self.paused = self.busy = self.eof = False
        self.audible = None
        self.body.tag_remove("playing", "1.0", "end")
        self.pause_button.config(text="暂停", state="disabled")

    def start(self, position=None):
        if not self.book or self.failure:
            return
        self.invalidate()
        self.active = self.follow = self.first = True
        self.unvoiced = 0
        self.position = self.cursor if position is None else position
        self.play_origin = self.position
        self.body.tag_remove("picked", "1.0", "end")
        self.pause_button.config(state="normal")
        self.status.set("正在准备首句…" if self.ready else "正在加载本地模型…")

    def change_voice_mode(self, event=None):
        self.pacing = Pacing()
        self.db.execute("INSERT OR REPLACE INTO preferences VALUES('voice_mode', ?)", (self.voice_mode.get(),))
        self.db.commit()
        if self.active:
            paused = self.paused
            point = (self.audible.doc, self.audible.start) if self.audible else self.play_origin
            self.start(point)
            if paused:
                self.toggle()

    def toggle(self):
        if self.active:
            self.paused = not self.paused
            with self.player.lock:
                self.player.paused = self.paused
            self.pause_button.config(text="继续" if self.paused else "暂停")
            self.status.set("已暂停" if self.paused else "继续朗读")

    def space(self, event=None):
        if self.active:
            self.toggle()
        elif self.book:
            self.start()
        return "break"

    def highlight(self, span):
        self.audible = span
        self.db.execute("INSERT OR REPLACE INTO progress VALUES(?,?,?)", (self.identity, span.doc, span.start))
        self.db.commit()
        if self.follow and not self.body.tag_ranges("sel"):
            self.display(span.doc)
            self.cursor = (span.doc, span.start)
            self.body.see(f"1.0+{span.start}c")
            self.mark_entry(span.start)
        self.body.tag_remove("playing", "1.0", "end")
        if self.doc == span.doc:
            self.body.tag_add("playing", f"1.0+{span.start}c", f"1.0+{span.end}c")

    def release_opening(self):
        if self.opening_audio is not None:
            self.player.append(*self.opening_audio)
            self.opening_audio = None

    def poll(self):
        while True:
            try:
                msg = self.replies.get_nowait()
            except Empty:
                break
            if msg[0] == "ready":
                self.ready = True
                if not self.active and not self.book:
                    self.status.set("离线模型已就绪")
            elif msg[0] == "fatal":
                raise WorkerUnavailable(msg[1])
            elif msg[1] == self.epoch.value:
                if msg[0] == "error":
                    raise ValueError(msg[2])
                if msg[0] == "unvoiced":
                    self.busy = False
                    self.unvoiced += 1
                    continue
                _, _, span, pcm, elapsed = msg
                self.busy = False
                self.pacing.observe(len(span.text), len(pcm) / 48000, elapsed)
                if self.first:
                    self.first = False
                    self.opening_audio = (span, pcm)
                else:
                    self.release_opening()
                    self.player.append(span, pcm)
        if not self.worker.is_alive() and self.worker.exitcode is not None:
            raise WorkerUnavailable("语音进程已退出。请关闭并重新打开听页。")
        while not self.player.events.empty():
            self.pending_marks.append(self.player.events.get())
        while self.pending_marks and self.pending_marks[0][0] <= time.monotonic():
            self.highlight(self.pending_marks.popleft()[1])
        if not self.active or not self.ready:
            return
        if self.player.stream is not None and not self.player.stream.active:
            raise ValueError("音频输出已中断。请检查播放设备，再从此处朗读。")
        buffered = self.player.buffered()
        opening_seconds = len(self.opening_audio[1]) / 48000 if self.opening_audio else 0
        if self.eof and not self.busy and buffered <= 0 and not self.paused and not self.pending_marks:
            completed = self.audible is not None
            self.invalidate()
            summary = "全书朗读完毕" if completed else "当前位置之后没有可朗读的正文"
            if self.unvoiced:
                summary += f"（{self.unvoiced} 个片段未能发音）"
            self.status.set(summary)
            return
        if not self.busy and not self.eof and buffered < self.pacing.buffer_goal:
            limit = self.pacing.limit(self.first, buffered + opening_seconds)
            span = next_passage(self.book, self.position, limit, self.first)
            if span is None:
                self.eof = True
                self.release_opening()
            else:
                self.requests.put((self.epoch.value, span, VOICE_MODES[self.voice_mode.get()]))
                self.position = (span.doc, span.end)
                self.busy = True
                if self.opening_audio and not self.opening_deadline:
                    self.opening_deadline = time.monotonic() + self.pacing.opening_delay(opening_seconds, span)
        if self.opening_audio and time.monotonic() >= self.opening_deadline:
            self.release_opening()
        if not self.paused:
            if self.opening_audio:
                self.status.set("正在预缓冲开头…")
            elif buffered > 0:
                self.status.set(f"正在朗读　·　已备好 {buffered:.0f} 秒")
            else:
                self.status.set("正在准备下一段…" if self.audible else "正在准备首句…")

    def tick(self):
        try:
            if not self.failure:
                self.poll()
        except Exception as exc:
            self.invalidate()
            if isinstance(exc, WorkerUnavailable) or not self.worker.is_alive():
                self.failure = str(exc)
                self.play_button.config(state="disabled")
            self.status.set("朗读已中止")
            messagebox.showerror("朗读遇到问题", str(exc), parent=self.root)
        if not self.closed:
            self.timer = self.root.after(35, self.tick)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.root.after_cancel(self.timer)
        self.invalidate()
        self.requests.put(None)
        self.worker.join(.5)
        if self.worker.is_alive():
            self.worker.terminate()
            self.worker.join(1)
        if self.book:
            self.book.close()
        self.db.close()
        self.root.destroy()


def main():
    mp.freeze_support()
    root = tk.Tk()
    app = Reader(root)
    if len(sys.argv) > 1:
        app.open_book(sys.argv[1])
    root.mainloop()
