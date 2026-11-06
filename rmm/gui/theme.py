"""Colors, fonts and ttk style setup (dark + light), built on the 'clam' theme."""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        "bg": "#0f1117", "panel": "#171a24", "header": "#13161f", "card": "#161922", "field": "#12151e",
        "text": "#f1f5f9", "muted": "#8b95a7",
        "accent": "#5b8def", "ok": "#22c55e", "warn": "#f59e0b", "err": "#ef4444", "select": "#24375e",
        "border": "#262b3b",
        # derived shades
        "hover": "#222736", "pressed": "#2c3347", "disabled": "#525b6d", "select_text": "#ffffff",
        "select_inactive": "#1c273d", "accent_hover": "#739ef2", "accent_pressed": "#4778db",
        "accent_text": "#ffffff", "ok_hover": "#34d369", "ok_pressed": "#16a34a",
        "err_hover": "#f87171", "err_pressed": "#dc2626", "scroll": "#2e3446",
        "scroll_hover": "#434c66", "border_hover": "#3d465e", "info": "#60a5fa", "link": "#7dd3fc",
        "toast_bg": "#1e2330", "badge_bg": "#1e2638",
    },
    "light": {
        "bg": "#f4f6fb", "panel": "#ffffff", "header": "#eef2f9", "card": "#ffffff", "field": "#ffffff",
        "text": "#0f172a", "muted": "#64748b",
        "accent": "#3b82f6", "ok": "#16a34a", "warn": "#d97706", "err": "#dc2626", "select": "#dbeafe",
        "border": "#cbd5e1",
        "hover": "#e8edf5", "pressed": "#dce3ef", "disabled": "#94a3b8", "select_text": "#0f172a",
        "select_inactive": "#e2e8f0", "accent_hover": "#2563eb", "accent_pressed": "#1d4ed8",
        "accent_text": "#ffffff", "ok_hover": "#15803d", "ok_pressed": "#166534",
        "err_hover": "#ef4444", "err_pressed": "#b91c1c", "scroll": "#cbd5e1",
        "scroll_hover": "#94a3b8", "border_hover": "#94a3b8", "info": "#2563eb", "link": "#2563eb",
        "toast_bg": "#ffffff", "badge_bg": "#e0e7ff",
    },
    "archotech": {
        "bg": "#0b0d0a", "panel": "#131711", "header": "#0f120d", "card": "#131711", "field": "#0e110c",
        "text": "#f5f7f2", "muted": "#9aa690",
        "accent": "#d4af37", "ok": "#2dd4bf", "warn": "#f59e0b", "err": "#ef4444", "select": "#2e361c",
        "border": "#2a3322",
        "hover": "#1d2318", "pressed": "#273020", "disabled": "#56614d", "select_text": "#ffffff",
        "select_inactive": "#1c2215", "accent_hover": "#e5c158", "accent_pressed": "#b59024",
        "accent_text": "#0b0d0a", "ok_hover": "#5eead4", "ok_pressed": "#14b8a6",
        "err_hover": "#f87171", "err_pressed": "#dc2626", "scroll": "#2b3324",
        "scroll_hover": "#3f4b35", "border_hover": "#4a573d", "info": "#e5c158", "link": "#fde047",
        "toast_bg": "#191f16", "badge_bg": "#242b18",
    },
    "glitterworld": {
        "bg": "#090b16", "panel": "#111528", "header": "#0c0f1f", "card": "#111528", "field": "#0b0e1c",
        "text": "#f0f6ff", "muted": "#8a9bc4",
        "accent": "#06b6d4", "ok": "#10b981", "warn": "#f59e0b", "err": "#f43f5e", "select": "#163554",
        "border": "#1e294b",
        "hover": "#19203a", "pressed": "#222b4d", "disabled": "#4c597a", "select_text": "#ffffff",
        "select_inactive": "#13233b", "accent_hover": "#22d3ee", "accent_pressed": "#0891b2",
        "accent_text": "#041016", "ok_hover": "#34d399", "ok_pressed": "#059669",
        "err_hover": "#fb7185", "err_pressed": "#e11d48", "scroll": "#212b4c",
        "scroll_hover": "#324170", "border_hover": "#384b82", "info": "#38bdf8", "link": "#67e8f9",
        "toast_bg": "#151b33", "badge_bg": "#142942",
    },
    "bloodmoon": {
        "bg": "#11090b", "panel": "#1b1013", "header": "#160c0f", "card": "#1b1013", "field": "#140b0d",
        "text": "#fdf2f4", "muted": "#b08d94",
        "accent": "#e11d48", "ok": "#22c55e", "warn": "#f59e0b", "err": "#ef4444", "select": "#471623",
        "border": "#361c22",
        "hover": "#26161b", "pressed": "#331d23", "disabled": "#664b51", "select_text": "#ffffff",
        "select_inactive": "#2b1219", "accent_hover": "#f43f5e", "accent_pressed": "#be123c",
        "accent_text": "#ffffff", "ok_hover": "#34d369", "ok_pressed": "#16a34a",
        "err_hover": "#f87171", "err_pressed": "#dc2626", "scroll": "#381d24",
        "scroll_hover": "#522a34", "border_hover": "#5c2f3a", "info": "#fb7185", "link": "#fda4af",
        "toast_bg": "#241317", "badge_bg": "#36151e",
    },
    "oled": {
        "bg": "#000000", "panel": "#0a0a0e", "header": "#050508", "card": "#0a0a0e", "field": "#040406",
        "text": "#fafafa", "muted": "#8e8e99",
        "accent": "#8b5cf6", "ok": "#22c55e", "warn": "#f59e0b", "err": "#ef4444", "select": "#271b47",
        "border": "#1f1f29",
        "hover": "#14141c", "pressed": "#1e1e2a", "disabled": "#4d4d59", "select_text": "#ffffff",
        "select_inactive": "#17112b", "accent_hover": "#a78bfa", "accent_pressed": "#7c3aed",
        "accent_text": "#ffffff", "ok_hover": "#34d369", "ok_pressed": "#16a34a",
        "err_hover": "#f87171", "err_pressed": "#dc2626", "scroll": "#22222e",
        "scroll_hover": "#353547", "border_hover": "#38384d", "info": "#a78bfa", "link": "#c4b5fd",
        "toast_bg": "#101017", "badge_bg": "#1d1536",
    },
}

