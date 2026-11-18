"""Shared dark look for the desktop app and its tool windows."""
from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from ctypes import wintypes
from tkinter import font as tkfont, ttk

INK = "#0b0d12"
PANEL = "#12151c"
RAISED = "#1a1e27"
HOVER = "#242a35"
LINE = "#2a303c"
TEXT = "#e8ebf0"
MUTED = "#8b94a5"
ACCENT = "#c6f135"
ACCENT_HOVER = "#d8ff57"
ACCENT_DIM = "#28330f"
DANGER = "#ff6b5e"


def fonts(root: tk.Misc) -> dict[str, tuple]:
    families = set(tkfont.families(root))
    display = "Bahnschrift" if "Bahnschrift" in families else "Segoe UI"
    return {"ui": ("Segoe UI", 10), "bold": ("Segoe UI Semibold", 10), "small": (display, 8),
            "title": (display, 15, "bold"), "hero": ("Segoe UI", 12), "mono": ("Consolas", 9)}


def dark_titlebar(window: tk.Misc) -> None:
    if sys.platform != "win32":
        return
    try:
        window.update_idletasks()
        get_parent = ctypes.windll.user32.GetParent
        get_parent.argtypes, get_parent.restype = [wintypes.HWND], wintypes.HWND
        hwnd = get_parent(window.winfo_id()) or window.winfo_id()
        dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute
        dwm.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwm(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)                  # dark mode
        color = int(INK[5:7] + INK[3:5] + INK[1:3], 16)
        dwm(hwnd, 35, ctypes.byref(ctypes.c_int(color)), 4)              # caption (Windows 11)
    except (AttributeError, OSError, tk.TclError):
        pass


def app_icon(root: tk.Misc) -> tk.PhotoImage:
    size = 32
    icon = tk.PhotoImage(master=root, width=size, height=size)
    icon.put(INK, to=(0, 0, size, size))
    icon.put(MUTED, to=(5, 5, 21, 21))
    icon.put(INK, to=(8, 8, 18, 18))
    icon.put(ACCENT, to=(11, 11, 27, 27))
    return icon


def apply_theme(root: tk.Tk) -> dict[str, tuple]:
    f = fonts(root)
    root.configure(bg=INK)
    for pattern, value in (("*Toplevel.background", INK), ("*Text.background", PANEL), ("*Text.foreground", TEXT),
                           ("*Text.insertBackground", ACCENT), ("*Text.selectBackground", ACCENT_DIM),
                           ("*Text.selectForeground", TEXT), ("*Text.relief", "flat"),
                           ("*Listbox.background", PANEL), ("*Listbox.foreground", TEXT),
                           ("*Listbox.selectBackground", ACCENT_DIM), ("*Listbox.selectForeground", ACCENT),
                           ("*TCombobox*Listbox.background", RAISED), ("*TCombobox*Listbox.foreground", TEXT),
                           ("*TCombobox*Listbox.selectBackground", ACCENT_DIM),
                           ("*TCombobox*Listbox.selectForeground", ACCENT),
                           ("*Menu.background", RAISED), ("*Menu.foreground", TEXT),
                           ("*Menu.activeBackground", ACCENT), ("*Menu.activeForeground", INK),
                           ("*Menu.relief", "flat"), ("*Menu.borderWidth", 0)):
        root.option_add(pattern, value)

    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=INK, foreground=TEXT, fieldbackground=RAISED, bordercolor=LINE, lightcolor=LINE,
                darkcolor=LINE, troughcolor=PANEL, focuscolor=ACCENT, selectbackground=ACCENT_DIM,
                selectforeground=TEXT, insertcolor=ACCENT, font=f["ui"])
    s.map(".", foreground=[("disabled", "#566072")])
    s.configure("Panel.TFrame", background=PANEL)
    s.configure("TLabelframe", background=INK, bordercolor=LINE)
    s.configure("TLabelframe.Label", background=INK, foreground=MUTED)
    s.configure("Panel.TLabel", background=PANEL, foreground=MUTED)
    s.configure("Section.TLabel", background=PANEL, foreground=MUTED, font=f["small"])
    s.configure("TButton", background=RAISED, foreground=TEXT, borderwidth=0, focusthickness=0, padding=(10, 6),
                relief="flat")
    s.map("TButton", background=[("disabled", PANEL), ("pressed", LINE), ("active", HOVER)])
    s.configure("Accent.TButton", background=ACCENT, foreground=INK, font=f["bold"], padding=(16, 8))
    s.map("Accent.TButton", background=[("disabled", RAISED), ("pressed", ACCENT), ("active", ACCENT_HOVER)],
          foreground=[("disabled", "#566072"), ("active", INK)])
    s.configure("Danger.TButton", foreground=DANGER)
    s.map("Danger.TButton", background=[("active", "#3a1b19")])
    s.configure("Header.TButton", background=INK, foreground=MUTED, padding=(8, 4))
    s.map("Header.TButton", background=[("active", RAISED)], foreground=[("active", TEXT)])
    s.configure("Ghost.TButton", background=PANEL, foreground=MUTED, padding=(8, 5))
    s.map("Ghost.TButton", background=[("active", RAISED)], foreground=[("active", TEXT)])
    for name in ("TEntry", "TSpinbox", "TCombobox"):
        s.configure(name, fieldbackground=RAISED, foreground=TEXT, bordercolor=LINE, lightcolor=RAISED,
                    darkcolor=RAISED, arrowcolor=MUTED, background=RAISED, padding=5, insertcolor=ACCENT)
        s.map(name, bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
              fieldbackground=[("readonly", RAISED)], foreground=[("readonly", TEXT)])
    s.configure("Hero.TEntry", padding=(12, 9))
    s.configure("TCheckbutton", background=PANEL, foreground=TEXT, indicatorbackground=RAISED,
                indicatorforeground=INK, indicatorrelief="flat", padding=3)
    s.map("TCheckbutton", background=[("active", PANEL)], indicatorbackground=[("selected", ACCENT)])
    s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=32, borderwidth=0)
    s.map("Treeview", background=[("selected", ACCENT_DIM)], foreground=[("selected", ACCENT)])
    s.configure("Treeview.Heading", background=PANEL, foreground=MUTED, relief="flat", borderwidth=0,
                font=f["small"], padding=(8, 7))
    s.map("Treeview.Heading", background=[("active", RAISED)], foreground=[("active", TEXT)])
    s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    for bar in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        s.configure(bar, background=LINE, troughcolor=PANEL, bordercolor=PANEL, lightcolor=LINE, darkcolor=LINE,
                    arrowcolor=MUTED, relief="flat", arrowsize=11)
        s.map(bar, background=[("active", MUTED)])
    s.configure("TSeparator", background=LINE)
    s.configure("TPanedwindow", background=LINE)
    s.configure("TNotebook", background=INK, bordercolor=LINE)
    s.configure("TNotebook.Tab", background=RAISED, foreground=MUTED, padding=(10, 5))
    s.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", ACCENT)])
    return f
