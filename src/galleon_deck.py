#!/usr/bin/env python3
"""Stream Deck driver for the Corsair Galleon 100 SD's built-in deck (1b1c:2b18).

Elgato's software is Windows/macOS only, so this takes its place: profiles of
pages of 12 keys with custom images, a free-form top screen, the two dials,
built-in visual themes, and profiles that switch automatically by active app.

Protocol (from Julusian/node-elgato-stream-deck, "Galleon K100 SD"):
  - feature 03 27 every 500 ms is a keep-alive. Without it the firmware stays
    in standalone mode (its own numpad) and ignores everything else. Once the
    pings stop it falls back to standalone mode by itself, so the keyboard is
    never left without a numpad if this service dies.
  - feature 03 08 <pct> sets brightness.
  - output 02 07: key image (160x160 JPEG), 1024-byte packets.
  - output 02 0c: top screen region (720x384 JPEG), 1024-byte packets.
  - input 01 00: 12 key states. input 01 03: dials; byte 4 is 0 for press
    states or 1 for rotation, followed by one signed byte per dial.

Files (all reloaded automatically on save):
  ~/.config/galleon-deck/config.toml          global settings, auto-switch rules
  ~/.config/galleon-deck/profiles/<name>.toml  one profile: theme + pages
  ~/.config/galleon-deck/themes/<name>.toml    optional extra/overriding themes
"""

import array
import fcntl
import glob
import io
import json
import os
import random
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

import evdev
from evdev import ecodes
from PIL import Image, ImageDraw, ImageFont

VERSION = "1.1.0"  # add-ons can require a minimum version
VID, PID = "00001B1C", "00002B18"
KEYS = 12
KEY_PX = 160
LCD_W, LCD_H = 720, 384
PING_INTERVAL = 0.5
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "galleon-deck")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.toml")
PROFILE_DIR = os.path.join(CONFIG_DIR, "profiles")
THEME_DIR = os.path.join(CONFIG_DIR, "themes")


def log(*args):
    print(*args, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- themes ---

# Every theme has every field; user themes and a profile's [theme_overrides]
# only need the fields they change. key_style "hud" draws cut corners with
# bright corner ticks (in the `tick` colour) instead of rounded ones; there,
# `radius` is the size of the cut.
THEMES = {
    "amber": {  # warm CRT amber: orange-red on burnt brown, gold highlights, red frame
        "bg": "#1f1204", "key_bg": "#251505", "fg": "#f04a14", "accent": "#e0a93f",
        "dim": "#7f6a63", "border": "#5a2a0c", "frame": "#e0231a", "radius": 6, "border_width": 3,
        "font": "Quantico", "clock_font": "Quantico:bold",
    },
    "cyberpunk": {
        "bg": "#08040f", "key_bg": "#0d0718", "fg": "#00f0ff", "accent": "#ff00aa",
        "dim": "#6e6a86", "border": "#2a1f45", "radius": 18, "border_width": 3,
        "font": "JetBrains Mono", "clock_font": "JetBrains Mono:bold",
    },
    "synthwave": {
        "bg": "#12041f", "key_bg": "#1c0833", "fg": "#ff9e3d", "accent": "#ff2a6d",
        "dim": "#8a5fa8", "border": "#4a1d6e", "radius": 24, "border_width": 3,
        "font": "Orbitron", "clock_font": "Orbitron:bold",
    },
    "matrix": {
        "bg": "#000000", "key_bg": "#020a03", "fg": "#00ff41", "accent": "#b6ff5c",
        "dim": "#0f6b2a", "border": "#0c3d18", "radius": 4, "border_width": 2,
        "font": "FiraCode Nerd Font Mono", "clock_font": "FiraCode Nerd Font Mono:bold",
    },
    "arctic": {
        "bg": "#0b1018", "key_bg": "#121a26", "fg": "#d8e6f3", "accent": "#88c0d0",
        "dim": "#5b6b80", "border": "#26344a", "radius": 22, "border_width": 2,
        "font": "Rajdhani:semibold", "clock_font": "Rajdhani:bold",
    },
    "hud": {
        "bg": "#050505", "key_bg": "#0d0d0d", "fg": "#ff8c1a", "accent": "#19e6d4",
        "dim": "#6b6b6b", "border": "#333333", "radius": 2, "border_width": 2,
        "font": "Quantico", "clock_font": "Quantico:bold",
    },
    "mono": {
        "bg": "#000000", "key_bg": "#0a0a0a", "fg": "#f2f2f2", "accent": "#ffffff",
        "dim": "#777777", "border": "#2a2a2a", "radius": 20, "border_width": 2,
        "font": "Roboto", "clock_font": "Roboto:light",
    },
}
DEFAULT_THEME = "amber"
THEME_DEFAULTS = {**THEMES[DEFAULT_THEME], "icon_font": "Symbols Nerd Font", "key_style": "rounded", "tick": "accent"}


def all_themes():
    themes = {name: {**THEME_DEFAULTS, **t} for name, t in THEMES.items()}
    for path in sorted(glob.glob(os.path.join(THEME_DIR, "*.toml"))):
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, "rb") as f:
                themes[name] = {**themes.get(name, THEME_DEFAULTS), **tomllib.load(f)}
        except (OSError, tomllib.TOMLDecodeError) as e:
            log(f"theme {name}: {e}")
    return themes


# ------------------------------------------------------- wallpaper theme ---

WALLPAPER_THEME = os.path.join(THEME_DIR, "wallpaper.toml")


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(c))) for c in rgb)


def _from_hsv(h, s, v):
    import colorsys
    return _hex(c * 255 for c in colorsys.hsv_to_rgb(h % 1, max(0, min(1, s)), max(0, min(1, v))))


def _luminance(colour):
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(a, b):
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _hue_gap(a, b):
    d = abs(a - b) % 1
    return min(d, 1 - d)


WALLPAPER_COMMANDS = [  # tried in order when [wallpaper_theme] command isn't set
    "noctalia msg wallpaper-get",
    "swww query",
    "hyprctl hyprpaper listactive",
    "gsettings get org.gnome.desktop.background picture-uri-dark",
    "gsettings get org.gnome.desktop.background picture-uri",
]


def wallpaper_path(settings):
    """The current wallpaper's path, from the configured command or the first
    common wallpaper tool that answers with an existing image file."""
    commands = [settings["command"]] if settings.get("command") else WALLPAPER_COMMANDS
    for command in commands:
        try:
            out = cmd_output(shlex_split(command))
        except ValueError:
            continue
        for token in re.findall(r"(?:file://)?(/[^\s'\",]+\.(?:png|jpe?g|webp|bmp|gif))", out, re.I):
            path = os.path.expanduser(token.replace("file://", ""))
            if os.path.isfile(path):
                return path
    return None


def shlex_split(cmd):
    import shlex
    return shlex.split(cmd)


def theme_from_image(path, base=None):
    """A theme from an image: its most vivid colours for text/accents, a dark tint of
    its overall colour for the background, with fg/bg contrast kept readable."""
    import colorsys
    im = Image.open(path).convert("RGB")
    im.thumbnail((320, 320))
    raw = im.tobytes()
    px = [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]
    hsv = [colorsys.rgb_to_hsv(r / 255, g / 255, b / 255) for r, g, b in px]

    def clusters(pixels, n):
        q = Image.new("RGB", (len(pixels), 1))
        q.putdata(pixels)
        q = q.quantize(colors=n, method=Image.Quantize.MEDIANCUT)
        pal = q.getpalette()
        out = []
        for count, i in q.getcolors():
            rgb = tuple(pal[i * 3:i * 3 + 3])
            out.append((count, colorsys.rgb_to_hsv(*(c / 255 for c in rgb))))
        return out

    vivid = [p for p, (h, sat, val) in zip(px, hsv) if sat > 0.45 and val > 0.35]
    if len(vivid) < 50:  # a muted image: fall back to its most colourful pixels
        vivid = [p for _, p in sorted(zip((sat * val for _, sat, val in hsv), px))[-500:]]
    ranked = sorted(clusters(vivid, 8), key=lambda c: c[0] * c[1][1] * c[1][2], reverse=True)
    picks = []
    for _, col in ranked:  # distinct hues, most prominent first
        if all(_hue_gap(col[0], p[0]) > 0.07 for p in picks):
            picks.append(col)
    fg_h, fg_s, fg_v = picks[0]
    ac_h, ac_s, ac_v = picks[1] if len(picks) > 1 else (fg_h + 0.08, fg_s * 0.6, 1.0)
    fr_h, fr_s, fr_v = picks[2] if len(picks) > 2 else (ac_h, ac_s, ac_v)

    count, (bg_h, bg_s, _) = max(clusters(px, 8), key=lambda c: c[0])  # the image's overall tint
    if bg_s < 0.15:
        bg_h, bg_s = fg_h, 0.3
    bg_s = min(0.8, max(0.35, bg_s))
    theme = {**(base or THEME_DEFAULTS)}
    theme.update(
        fg=_from_hsv(fg_h, max(fg_s, 0.75), max(fg_v, 0.92)),
        accent=_from_hsv(ac_h, max(ac_s, 0.6), max(ac_v, 0.85)),
        frame=_from_hsv(fr_h, max(fr_s, 0.7), max(fr_v, 0.8)),
        dim=_from_hsv(bg_h, 0.18, 0.58),
        border=_from_hsv(fg_h, 0.75, 0.38),
    )
    v = 0.11
    while True:  # darken the background until the text reads clearly
        theme["bg"], theme["key_bg"] = _from_hsv(bg_h, bg_s, v), _from_hsv(bg_h, bg_s, v + 0.04)
        if _contrast(theme["fg"], theme["key_bg"]) >= 4.5 or v <= 0.03:
            break
        v -= 0.02
    return theme


