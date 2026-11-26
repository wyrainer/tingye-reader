import sys
from dataclasses import dataclass
from tkinter import ttk


@dataclass(frozen=True)
class Palette:
    paper: str
    side: str
    text: str
    muted: str
    border: str
    control: str
    hover: str
    thumb: str
    selection: str
    playing: str
    picked: str


PALETTES = {
    "light": Palette(paper="#ffffff", side="#f9f9f9", text="#171717", muted="#606060",
                     border="#dedede", control="#efefef", hover="#e3e3e3", thumb="#aaaaaa",
                     selection="#315f96", playing="#d9eee3", picked="#f6e6b9"),
    "dark": Palette(paper="#212121", side="#171717", text="#ececec", muted="#b4b4b4",
                    border="#454545", control="#303030", hover="#414141", thumb="#686868",
                    selection="#365b85", playing="#284c3f", picked="#594b30"),
}


def style_controls(root, p):
    style = ttk.Style(root)
    style.configure(".", background=p.paper, foreground=p.text, fieldbackground=p.control,
                    bordercolor=p.border, lightcolor=p.border, darkcolor=p.border,
                    troughcolor=p.side, selectbackground=p.selection, selectforeground="white")
    style.map(".", foreground=[("disabled", p.muted)], background=[("disabled", p.side), ("active", p.hover)],
              selectbackground=[("!focus", p.selection)])
    style.configure("Sidebar.TFrame", background=p.side)
    style.configure("Sidebar.TLabel", background=p.side)
    style.configure("Muted.Sidebar.TLabel", foreground=p.muted)
    style.configure("Muted.TLabel", foreground=p.muted)
    style.configure("TSeparator", background=p.border)
    style.configure("TScrollbar", background=p.thumb, troughcolor=p.side, borderwidth=0, arrowsize=12,
                    arrowcolor=p.text, lightcolor=p.thumb, darkcolor=p.thumb)
    for name in ("TButton", "TMenubutton", "TRadiobutton"):
        style.configure(name, font=("Microsoft YaHei UI", 10, "bold"), padding=(10, 5),
                        relief="flat", borderwidth=0, focusthickness=1, width=0)
    for name in ("TButton", "TMenubutton"):
        style.configure(name, background=p.control, foreground=p.text, arrowcolor=p.text)
        style.map(name, background=[("disabled", p.control), ("pressed", p.hover), ("active", p.hover)],
                  foreground=[("disabled", p.muted)], lightcolor=[("pressed", p.border)],
                  darkcolor=[("pressed", p.border)])
    style.configure("TEntry", insertcolor=p.text)
    style.map("TCombobox", foreground=[("readonly", p.text)], fieldbackground=[("readonly", p.control)],
              selectforeground=[("readonly", p.text)], selectbackground=[("readonly", p.control)],
              arrowcolor=[("readonly", p.text)], bordercolor=[("focus", p.selection)])
    style.configure("TRadiobutton", indicatorbackground=p.control, indicatorforeground=p.text,
                    upperbordercolor=p.thumb, lowerbordercolor=p.thumb)
    style.map("TRadiobutton", indicatorbackground=[("pressed", p.hover), ("selected", p.control)])


def style_title_bar(window, dark):
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        hwnd = wintypes.HWND(int(window.frame(), 0))
        enabled = wintypes.BOOL(dark)
        # Windows versions supporting immersive dark title bars use attribute 20.
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(enabled), ctypes.sizeof(enabled))
        # Windows 11 also lets the app set the caption when Windows itself uses light mode.
        color = wintypes.DWORD(0x212121 if dark else 0xFFFFFFFF)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), ctypes.sizeof(color))
        text = wintypes.DWORD(0xECECEC if dark else 0xFFFFFFFF)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text), ctypes.sizeof(text))