THEME_LABELS: dict[str, str] = {
    "dark": "Dark",
    "light": "Light",
    "archotech": "★ Archotech Gold",
    "glitterworld": "★ Glitterworld",
    "bloodmoon": "★ Blood Moon",
    "oled": "★ Void OLED",
}

_FONT_NAMES = ("RmmBody", "RmmBold", "RmmHeading", "RmmTitle", "RmmSmall", "RmmMono")


def ui_scale(root: tk.Misc) -> float:
    """Pixels-per-96-dpi factor (1.0 at 100 %, 1.5 at 150 %)."""
    try:
        return max(1.0, root.winfo_fpixels("1i") / 96.0)
    except tk.TclError:
        return 1.0


def make_fonts(root: tk.Misc) -> dict[str, tkfont.Font]:
    """Create the named fonts once (later calls reuse them) and point Tk's standard fonts at them.
    Sizes are in points, so Tk's DPI scaling enlarges them on high-DPI screens."""
    cached = getattr(root, "_rmm_fonts", None)
    if cached is None:
        fams = set(tkfont.families(root))
        body = "Segoe UI" if "Segoe UI" in fams else tkfont.nametofont("TkDefaultFont", root).actual("family")
        mono = next((f for f in ("Consolas", "Cascadia Mono", "Courier New") if f in fams),
                    tkfont.nametofont("TkFixedFont", root).actual("family"))
        spec = {
            "RmmBody": dict(family=body, size=10), "RmmBold": dict(family=body, size=10, weight="bold"),
            "RmmHeading": dict(family=body, size=11, weight="bold"),
            "RmmTitle": dict(family=body, size=14, weight="bold"),
            "RmmSmall": dict(family=body, size=9), "RmmMono": dict(family=mono, size=9),
        }
        # keep the Font objects alive: tkinter deletes a named font when its Font object is garbage-collected
        cached = root._rmm_fonts = {n: tkfont.Font(root, name=n, exists=n in tkfont.names(root), **spec[n])
                                    for n in _FONT_NAMES}
        for std, src in (("TkDefaultFont", "RmmBody"), ("TkTextFont", "RmmBody"), ("TkMenuFont", "RmmBody"),
                         ("TkHeadingFont", "RmmBold"), ("TkCaptionFont", "RmmHeading"),
                         ("TkSmallCaptionFont", "RmmSmall"), ("TkTooltipFont", "RmmSmall"), ("TkFixedFont", "RmmMono")):
            try:
                tkfont.nametofont(std, root).configure(**spec[src])
            except tk.TclError:
                pass
    b, bold, h, t, s, m = (cached[n] for n in _FONT_NAMES)
    return {"body": b, "default": b, "normal": b, "bold": bold, "heading": h, "title": t, "small": s, "mono": m}