def write_wallpaper_theme(settings, image=None):
    """Generate themes/wallpaper.toml from the current wallpaper (or image). Returns its path or None."""
    image = image or wallpaper_path(settings)
    if not image:
        log("wallpaper theme: no wallpaper found")
        return None
    base = {**THEME_DEFAULTS, **{k: settings[k] for k in ("font", "clock_font", "radius", "border_width") if k in settings}}
    theme = theme_from_image(image, base)
    os.makedirs(THEME_DIR, exist_ok=True)
    lines = [
        "# Generated from your wallpaper by galleon-deck; regenerated by the settings page's",
        "# wallpaper key (or automatically, with [wallpaper_theme] auto = true). Edits here",
        "# are overwritten; copy this file under another name to keep a version.",
        f"# {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        f"source = {json.dumps(image)}",
        f"source_mtime = {int(os.stat(image).st_mtime)}",
    ]
    for key in ("bg", "key_bg", "fg", "accent", "frame", "dim", "border", "radius", "border_width", "font", "clock_font"):
        lines.append(f"{key} = {json.dumps(theme[key])}")
    with open(WALLPAPER_THEME + ".tmp", "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(WALLPAPER_THEME + ".tmp", WALLPAPER_THEME)
    log(f"wallpaper theme: generated from {image}")
    return WALLPAPER_THEME


# ---------------------------------------------------------------- device ---

def find_hidraw():
    """The deck's interface 0 hidraw node, or None if unplugged."""
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(node, "device/uevent")) as f:
                uevent = f.read()
        except OSError:
            continue
        if f"{VID}:{PID}" not in uevent:
            continue
        if ":1.0/" in os.path.realpath(os.path.join(node, "device")):
            return "/dev/" + os.path.basename(node)
    return None


class Deck:
    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.lock = threading.Lock()

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def feature(self, payload):
        buf = array.array("B", list(payload) + [0] * (32 - len(payload)))
        req = 0xC0000000 | (32 << 16) | (ord("H") << 8) | 0x06  # HIDIOCSFEATURE(32)
        with self.lock:
            fcntl.ioctl(self.fd, req, buf, True)

    def ping(self):
        self.feature([0x03, 0x27])

    def brightness(self, pct):
        self.feature([0x03, 0x08, max(0, min(100, int(pct)))])

    def _write(self, packets):
        for pkt in packets:
            with self.lock:  # per packet, so pings can interleave
                os.write(self.fd, pkt)

    def key_image(self, index, jpeg):
        packets, part, off = [], 0, 0
        while off < len(jpeg):
            chunk = jpeg[off:off + 1016]
            last = off + len(chunk) >= len(jpeg)
            hdr = bytes([0x02, 0x07, index, int(last)]) + len(chunk).to_bytes(2, "little") + part.to_bytes(2, "little")
            packets.append((hdr + chunk).ljust(1024, b"\0"))
            off += len(chunk)
            part += 1
        self._write(packets)

    def lcd_image(self, jpeg, x=0, y=0, w=LCD_W, h=LCD_H):
        packets, part, off = [], 0, 0
        while off < len(jpeg):
            chunk = jpeg[off:off + 1008]
            last = off + len(chunk) >= len(jpeg)
            hdr = (bytes([0x02, 0x0C]) + x.to_bytes(2, "little") + y.to_bytes(2, "little")
                   + w.to_bytes(2, "little") + h.to_bytes(2, "little") + bytes([int(last)])
                   + part.to_bytes(2, "little") + len(chunk).to_bytes(2, "little") + b"\0")
            packets.append((hdr + chunk).ljust(1024, b"\0"))
            off += len(chunk)
            part += 1
        self._write(packets)

    def read(self):
        return os.read(self.fd, 1024)


def jpeg(img):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


# --------------------------------------------------------------- drawing ---

_fonts = {}


def font(family, size):
    key = (family, size)
    if key not in _fonts:
        path = subprocess.run(["fc-match", "-f", "%{file}", family], capture_output=True, text=True).stdout
        try:
            _fonts[key] = ImageFont.truetype(path, size)
        except OSError:
            _fonts[key] = ImageFont.load_default(size)
    return _fonts[key]


def fit(draw, text, family, size, max_w):
    """Largest font size <= size that fits text in max_w."""
    while size > 10:
        f = font(family, size)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 2
    return font(family, 10)


def colour(value, theme):
    """A "#rrggbb" value, or the name of a theme colour ("accent", "dim", ...)."""
    return theme.get(value, value) if isinstance(value, str) and not value.startswith("#") else value


