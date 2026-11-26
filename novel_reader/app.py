import multiprocessing as mp
import os
import sqlite3
import sys
import time
import tkinter as tk
from collections import Counter, deque
from pathlib import Path
from queue import Empty
from tkinter import filedialog, font, messagebox, ttk

from .appearance import PALETTES, style_controls, style_title_bar
from .book import Book
from .model_paths import PACK, locate_models, missing_files
from .mac_speech import MacSpeechClient
from .speech import AudioPlayer, Pacing, VoiceSettings, sentence_start, synthesis_worker
from .segments import DEFAULT_TARGET, Passage, choose_cut, next_passage, spoken_count
from .voice_panel import VoicePanel
from .volume import VolumeSlider


ROOT = Path(__file__).resolve().parents[1]
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
        self.db.execute("CREATE TABLE IF NOT EXISTS recent_files (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE)")
        prefs = dict(self.db.execute("SELECT name,value FROM preferences"))
        try:
            self.player.volume = max(0, min(100, int(prefs.get("volume", 100))))
        except (ValueError, TypeError):
            self.player.volume = 100
        self.volume_timer = None
        self.appearance = "dark" if prefs.get("appearance") == "dark" else "light"
        steps = VOICE_MODES.get(prefs.get("voice_mode"), 4)
        try:
            self.settings = VoiceSettings(steps, float(prefs.get("speed", 1)),
                                          float(prefs.get("guidance", 3)), int(prefs.get("softness", 0)),
                                          prefs.get("use_prosody", "1") != "0",
                                          prefs.get("provider", "zipvoice"), prefs.get("mac_voice", "male_1"))
        except (ValueError, TypeError):
            self.settings = VoiceSettings(steps)
        self.voice_panel = None
        self.previewing = self.preview_playing = False
        self.resume_after_preview = None
        context = mp.get_context("spawn")
        self.epoch = context.Value("L", 0)
        self.requests, self.replies = context.Queue(), context.Queue()
        self.mac_client = MacSpeechClient(self.replies)
        self.heard = deque(maxlen=256)
        self.worker = None
        self.layout()
        root.bind("<Map>", self.on_window_map, add="+")
        self.apply_appearance()
        if self.settings.provider == "zipvoice":
            self.ensure_local_worker()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Control-o>", lambda e: self.choose())
        root.bind("<Control-Return>", lambda e: self.read_from_here())
        root.bind("<space>", self.space)
        self.timer = root.after(35, self.tick)

    def ensure_local_worker(self):
        if self.worker is not None or self.failure:
            return
        key = f"model_path:{PACK}"
        exe = Path(sys.executable) if getattr(sys, "frozen", False) else None
        pref = self.db.execute("SELECT value FROM preferences WHERE name=?", (key,)).fetchone()
        model_dir = locate_models(ROOT, exe, pref[0] if pref else None)
        if model_dir is None:
            model_dir = self.choose_models()
            if model_dir:
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO preferences VALUES(?,?)", (key, str(model_dir)))
        if model_dir:
            self.worker = mp.get_context("spawn").Process(target=synthesis_worker, daemon=True,
                                          args=(str(model_dir), self.requests, self.replies, self.epoch))
            self.worker.start()
        else:
            self.failure = "未选择模型。重新打开听页可选择模型文件夹。"
            self.status.set(self.failure)

    def available(self, settings):
        return settings.provider == "mac" or not self.failure

    def choose_models(self):
        while True:
            selected = filedialog.askdirectory(parent=self.root, mustexist=True,
                                               title=f"选择模型库或模型文件夹 {PACK}")
            if not selected:
                return None
            folder = Path(selected)
            if folder.name != PACK:
                folder = folder / PACK
            missing = missing_files(folder)
            if not missing:
                return folder
            messagebox.showwarning("模型文件不齐全",
                                   f"请选择包含完整 {PACK} 的模型库，或该版本文件夹。\n"
                                   f"缺少文件：{missing[0]}", parent=self.root)

    def layout(self):
        ttk.Style(self.root).theme_use("clam")
        header = ttk.Frame(self.root, padding=(16, 12))
        header.pack(fill="x")
        ttk.Label(header, text="听页", font=(UI, 18, "bold")).pack(side="left")
        recent = ttk.Menubutton(header, text="最近打开文件", direction="below", takefocus=True)
        self.recent_menu = tk.Menu(recent, tearoff=False, postcommand=self.populate_recent_menu)
        recent.configure(menu=self.recent_menu)
        recent.pack(side="right", padx=(8, 0))
        self.button(header, "打开 EPUB…", self.choose).pack(side="right")
        self.button(header, "语音调节…", self.open_voice_panel).pack(side="right", padx=(0, 8))
        self.theme_button = self.button(header, "", self.toggle_appearance)
        self.theme_button.pack(side="right", padx=(0, 8))
        self.theme_button.bind("<space>", self.toggle_appearance)
        ttk.Label(header, text="本地 EPUB 朗读器", style="Muted.TLabel",
                  font=(UI, 10)).pack(side="left", padx=12)

        bar = ttk.Frame(self.root, style="Sidebar.TFrame", padding=(12, 8))
        bar.pack(side="bottom", fill="x")
        footer = ttk.Frame(bar, style="Sidebar.TFrame")
        footer.pack(side="left")
        self.play_button = self.button(footer, "从此处朗读", self.read_from_here)
        self.play_button.pack(side="left")
        self.pause_button = self.button(footer, "暂停", self.toggle)
        self.pause_button.pack(side="left", padx=4)
        self.stop_button = self.button(footer, "停止", self.stop_reading)
        self.stop_button.pack(side="left")
        self.stop_button.config(state="disabled")
        for label, direction in (("上一段", -1), ("下一段", 1)):
            self.button(footer, label, lambda d=direction: self.skip_passage(d)).pack(side="left", padx=(4, 0))
        self.jump_button = self.button(footer, "跳转到朗读位置", self.jump_to_reading)
        self.jump_button.pack(side="left")
        self.jump_button.config(state="disabled")
        self.jump_button.bind("<space>", self.jump_to_reading)
        self.play_button.config(state="disabled")
        self.pause_button.config(state="disabled")
        self.status = tk.StringVar(value=("已选择 Mac mini · CosyVoice 3" if self.settings.provider == "mac"
                                          else "正在准备本地语音模型…"))
        volume = ttk.Frame(bar, style="Sidebar.TFrame")
        volume.pack(side="right", padx=(8, 0))
        ttk.Label(volume, text="音量", style="Sidebar.TLabel", font=(UI, 10)).pack(side="left")
        self.volume_text = tk.StringVar(value=f"{self.player.volume}%")
        ttk.Label(volume, textvariable=self.volume_text, width=5, style="Sidebar.TLabel").pack(side="right")
        self.volume_slider = VolumeSlider(volume, self.player.volume, self.change_volume)
        self.volume_slider.pack(side="left", padx=4, fill="x", expand=True)

        self.area = tk.PanedWindow(self.root, orient="horizontal", sashwidth=2, bd=0)
        self.area.pack(fill="both", expand=True)
        side = ttk.Frame(self.area, style="Sidebar.TFrame")
        self.area.add(side, width=235, minsize=135, stretch="never")
        ttk.Label(side, text="章节目录", style="Sidebar.TLabel", font=(UI, 12, "bold"),
                  anchor="w", padding=(17, 15)).pack(fill="x")
        self.toc = tk.Listbox(side,
                              bd=0, highlightthickness=0, activestyle="none", exportselection=False,
                              font=(UI, 11))
        sidebar_scroll = ttk.Scrollbar(side, command=self.toc.yview)
        sidebar_scroll.pack(side="right", fill="y")
        self.toc.config(yscrollcommand=sidebar_scroll.set)
        self.toc.pack(fill="both", expand=True, padx=(12, 0), pady=(0, 14))
        self.toc.bind("<<ListboxSelect>>", self.browse)
        main = ttk.Frame(self.area, padding=(22, 15))
        self.area.add(main, minsize=360, stretch="always")
        self.heading = tk.StringVar(value="打开一本 EPUB 开始阅读")
        title_row = ttk.Frame(main)
        title_row.pack(fill="x")
        ttk.Label(title_row, textvariable=self.status, style="Muted.TLabel",
                  font=(UI, 10), anchor="e").pack(side="right", padx=(8, 0))
        ttk.Label(title_row, textvariable=self.heading, anchor="w", font=(UI, 17, "bold")).pack(fill="x")
        self.location = tk.StringVar(value="点选正文中的文字，即可从那里开始朗读。")
        ttk.Label(main, textvariable=self.location, style="Muted.TLabel", anchor="w",
                  font=(UI, 10), padding=(0, 5)).pack(fill="x")
        ttk.Separator(main).pack(fill="x", pady=(5, 10))
        scroll = ttk.Scrollbar(main)
        scroll.pack(side="right", fill="y")
        self.body = tk.Text(main, wrap="word", font=("Microsoft YaHei", 14),
                            relief="flat", borderwidth=0, highlightthickness=0, padx=3, pady=5, spacing1=4, spacing3=8,
                            cursor="xterm", state="disabled", yscrollcommand=scroll.set, exportselection=False)
        scroll.config(command=self.manual_scroll)
        scroll.bind("<ButtonPress-1>", self.suspend_follow)
        scroll.bind("<MouseWheel>", self.suspend_follow)
        self.text_font = font.Font(font=self.body.cget("font"))
        self.body.bind("<Configure>", self.resize_body)
        self.body.bind("<MouseWheel>", self.suspend_follow)
        self.body.bind("<B2-Motion>", self.suspend_follow)
        for key in ("Prior", "Next", "Up", "Down", "Home", "End"):
            self.body.bind(f"<{key}>", self.page_key)
            self.root.bind(f"<{key}>", self.page_key)
        self.body.pack(fill="both", expand=True)
        self.context_menu = tk.Menu(self.body, tearoff=False)
        self.context_menu.add_command(label="从此处朗读", command=self.play_context)
        self.context_target = None
        self.body.bind("<Button-1>", self.pick)
        self.body.bind("<ButtonRelease-1>", self.finish_pick)
        self.body.bind("<Button-3>", self.show_context)
        for key, delta in (("Left", -1), ("Right", 1)):
            self.body.bind(f"<{key}>", lambda e, d=delta: self.move(d))

    def button(self, parent, text, action):
        button = ttk.Button(parent, text=text, command=action, cursor="hand2")
        button.bind("<space>", self.space)
        return button

    def change_volume(self, value):
        self.player.volume = value
        self.volume_text.set(f"{value}%")
        if self.volume_timer is not None:
            self.root.after_cancel(self.volume_timer)
        self.volume_timer = self.root.after(350, self.save_volume)

    def save_volume(self):
        self.volume_timer = None
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO preferences VALUES('volume', ?)", (self.player.volume,))

    def toggle_appearance(self, event=None):
        self.appearance = "light" if self.appearance == "dark" else "dark"
        self.apply_appearance()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO preferences VALUES('appearance', ?)", (self.appearance,))
        return "break"

    def on_window_map(self, event):
        if event.widget is self.root:
            style_title_bar(self.root, self.appearance == "dark")

    def apply_appearance(self):
        dark = self.appearance == "dark"
        p = PALETTES[self.appearance]
        style_controls(self.root, p)
        self.volume_slider.set_colors(p, dark)
        if self.root.winfo_ismapped():
            style_title_bar(self.root, dark)
        self.root.configure(background=p.paper)
        self.theme_button.configure(text="切换浅色" if dark else "切换深色")
        self.area.configure(background=p.border)
        self.toc.configure(bg=p.side, fg=p.text, selectbackground=p.hover, selectforeground=p.text)
        self.body.configure(bg=p.paper, fg=p.text, insertbackground=p.text, selectbackground=p.selection,
                            inactiveselectbackground=p.selection, selectforeground="white")
        self.body.tag_configure("playing", background=p.playing)
        self.body.tag_configure("picked", underline=True, background=p.picked, foreground=p.text)
        self.body.tag_raise("sel")
        for menu in (self.recent_menu, self.context_menu):
            menu.configure(bg=p.control, fg=p.text, activebackground=p.hover, activeforeground=p.text,
                           disabledforeground=p.muted)

    def choose(self):
        name = filedialog.askopenfilename(filetypes=[("EPUB 电子书", "*.epub"), ("所有文件", "*.*")])
        if name:
            self.open_book(Path(name))

    def populate_recent_menu(self):
        self.recent_menu.delete(0, "end")
        paths = [Path(row[0]) for row in self.db.execute("SELECT path FROM recent_files ORDER BY id DESC LIMIT 10")]
        names = Counter(path.name.casefold() for path in paths)
        for path in paths:
            label = path.name if names[path.name.casefold()] == 1 else f"{path.name}　({path.parent})"
            self.recent_menu.add_command(label=label, command=lambda p=path: self.open_book(p))
        if not paths:
            self.recent_menu.add_command(label="暂无最近打开的文件", state="disabled")

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
        except FileNotFoundError:
            if new:
                new.close()
            messagebox.showwarning("找不到 EPUB 文件", f"文件可能已移动或删除。原位置为\n{path}", parent=self.root)
            return
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
        self.heard.clear()
        self.play_button.config(state="normal" if self.available(self.settings) else "disabled")
        self.status.set(("可从上次位置续听" if saved else "点选正文后开始朗读") if self.available(self.settings) else self.failure)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO recent_files(path) VALUES(?)", (str(path),))
            self.db.execute("DELETE FROM recent_files WHERE id IN "
                            "(SELECT id FROM recent_files ORDER BY id DESC LIMIT -1 OFFSET 10)")

    def display(self, doc):
        if self.doc == doc:
            return
        content, _ = self.book.document(doc)
        self.body.config(state="normal")
        self.body.delete("1.0", "end")
        # Keep the scroll padding on its own line so the final passage's
        # selection and playback colors cover text rather than blank space.
        self.body.insert("1.0", content + "\n")
        self.body.tag_add("page_tail", "end-1c linestart", "end")
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
        text, _ = self.book.document(self.doc)
        offset = min(offset, len(text))
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
        offset = len(self.body.get("1.0", hit))
        self.context_target = self.book, self.doc, offset
        self.context_menu.entryconfigure(0, state="normal" if self.available(self.settings) and text.strip() else "disabled")
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()
        return "break"

    def play_context(self):
        if self.context_target and self.context_target[0] is self.book:
            _, doc, offset = self.context_target
            self.read_from_here((doc, offset))

    def read_from_here(self, position=None):
        if not self.book:
            return
        if position is None:
            selected = self.body.tag_ranges("sel")
            position = (self.doc, len(self.body.get("1.0", selected[0]))) if selected else self.cursor
        doc, offset = position
        text, _ = self.book.document(doc)
        self.start((doc, sentence_start(text, offset)))

    def move(self, delta):
        if self.book:
            self.follow = False
            text, _ = self.book.document(self.doc)
            self.choose_offset(min(len(text), max(0, self.cursor[1] + delta)), scroll=True)
        return "break"

    def suspend_follow(self, event=None):
        self.follow = False

    def manual_scroll(self, *args):
        self.suspend_follow()
        self.body.yview(*args)

    def page_key(self, event):
        if event.widget not in (self.body, self.root) and not isinstance(event.widget, ttk.Button):
            return
        self.suspend_follow()
        if event.state & 1:  # Preserve native Shift-selection bindings.
            return
        key = event.keysym
        if key in ("Home", "End"):
            self.body.yview_moveto(0 if key == "Home" else 1)
        else:
            self.body.yview_scroll(-1 if key in ("Prior", "Up") else 1,
                                   "pages" if key in ("Prior", "Next") else "units")
        return "break"

    def resize_body(self, event):
        inset = self.body.winfo_pixels(self.body.cget("pady"))
        padding = (event.height - 2 * inset - 2 * self.text_font.metrics("linespace")
                   - self.body.winfo_pixels(self.body.cget("spacing3")))
        self.body.tag_configure("page_tail", spacing1=0, spacing3=max(0, padding))
        if self.follow and self.audible:
            self.place_passage(self.audible)

    def place_passage(self, span, at_top=False):
        start = f"1.0+{span.start}c"
        if not at_top:
            first = self.body.bbox(start)
            last = self.body.bbox(f"1.0+{span.end - 1}c")
            inset = self.body.winfo_pixels(self.body.cget("pady"))
            # Tk clips bbox height at the bottom, and can return a negative
            # y at the top. A nonempty box alone does not mean full visibility.
            if (first and last and first[1] >= inset and last[3] >= self.text_font.metrics("linespace")
                    and last[1] + last[3] <= self.body.winfo_height() - inset):
                return
        self.body.yview(start)

    def jump_to_reading(self, event=None):
        if self.audible:
            self.follow = True
            self.show_reading(self.audible, at_top=True)
            self.body.focus_set()
        return "break"

    def invalidate(self):
        self.epoch.value += 1
        self.mac_client.cancel()
        self.segment_notice = ""
        self.player.reset()
        self.pending_marks.clear()
        self.opening_audio = None
        self.opening_deadline = 0.0
        self.active = self.paused = self.busy = self.eof = False
        self.audible = None
        self.body.tag_remove("playing", "1.0", "end")
        self.pause_button.config(text="暂停", state="disabled")
        self.stop_button.config(state="disabled")
        self.jump_button.config(state="disabled")

    def start(self, position, *, follow=True):
        if not self.book or not self.available(self.settings):
            return
        self.invalidate()
        self.active = self.first = True
        self.follow = follow
        self.unvoiced = 0
        self.position = position
        self.play_origin = self.position
        self.body.tag_remove("picked", "1.0", "end")
        self.pause_button.config(state="normal")
        self.stop_button.config(state="normal")
        self.status.set("正在准备首句…" if self.ready or self.settings.provider == "mac" else "正在加载本地模型…")

    def stop_reading(self):
        self.invalidate()
        self.status.set("朗读已停止")

    def skip_passage(self, direction):
        if not self.book:
            return
        point = (self.audible.doc, self.audible.start) if self.audible else (
            self.play_origin if self.active else self.cursor)
        if direction > 0:
            if self.audible:
                target = self.audible.next_position
            else:
                window = next_passage(self.book, point)
                if window is None:
                    return
                end, _ = choose_cut(window.text, window.more, self.pacing.target_length(0, opening=True))
                target = window.prefix(end).next_position
            if next_passage(self.book, target) is None:
                self.status.set("已是最后一段")
                return
        else:
            previous = [p for p in self.heard if p < point]
            if previous:
                target = max(previous)
            else:
                doc, offset = point
                while not offset and doc > 0:
                    doc -= 1
                    offset = len(self.book.document(doc)[0])
                text = self.book.document(doc)[0]
                target = (doc, sentence_start(text, max(0, offset - 1)))
        self.display(target[0])
        self.choose_offset(target[1], scroll=True)
        self.start(target)

    def apply_settings(self, settings):
        if settings == self.settings:
            return
        mode = next(name for name, steps in VOICE_MODES.items() if steps == settings.steps)
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO preferences VALUES(?,?)",
                                [("voice_mode", mode), ("speed", settings.speed),
                                  ("guidance", settings.guidance), ("softness", settings.softness),
                                  ("use_prosody", int(settings.use_prosody)),
                                  ("provider", settings.provider), ("mac_voice", settings.mac_voice)])
        self.settings = settings
        if settings.provider == "zipvoice":
            self.ensure_local_worker()
        self.play_button.config(state="normal" if self.book and self.available(settings) else "disabled")
        self.pacing = Pacing()
        if self.active:
            paused = self.paused
            point = (self.audible.doc, self.audible.start) if self.audible else self.play_origin
            self.start(point, follow=self.follow)
            if paused:
                self.toggle()

    def open_voice_panel(self):
        if self.voice_panel is None:
            self.voice_panel = VoicePanel(self)
        else:
            self.voice_panel.lift()

    def preview_voice(self, text, settings):
        if settings.provider == "zipvoice":
            self.ensure_local_worker()
        if not self.available(settings):
            self.voice_panel.feedback.set(self.failure)
            return
        if self.active:
            point = (self.audible.doc, self.audible.start) if self.audible else self.play_origin
            self.resume_after_preview = (point, self.paused, self.follow)
        if not self.previewing and self.resume_after_preview is None:
            self.status_before_preview = self.status.get()
        self.invalidate()
        self.previewing, self.preview_playing = True, False
        self.voice_panel.stop.state(["!disabled"])
        self.voice_panel.feedback.set("正在向 Mac 请求试听…" if settings.provider == "mac" else "正在本地合成试听…")
        self.status.set("语音试听中")
        self.preview_text = text
        self.preview_settings = settings
        self.preview_pending = True
        self.preview_waiting = None
        self.submit_speech(Passage(-1, 0, len(text), text), settings)

    def submit_speech(self, window, settings, target=DEFAULT_TARGET):
        if settings.provider == "zipvoice":
            self.requests.put((self.epoch.value, window, settings, target))
        else:
            end, _ = choose_cut(window.text, window.more, target)
            self.mac_client.submit(self.epoch.value, window.prefix(end), settings.mac_voice)

    def stop_preview(self, message="试听已停止"):
        if self.previewing:
            self.invalidate()
            self.previewing = self.preview_playing = False
            self.voice_panel.stop.state(["disabled"])
            self.voice_panel.feedback.set(message)
            self.status.set("关闭语音调节后返回正文" if self.resume_after_preview else self.status_before_preview)

    def close_voice_panel(self, save=False):
        panel = self.voice_panel
        if save:
            self.apply_settings(panel.values())
        self.stop_preview()
        panel.destroy()
        self.voice_panel = None
        if self.resume_after_preview is not None:
            point, paused, follow = self.resume_after_preview
            self.resume_after_preview = None
            if self.available(self.settings):
                self.start(point, follow=follow)
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
            self.read_from_here()
        return "break"

    def highlight(self, span):
        self.audible = span
        self.heard.append((span.doc, span.start))
        self.db.execute("INSERT OR REPLACE INTO progress VALUES(?,?,?)", (self.identity, span.doc, span.start))
        self.db.commit()
        self.jump_button.config(state="normal")
        self.show_reading(span)

    def show_reading(self, span, at_top=False):
        if self.follow:
            self.display(span.doc)
            self.cursor = (span.doc, span.start)
            self.place_passage(span, at_top)
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
                if not self.active and not self.book and not self.previewing and self.settings.provider == "zipvoice":
                    self.status.set("离线模型已就绪")
            elif msg[0] == "fatal":
                self.failure = msg[1]
                self.ready = False
                current = self.preview_settings if self.previewing else self.settings
                if current.provider == "zipvoice":
                    raise WorkerUnavailable(self.failure)
            elif msg[1] == self.epoch.value:
                if self.previewing:
                    if msg[0] == "audio":
                        self.player.append(msg[2], msg[3])
                        end = msg[2].end
                        self.preview_pending = end < len(self.preview_text)
                        if self.preview_pending:
                            if self.preview_settings.provider == "mac":
                                self.preview_waiting = msg[2]
                            else:
                                text = self.preview_text
                                self.submit_speech(Passage(-1, end, len(text), text[end:]), self.preview_settings)
                        self.preview_playing = True
                        self.voice_panel.feedback.set("正在播放试听…")
                    else:
                        self.stop_preview(msg[2] if msg[0] in ("error", "segment_error") else "这段文字未能发音，请换一句试听。")
                    continue
                if msg[0] == "segment_error":
                    self.busy = False
                    self.eof = True
                    self.segment_notice = msg[2]
                    self.release_opening()
                    continue
                if msg[0] == "error":
                    raise ValueError(msg[2])
                self.position = msg[2].next_position
                if msg[0] == "unvoiced":
                    self.busy = False
                    self.unvoiced += 1
                    continue
                _, _, span, pcm, elapsed = msg
                self.busy = False
                self.pacing.observe(spoken_count(span.text), len(pcm) / 48000, elapsed)
                if self.first:
                    self.first = False
                    self.opening_audio = (span, pcm)
                else:
                    self.release_opening()
                    self.player.append(span, pcm)
        if self.worker is not None and not self.failure and not self.worker.is_alive() and self.worker.exitcode is not None:
            self.failure = "本地 ZipVoice 进程已退出。请重新打开听页。"
            current = self.preview_settings if self.previewing else self.settings
            if current.provider == "zipvoice":
                raise WorkerUnavailable(self.failure)
        if self.previewing:
            if self.preview_waiting is not None:
                while not self.player.events.empty():
                    self.pending_marks.append(self.player.events.get())
                while self.pending_marks and self.pending_marks[0][0] <= time.monotonic():
                    _, span = self.pending_marks.popleft()
                    if span == self.preview_waiting:
                        self.preview_waiting = None
                        text = self.preview_text
                        self.submit_speech(Passage(-1, span.end, len(text), text[span.end:]), self.preview_settings)
                        break
            if self.preview_playing and self.player.stream is not None and not self.player.stream.active:
                self.stop_preview("音频输出已中断，请检查播放设备后重新试听。")
            elif self.preview_playing and not self.preview_pending and self.player.buffered() <= 0:
                self.stop_preview("试听结束，可调整后再次试听。")
            return
        if not self.active or self.book is None:
            return
        while not self.player.events.empty():
            self.pending_marks.append(self.player.events.get())
        while self.pending_marks and self.pending_marks[0][0] <= time.monotonic():
            span = self.pending_marks.popleft()[1]
            self.highlight(span)
        if self.settings.provider == "zipvoice" and not self.ready:
            return
        if self.player.stream is not None and not self.player.stream.active:
            raise ValueError("音频输出已中断。请检查播放设备，再从此处朗读。")
        buffered = self.player.buffered()
        opening_seconds = len(self.opening_audio[1]) / 48000 if self.opening_audio else 0
        if self.eof and not self.busy and buffered <= 0 and not self.paused and not self.pending_marks:
            completed = self.audible is not None
            notice = self.segment_notice
            self.invalidate()
            if notice:
                self.status.set("朗读已停在无法断句的位置")
                messagebox.showwarning("无法继续断句", notice, parent=self.root)
                return
            summary = "全书朗读完毕" if completed else "当前位置之后没有可朗读的正文"
            if self.unvoiced:
                summary += f"（{self.unvoiced} 个片段未能发音）"
            self.status.set(summary)
            return
        mac = self.settings.provider == "mac"
        can_prefetch = buffered < self.pacing.buffer_goal and not (mac and self.paused)
        if not self.busy and not self.eof and can_prefetch:
            span = next_passage(self.book, self.position)
            if span is None:
                self.eof = True
                self.release_opening()
            else:
                target = self.pacing.target_length(buffered + opening_seconds, opening=self.first)
                self.submit_speech(span, self.settings, target)
                self.busy = True
                if self.opening_audio and not self.opening_deadline:
                    self.opening_deadline = time.monotonic() + self.pacing.opening_delay(opening_seconds, span, target)
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
            self.poll()
        except Exception as exc:
            self.stop_preview(str(exc))
            self.invalidate()
            if isinstance(exc, WorkerUnavailable):
                self.failure = str(exc)
                self.play_button.config(state="normal" if self.book and self.available(self.settings) else "disabled")
                if self.voice_panel is not None:
                    self.voice_panel.feedback.set(self.failure)
            self.status.set("朗读已中止")
            messagebox.showerror("朗读遇到问题", str(exc), parent=self.root)
        if not self.closed:
            self.timer = self.root.after(35, self.tick)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.volume_timer is not None:
            self.root.after_cancel(self.volume_timer)
            self.save_volume()
        self.root.after_cancel(self.timer)
        self.invalidate()
        self.mac_client.close()
        if self.worker is not None:
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