def titlebar_dark(root: tk.Misc, dark: bool) -> None:
    """Windows 10/11: ask DWM for a dark/light title bar (needs the window to be mapped). Best effort."""
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        val = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (new, old builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
    except Exception:
        pass


def _tk_options(c: dict, f: dict) -> dict[str, object]:
    """Option-database entries for classic tk widgets (Text, Listbox, Menu, simpledialog's Entry/Button…)."""
    return {
        "*Text.background": c["field"], "*Text.foreground": c["text"], "*Text.insertBackground": c["text"],
        "*Text.selectBackground": c["select"], "*Text.selectForeground": c["select_text"],
        "*Text.inactiveSelectBackground": c["select_inactive"], "*Text.relief": "flat", "*Text.borderWidth": 0,
        "*Text.highlightThickness": 1, "*Text.highlightBackground": c["border"], "*Text.highlightColor": c["accent"],
        "*Text.font": f["body"],
        "*Listbox.background": c["field"], "*Listbox.foreground": c["text"], "*Listbox.selectBackground": c["select"],
        "*Listbox.selectForeground": c["select_text"], "*Listbox.relief": "flat", "*Listbox.borderWidth": 0,
        "*Listbox.highlightThickness": 1, "*Listbox.highlightBackground": c["border"],
        "*Listbox.highlightColor": c["accent"], "*Listbox.activeStyle": "none", "*Listbox.font": f["body"],
        "*TCombobox*Listbox.background": c["field"], "*TCombobox*Listbox.foreground": c["text"],
        "*TCombobox*Listbox.selectBackground": c["select"], "*TCombobox*Listbox.selectForeground": c["select_text"],
        "*TCombobox*Listbox.font": f["body"], "*TCombobox*Listbox.highlightThickness": 0,
        "*Menu.background": c["panel"], "*Menu.foreground": c["text"], "*Menu.activeBackground": c["select"],
        "*Menu.activeForeground": c["select_text"], "*Menu.disabledForeground": c["disabled"],
        "*Menu.selectColor": c["text"], "*Menu.relief": "flat", "*Menu.borderWidth": 1, "*Menu.activeBorderWidth": 0,
        "*Menu.font": f["body"],
        "*Canvas.background": c["bg"], "*Canvas.highlightThickness": 0,
        "*Toplevel.background": c["bg"], "*Frame.background": c["bg"],
        "*Label.background": c["bg"], "*Label.foreground": c["text"],
        "*Entry.background": c["field"], "*Entry.foreground": c["text"], "*Entry.insertBackground": c["text"],
        "*Entry.selectBackground": c["select"], "*Entry.selectForeground": c["select_text"], "*Entry.relief": "flat",
        "*Entry.highlightThickness": 1, "*Entry.highlightBackground": c["border"], "*Entry.highlightColor": c["accent"],
        "*Button.background": c["panel"], "*Button.foreground": c["text"], "*Button.activeBackground": c["hover"],
        "*Button.activeForeground": c["text"], "*Button.relief": "flat", "*Button.borderWidth": 0,
        "*Button.highlightThickness": 1, "*Button.highlightBackground": c["border"],
        "*Checkbutton.background": c["bg"], "*Checkbutton.foreground": c["text"],
        "*Checkbutton.selectColor": c["field"], "*Checkbutton.activeBackground": c["bg"],
        "*Radiobutton.background": c["bg"], "*Radiobutton.foreground": c["text"],
        "*Radiobutton.selectColor": c["field"], "*Radiobutton.activeBackground": c["bg"],
    }


# classic widget class -> {option: palette key} used to re-color already-created widgets on a live theme switch
_RETHEME = {
    "Text": {"background": "field", "foreground": "text", "insertbackground": "text", "selectbackground": "select",
             "selectforeground": "select_text", "highlightbackground": "border", "highlightcolor": "accent"},
    "Listbox": {"background": "field", "foreground": "text", "selectbackground": "select",
                "selectforeground": "select_text", "highlightbackground": "border", "highlightcolor": "accent"},
    "Menu": {"background": "panel", "foreground": "text", "activebackground": "select",
             "activeforeground": "select_text", "disabledforeground": "disabled", "selectcolor": "text"},
    "Canvas": {"background": "bg"}, "Frame": {"background": "bg"}, "Toplevel": {"background": "bg"},
    "Tk": {"background": "bg"}, "Label": {"background": "bg", "foreground": "text"},
    "Entry": {"background": "field", "foreground": "text", "insertbackground": "text"},
    "Button": {"background": "panel", "foreground": "text", "activebackground": "hover"},
}


def _retheme_tk(widget: tk.Misc, c: dict) -> None:
    stack = [widget]
    while stack:
        w = stack.pop()
        opts = _RETHEME.get(w.winfo_class())
        if opts and not getattr(w, "_rmm_custom_colors", False):
            try:
                w.configure(**{k: c[v] for k, v in opts.items()})
            except tk.TclError:
                pass
        stack.extend(w.winfo_children())


def apply_theme(root: tk.Misc, name: str = "dark") -> dict:
    """Style every ttk widget + the tk option database for palette `name`; returns the colors dict."""
    name = name if name in PALETTES else "dark"
    c = dict(PALETTES[name], name=name)
    f = make_fonts(root)
    px = ui_scale(root)

    def p(n: float) -> int:
        return max(1, round(n * px))

    s = ttk.Style(root)
    s.theme_use("clam")
    flat = dict(lightcolor=c["bg"], darkcolor=c["bg"])
    s.configure(".", background=c["bg"], foreground=c["text"], fieldbackground=c["field"], bordercolor=c["border"],
                troughcolor=c["field"], selectbackground=c["select"], selectforeground=c["select_text"],
                insertcolor=c["text"], focuscolor=c["accent"], arrowcolor=c["muted"], font=f["body"],
                relief="flat", **flat)
    s.map(".", foreground=[("disabled", c["disabled"])])

    # frames / labels
    s.configure("TFrame", background=c["bg"])
    s.configure("Card.TFrame", background=c["panel"], bordercolor=c["border"], relief="solid", borderwidth=1)
    s.configure("Panel.TFrame", background=c["panel"])
    s.configure("Header.TFrame", background=c["header"])
    s.configure("Hero.TFrame", background=c["panel"], bordercolor=c["accent"], relief="solid", borderwidth=1)
    s.configure("Surface.TFrame", background=c["field"], bordercolor=c["border"], relief="solid", borderwidth=1)
    s.configure("Toolbar.TFrame", background=c["panel"], bordercolor=c["border"], relief="solid", borderwidth=1)
    s.configure("Status.TFrame", background=c["header"])
    s.configure("TLabel", background=c["bg"], foreground=c["text"])
    s.configure("Panel.TLabel", background=c["panel"], foreground=c["text"])
    s.configure("PanelMuted.TLabel", background=c["panel"], foreground=c["muted"])
    s.configure("PanelHeading.TLabel", background=c["panel"], foreground=c["text"], font=f["heading"])
    s.configure("HeaderTitle.TLabel", background=c["header"], foreground=c["text"], font=f["heading"])
    s.configure("HeaderSub.TLabel", background=c["header"], foreground=c["muted"], font=f["small"])
    s.configure("Muted.TLabel", foreground=c["muted"])
    s.configure("Small.TLabel", foreground=c["muted"], font=f["small"])
    s.configure("Heading.TLabel", font=f["heading"])
    s.configure("Title.TLabel", font=f["title"])
    s.configure("Subtitle.TLabel", foreground=c["muted"], font=f["body"])
    s.configure("Ok.TLabel", foreground=c["ok"])
    s.configure("Warn.TLabel", foreground=c["warn"])
    s.configure("Error.TLabel", foreground=c["err"])
    s.configure("Link.TLabel", foreground=c["link"])
    s.configure("Pill.TLabel", background=c["badge_bg"], foreground=c["accent"], font=f["small"], padding=(p(8), p(2)))
    s.configure("PillOk.TLabel", background=c["badge_bg"], foreground=c["ok"], font=f["small"], padding=(p(8), p(2)))
    s.configure("PillWarn.TLabel", background=c["badge_bg"], foreground=c["warn"], font=f["small"], padding=(p(8), p(2)))
    s.configure("Status.TLabel", background=c["header"], foreground=c["muted"], padding=(p(8), p(4)))
    for lvl, col in (("Info", c["accent"]), ("Ok", c["ok"]), ("Warn", c["warn"]), ("Error", c["err"])):
        s.configure(f"Toast{lvl}.TLabel", background=c["header"], foreground=col, font=f["bold"], padding=(p(8), p(4)))
    s.configure("TSeparator", background=c["border"])
    s.configure("TLabelframe", background=c["bg"], bordercolor=c["border"], relief="solid", borderwidth=1, **flat)
    s.configure("TLabelframe.Label", background=c["bg"], foreground=c["accent"], font=f["bold"])

    # buttons
    btn_pad = (p(12), p(5))
    for style, base, hov, prs, fg in (("TButton", c["panel"], c["hover"], c["pressed"], c["text"]),
                                      ("Accent.TButton", c["accent"], c["accent_hover"], c["accent_pressed"], c["accent_text"]),
                                      ("Success.TButton", c["ok"], c["ok_hover"], c["ok_pressed"], "#ffffff"),
                                      ("Danger.TButton", c["err"], c["err_hover"], c["err_pressed"], "#ffffff"),
                                      ("Ghost.TButton", c["bg"], c["hover"], c["pressed"], c["muted"])):
        border = c["border"] if style == "TButton" else (c["bg"] if style == "Ghost.TButton" else base)
        font_use = f["bold"] if style in ("Accent.TButton", "Success.TButton", "Danger.TButton") else f["body"]
        s.configure(style, background=base, foreground=fg, bordercolor=border, lightcolor=base, darkcolor=base,
                    focuscolor=base, padding=btn_pad, relief="flat", anchor="center", focusthickness=0, font=font_use)
        state_bg = [("disabled", c["panel"] if style == "TButton" else c["pressed"]), ("pressed", prs), ("active", hov)]
        s.map(style, background=state_bg, lightcolor=state_bg, darkcolor=state_bg,
              foreground=[("disabled", c["disabled"]), ("active", c["text"] if style == "Ghost.TButton" else fg)],
              focuscolor=state_bg,
              bordercolor=[("focus", c["accent"] if style == "TButton" else border), ("disabled", c["border"])])
    s.configure("Small.TButton", padding=(p(8), p(3)))
    s.configure("Header.TButton", background=c["panel"], foreground=c["text"], bordercolor=c["border"],
                padding=(p(10), p(3)), font=f["small"])
    s.map("Header.TButton", background=[("pressed", c["pressed"]), ("active", c["hover"])],
          bordercolor=[("active", c["accent"])])
    s.configure("Toolbutton", background=c["bg"], foreground=c["text"], bordercolor=c["bg"], padding=(p(8), p(4)), **flat)
    s.map("Toolbutton", background=[("pressed", c["pressed"]), ("selected", c["select"]), ("active", c["hover"])],
          lightcolor=[("active", c["hover"])], darkcolor=[("active", c["hover"])])
    s.configure("TMenubutton", background=c["panel"], foreground=c["text"], bordercolor=c["border"],
                lightcolor=c["panel"], darkcolor=c["panel"], arrowcolor=c["muted"], padding=btn_pad)
    s.map("TMenubutton", background=[("active", c["hover"]), ("pressed", c["pressed"])])

    # check / radio
    for style in ("TCheckbutton", "TRadiobutton"):
        s.configure(style, background=c["bg"], foreground=c["text"], indicatorbackground=c["field"],
                    indicatorforeground=c["accent_text"], upperbordercolor=c["border_hover"],
                    lowerbordercolor=c["border_hover"], indicatormargin=(0, 0, p(6), 0), padding=p(2), focuscolor=c["bg"])
        s.map(style, background=[("active", c["bg"])], foreground=[("disabled", c["disabled"])],
              indicatorbackground=[("disabled", c["bg"]), ("selected", c["accent"]), ("pressed", c["hover"]),
                                   ("active", c["hover"])],
              upperbordercolor=[("selected", c["accent"]), ("focus", c["accent"])],
              lowerbordercolor=[("selected", c["accent"]), ("focus", c["accent"])])

    # entry-like fields
    for style in ("TEntry", "TCombobox", "TSpinbox"):
        s.configure(style, fieldbackground=c["field"], foreground=c["text"], bordercolor=c["border"],
                    lightcolor=c["field"], darkcolor=c["field"], insertcolor=c["text"], background=c["panel"],
                    arrowcolor=c["muted"], selectbackground=c["select"], selectforeground=c["select_text"],
                    padding=(p(6), p(4)), arrowsize=p(12))
        s.map(style, fieldbackground=[("disabled", c["bg"]), ("readonly", c["field"])],
              foreground=[("disabled", c["disabled"])],
              bordercolor=[("focus", c["accent"]), ("hover", c["border_hover"])],
              lightcolor=[("focus", c["field"])], darkcolor=[("focus", c["field"])],
              background=[("disabled", c["bg"]), ("pressed", c["pressed"]), ("active", c["hover"])],
              arrowcolor=[("disabled", c["disabled"]), ("active", c["text"])])
    s.map("TCombobox", selectbackground=[("readonly", c["field"])], selectforeground=[("readonly", c["text"])],
          fieldbackground=[("disabled", c["bg"]), ("readonly", c["field"])])
    s.configure("Placeholder.TEntry", foreground=c["muted"])

    # notebook
    s.configure("TNotebook", background=c["header"], bordercolor=c["border"], tabmargins=(p(8), p(6), p(8), 0), **flat)
    s.configure("TNotebook.Tab", background=c["header"], foreground=c["muted"], bordercolor=c["header"],
                padding=(p(16), p(8)), font=f["bold"], focuscolor=c["header"], **flat)
    s.map("TNotebook.Tab", background=[("selected", c["bg"]), ("active", c["hover"])],
          foreground=[("selected", c["accent"]), ("active", c["text"])],
          bordercolor=[("selected", c["border"])], lightcolor=[("selected", c["accent"])],
          darkcolor=[("selected", c["bg"])], expand=[("selected", (0, 0, 0, 0))])

    # treeview
    s.configure("Treeview", background=c["field"], fieldbackground=c["field"], foreground=c["text"],
                bordercolor=c["border"], lightcolor=c["field"], darkcolor=c["field"], rowheight=p(26), borderwidth=1)
    s.map("Treeview", background=[("selected", "!focus", c["select_inactive"]), ("selected", c["select"])],
          foreground=[("selected", c["select_text"])], bordercolor=[("focus", c["accent"])])
    s.configure("Treeview.Heading", background=c["panel"], foreground=c["muted"], bordercolor=c["border"],
                lightcolor=c["panel"], darkcolor=c["panel"], relief="flat", padding=(p(8), p(6)), font=f["bold"])
    s.map("Treeview.Heading", background=[("pressed", c["pressed"]), ("active", c["hover"])],
          foreground=[("active", c["text"])], lightcolor=[("active", c["hover"])], darkcolor=[("active", c["hover"])])
    s.configure("Item", padding=(p(4), 0))

    # scrollbars: arrow-less, thin, rounded-looking thumb
    for orient in ("Vertical", "Horizontal"):
        st = f"{orient}.TScrollbar"
        s.layout(st, [(f"{orient}.Scrollbar.trough", {"sticky": "nswe", "children": [
            (f"{orient}.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        s.configure(st, background=c["scroll"], troughcolor=c["bg"], bordercolor=c["bg"], lightcolor=c["scroll"],
                    darkcolor=c["scroll"], gripcount=0, arrowsize=p(10), relief="flat")
        s.map(st, background=[("pressed", c["accent"]), ("active", c["scroll_hover"])],
              lightcolor=[("pressed", c["accent"]), ("active", c["scroll_hover"])],
              darkcolor=[("pressed", c["accent"]), ("active", c["scroll_hover"])])

    # progress, paned window, scale
    for orient in ("Horizontal", "Vertical"):
        s.configure(f"{orient}.TProgressbar", background=c["accent"], troughcolor=c["field"], bordercolor=c["border"],
                    lightcolor=c["accent"], darkcolor=c["accent"], thickness=p(8))
    s.configure("TPanedwindow", background=c["bg"])
    s.configure("Sash", sashthickness=p(6), gripcount=0, background=c["bg"], bordercolor=c["bg"], **flat)
    s.configure("TScale", background=c["panel"], troughcolor=c["field"], bordercolor=c["border"], **flat)

    # classic tk widgets
    for k, v in _tk_options(c, f).items():
        root.option_add(k, v)
    try:
        root.configure(background=c["bg"])
    except tk.TclError:
        pass
    _retheme_tk(root, c)
    titlebar_dark(root, name != "light")
    return c


def _selftest() -> None:
    root = tk.Tk()
    root.withdraw()
    try:
        for name in (*PALETTES, "nope"):
            c = apply_theme(root, name)
            assert c["name"] in PALETTES and c["bg"].startswith("#")
        assert c["bg"] == "#0f1117" and c["accent"] == "#5b8def"
        s = ttk.Style(root)
        assert s.lookup("Treeview", "rowheight") and s.lookup("Accent.TButton", "background") == c["accent"]
        t = tk.Text(root)
        apply_theme(root, "light")
        assert str(t.cget("background")) == PALETTES["light"]["field"]
        f = make_fonts(root)
        assert f["mono"].actual("size") in (9, -12) and f["heading"].actual("weight") == "bold"
    finally:
        root.destroy()


if __name__ == "__main__":
    _selftest()
    print("theme ok")