def draw_frame(d, box, theme, outline, width, tick=None):
    """A key's or the screen's outline in the theme's key_style."""
    if theme.get("key_style") != "hud":
        d.rounded_rectangle(box, radius=theme["radius"], outline=outline, width=width)
        return
    x0, y0, x1, y1 = box
    cut = theme["radius"]
    d.polygon([(x0 + cut, y0), (x1, y0), (x1, y1 - cut), (x1 - cut, y1), (x0, y1), (x0, y0 + cut)],
              outline=outline, width=width)
    tick = colour(tick or theme.get("tick", "accent"), theme)
    arm = max(12, (x1 - x0) // 7)
    d.line([(x1 - arm, y0), (x1, y0), (x1, y0 + arm)], fill=tick, width=width + 1, joint="curve")
    d.line([(x0, y1 - arm), (x0, y1), (x0 + arm, y1)], fill=tick, width=width + 1, joint="curve")


def render_key(spec, theme, pressed=False):
    bg = colour(spec.get("bg", theme["key_bg"]), theme)
    fg = colour(spec.get("fg", theme["fg"]), theme)
    if pressed:
        bg, fg = fg, bg
    img = Image.new("RGB", (KEY_PX, KEY_PX), bg)
    d = ImageDraw.Draw(img)
    if not spec:
        return img
    bw = theme["border_width"]
    draw_frame(d, [bw, bw, KEY_PX - 1 - bw, KEY_PX - 1 - bw], theme,
               colour(spec.get("border", theme["border"]), theme), bw, tick=spec.get("border"))
    label = str(spec.get("label", ""))
    icon = spec.get("icon")
    image = spec.get("image")
    if image:
        try:
            pic = Image.open(os.path.expanduser(os.path.join(CONFIG_DIR, image))).convert("RGBA")
            pic.thumbnail((96, 96) if label else (124, 124))
            y = 16 if label else (KEY_PX - pic.height) // 2
            img.paste(pic, ((KEY_PX - pic.width) // 2, y), pic)
        except OSError as e:
            log(f"image {image}: {e}")
    elif icon:
        f = font(theme["icon_font"], 72 if label else 96)
        d.text((KEY_PX // 2, 62 if label else KEY_PX // 2), icon, font=f, fill=fg, anchor="mm")
    if label:
        if icon or image:
            f = fit(d, label, theme["font"], 26, KEY_PX - 16)
            d.text((KEY_PX // 2, 128), label, font=f, fill=fg, anchor="mm")
        else:
            f = fit(d, label, theme["font"], 72 if len(label) <= 2 else 40, KEY_PX - 20)
            d.text((KEY_PX // 2, KEY_PX // 2), label, font=f, fill=fg, anchor="mm")
    return img


def render_theme_key(name, theme, current):
    """A key that previews a theme in its own colours and font."""
    img = Image.new("RGB", (KEY_PX, KEY_PX), theme["key_bg"])
    d = ImageDraw.Draw(img)
    bw = theme["border_width"] + (2 if current else 0)
    draw_frame(d, [bw, bw, KEY_PX - 1 - bw, KEY_PX - 1 - bw], theme,
               theme["accent"] if current else theme["border"], bw)
    for i, c in enumerate((theme["fg"], theme["accent"], theme["dim"])):
        d.ellipse([34 + i * 34, 34, 58 + i * 34, 58], fill=c)
    d.text((KEY_PX // 2, 100), name.upper(), font=fit(d, name.upper(), theme["font"], 28, KEY_PX - 20),
           fill=theme["fg"], anchor="mm")
    if current:
        d.text((KEY_PX // 2, 132), "ACTIVE", font=font(theme["font"], 16), fill=theme["accent"], anchor="mm")
    return img


def cmd_output(argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=1).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def audio_backend():
    return "wpctl" if shutil_which("wpctl") else "pactl" if shutil_which("pactl") else None


def volume():
    """(percent, muted) of the default output, via PipeWire (wpctl) or PulseAudio (pactl)."""
    backend = audio_backend()
    try:
        if backend == "wpctl":
            out = cmd_output(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])  # "Volume: 0.42 [MUTED]"
            return round(float(out.split()[1]) * 100), "MUTED" in out
        if backend == "pactl":
            vol = cmd_output(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])  # "... / 42% / ..."
            mute = cmd_output(["pactl", "get-sink-mute", "@DEFAULT_SINK@"])   # "Mute: yes"
            return int(re.search(r"(\d+)%", vol).group(1)), mute.endswith("yes")
    except (IndexError, ValueError, AttributeError):
        pass
    return None, False


def change_volume(percent):
    """Raise (positive) or lower the default output's volume, capped at 100%."""
    backend = audio_backend()
    if backend == "wpctl":
        cmd = ["wpctl", "set-volume"] + (["-l", "1.0"] if percent > 0 else [])
        subprocess.run(cmd + ["@DEFAULT_AUDIO_SINK@", f"{abs(percent)}%{'+' if percent > 0 else '-'}"], timeout=2)
    elif backend == "pactl":
        vol, _ = volume()
        target = max(0, min(100, (vol or 0) + percent))
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{target}%"], timeout=2)


def toggle_mute():
    backend = audio_backend()
    if backend == "wpctl":
        subprocess.run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"], timeout=2)
    elif backend == "pactl":
        subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"], timeout=2)


def render_lcd(state, theme):
    img = Image.new("RGB", (LCD_W, LCD_H), theme["bg"])
    d = ImageDraw.Draw(img)
    fam, fg, accent, dim = theme["font"], theme["fg"], theme["accent"], theme["dim"]
    if theme.get("key_style") == "hud":
        draw_frame(d, [4, 4, LCD_W - 5, LCD_H - 5], {**theme, "radius": 28},
                   colour(theme.get("frame", "accent"), theme), 3)
    else:
        d.rectangle([4, 4, LCD_W - 5, LCD_H - 5], outline=colour(theme.get("frame", "accent"), theme), width=3)

    # Profile / page and page position, like the Stream Deck's page indicator.
    head = f"{state['profile']} / {state['page']}".upper()
    d.text((28, 40), head, font=fit(d, head, fam, 32, 480), fill=accent, anchor="lm")
    n, cur = state["page_count"], state["page_index"]
    for i in range(n):
        x = LCD_W - 36 - (n - 1 - i) * 30
        box = [x - 9, 31, x + 9, 49]
        d.ellipse(box, fill=accent if i == cur else None, outline=accent if i == cur else dim, width=2)
    d.line([24, 70, LCD_W - 24, 70], fill=dim, width=1)

    if state.get("error"):
        d.text((LCD_W // 2, 130), "CONFIG ERROR", font=font(fam, 44), fill=accent, anchor="mm")
        lines, line = [], ""
        for word in state["error"].split():
            if d.textlength(line + " " + word, font=font(fam, 20)) > LCD_W - 60:
                lines.append(line)
                line = word
            else:
                line = (line + " " + word).strip()
        lines.append(line)
        d.multiline_text((28, 180), "\n".join(lines[:6]), font=font(fam, 20), fill=fg, spacing=6)
        return img

    d.text((LCD_W // 2, 150), state["clock"], font=font(theme["clock_font"], 110), fill=fg, anchor="mm")
    d.text((LCD_W // 2, 232), state["date"], font=fit(d, state["date"], fam, 30, LCD_W - 60), fill=dim, anchor="mm")

    media = state.get("media")
    if media:
        f = fit(d, media, fam, 28, LCD_W - 110)
        d.text((56, 286), state.get("media_icon", ""), font=font(theme["icon_font"], 30), fill=accent, anchor="lm")
        d.text((96, 286), media, font=f, fill=fg, anchor="lm")

    vol, muted = state.get("volume", (None, False))
    if vol is not None:
        x0, x1, y = 96, LCD_W - 130, 340
        r = 0 if theme.get("key_style") == "hud" else 9
        d.text((56, y), "󰝟" if muted else "󰕾", font=font(theme["icon_font"], 30), fill=dim if muted else accent, anchor="lm")
        d.rounded_rectangle([x0, y - 9, x1, y + 9], radius=r, outline=dim, width=2)
        fill_w = int((x1 - x0 - 6) * min(vol, 100) / 100)
        if fill_w > 0:
            d.rounded_rectangle([x0 + 3, y - 6, x0 + 3 + fill_w, y + 6], radius=r * 2 // 3, fill=dim if muted else fg)
        d.text((LCD_W - 40, y), "MUTE" if muted else f"{vol}%", font=font(fam, 26), fill=dim if muted else fg, anchor="rm")
    return img


# ------------------------------------------------------------------ boot ---

BOOT_FPS = 30
BOOT_TITLE = "GALLEON DECK"
BOOT_LINES = [  # "LABEL|STATUS"
    "DECK LINK 1B1C:2B18|OK",
    "PROFILES|LOADED",
    "KEYS|ENCRYPTED",
    "DIALS|READY",
]


def _scanlines():
    over = Image.new("RGBA", (LCD_W, LCD_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    for y in range(0, LCD_H, 3):
        d.line([0, y, LCD_W, y], fill=(0, 0, 0, 70))
    return over


def _blend(a, b, t):
    """Mix two #rrggbb colours."""
    ca, cb = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (a, b))
    return tuple(round(x + (y - x) * t) for x, y in zip(ca, cb))


def config_path(path):
    """A path from the config: absolute, ~, or relative to the config folder."""
    path = os.path.expanduser(str(path))
    return path if os.path.isabs(path) else os.path.join(CONFIG_DIR, path)


def load_logo(path, theme, fallback, box=(LCD_W - 60, LCD_H - 80)):
    """A logo image trimmed to its content, or `fallback` drawn as a text logo."""
    if path:
        try:
            img = Image.open(config_path(path)).convert("RGBA")
            img = img.crop(img.getchannel("A").getbbox() or (0, 0, *img.size))
            img.thumbnail(box)
            return img
        except OSError as e:
            log(f"logo {path}: {e}")
    img = Image.new("RGBA", box, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    text = str(fallback).strip() or "GALLEON DECK"
    d.text((img.width // 2, img.height // 2), text, font=fit(d, text, theme["clock_font"], 90, img.width - 20),
           fill=theme["fg"], anchor="mm")
    return img


def glitch(img, rng, amount):
    """Digital interference: bands slide sideways, the colour channels separate,
    and now and then the whole frame flares. amount 0 leaves the image alone."""
    if amount <= 0.02:
        return img
    w, h = img.size
    out = Image.new("RGB", (w, h), (0, 0, 0))
    y = 0
    while y < h:
        band = rng.randint(max(2, h // 20), max(3, h // 5))
        dx = int(rng.uniform(-w * 0.25, w * 0.25) * amount) if rng.random() < 0.55 else 0
        out.paste(img.crop((0, y, w, y + band)), (dx, y))
        y += band
    r, g, b = out.split()
    r = r.transform(r.size, Image.AFFINE, (1, 0, -int(w * 0.05 * amount), 0, 1, 0))
    b = b.transform(b.size, Image.AFFINE, (1, 0, int(w * 0.03 * amount), 0, 1, 0))
    out = Image.merge("RGB", (r, g, b))
    if rng.random() < 0.3 * amount:
        out = Image.eval(out, lambda v: min(255, int(v * 1.5) + 20))
    return out


CIPHER = "0123456789ABCDEF#$%&*+=/<>?!"


class KeyDecrypt:
    """Keys show scrambling ciphertext, then decrypt one by one in random order:
    the real key is revealed block by block through the noise, then flashes."""

    BLOCK = 16

    def __init__(self, theme, real_keys, rng, mono, stretch):
        self.theme, self.real, self.rng = theme, real_keys, rng
        self.font = font(mono + ":bold", 26)
        self.order = rng.sample(range(KEYS), KEYS)
        self.gap = max(1, round(4 * stretch))        # frames between keys starting
        self.length = max(2, round(12 * stretch))    # frames to reveal one key
        self.cells = (KEY_PX // self.BLOCK) ** 2
        self.reveal = {i: rng.sample(range(self.cells), self.cells) for i in range(KEYS)}
        self.start = None  # frame decryption began
        self.done = set()

    def cipher(self):
        th = self.theme
        img = Image.new("RGB", (KEY_PX, KEY_PX), th["key_bg"])
        d = ImageDraw.Draw(img)
        bw = th["border_width"]
        draw_frame(d, [bw, bw, KEY_PX - 1 - bw, KEY_PX - 1 - bw], th, th["border"], bw)
        for row in range(4):
            for col in range(4):
                ch = self.rng.choice(CIPHER)
                fill = th["fg"] if self.rng.random() < 0.2 else th["dim"]
                d.text((26 + col * 36, 26 + row * 36), ch, font=self.font, fill=fill, anchor="mm")
        return img

    def resolving(self, i, frac):
        n = int(self.cells * frac)
        per_row = KEY_PX // self.BLOCK
        mask = Image.new("L", (KEY_PX, KEY_PX), 0)
        hot = Image.new("L", (KEY_PX, KEY_PX), 0)
        dm, dh = ImageDraw.Draw(mask), ImageDraw.Draw(hot)
        fresh = max(1, self.cells // self.length)
        for k, cell in enumerate(self.reveal[i][:n]):
            y, x = divmod(cell, per_row)
            box = [x * self.BLOCK, y * self.BLOCK, (x + 1) * self.BLOCK - 1, (y + 1) * self.BLOCK - 1]
            dm.rectangle(box, fill=255)
            if k >= n - fresh:  # the newest blocks glow in the accent colour
                dh.rectangle(box, fill=150)
        img = Image.composite(self.real(i), self.cipher(), mask)
        return Image.composite(Image.new("RGB", img.size, self.theme["accent"]), img, hot)

    def frame(self, f):
        """Key updates for frame f (counted from the start of the terminal text)."""
        out = {}
        scramble = f % 3 == 0  # ciphertext changes at 10 fps
        for i in range(KEYS):
            if i in self.done:
                continue
            if self.start is None or f < self.start + self.order.index(i) * self.gap:
                if scramble:
                    out[i] = self.cipher()
                continue
            t = f - self.start - self.order.index(i) * self.gap
            if t < self.length:
                out[i] = self.resolving(i, (t + 1) / self.length)
            elif t == self.length:  # lock-in flash
                out[i] = Image.eval(self.real(i), lambda v: min(255, int(v * 1.7) + 40))
            else:
                out[i] = "real"
                self.done.add(i)
        return out


def boot_frames(theme, boot, footer, real_keys, speed=1.0):
    """Yield (lcd_image, {key_index: key_image | "real"}) for the built-in boot animation.
    real_keys(i) renders key i as it will look afterwards; speed 0.5 = half speed."""
    import random
    stretch = 1 / max(0.1, float(speed))
    frames = lambda n: max(1, round(n * stretch))  # noqa: E731
    bg, fg, accent = theme["bg"], theme["fg"], theme["accent"]
    frame_col = colour(theme.get("frame", "accent"), theme)
    scan = _scanlines()
    mono = boot.get("font", "JetBrains Mono")
    rng = random.Random()
    keys = KeyDecrypt(theme, real_keys, rng, mono, stretch)
    kf = 0  # key frame counter

    def finish(img):
        img = img.convert("RGBA")
        img.alpha_composite(scan)
        return img.convert("RGB")

    # 1. CRT power-on: a line snaps across, then opens vertically.
    n = frames(12)
    for f in range(n):
        t = (f + 1) / n
        img = Image.new("RGB", (LCD_W, LCD_H), "#000000")
        d = ImageDraw.Draw(img)
        w = LCD_W * min(1, t * 2)
        h = 2 if t <= 0.5 else max(2, LCD_H * (t - 0.5) * 2)
        glow = _blend("#ffffff", bg, max(0, t - 0.5) * 2)
        d.rectangle([(LCD_W - w) / 2, (LCD_H - h) / 2, (LCD_W + w) / 2, (LCD_H + h) / 2], fill=glow)
        yield finish(img), {}

    # 2. Terminal boot text, typed out, over keys full of ciphertext.
    title = boot.get("title", BOOT_TITLE)
    rows = []  # "LABEL|STATUS" -> ("> LABEL ........ ", "STATUS")
    for line in boot.get("lines", BOOT_LINES)[:6]:
        label, _, status = str(line).partition("|")
        rows.append((f"> {label} ".ljust(28, ".") + " ", status))
    f_title, f_line = font(mono + ":bold", 30), font(mono, 24)
    total = len(title) + sum(len(a) + len(b) for a, b in rows)
    step = total / frames(40)  # characters per frame, fractional at slow speeds
    shown, f = 0.0, 0
    while shown < total + step * frames(6):
        shown += step
        f += 1
        img = Image.new("RGB", (LCD_W, LCD_H), bg)
        d = ImageDraw.Draw(img)
        d.rectangle([4, 4, LCD_W - 5, LCD_H - 5], outline=frame_col, width=3)
        left = int(shown)
        d.text((32, 40), title[:left], font=f_title, fill=accent, anchor="lm")
        cursor = (32 + d.textlength(title[:left], font=f_title), 40)
        left -= len(title)
        y = 100
        for text, status in rows:
            if left <= 0:
                break
            d.text((32, y), text[:left], font=f_line, fill=fg, anchor="lm")
            x = 32 + d.textlength(text[:left], font=f_line)
            part = status[:max(0, left - len(text))]
            if part:
                d.text((x, y), part, font=f_line, fill=accent, anchor="lm")
                x += d.textlength(part, font=f_line)
            cursor = (x, y)
            left -= len(text) + len(status)
            y += 44
        if (f // frames(1)) % 16 < 10:  # blinking block cursor
            d.rectangle([cursor[0] + 4, cursor[1] - 12, cursor[0] + 18, cursor[1] + 12], fill=fg)
        yield finish(img), keys.frame(kf)
        kf += 1

    # 3. Logo glitches in; the keys start decrypting.
    logo = load_logo(boot.get("logo"), theme, title.split("//")[0].strip() or title)
    keys.start = kf
    n = frames(36)
    for f in range(n):
        p = (f + 1) / n
        g = (1 - p) ** 2  # glitch intensity
        img = Image.new("RGB", (LCD_W, LCD_H), bg)
        if logo:
            layer = Image.new("RGBA", (LCD_W, LCD_H), (0, 0, 0, 0))
            layer.paste(logo, ((LCD_W - logo.width) // 2, 16), logo)
            if g > 0.02:
                out = Image.new("RGBA", layer.size, (0, 0, 0, 0))
                y = 0
                while y < LCD_H:  # shift random horizontal bands
                    band = rng.randint(6, 40)
                    dx = int(rng.uniform(-60, 60) * g) if rng.random() < 0.6 else 0
                    out.paste(layer.crop((0, y, LCD_W, y + band)), (dx, y))
                    y += band
                r, gg, b, a = out.split()  # red/cyan split
                r = r.transform(r.size, Image.AFFINE, (1, 0, -int(14 * g), 0, 1, 0))
                layer = Image.merge("RGBA", (r, gg, b, a))
            alpha = layer.getchannel("A").point(lambda v: int(v * min(1, p * 1.6)))
            layer.putalpha(alpha)
            img = img.convert("RGBA")
            img.alpha_composite(layer)
            img = img.convert("RGB")
        if rng.random() < 0.6 * g:  # occasional flicker frame
            img = Image.eval(img, lambda v: min(255, int(v * 1.6)))
        yield finish(img), keys.frame(kf)
        kf += 1

    # 4. Hold the logo and type the footer until every key is decrypted.
    base = img
    f_foot = font(mono + ":bold", 26)
    f = 0
    while f < frames(len(footer) + 18) or len(keys.done) < KEYS:
        img = base.copy()
        d = ImageDraw.Draw(img)
        d.text((LCD_W // 2, LCD_H - 34), footer[:f // frames(1) + 1], font=f_foot, fill=accent, anchor="mm")
        yield finish(img), keys.frame(kf)
        kf += 1
        f += 1


def gif_frames(path, theme):
    """Yield (lcd_image, {}, seconds) from a GIF/APNG/WebP, fitted to the top screen."""
    anim = Image.open(os.path.expanduser(path))
    for index in range(getattr(anim, "n_frames", 1)):
        anim.seek(index)
        frame = anim.convert("RGBA")
        frame.thumbnail((LCD_W, LCD_H))
        img = Image.new("RGB", (LCD_W, LCD_H), theme["bg"])
        img.paste(frame, ((LCD_W - frame.width) // 2, (LCD_H - frame.height) // 2), frame)
        yield img, {}, anim.info.get("duration", 1000 / BOOT_FPS) / 1000


# --------------------------------------------------------------- actions ---

MEDIA_KEYS = {"play-pause": ecodes.KEY_PLAYPAUSE, "next": ecodes.KEY_NEXTSONG,
              "previous": ecodes.KEY_PREVIOUSSONG, "stop": ecodes.KEY_STOPCD}


class Keyboard:
    """A uinput keyboard for numpad keys and shortcuts."""

    def __init__(self):
        keys = [c for c in ecodes.keys if isinstance(c, int) and 0 < c < 0x2FF]
        self.ui = evdev.UInput({ecodes.EV_KEY: keys}, name="galleon-deck")
        self.numlock_checked = False

    @staticmethod
    def codes(combo):
        out = []
        for name in str(combo).replace(" ", "").upper().split("+"):
            name = {"CTRL": "LEFTCTRL", "SHIFT": "LEFTSHIFT", "ALT": "LEFTALT", "SUPER": "LEFTMETA",
                    "WIN": "LEFTMETA", "META": "LEFTMETA"}.get(name, name)
            code = ecodes.ecodes.get("KEY_" + name)
            if code is None:
                raise ValueError(f"unknown key {name!r}")
            out.append(code)
        return out

    def ensure_numlock(self):
        """Numpad keys type digits only with NumLock on. Hyprland gives our virtual
        keyboard its own lock state; on X11 it's global. Other desktops share the
        lock state across keyboards, so the user's NumLock applies as usual."""
        if self.numlock_checked:
            return
        self.numlock_checked = True
        try:
            if hyprland():
                for kb in json.loads(cmd_output(["hyprctl", "devices", "-j"]))["keyboards"]:
                    if kb["name"] == "galleon-deck" and not kb.get("numLock"):
                        self.tap([ecodes.KEY_NUMLOCK])
            elif x11() and shutil_which("xset"):
                if re.search(r"Num Lock:\s+off", cmd_output(["xset", "q"])):
                    self.tap([ecodes.KEY_NUMLOCK])
        except (ValueError, KeyError):
            pass

    def press(self, codes):
        for c in codes:
            self.ui.write(ecodes.EV_KEY, c, 1)
        self.ui.syn()

    def release(self, codes):
        for c in reversed(codes):
            self.ui.write(ecodes.EV_KEY, c, 0)
        self.ui.syn()

    def tap(self, codes):
        self.press(codes)
        self.release(codes)


def run(cmd):
    """Run a shell command. Under uwsm, launch through it so apps get the full
    session environment (uwsm strips some variables, DISPLAY included)."""
    argv = ["sh", "-c", cmd]
    if os.environ.get("UWSM_WAIT_VARNAMES") or _uwsm_session():
        argv = ["uwsm", "app", "--"] + argv
    return subprocess.Popen(argv, env=session_env(), start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def session_env():
    """Our environment with empty or missing variables filled in from the
    systemd user manager. The service can start before the compositor has
    exported WAYLAND_DISPLAY, and would otherwise hand apps an empty one."""
    env = dict(os.environ)
    try:
        out = subprocess.run(["systemctl", "--user", "show-environment"],
                             capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.TimeoutExpired):
        return env
    for line in out.splitlines():
        name, sep, value = line.partition("=")
        if sep and value and not env.get(name):
            env[name] = value
    return env


_uwsm = None


def _uwsm_session():
    global _uwsm
    if _uwsm is None:
        _uwsm = bool(shutil_which("uwsm")) and subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", "wayland-wm@*.service"]).returncode == 0
    return _uwsm


def shutil_which(name):
    import shutil
    return shutil.which(name)


def hyprland():
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))


def run_quiet(argv):
    subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ------------------------------------------------------------ toml write ---
# tomllib only reads. These write the small subset this project uses, editing
# files in place so hand-written comments survive.

def toml_str(text):
    out = ['"']
    for ch in str(text):
        o = ord(ch)
        if ch in '"\\':
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif o < 0x20 or o == 0x7F or 0xE000 <= o <= 0xF8FF:  # control, and icon glyphs (invisible in editors)
            out.append(f"\\u{o:04X}")
        elif o > 0xFFFF:
            out.append(f"\\U{o:08X}")
        else:
            out.append(ch)
    return "".join(out) + '"'


def toml_key(key):
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else toml_str(key)


def toml_value(value, multiline=False):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return toml_str(value)
    if isinstance(value, dict):
        if not value:
            return "{}"
        return "{ " + ", ".join(f"{toml_key(k)} = {toml_value(v)}" for k, v in value.items()) + " }"
    if isinstance(value, (list, tuple)):
        if multiline and value:
            return "[\n" + "".join(f"  {toml_value(v)},\n" for v in value) + "]"
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    raise TypeError(f"can't write {type(value).__name__} to TOML")


def _depth(line):
    """Net bracket depth of a line, ignoring strings and comments."""
    depth, quote, i = 0, None, 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            break
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        i += 1
    return depth


def _comment(line):
    """The trailing `# comment` of a line (outside strings), or ""."""
    quote, i = None, 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return line[i:]
        i += 1
    return ""


def _write_atomic(path, text):
    with open(path + ".tmp", "w") as f:
        f.write(text)
    os.replace(path + ".tmp", path)


def set_value(path, table, key, value, remove=False):
    """Set (or remove) `key` in `[table]` (None = top level) of a TOML file, in place."""
    try:
        with open(path) as f:
            lines = f.read().split("\n")
    except FileNotFoundError:
        lines = []
    header = f"[{table}]" if table else None
    # the section's line range
    start = 0
    if header:
        start = next((i + 1 for i, l in enumerate(lines) if l.strip() == header), None)
        if start is None:
            if not remove:
                while lines and lines[-1] == "":
                    lines.pop()
                lines += ["", header, f"{key} = {toml_value(value, multiline=isinstance(value, list))}", ""]
                _write_atomic(path, "\n".join(lines))
            return
    end = next((i for i in range(start, len(lines)) if re.match(r"\s*\[", lines[i]) and _depth(lines[i]) >= 0
                and re.match(r"\s*\[\[?[A-Za-z0-9_.\"-]+\]\]?\s*(#.*)?$", lines[i])), len(lines))
    new = [] if remove else f"{key} = {toml_value(value, multiline=isinstance(value, list))}".split("\n")
    for i in range(start, end):
        if re.match(rf"\s*{re.escape(key)}\s*=", lines[i]):
            j, depth = i, _depth(lines[i])
            while depth > 0 and j + 1 < len(lines):  # a value spanning lines
                j += 1
                depth += _depth(lines[j])
            comment = _comment(lines[i]) if j == i and len(new) == 1 else ""
            if comment:  # keep a trailing comment, aligned where it was
                col = len(lines[i]) - len(comment)
                new = [new[0].ljust(col) if len(new[0]) < col else new[0] + "  "]
                new[0] += comment
            lines[i:j + 1] = new
            break
    else:
        if remove:
            return
        at = end
        while at > start and lines[at - 1].strip() == "":
            at -= 1
        lines[at:at] = new
    _write_atomic(path, "\n".join(lines))


def set_top_level(path, key, value):
    """Set `key = value` at the top of a TOML file, keeping its comments and layout."""
    set_value(path, None, key, value)


def write_profile(path, profile):
    """Write a profile file from a dict: theme, start_page, settings_page,
    auto_switch, theme_overrides, page[]. Keeps the file's leading comment block."""
    head = []
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    head.append(line.rstrip("\n"))
                else:
                    break
    except FileNotFoundError:
        head = ["# galleon-deck profile. Edit here or in the configurator (galleon-deck-config).", ""]
    while head and not head[-1].strip():
        head.pop()
    out = head + [""] if head else []
    for key in ("theme", "start_page", "logo"):
        if profile.get(key):
            out.append(f"{key} = {toml_value(profile[key])}")
    if profile.get("settings_page") is False:
        out.append("settings_page = false")
    if profile.get("auto_switch"):
        out.append(f"auto_switch = {toml_value(profile['auto_switch'], multiline=True)}")
    if profile.get("theme_overrides"):
        out += ["", "[theme_overrides]"] + [f"{toml_key(k)} = {toml_value(v)}" for k, v in profile["theme_overrides"].items()]
    for page in profile.get("page", []):
        if page.get("settings"):
            continue  # generated at load time
        keys = list(page.get("keys", []))
        while keys and not keys[-1]:
            keys.pop()
        out += ["", "[[page]]", f"name = {toml_value(page['name'])}", "keys = ["]
        out += [f"  {toml_value(k)}," for k in keys]
        out.append("]")
    _write_atomic(path, "\n".join(out) + "\n")


# ---------------------------------------------------------------- config ---

def validate_pages(pages, where):
    if not pages:
        raise ValueError(f"{where}: no [[page]] entries")
    for p in pages:
        if "name" not in p:
            raise ValueError(f"{where}: a [[page]] has no name")
        keys = p.get("keys", [])
        if len(keys) > KEYS:
            raise ValueError(f"{where}: page {p['name']!r} has {len(keys)} keys; the deck has {KEYS}")
        p["keys"] = keys + [{}] * (KEYS - len(keys))
        for k in p["keys"]:
            for field in ("key", "keys"):
                if field in k:
                    Keyboard.codes(k[field])  # validate now, not on first press


def load_config():
    with open(CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)
    themes = all_themes()
    profiles = {}
    for path in sorted(glob.glob(os.path.join(PROFILE_DIR, "*.toml"))):
        name = os.path.splitext(os.path.basename(path))[0]
        with open(path, "rb") as f:
            try:
                prof = tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                raise ValueError(f"profiles/{name}.toml: {e}") from None
        validate_pages(prof.get("page", []), f"profiles/{name}.toml")
        for rule in prof.get("auto_switch", []):
            try:
                re.compile(rule.get("class", ""))
                re.compile(rule.get("title", ""))
            except (re.error, AttributeError) as e:
                raise ValueError(f"profiles/{name}.toml: auto_switch rule {rule!r}: {e}") from None
        theme_name = prof.get("theme", DEFAULT_THEME)
        if theme_name not in themes:
            raise ValueError(f"profiles/{name}.toml: unknown theme {theme_name!r} (have: {', '.join(themes)})")
        if prof.get("settings_page", True):
            prof["page"].append({"name": "settings", "keys": [{}] * KEYS, "settings": True})
        prof.update(name=name, path=path, theme_name=theme_name,
                    theme={**themes[theme_name], **prof.get("theme_overrides", {})})
        profiles[name] = prof
    if not profiles:
        raise ValueError("no profiles in profiles/*.toml")
    for rule in cfg.get("auto_switch", {}).get("rules", []):
        if rule.get("profile") not in profiles:
            raise ValueError(f"auto_switch rule points at unknown profile {rule.get('profile')!r}")
        re.compile(rule.get("class", ""))
        re.compile(rule.get("title", ""))
    cfg.update(profiles=profiles, themes=themes, switch_rules=switch_rules(cfg, profiles))
    return cfg


def switch_rules(cfg, profiles):
    """Auto-switch rules in the order they're checked: each profile's own
    `auto_switch` rules first (an add-on's profile brings its game's rule and
    takes it away when removed), then the rules in config.toml."""
    rules = [{**rule, "profile": name} for name, prof in profiles.items() for rule in prof.get("auto_switch", [])]
    return rules + cfg.get("auto_switch", {}).get("rules", [])


def config_mtime():
    """Latest mtime across every config file, so a save anywhere triggers a reload."""
    paths = [CONFIG_PATH] + glob.glob(os.path.join(PROFILE_DIR, "*.toml")) + glob.glob(os.path.join(THEME_DIR, "*.toml"))
    return max((os.stat(p).st_mtime for p in paths if os.path.exists(p)), default=0), len(paths)


# --------------------------------------------------------- window events ---

def x11():
    return os.environ.get("XDG_SESSION_TYPE") == "x11" or (os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"))


def focus_source():
    """Which desktop we can follow window focus on, or None."""
    if hyprland():
        return "hyprland"
    if os.environ.get("SWAYSOCK") and shutil_which("swaymsg"):
        return "sway"
    if os.environ.get("I3SOCK") and shutil_which("i3-msg"):
        return "i3"
    if os.environ.get("NIRI_SOCKET") and shutil_which("niri"):
        return "niri"
    if x11() and shutil_which("xprop"):
        return "x11"
    return None


def _lines(argv):
    """Yield output lines of a long-running command."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        yield from proc.stdout
    finally:
        proc.kill()


def focus_events(callback):
    """Call callback(class, title) whenever window focus changes. Supported:
    Hyprland, Sway, i3, niri, and X11 desktops. GNOME and KDE on Wayland don't
    expose the focused window, so auto-switching is off there."""
    source = focus_source()
    if not source:
        log("auto-switch: this desktop doesn't expose window focus; switch profiles by key or dial")
        return
    while True:
        try:
            if source == "hyprland":
                sock_path = os.path.join(os.environ.get("XDG_RUNTIME_DIR", ""), "hypr",
                                         os.environ["HYPRLAND_INSTANCE_SIGNATURE"], ".socket2.sock")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.connect(sock_path)
                    for line in s.makefile(errors="replace"):
                        event, _, payload = line.rstrip("\n").partition(">>")
                        if event == "activewindow":
                            cls, _, title = payload.partition(",")
                            callback(cls, title)
            elif source in ("sway", "i3"):
                tool = "swaymsg" if source == "sway" else "i3-msg"
                for line in _lines([tool, "-t", "subscribe", "-m", '["window"]']):
                    ev = json.loads(line)
                    if ev.get("change") in ("focus", "title"):
                        c = ev.get("container", {})
                        cls = c.get("app_id") or (c.get("window_properties") or {}).get("class") or ""
                        callback(cls, c.get("name") or "")
            elif source == "niri":
                windows, focused = {}, None
                for line in _lines(["niri", "msg", "--json", "event-stream"]):
                    ev = json.loads(line)
                    if "WindowsChanged" in ev:
                        windows = {w["id"]: w for w in ev["WindowsChanged"]["windows"]}
                        focused = next((w["id"] for w in windows.values() if w.get("is_focused")), focused)
                    elif "WindowOpenedOrChanged" in ev:
                        w = ev["WindowOpenedOrChanged"]["window"]
                        windows[w["id"]] = w
                        if w.get("is_focused"):
                            focused = w["id"]
                    elif "WindowFocusChanged" in ev:
                        focused = ev["WindowFocusChanged"]["id"]
                    else:
                        continue
                    w = windows.get(focused, {})
                    callback(w.get("app_id") or "", w.get("title") or "")
            elif source == "x11":
                for line in _lines(["xprop", "-root", "-spy", "_NET_ACTIVE_WINDOW"]):
                    m = re.search(r"window id # (0x[0-9a-f]+)", line)
                    if not m or int(m.group(1), 16) == 0:
                        continue
                    props = cmd_output(["xprop", "-id", m.group(1), "WM_CLASS", "_NET_WM_NAME"])
                    classes = re.findall(r'"([^"]*)"', props.split("\n")[0]) if props else []
                    title = re.search(r'_NET_WM_NAME\(\w+\) = "(.*)"', props)
                    callback(classes[-1] if classes else "", title.group(1) if title else "")
        except (OSError, ValueError, KeyError) as e:
            log(f"window focus ({source}): {e}")
        time.sleep(3)


# ------------------------------------------------------------------ main ---

class App:
    def __init__(self):
        self.cfg = None
        self.cfg_mtime = None
        self.error = None
        self.profile = None   # name
        self.page = 0
        self.history = []
        self.kb = Keyboard()
        self.deck = None
        self.key_state = [0] * KEYS
        self.held = {}  # key index -> codes held down
        self.dial_down = [False, False]
        self.dial_turned = [False, False]
        self.last_lcd = None
        self.lcd_dirty = True
        self.volume = (None, False)
        self.running = True
        self.lock = threading.RLock()  # the window-event thread also switches profiles
        self.auto_target = None
        self.published = None

    # config ---------------------------------------------------------------
    def reload_if_changed(self):
        mtime = config_mtime()
        if mtime == self.cfg_mtime:
            return
        self.cfg_mtime = mtime
        try:
            cfg = load_config()
        except (OSError, ValueError, re.error) as e:
            self.error = f"{e}"
            log("config:", self.error)
            self.lcd_dirty = True
            return
        first = self.cfg is None
        self.cfg, self.error = cfg, None
        if first or self.profile not in cfg["profiles"]:
            start = cfg.get("start_profile")
            self.profile = start if start in cfg["profiles"] else next(iter(cfg["profiles"]))
            self.page = self.start_page_index()
            self.history.clear()
        self.page = min(self.page, len(self.pages()) - 1)
        log(f"config loaded: profiles {', '.join(cfg['profiles'])}")
        if self.deck:
            self.deck.brightness(cfg.get("brightness", 70))
            self.draw_keys()
        self.lcd_dirty = True

    def prof(self):
        return self.cfg["profiles"][self.profile] if self.cfg else None

    def pages(self):
        return self.prof()["page"] if self.cfg else [{"name": "-", "keys": [{}] * KEYS}]

    @property
    def theme(self):
        return self.prof()["theme"] if self.cfg else THEME_DEFAULTS

    def start_page_index(self):
        names = [p["name"] for p in self.pages()]
        start = self.prof().get("start_page") if self.cfg else None
        return names.index(start) if start in names else 0

    def settings_keys(self):
        """The generated settings page: themes, brightness, then profiles."""
        slots = KEYS - 3 - min(len(self.cfg["profiles"]), 3)  # leave room for wallpaper, brightness, profiles
        keys = [{"set_theme": name} for name in self.cfg["themes"] if name != "wallpaper"][:slots]
        keys.append({"wallpaper_theme": True})
        keys.append({"icon": "󰃞", "label": "dimmer", "brightness": -10})
        keys.append({"icon": "󰃠", "label": "brighter", "brightness": 10})
        for name in self.cfg["profiles"]:
            if len(keys) >= KEYS:
                break
            keys.append({"icon": "󰀉" if name == self.profile else "󰀄", "label": name, "profile": name,
                         **({"fg": "accent"} if name == self.profile else {})})
        return keys + [{}] * (KEYS - len(keys))

    def keys(self):
        page = self.pages()[self.page]
        return self.settings_keys() if page.get("settings") else page["keys"]

    # drawing --------------------------------------------------------------
    def key_image(self, i, pressed=False):
        spec = self.keys()[i]
        if "wallpaper_theme" in spec:
            if "wallpaper" not in self.cfg["themes"]:
                return render_key({"icon": "󰸉", "label": "wallpaper"}, self.theme, pressed)
            spec = {"set_theme": "wallpaper"}
        if "set_theme" in spec:
            name = spec["set_theme"]
            img = render_theme_key(name, self.cfg["themes"][name], name == self.prof()["theme_name"])
            return Image.eval(img, lambda v: 255 - v) if pressed else img
        return render_key(spec, self.theme, pressed)

    def publish_state(self):
        """Tell the configurator what the deck shows, so it can follow along."""
        if not self.cfg:
            return
        state = {"profile": self.profile, "page": self.pages()[self.page]["name"]}
        if state != self.published:
            try:
                with open(STATE_FILE + ".tmp", "w") as f:
                    json.dump(state, f)
                os.replace(STATE_FILE + ".tmp", STATE_FILE)
                self.published = state
            except OSError as e:
                log(f"state file: {e}")

    def draw_key(self, i, pressed=False):
        self.deck.key_image(i, jpeg(self.key_image(i, pressed)))

    def draw_keys(self):
        for i in range(KEYS):
            self.draw_key(i)

    def lcd_state(self):
        now = time.localtime()
        return {
            "profile": self.profile or "galleon-deck",
            "page": self.pages()[self.page]["name"],
            "page_index": self.page,
            "page_count": len(self.pages()),
            "clock": time.strftime(self.cfg.get("clock_format", "%H:%M") if self.cfg else "%H:%M", now),
            "date": time.strftime("%A %d %B", now).upper(),
            "volume": self.volume,
            "error": self.error,
            "theme": self.prof()["theme_name"] if self.cfg else "",
        }

    def draw_lcd(self):
        state = self.lcd_state()
        status = cmd_output(["playerctl", "status"]) if shutil_which("playerctl") else ""
        if status in ("Playing", "Paused"):
            state["media"] = cmd_output(["playerctl", "metadata", "--format", "{{artist}} — {{title}}"]).strip(" —")
            state["media_icon"] = "󰐊" if status == "Playing" else "󰏤"
        key = repr(sorted(state.items()))
        if key != self.last_lcd or self.lcd_dirty:
            self.deck.lcd_image(jpeg(render_lcd(state, self.theme)))
            self.last_lcd, self.lcd_dirty = key, False

    # navigation -----------------------------------------------------------
    def release_held(self):
        for i in list(self.held):
            self.kb.release(self.held.pop(i))

    def set_page(self, index, remember=True):
        index %= len(self.pages())
        if index == self.page:
            return
        self.release_held()
        if remember:
            self.history.append(self.page)
        self.page = index
        if self.deck:
            self.draw_keys()
        self.lcd_dirty = True

    def goto(self, name):
        names = [p["name"] for p in self.pages()]
        if name == "back":
            # Pages reached with the dial leave no history; fall back to the start page.
            self.set_page(self.history.pop() if self.history else self.start_page_index(), remember=False)
        elif name in names:
            self.set_page(names.index(name))
        else:
            log(f"no page named {name!r}")

    def set_profile(self, name):
        if not self.cfg or name not in self.cfg["profiles"] or name == self.profile:
            return
        self.release_held()
        self.profile = name
        self.page = self.start_page_index()
        self.history.clear()
        log(f"profile: {name}")
        self.transition()
        self.lcd_dirty = True

    def transition(self):
        """The profile switch: the new profile's logo resolves on the screen out of
        interference while its keys do the same. `logo` in the profile file sets the
        image; without one the profile's name is the logo. Any key or dial skips it."""
        if not self.deck:
            return
        settings = self.cfg.get("transition", {}) if self.cfg else {}
        if not settings.get("enabled", True):
            self.draw_keys()
            return
        speed = max(0.25, min(4.0, float(settings.get("speed", 1.0))))
        frames = max(2, round(9 / speed))
        hold = max(0.0, min(3.0, float(settings.get("hold", 0.5)))) / speed
        theme, rng = self.theme, random.Random()
        keys = [self.key_image(i) for i in range(KEYS)]
        logo = load_logo(self.prof().get("logo"), theme, self.profile.upper())
        def screen_at(done):
            screen = Image.new("RGB", (LCD_W, LCD_H), theme["bg"]).convert("RGBA")
            layer = Image.new("RGBA", (LCD_W, LCD_H), (0, 0, 0, 0))
            layer.paste(logo, ((LCD_W - logo.width) // 2, (LCD_H - logo.height) // 2), logo)
            layer.putalpha(layer.getchannel("A").point(lambda v: int(v * min(1, done * 1.6))))
            screen.alpha_composite(layer)
            screen = screen.convert("RGB")
            d = ImageDraw.Draw(screen)
            if theme.get("key_style") == "hud":
                draw_frame(d, [4, 4, LCD_W - 5, LCD_H - 5], {**theme, "radius": 28},
                           colour(theme.get("frame", "accent"), theme), 3)
            else:
                d.rectangle([4, 4, LCD_W - 5, LCD_H - 5], outline=colour(theme.get("frame", "accent"), theme), width=3)
            return screen

        def waited(seconds):
            """Sleep, unless a key or dial arrives: then the animation is over."""
            return bool(select.select([self.deck.fd], [], [], max(0.0, seconds))[0])

        try:
            for f in range(frames):
                done = (f + 1) / frames
                amount = (1 - done) ** 1.5
                self.deck.lcd_image(jpeg(glitch(screen_at(done), rng, amount)))
                for i, img in enumerate(keys):
                    self.deck.key_image(i, jpeg(glitch(img, rng, amount)))
                if waited(0.05 / speed):
                    break  # the press itself still counts
            else:
                self.draw_keys()  # keys first, so the clean logo holds alone for a beat
                if hold:
                    self.deck.lcd_image(jpeg(screen_at(1.0)))
                    waited(hold)
        except OSError as e:
            log(f"transition: {e}")
        self.draw_keys()
        self.last_lcd, self.lcd_dirty = None, True

    def set_theme(self, name):
        """Persist the theme into the profile file; the reload applies it."""
        set_top_level(self.prof()["path"], "theme", name)
        self.reload_if_changed()

    def apply_wallpaper_theme(self):
        """Regenerate the wallpaper theme from the current wallpaper and use it on this profile."""
        if write_wallpaper_theme(self.cfg.get("wallpaper_theme", {})):
            self.set_theme("wallpaper")

    def follow_wallpaper(self):
        """With [wallpaper_theme] auto = true, regenerate when the wallpaper changes."""
        settings = self.cfg.get("wallpaper_theme", {}) if self.cfg else {}
        if not settings.get("auto"):
            return
        current = self.cfg["themes"].get("wallpaper", {})
        path = wallpaper_path(settings)
        if path and (path != current.get("source") or int(os.stat(path).st_mtime) != current.get("source_mtime")):
            write_wallpaper_theme(settings, path)
            self.reload_if_changed()

    def set_brightness(self, delta):
        level = max(10, min(100, self.cfg.get("brightness", 70) + delta))
        set_top_level(CONFIG_PATH, "brightness", level)
        self.reload_if_changed()

    def on_focus(self, cls, title):
        """Auto-switch: only acts when the matching profile changes, so manual picks stick."""
        if not self.cfg:
            return
        target = self.cfg.get("auto_switch", {}).get("fallback")
        for rule in self.cfg["switch_rules"]:
            if (re.search(rule.get("class", ""), cls) and re.search(rule.get("title", ""), title)):
                target = rule["profile"]
                break
        with self.lock:
            if target != self.auto_target:
                self.auto_target = target
                if target:
                    self.set_profile(target)

    # input ----------------------------------------------------------------
    def key_down(self, i):
        spec = self.keys()[i]
        if not spec:
            return
        page, profile = self.page, self.profile
        try:
            if "key" in spec or "keys" in spec:
                if str(spec.get("key", "")).upper().startswith("KP"):
                    self.kb.ensure_numlock()
                codes = Keyboard.codes(spec.get("key") or spec.get("keys"))
                self.kb.press(codes)
                self.held[i] = codes
            if "exec" in spec:
                proc = run(spec["exec"])
                if spec.get("confirm"):
                    threading.Thread(target=self.confirm, args=(i, proc, page, profile), daemon=True).start()
            if "media" in spec:
                if shutil_which("playerctl"):
                    run_quiet(["playerctl", spec["media"]])
                else:
                    self.kb.tap([MEDIA_KEYS.get(spec["media"], ecodes.KEY_PLAYPAUSE)])
                self.lcd_dirty = True
            if "page" in spec:
                self.goto(spec["page"])
            if "profile" in spec:
                self.set_profile(spec["profile"])
            if "set_theme" in spec:
                self.set_theme(spec["set_theme"])
                return  # the reload redrew everything
            if "wallpaper_theme" in spec:
                self.apply_wallpaper_theme()
                return
            if "brightness" in spec:
                self.set_brightness(spec["brightness"])
        except (OSError, ValueError) as e:
            log(f"key {i}: {e}")
        if self.page == page and self.profile == profile:  # a switch has already redrawn every key
            self.draw_key(i, pressed=True)

    def confirm(self, i, proc, page, profile):
        """Flash key i with the result of its command: a tick on success, a cross on failure."""
        ok = proc.wait() == 0
        time.sleep(0.15)  # let the key-up redraw land first
        th = self.theme
        spec = {"icon": "󰄬" if ok else "󰅖", "label": "done" if ok else "failed",
                "bg": th["accent"] if ok else colour(th.get("frame", "#e0231a"), th), "fg": th["key_bg"],
                "border": th["key_bg"]}
        for _ in range(2):
            with self.lock:
                if not self.deck or (self.page, self.profile) != (page, profile):
                    return
                self.deck.key_image(i, jpeg(render_key(spec, th)))
            time.sleep(0.6)
            with self.lock:
                if self.deck and (self.page, self.profile) == (page, profile):
                    self.draw_key(i)
            time.sleep(0.2)

    def key_up(self, i):
        if i in self.held:
            self.kb.release(self.held.pop(i))
        if self.keys()[i]:
            self.draw_key(i)

    def change_volume(self, clicks):
        step = self.cfg.get("volume_step", 2) if self.cfg else 2
        change_volume(clicks * step)
        self.volume = volume()

    def cycle_profile(self, direction):
        names = list(self.cfg["profiles"])
        self.set_profile(names[(names.index(self.profile) + direction) % len(names)])

    def handle(self, data):
        if len(data) < 5 or data[0] != 0x01:
            return
        if data[1] == 0x00:  # keys
            states = data[4:4 + KEYS]
            for i, s in enumerate(states):
                if s and not self.key_state[i]:
                    self.key_down(i)
                elif not s and self.key_state[i]:
                    self.key_up(i)
            self.key_state = list(states)
        elif data[1] == 0x03 and len(data) >= 7:  # dials
            kind = data[4]
            vals = [data[5], data[6]]
            if kind == 1:  # rotation, signed clicks per dial
                d0, d1 = (v - 256 if v > 127 else v for v in vals)
                if d0:
                    self.change_volume(d0)
                if d1 and self.cfg:
                    if self.dial_down[1]:  # turned while held: switch profile
                        self.dial_turned[1] = True
                        self.cycle_profile(1 if d1 > 0 else -1)
                    else:
                        self.set_page(self.page + (1 if d1 > 0 else -1), remember=False)
            elif kind == 0:  # press states; act on release so press-and-turn works
                for n in (0, 1):
                    down = bool(vals[n])
                    if down and not self.dial_down[n]:
                        self.dial_turned[n] = False
                    elif not down and self.dial_down[n] and not self.dial_turned[n]:
                        if n == 0:
                            toggle_mute()
                            self.volume = volume()
                        elif self.cfg:
                            self.history.clear()
                            self.set_page(self.start_page_index(), remember=False)
                    self.dial_down[n] = down
            self.lcd_dirty = True

    # lifecycle ------------------------------------------------------------
    def connect(self):
        path = find_hidraw()
        if not path:
            return False
        try:
            self.deck = Deck(path)
            self.deck.ping()
        except OSError as e:
            log(f"open {path}: {e}")
            if self.deck:
                self.deck.close()
            self.deck = None
            return False
        log(f"connected: {path}")
        self.key_state = [0] * KEYS
        self.dial_down = [False, False]
        self.last_lcd = None
        self.lcd_dirty = True
        time.sleep(0.2)
        self.deck.brightness(self.cfg.get("brightness", 70) if self.cfg else 70)
        self.volume = volume()
        self.play_boot()
        self.draw_keys()
        return True

    def play_boot(self):
        """The boot animation, on login and on every replug. Any key or dial skips it."""
        boot = self.cfg.get("boot", {}) if self.cfg else {}
        if not boot.get("enabled", True):
            return
        blank = jpeg(Image.new("RGB", (KEY_PX, KEY_PX), self.theme["key_bg"]))
        for i in range(KEYS):
            self.deck.key_image(i, blank)
        if boot.get("gif"):
            frames = gif_frames(boot["gif"], self.theme)
        else:
            footer = f"DECK ONLINE // {self.profile.upper()}" if self.profile else "DECK ONLINE"
            frames = ((img, keys, 1 / BOOT_FPS) for img, keys in
                      boot_frames(self.theme, boot, footer, self.key_image, boot.get("speed", 1.0)))
        drawn = set()
        try:
            for img, keys, seconds in frames:
                due = time.time() + seconds
                self.deck.lcd_image(jpeg(img))
                for i, key in keys.items():
                    if key == "real":
                        self.draw_key(i)
                        drawn.add(i)
                    else:
                        self.deck.key_image(i, jpeg(key))
                while (left := due - time.time()) > 0:
                    r, _, _ = select.select([self.deck.fd], [], [], left)
                    if r and any(self.deck.read()[4:4 + KEYS]):  # a key or dial: skip
                        raise StopIteration
        except StopIteration:
            pass
        except OSError as e:
            if e.errno not in (11,):  # EAGAIN from the non-blocking read is harmless
                raise
        # Swallow whatever was pressed during the animation so it doesn't fire an action.
        while select.select([self.deck.fd], [], [], 0.05)[0]:
            try:
                self.deck.read()
            except BlockingIOError:
                break
        self.last_lcd, self.lcd_dirty = None, True

    def disconnect(self):
        self.release_held()
        if self.deck:
            self.deck.close()
        self.deck = None
        log("disconnected")

    def pinger(self):
        while self.running:
            deck = self.deck
            if deck:
                try:
                    deck.ping()
                except OSError:
                    pass  # the main loop notices and reconnects
            time.sleep(PING_INTERVAL)

    def main(self):
        threading.Thread(target=self.pinger, daemon=True).start()
        threading.Thread(target=focus_events, args=(self.on_focus,), daemon=True).start()
        with self.lock:
            self.reload_if_changed()
        next_tick, ticks = 0, 0
        while self.running:
            if not self.deck:
                with self.lock:
                    connected = self.connect()
                if not connected:
                    time.sleep(2)
                    continue
            try:
                r, _, _ = select.select([self.deck.fd], [], [], max(0.0, min(0.1, next_tick - time.time())))
                with self.lock:
                    if r:
                        self.handle(self.deck.read())
                    if time.time() >= next_tick:
                        next_tick = time.time() + 1
                        ticks += 1
                        if ticks % 10 == 0:
                            self.follow_wallpaper()
                        self.reload_if_changed()
                        self.volume = volume()
                        self.draw_lcd()
                    elif self.lcd_dirty:
                        self.draw_lcd()
                    self.publish_state()
            except OSError as e:
                log(f"device error: {e}")
                with self.lock:
                    self.disconnect()
                time.sleep(1)
        self.disconnect()


PID_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "galleon-deck.pid")
STATE_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "galleon-deck.state")


def deck_state():
    """What the running deck shows: {"profile": ..., "page": ...}, or None."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--version":
        print(VERSION)
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--theme-from-wallpaper":
        settings = {}
        try:
            with open(CONFIG_PATH, "rb") as f:
                settings = tomllib.load(f).get("wallpaper_theme", {})
        except (OSError, tomllib.TOMLDecodeError):
            pass
        path = write_wallpaper_theme(settings, sys.argv[2] if len(sys.argv) > 2 else None)
        if path:
            print(open(path).read())
        sys.exit(0 if path else 1)
    app = App()

    def stop(*_):
        app.running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with open(PID_FILE, "w") as f:  # lets the configurator restart us without systemd
        f.write(str(os.getpid()))
    try:
        app.main()
    finally:
        for path in (PID_FILE, STATE_FILE):
            try:
                os.remove(path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
