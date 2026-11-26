import tkinter as tk


class VolumeSlider(tk.Canvas):
    """An application volume slider with a filled track and keyboard focus."""

    def __init__(self, parent, value, on_change):
        super().__init__(parent, width=155, height=28, bd=0, highlightthickness=0,
                         takefocus=True, cursor="hand2")
        self.value = value
        self.on_change = on_change
        self.colors = None
        self.bind("<Configure>", self.draw)
        self.bind("<FocusIn>", self.draw)
        self.bind("<FocusOut>", self.draw)
        self.bind("<ButtonPress-1>", self.point)
        self.bind("<B1-Motion>", self.point)
        for key in ("Left", "Right", "Up", "Down", "Home", "End"):
            self.bind(f"<{key}>", self.key)

    def set_colors(self, palette, dark):
        self.colors = palette
        self.accent = "#60cdff" if dark else "#0067c0"
        self.configure(background=palette.side)
        self.draw()

    def draw(self, event=None):
        if self.colors is None:
            return
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 24:
            return
        left, right, y = 12, w - 12, h / 2
        x = left + (right - left) * self.value / 100
        self.create_line(left, y, right, y, width=4, fill=self.colors.thumb, capstyle="round")
        self.create_line(left, y, x, y, width=4, fill=self.accent, capstyle="round")
        self.create_oval(x-9, y-9, x+9, y+9, fill=self.colors.paper, outline=self.colors.thumb)
        self.create_oval(x-5, y-5, x+5, y+5, fill=self.accent, outline="")
        if self.focus_get() is self:
            self.create_rectangle(1, 1, w-2, h-2, outline=self.colors.muted, dash=(2, 2))

    def set_value(self, value):
        value = max(0, min(100, round(value)))
        if value != self.value:
            self.value = value
            self.on_change(value)
            self.draw()

    def point(self, event):
        self.focus_set()
        self.set_value(100 * (event.x - 12) / max(1, self.winfo_width() - 24))
        return "break"

    def key(self, event):
        values = {"Home": 0, "End": 100, "Left": self.value-1, "Down": self.value-1,
                  "Right": self.value+1, "Up": self.value+1}
        self.set_value(values[event.keysym])
        return "break"
