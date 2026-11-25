import tkinter as tk
from tkinter import ttk

from .speech import VoiceSettings


class VoicePanel(tk.Toplevel):
    def __init__(self, reader):
        super().__init__(reader.root)
        self.reader = reader
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
        ttk.Label(heading, text=f"当前生效　{saved.steps} 步　·　语速 {saved.speed:.2f} 倍　·　"
                                f"引导 {saved.guidance:.2f}　·　柔化 {saved.softness}%").pack(anchor="w", pady=(4, 0))

        self.steps = tk.IntVar(self)
        self.speed = tk.DoubleVar(self)
        self.guidance = tk.DoubleVar(self)
        self.softness = tk.IntVar(self)
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

        ttk.Separator(panel).grid(row=9, column=0, columnspan=3, sticky="ew", pady=8)
        ttk.Label(panel, text="试听文本").grid(row=10, column=0, sticky="w")
        self.sample = ttk.Entry(panel)
        self.sample.grid(row=10, column=1, columnspan=2, sticky="ew")
        self.sample.insert(0, "风从窗边吹过。她合上书，轻声问：“你也听见了吗？”")
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
        if reader.failure:
            self.listen.state(["disabled"])
        self.protocol("WM_DELETE_WINDOW", reader.close_voice_panel)
        self.bind("<Escape>", lambda e: reader.close_voice_panel())
        self.grab_set()
        self.listen.focus_set()

    @staticmethod
    def hint(panel, row, text):
        ttk.Label(panel, text=text, foreground="#536961", wraplength=475).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(1, 8))

    def slider(self, panel, row, title, variable, low, high, increment, fmt, explanation):
        ttk.Label(panel, text=title).grid(row=row, column=0, sticky="w")
        scale = tk.Scale(panel, orient="horizontal", from_=low, to=high, resolution=increment,
                         variable=variable, showvalue=False, highlightthickness=0, length=220)
        scale.grid(row=row, column=1, sticky="ew", padx=(0, 10))
        value = ttk.Label(panel, width=8, anchor="e")
        value.grid(row=row, column=2)
        variable.trace_add("write", lambda *_: value.configure(text=fmt.format(variable.get())))
        self.hint(panel, row + 1, explanation)

    def set_values(self, settings):
        self.steps.set(settings.steps)
        self.speed.set(settings.speed)
        self.guidance.set(settings.guidance)
        self.softness.set(settings.softness)

    def values(self):
        return VoiceSettings(self.steps.get(), self.speed.get(), self.guidance.get(), self.softness.get())

    def preview(self):
        text = self.sample.get().strip()
        if not 1 <= len(text) <= 120 or not any(c.isalnum() for c in text):
            self.feedback.set("请输入 1 到 120 个字，至少包含一个汉字、字母或数字。")
            return
        self.reader.preview_voice(text, self.values())
