import sys
import tkinter as tk
from tkinter import ttk

from .appearance import PALETTES, style_title_bar
from .mac_speech import PROVIDERS, VOICES
from .speech import VoiceSettings


class VoicePanel(tk.Toplevel):
    def __init__(self, reader):
        super().__init__(reader.root)
        self.withdraw()
        self.reader = reader
        self.colors = PALETTES[reader.appearance]
        self.configure(background=self.colors.paper)
        self.title("语音调节 · 听页")
        self.transient(reader.root)
        self.resizable(True, False)
        self.columnconfigure(0, weight=1)
        panel = ttk.Frame(self, padding=(20, 16))
        panel.grid(sticky="nsew")
        panel.columnconfigure(1, weight=1)
        heading = ttk.Frame(panel)
        heading.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        ttk.Label(heading, text="语音调节", font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        saved = reader.settings
        detail = (VOICES[saved.mac_voice] if saved.provider == "mac" else
                  f"{saved.steps} 步　·　语速 {saved.speed:.2f} 倍　·　引导 {saved.guidance:.2f}　·　柔化 {saved.softness}%")
        ttk.Label(heading, text=f"当前生效　{PROVIDERS[saved.provider]}　·　{detail}").pack(anchor="w", pady=(4, 0))
        selection = ttk.Frame(heading)
        selection.pack(anchor="w", pady=(8, 0))
        ttk.Label(selection, text="朗读服务").pack(side="left")
        self.provider_box = ttk.Combobox(selection, values=list(PROVIDERS.values()), state="readonly",
                                         exportselection=False, width=23)
        self.provider_box.pack(side="left", padx=6)
        self.voice_label = ttk.Label(selection, text="音色")
        self.voice_box = ttk.Combobox(selection, values=list(VOICES.values()), state="readonly",
                                      exportselection=False, width=13)
        self.provider_box.bind("<<ComboboxSelected>>", self.change_source)
        self.voice_box.bind("<<ComboboxSelected>>", lambda event: reader.stop_preview())

        self.steps = tk.IntVar(self)
        self.speed = tk.DoubleVar(self)
        self.guidance = tk.DoubleVar(self)
        self.softness = tk.IntVar(self)
        self.use_prosody = tk.BooleanVar(self)
        ttk.Label(panel, text="合成步数").grid(row=1, column=0, sticky="w", padx=(0, 16))
        choices = ttk.Frame(panel)
        choices.grid(row=1, column=1, columnspan=2, sticky="w")
        for n in (4, 8):
            ttk.Radiobutton(choices, text=f"{n} 步", variable=self.steps, value=n).pack(side="left", padx=(0, 12))
        self.hint(panel, 2, "4 步出声快。8 步计算更久，可用同一句话比较咬字和尾音。")
        self.slider(panel, 3, "朗读语速", self.speed, .75, 1.25, .05, "{:.2f} 倍",
                    "1.00 倍为原速。向左放慢，向右加快，会影响每句话的时长。")
        self.slider(panel, 5, "模型引导强度", self.guidance, 1, 4, .25, "{:.2f}",
                    "数值越大，合成时越强调文本和参考声音的引导。默认 3.00，可小幅调整后试听。")
        self.slider(panel, 7, "高频柔化", self.softness, 0, 100, 5, "{:.0f}%",
                    "0% 保留原声。向右可减弱刺耳的高频；太高会让齿音和咬字变闷。")

        ttk.Checkbutton(panel, text="WeTTS 自动断句（本地）", variable=self.use_prosody).grid(
            row=9, column=0, columnspan=3, sticky="w", pady=(4, 8))
        self.local_controls = [w for w in panel.grid_slaves() if 1 <= int(w.grid_info()["row"]) <= 9]
        ttk.Label(panel, text="试听文本").grid(row=10, column=0, sticky="w")
        self.sample = ttk.Entry(panel)
        self.sample.grid(row=10, column=1, columnspan=2, sticky="ew")
        self.sample.insert(0, "风从窗边吹过，她合上手里的书，侧耳听了片刻，才轻声问道：“刚才走廊里的脚步声，你也听见了吗？”")
        self.hint(panel, 11, "可换成书中一句话。试听时会暂时中断正文，关闭本页后恢复原来的朗读状态。")
        self.feedback = tk.StringVar(self, value=reader.failure or "")
        ttk.Label(panel, textvariable=self.feedback, wraplength=480).grid(
            row=12, column=0, columnspan=3, sticky="w")
        actions = ttk.Frame(panel)
        actions.grid(row=13, column=0, columnspan=3, sticky="ew", pady=(9, 0))
        self.listen = ttk.Button(actions, text="试听当前设置", command=self.preview)
        self.listen.pack(side="left")
        self.stop = ttk.Button(actions, text="停止试听", command=reader.stop_preview, state="disabled")
        self.stop.pack(side="left", padx=5)
        ttk.Button(actions, text="恢复默认", command=lambda: self.set_values(VoiceSettings())).pack(side="left")
        ttk.Button(actions, text="保存并关闭", command=lambda: reader.close_voice_panel(True)).pack(side="right")
        ttk.Button(actions, text="取消", command=reader.close_voice_panel).pack(side="right", padx=5)
        self.set_values(reader.settings)
        self.protocol("WM_DELETE_WINDOW", reader.close_voice_panel)
        self.bind("<Escape>", lambda e: reader.close_voice_panel())
        self.center_on_reader()
        style_title_bar(self, reader.appearance == "dark")
        self.grab_set()
        self.listen.focus_set()

    def center_on_reader(self):
        parent = self.reader.root
        self.update_idletasks()
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class MonitorInfo(ctypes.Structure):
                _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                            ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

            user = ctypes.windll.user32
            user.MonitorFromWindow.restype = wintypes.HANDLE
            owner = wintypes.HWND(int(parent.frame(), 0))
            monitor = user.MonitorFromWindow(owner, 2)  # MONITOR_DEFAULTTONEAREST
            info = MonitorInfo()
            info.size = ctypes.sizeof(info)
            frame, dialog = wintypes.RECT(), wintypes.RECT()
            if (user.GetMonitorInfoW(wintypes.HANDLE(monitor), ctypes.byref(info))
                    and user.GetWindowRect(owner, ctypes.byref(frame))
                    and user.GetWindowRect(wintypes.HWND(int(self.frame(), 0)), ctypes.byref(dialog))):
                width, height = dialog.right - dialog.left, dialog.bottom - dialog.top
                x = (frame.left + frame.right - width) // 2
                y = (frame.top + frame.bottom - height) // 2
                x = max(info.work.left, min(x, info.work.right - width))
                y = max(info.work.top, min(y, info.work.bottom - height))
                self.geometry(f"+{x}+{y}")
                self.deiconify()
                return
        self.tk.call("tk::PlaceWindow", self, "widget", parent)

    @staticmethod
    def hint(panel, row, text):
        ttk.Label(panel, text=text, style="Muted.TLabel", wraplength=475).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(1, 8))

    def slider(self, panel, row, title, variable, low, high, increment, fmt, explanation):
        ttk.Label(panel, text=title).grid(row=row, column=0, sticky="w")
        scale = tk.Scale(panel, orient="horizontal", from_=low, to=high, resolution=increment,
                         variable=variable, showvalue=False, highlightthickness=0, length=220,
                         bg=self.colors.thumb, fg=self.colors.text, troughcolor=self.colors.control,
                         activebackground=self.colors.muted, relief="flat", borderwidth=0)
        scale.grid(row=row, column=1, sticky="ew", padx=(0, 10))
        value = ttk.Label(panel, width=8, anchor="e")
        value.grid(row=row, column=2)
        variable.trace_add("write", lambda *_: value.configure(text=fmt.format(variable.get())))
        self.hint(panel, row + 1, explanation)

    def set_values(self, settings):
        self.provider_box.current(list(PROVIDERS).index(settings.provider))
        self.voice_box.current(list(VOICES).index(settings.mac_voice))
        self.change_source()
        self.steps.set(settings.steps)
        self.speed.set(settings.speed)
        self.guidance.set(settings.guidance)
        self.softness.set(settings.softness)
        self.use_prosody.set(settings.use_prosody)

    def change_source(self, event=None):
        self.reader.stop_preview()
        mac = self.provider_box.get() == PROVIDERS["mac"]
        if mac:
            self.voice_label.pack(side="left")
            self.voice_box.pack(side="left", padx=6)
        else:
            self.voice_label.pack_forget()
            self.voice_box.pack_forget()
        for widget in self.local_controls:
            if mac:
                widget.grid_remove()
            else:
                widget.grid()
        self.feedback.set("" if mac else self.reader.failure)

    def values(self):
        return VoiceSettings(self.steps.get(), self.speed.get(), self.guidance.get(), self.softness.get(),
                             self.use_prosody.get(), list(PROVIDERS)[self.provider_box.current()],
                             list(VOICES)[self.voice_box.current()])

    def preview(self):
        text = self.sample.get().strip()
        settings = self.values()
        if len(text) > 120 or not any(c.isalnum() for c in text):
            self.feedback.set("试听至少需要 1 个朗读字符（标点和空白不计），输入总长最多 120 字符。")
            return
        self.reader.preview_voice(text, settings)
