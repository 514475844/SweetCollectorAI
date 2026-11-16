"""R107 probe: measure the effective QPalette colors for each skin preset and
compute key foreground/background contrast ratios (WCAG)."""
import sys, os, importlib.util
ROOT = os.path.join(os.path.dirname(__file__), "..")
MRM = os.path.join(ROOT, "media_resource_manager")
spec = importlib.util.spec_from_file_location(
    "theme_probe_mod", os.path.join(MRM, "gui", "theme.py"))
theme = importlib.util.module_from_spec(spec)
spec.loader.exec_module(theme)

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPalette, QColor

def _lin(v):
    v = v / 255.0
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

def rel_lum(rgb):
    r, g, b = rgb
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)

def contrast(c1, c2):
    l1 = rel_lum(c1); l2 = rel_lum(c2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)

def hx(c):
    return (c.red(), c.green(), c.blue())

# Stub the arrow-asset helper (it does a relative import into the package
# which isn't available when theme is loaded standalone). It only returns URLs.
theme._arrow_assets = lambda dark: ("", "")

PRESETS = ["classic", "violet_look", "modern", "night", "qbittorrent"]
app = QApplication.instance() or QApplication(sys.argv)
roles = {
    "Window": QPalette.ColorRole.Window,
    "WindowText": QPalette.ColorRole.WindowText,
    "Base": QPalette.ColorRole.Base,
    "Text": QPalette.ColorRole.Text,
    "Button": QPalette.ColorRole.Button,
    "ButtonText": QPalette.ColorRole.ButtonText,
    "Highlight": QPalette.ColorRole.Highlight,
    "HighlightedText": QPalette.ColorRole.HighlightedText,
}

for preset in PRESETS:
    theme.apply_preset(app, preset)
    pal = app.palette()
    cols = {name: hx(pal.color(role)) for name, role in roles.items()}
    print(f"\n=== {preset} (dark={theme.preset_dark(preset)}, accent={theme.preset_accent(preset)}) ===")
    for name, c in cols.items():
        print(f"  {name:14s} {c}")
    pairs = [
        ("Text on Base", cols["Text"], cols["Base"]),
        ("Text on Window", cols["Text"], cols["Window"]),
        ("WindowText on Window", cols["WindowText"], cols["Window"]),
        ("ButtonText on Button", cols["ButtonText"], cols["Button"]),
        ("HighlightedText on Highlight", cols["HighlightedText"], cols["Highlight"]),
        ("Text on Highlight", cols["Text"], cols["Highlight"]),
    ]
    for label, fg, bg in pairs:
        print(f"    {label:32s} {contrast(fg,bg):6.2f}:1")
