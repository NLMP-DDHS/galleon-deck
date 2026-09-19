#!/usr/bin/env python3
"""Galleon Deck Configurator: a GTK4/libadwaita editor for galleon-deck.

Edits the same files the service reads (~/.config/galleon-deck); every change is
saved as you make it and the service picks it up within a second, so the real
deck updates live. Previews are drawn with the service's own renderer.
"""

import io
import os
import shutil
import subprocess
import sys
import time
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import galleon_deck as gd  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

APP_ID = "io.github.galleondeck.Config"
KEY_PREVIEW = 116
LCD_PREVIEW = (540, 288)

ACTIONS = [  # (label, spec key)
    ("Nothing", None),
    ("Press a key", "key"),
    ("Shortcut", "keys"),
    ("Run a command", "exec"),
    ("Media control", "media"),
    ("Go to page", "page"),
    ("Switch profile", "profile"),
]
MEDIA = ["play-pause", "next", "previous", "stop"]
COLOUR_CHOICES = ["Theme default", "fg", "accent", "frame", "dim", "border", "key_bg", "bg", "Custom"]
THEME_COLOURS = ["bg", "key_bg", "fg", "accent", "frame", "dim", "border"]
MODIFIERS = {"CONTROL_MASK": "CTRL", "SHIFT_MASK": "SHIFT", "ALT_MASK": "ALT", "SUPER_MASK": "SUPER"}

CSS = b"""
.deck { background: #000; border-radius: 18px; padding: 18px; }
.deck-key { padding: 0; border-radius: 10px; min-width: 0; min-height: 0; background: none; box-shadow: none; }
.deck-key.selected { outline: 3px solid @accent_color; outline-offset: 2px; }
.deck-lcd { border-radius: 6px; }
.glyph { font-family: "Symbols Nerd Font"; font-size: 26px; }
.glyph-name { font-size: 9px; opacity: 0.7; }
.theme-tile { padding: 0; border-radius: 12px; }
.theme-tile.selected { outline: 3px solid @accent_color; outline-offset: 2px; }
"""


def texture(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return Gdk.Texture.new_from_bytes(GLib.Bytes.new(buf.getvalue()))


def rgba(hex_colour):
    c = Gdk.RGBA()
    c.parse(hex_colour if isinstance(hex_colour, str) and hex_colour.startswith("#") else "#888888")
    return c


def rgba_hex(c):
    return "#%02x%02x%02x" % (round(c.red * 255), round(c.green * 255), round(c.blue * 255))


def systemd_unit():
    return shutil.which("systemctl") and subprocess.run(
        ["systemctl", "--user", "cat", "galleon-deck.service"], capture_output=True).returncode == 0


def service_running():
    if systemd_unit():
        return subprocess.run(["systemctl", "--user", "is-active", "--quiet", "galleon-deck.service"]).returncode == 0
    try:
        with open(gd.PID_FILE) as f:
            os.kill(int(f.read().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def restart_service():
    """Restart through systemd when installed as a user unit, else by PID (XDG autostart installs)."""
    if systemd_unit():
        r = subprocess.run(["systemctl", "--user", "restart", "galleon-deck.service"], capture_output=True, text=True)
        return r.returncode == 0, r.stderr.strip()
    try:
        with open(gd.PID_FILE) as f:
            os.kill(int(f.read().strip()), 15)
        time.sleep(1)
    except (OSError, ValueError):
        pass
    exe = shutil.which("galleon-deck")
    if not exe:
        return False, "galleon-deck is not on PATH"
    subprocess.Popen([exe], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True, ""


def icon_names():
    """(glyph, name) for every icon in Symbols Nerd Font, or a small built-in set."""
    try:
        from fontTools.ttLib import TTFont
        path = subprocess.run(["fc-match", "-f", "%{file}", "Symbols Nerd Font"], capture_output=True, text=True).stdout
        cmap = TTFont(path, lazy=True).getBestCmap()
        return sorted(((chr(cp), name) for cp, name in cmap.items() if cp > 0xE000), key=lambda t: t[1])
    except Exception:  # fontTools missing or font not found
        return [(chr(cp), name) for cp, name in (
            (0xF120, "fa-terminal"), (0xF07B, "fa-folder"), (0xF269, "fa-firefox"), (0xF1B6, "fa-steam"),
            (0xF066F, "md-discord"), (0xF040E, "md-play_pause"), (0xF04AE, "md-skip_previous"),
            (0xF04AD, "md-skip_next"), (0xF04DB, "md-stop"), (0xF036D, "md-microphone_off"),
            (0xF0100, "md-camera"), (0xF02B4, "md-google_controller"), (0xF004D, "md-arrow_left"),
            (0xF02DC, "md-home"), (0xF0311, "md-keyboard_return"), (0xF003B, "md-apps"),
        )]


class Model:
    """The config on disk, in the raw shape the files have (no generated pages)."""

    def __init__(self):
        self.load()

    def load(self):
        with open(gd.CONFIG_PATH, "rb") as f:
            self.config = tomllib.load(f)
        self.profiles = {}
        for path in sorted(os.listdir(gd.PROFILE_DIR)):
            if path.endswith(".toml"):
                full = os.path.join(gd.PROFILE_DIR, path)
                with open(full, "rb") as f:
                    data = tomllib.load(f)
                for page in data.setdefault("page", []):
                    keys = page.setdefault("keys", [])
                    page["keys"] = (keys + [{}] * gd.KEYS)[:gd.KEYS]
                self.profiles[path[:-5]] = {"path": full, "data": data}
        self.themes = gd.all_themes()
        self.mtime = gd.config_mtime()

    def theme_for(self, profile):
        data = self.profiles[profile]["data"]
        name = data.get("theme", gd.DEFAULT_THEME)
        return {**self.themes.get(name, gd.THEME_DEFAULTS), **data.get("theme_overrides", {})}

    def save_profile(self, name):
        p = self.profiles[name]
        gd.write_profile(p["path"], p["data"])
        self.mtime = gd.config_mtime()

    def set(self, table, key, value, remove=False):
        gd.set_value(gd.CONFIG_PATH, table, key, value, remove=remove)
        section = self.config.setdefault(table, {}) if table else self.config
        if remove:
            section.pop(key, None)
        else:
            section[key] = value
        self.mtime = gd.config_mtime()


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Galleon Deck", default_width=1200, default_height=800)
        self.model = Model()
        self.profile = self.model.config.get("start_profile") if self.model.config.get("start_profile") in self.model.profiles else next(iter(self.model.profiles))
        self.page = 0
        self.key = 0
        self.pending = {}  # debounce source ids
        self.icons = None

        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        header = Adw.HeaderBar()
        view.add_top_bar(header)
        self.profile_list = Gtk.StringList()
        self.profile_drop = Gtk.DropDown(model=self.profile_list, tooltip_text="Profile")
        self.profile_drop.connect("notify::selected", self.on_profile_selected)
        header.pack_start(self.profile_drop)
        header.pack_start(self.menu_button("view-more-symbolic", "Profile actions", [
            ("New profile…", self.new_profile), ("Duplicate profile…", self.duplicate_profile),
            ("Rename profile…", self.rename_profile), ("Delete profile…", self.delete_profile),
            ("Start with this profile", self.make_start_profile),
        ]))
        self.status = Gtk.Label(css_classes=["dim-label"])
        header.pack_end(self.menu_button("open-menu-symbolic", "Menu", [
            ("Replay boot animation", self.replay_boot), ("Restart service", self.restart_service),
            ("Open config folder", self.open_folder), ("About", self.about),
        ]))
        header.pack_end(self.status)

        self.banner = Adw.Banner(revealed=False)
        view.add_top_bar(self.banner)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=640, shrink_start_child=False, shrink_end_child=False)
        view.set_content(paned)
        paned.set_start_child(self.build_deck())

        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        switcher_bar = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=8, margin_bottom=4)
        switcher_bar.append(switcher)
        right.append(switcher_bar)
        self.stack.set_vexpand(True)
        right.append(self.stack)
        paned.set_end_child(right)
        self.key_page = Adw.PreferencesPage()
        self.look_page = Adw.PreferencesPage()
        self.settings_page = Adw.PreferencesPage()
        self.stack.add_titled_with_icon(self.key_page, "key", "Key", "input-keyboard-symbolic")
        self.stack.add_titled_with_icon(self.look_page, "look", "Look", "applications-graphics-symbolic")
        self.stack.add_titled_with_icon(self.settings_page, "settings", "Settings", "preferences-system-symbolic")

        self.refresh_all()
        GLib.timeout_add_seconds(2, self.watch)

    # -------------------------------------------------------------- helpers
    def menu_button(self, icon, tooltip, items):
        menu = Gio.Menu()
        group = Gio.SimpleActionGroup()
        for i, (label, handler) in enumerate(items):
            name = f"a{i}"
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, h=handler: h())
            group.add_action(action)
            menu.append(label, f"m.{name}")
        button = Gtk.MenuButton(icon_name=icon, menu_model=menu, tooltip_text=tooltip)
        button.insert_action_group("m", group)
        return button

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast.new(text))

    def debounce(self, name, fn, ms=400):
        if name in self.pending:
            GLib.source_remove(self.pending[name])

        def fire():
            self.pending.pop(name, None)
            fn()
            return False
        self.pending[name] = GLib.timeout_add(ms, fire)

    def ask_text(self, heading, body, initial, done):
        dialog = Adw.AlertDialog.new(heading, body)
        entry = Gtk.Entry(text=initial, activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", "OK")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda d, r: r == "ok" and entry.get_text().strip() and done(entry.get_text().strip()))
        dialog.present(self)

    def confirm(self, heading, body, label, done):
        dialog = Adw.AlertDialog.new(heading, body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", label)
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda d, r: r == "ok" and done())
        dialog.present(self)

    @property
    def data(self):
        return self.model.profiles[self.profile]["data"]

    def pages(self):
        """Editable pages, plus the generated settings page when enabled."""
        pages = list(self.data["page"])
        if self.data.get("settings_page", True):
            pages.append({"name": "settings", "settings": True, "keys": [{}] * gd.KEYS})
        return pages

    def spec(self):
        page = self.pages()[self.page]
        return None if page.get("settings") else page["keys"][self.key]

    def changed_profile(self):
        """The current profile was edited: redraw and save soon."""
        self.refresh_deck()
        name = self.profile
        self.debounce("profile:" + name, lambda: self.model.save_profile(name))

    # ----------------------------------------------------------------- deck
    def build_deck(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=16, margin_bottom=16,
                      margin_start=16, margin_end=16, halign=Gtk.Align.CENTER)
        bar = Gtk.Box(spacing=6)
        self.page_buttons = Gtk.Box(css_classes=["linked"])
        bar.append(self.page_buttons)
        bar.append(self.menu_button("list-add-symbolic", "Page actions", [
            ("Add page…", self.add_page), ("Rename page…", self.rename_page), ("Delete page…", self.delete_page),
            ("Move page left", lambda: self.move_page(-1)), ("Move page right", lambda: self.move_page(1)),
            ("Make start page", self.make_start_page),
        ]))
        box.append(bar)

        deck = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, css_classes=["deck"])
        self.lcd = Gtk.Picture(css_classes=["deck-lcd"], can_shrink=False)
        self.lcd.set_size_request(*LCD_PREVIEW)
        deck.append(self.lcd)
        grid = Gtk.Grid(row_spacing=12, column_spacing=12, halign=Gtk.Align.CENTER)
        self.key_buttons, self.key_pictures = [], []
        for i in range(gd.KEYS):
            pic = Gtk.Picture(can_shrink=False)
            pic.set_size_request(KEY_PREVIEW, KEY_PREVIEW)
            button = Gtk.Button(child=pic, css_classes=["deck-key"], tooltip_text=f"Key {i + 1}")
            button.connect("clicked", lambda _b, i=i: self.select_key(i))
            drag = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
            drag.connect("prepare", lambda _s, _x, _y, i=i: Gdk.ContentProvider.new_for_value(GObject.Value(GObject.TYPE_INT, i)))
            drag.connect("drag-begin", lambda s, _d, p=pic: s.set_icon(Gtk.WidgetPaintable.new(p), 40, 40))
            button.add_controller(drag)
            drop = Gtk.DropTarget.new(GObject.TYPE_INT, Gdk.DragAction.MOVE)
            drop.connect("drop", lambda _t, value, _x, _y, i=i: self.swap_keys(value, i))
            button.add_controller(drop)
            grid.attach(button, i % 3, i // 3, 1, 1)
            self.key_buttons.append(button)
            self.key_pictures.append(pic)
        deck.append(grid)
        box.append(deck)
        box.append(Gtk.Label(label="Click a key to edit it; drag keys to rearrange them.", css_classes=["dim-label"]))
        return box

    def refresh_deck(self):
        pages = self.pages()
        self.page = min(self.page, len(pages) - 1)
        theme = self.model.theme_for(self.profile)
        page = pages[self.page]
        if page.get("settings"):
            try:
                fake = object.__new__(gd.App)
                fake.cfg, fake.profile = gd.load_config(), self.profile
                fake.page = len(fake.pages()) - 1
                images = [fake.key_image(i) for i in range(gd.KEYS)]
            except Exception as e:  # noqa: BLE001 - show whatever went wrong
                images = [gd.render_key({}, theme)] * gd.KEYS
                self.show_error(str(e))
        else:
            images = [gd.render_key(spec, theme) for spec in page["keys"]]
        for i, img in enumerate(images):
            self.key_pictures[i].set_paintable(texture(img.resize((KEY_PREVIEW, KEY_PREVIEW))))
            if i == self.key:
                self.key_buttons[i].add_css_class("selected")
            else:
                self.key_buttons[i].remove_css_class("selected")
        now = time.localtime()
        state = {
            "profile": self.profile, "page": page["name"], "page_index": self.page, "page_count": len(pages),
            "clock": time.strftime(self.model.config.get("clock_format", "%H:%M"), now),
            "date": time.strftime("%A %d %B", now).upper(), "volume": (42, False),
            "media": "Artist — Now playing", "media_icon": "󰐊", "error": None,
        }
        self.lcd.set_paintable(texture(gd.render_lcd(state, theme).resize(LCD_PREVIEW)))

    def refresh_page_buttons(self):
        while (child := self.page_buttons.get_first_child()):
            self.page_buttons.remove(child)
        first = None
        for i, page in enumerate(self.pages()):
            b = Gtk.ToggleButton(label=page["name"], active=i == self.page)
            if page.get("settings"):
                b.set_icon_name("emblem-system-symbolic")
                b.set_tooltip_text("Settings page (generated: themes, brightness, profiles)")
            if first:
                b.set_group(first)
            first = first or b
            b.connect("toggled", lambda btn, i=i: btn.get_active() and self.select_page(i))
            self.page_buttons.append(b)

    def select_page(self, i):
        if i != self.page:
            self.page = i
            self.refresh_deck()
            self.build_key_page()

    def select_key(self, i):
        self.key = i
        self.refresh_deck()
        self.build_key_page()
        self.stack.set_visible_child_name("key")

    def swap_keys(self, a, b):
        page = self.pages()[self.page]
        if page.get("settings") or a == b:
            return False
        keys = page["keys"]
        keys[a], keys[b] = keys[b], keys[a]
        self.key = b
        self.changed_profile()
        self.build_key_page()
        return True

    # ------------------------------------------------------------- key page
    def clear_page(self, page):
        while (child := page.get_first_child()):
            # PreferencesPage wraps groups in internal widgets; remove groups we added
            break
        for group in getattr(page, "_groups", []):
            page.remove(group)
        page._groups = []

    def add_group(self, page, group):
        page.add(group)
        page._groups = getattr(page, "_groups", []) + [group]

    def build_key_page(self):
        page = self.key_page
        self.clear_page(page)
        spec = self.spec()
        if spec is None:
            g = Adw.PreferencesGroup(title="Settings page",
                                     description="This page is generated: a key per theme, one that builds a theme "
                                                 "from your wallpaper, brightness, and a key per profile. Turn it off "
                                                 "under Look.")
            self.add_group(page, g)
            return

        look = Adw.PreferencesGroup(title=f"Key {self.key + 1}", description="Top-left is 1; keys run left to right, top to bottom.")
        clear = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER, css_classes=["flat"])
        clear.connect("clicked", lambda _b: self.clear_key())
        look.set_header_suffix(clear)
        self.add_group(page, look)

        label = Adw.EntryRow(title="Label", text=str(spec.get("label", "")))
        label.connect("changed", lambda r: self.set_field("label", r.get_text()))
        look.add(label)

        icon_row = Adw.ActionRow(title="Icon", subtitle="Any Nerd Font icon")
        self.icon_glyph = Gtk.Label(label=spec.get("icon", ""), css_classes=["glyph"])
        icon_row.add_suffix(self.icon_glyph)
        pick = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        pick.connect("clicked", lambda _b: self.pick_icon())
        icon_row.add_suffix(pick)
        rm = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"], tooltip_text="No icon")
        rm.connect("clicked", lambda _b: (self.set_field("icon", ""), self.icon_glyph.set_label("")))
        icon_row.add_suffix(rm)
        look.add(icon_row)

        image_row = Adw.ActionRow(title="Image", use_markup=False, subtitle=spec.get("image") or "Instead of an icon: a PNG/JPEG")
        choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose.connect("clicked", lambda _b: self.pick_file("Key image", ["image/png", "image/jpeg", "image/webp"],
                                                           lambda path: (self.set_field("image", path), self.build_key_page())))
        image_row.add_suffix(choose)
        rm = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"], tooltip_text="No image")
        rm.connect("clicked", lambda _b: (self.set_field("image", ""), self.build_key_page()))
        image_row.add_suffix(rm)
        look.add(image_row)

        for field, title, default in (("fg", "Text and icon colour", "fg"), ("bg", "Background", "key_bg"), ("border", "Border", "border")):
            look.add(self.colour_row(title, spec.get(field), lambda v, f=field: self.set_field(f, v), default))

        action = Adw.PreferencesGroup(title="When pressed")
        self.add_group(page, action)
        current = next((i for i, (_, k) in enumerate(ACTIONS) if k and k in spec), 0)
        kind = Adw.ComboRow(title="Action", model=Gtk.StringList.new([a for a, _ in ACTIONS]), selected=current)
        kind.connect("notify::selected", lambda r, _p: self.set_action_kind(ACTIONS[r.get_selected()][1]))
        action.add(kind)
        k = ACTIONS[current][1]
        if k in ("key", "keys"):
            row = Adw.EntryRow(title="Key name, e.g. KP7, F5, ENTER" if k == "key" else "Shortcut, e.g. CTRL+SHIFT+T",
                               text=str(spec.get(k, "")))
            row.connect("changed", lambda r: self.set_key_field(k, r))
            rec = Gtk.Button(label="Record", valign=Gtk.Align.CENTER, tooltip_text="Press the key or shortcut to use")
            rec.connect("clicked", lambda _b: self.record_key(k, row))
            row.add_suffix(rec)
            action.add(row)
        elif k == "exec":
            row = Adw.EntryRow(title="Command", text=str(spec.get("exec", "")))
            row.connect("changed", lambda r: self.set_field("exec", r.get_text()))
            action.add(row)
            confirm = Adw.SwitchRow(title="Flash ✓ / ✗ when it finishes", active=bool(spec.get("confirm")))
            confirm.connect("notify::active", lambda r, _p: self.set_field("confirm", r.get_active() or ""))
            action.add(confirm)
        elif k == "media":
            row = Adw.ComboRow(title="Control", model=Gtk.StringList.new(MEDIA),
                               selected=MEDIA.index(spec["media"]) if spec.get("media") in MEDIA else 0)
            row.connect("notify::selected", lambda r, _p: self.set_field("media", MEDIA[r.get_selected()]))
            action.add(row)
        elif k == "page":
            names = [p["name"] for p in self.data["page"]] + ["back"]
            row = Adw.ComboRow(title="Page", model=Gtk.StringList.new(names),
                               selected=names.index(spec["page"]) if spec.get("page") in names else 0)
            row.connect("notify::selected", lambda r, _p: self.set_field("page", names[r.get_selected()]))
            action.add(row)
        elif k == "profile":
            names = list(self.model.profiles)
            row = Adw.ComboRow(title="Profile", model=Gtk.StringList.new(names),
                               selected=names.index(spec["profile"]) if spec.get("profile") in names else 0)
            row.connect("notify::selected", lambda r, _p: self.set_field("profile", names[r.get_selected()]))
            action.add(row)

    def colour_row(self, title, value, on_change, default="fg"):
        """A dropdown of theme colours plus a custom colour button; `default` is
        the theme colour used when nothing is chosen."""
        choices = COLOUR_CHOICES
        row = Adw.ActionRow(title=title)
        button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER)
        custom = isinstance(value, str) and value.startswith("#")
        selected = choices.index("Custom") if custom else (choices.index(value) if value in choices else 0)
        drop = Gtk.DropDown(model=Gtk.StringList.new(choices), selected=selected, valign=Gtk.Align.CENTER)
        button.set_rgba(rgba(value if custom else self.model.theme_for(self.profile).get(value if value in THEME_COLOURS else default, "#888888")))
        button.set_sensitive(custom)

        def picked(*_a):
            choice = choices[drop.get_selected()]
            button.set_sensitive(choice == "Custom")
            if choice == "Custom":
                on_change(rgba_hex(button.get_rgba()))
            else:
                on_change("" if drop.get_selected() == 0 else choice)
        drop.connect("notify::selected", picked)
        button.connect("notify::rgba", lambda *_a: choices[drop.get_selected()] == "Custom" and on_change(rgba_hex(button.get_rgba())))
        row.add_suffix(drop)
        row.add_suffix(button)
        return row

    def set_field(self, field, value):
        spec = self.spec()
        if spec is None:
            return
        page_keys = self.pages()[self.page]["keys"]
        if not spec:  # an empty {} may be shared; give this key its own dict
            spec = {}
            page_keys[self.key] = spec
        if value in ("", None, False):
            spec.pop(field, None)
        else:
            spec[field] = value
        self.changed_profile()

    def set_key_field(self, field, row):
        text = row.get_text().strip()
        try:
            if text:
                gd.Keyboard.codes(text)
            row.remove_css_class("error")
            self.set_field(field, text)
        except ValueError:
            row.add_css_class("error")

    def set_action_kind(self, kind):
        spec = self.spec()
        if spec is None:
            return
        if not spec:
            spec = {}
            self.pages()[self.page]["keys"][self.key] = spec
        for _, k in ACTIONS:
            if k and k != kind:
                spec.pop(k, None)
        if kind != "exec":
            spec.pop("confirm", None)
        defaults = {"media": "play-pause", "page": "back", "profile": next(iter(self.model.profiles))}
        if kind and kind not in spec and kind in defaults:
            spec[kind] = defaults[kind]
        self.changed_profile()
        self.build_key_page()

    def clear_key(self):
        spec = self.spec()
        if spec is not None:
            self.pages()[self.page]["keys"][self.key] = {}
            self.changed_profile()
            self.build_key_page()

    def record_key(self, field, row):
        dialog = Adw.AlertDialog.new("Press the key" if field == "key" else "Press the shortcut",
                                     "Waiting for a key… (Escape cancels.) Shortcuts your desktop grabs, "
                                     "like SUPER combinations, may not reach this window.")
        dialog.add_response("cancel", "Cancel")
        ctl = Gtk.EventControllerKey()

        def pressed(_c, keyval, keycode, state):
            name = gd.ecodes.KEY.get(keycode - 8)
            name = (name[0] if isinstance(name, list) else name or "").removeprefix("KEY_")
            if not name or name == "ESC" and field == "key" and not state:
                dialog.close()
                return True
            if name in ("LEFTCTRL", "RIGHTCTRL", "LEFTSHIFT", "RIGHTSHIFT", "LEFTALT", "RIGHTALT", "LEFTMETA", "RIGHTMETA") and field == "keys":
                return True  # wait for the main key of the shortcut
            mods = [m for flag, m in MODIFIERS.items() if state & getattr(Gdk.ModifierType, flag)] if field == "keys" else []
            row.set_text("+".join(mods + [name]))
            dialog.close()
            return True
        ctl.connect("key-pressed", pressed)
        dialog.add_controller(ctl)
        dialog.present(self)

    def pick_icon(self):
        if self.icons is None:
            self.icons = icon_names()
        dialog = Adw.Dialog(title="Choose an icon", content_width=560, content_height=560)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        box.append(header)
        search = Gtk.SearchEntry(placeholder_text=f"Search {len(self.icons)} icons (e.g. discord, play, folder)",
                                 margin_start=12, margin_end=12, margin_bottom=8)
        box.append(search)
        store = Gtk.StringList.new([f"{g}\t{n}" for g, n in self.icons])
        query = {"text": ""}
        flt = Gtk.CustomFilter.new(lambda item: query["text"] in item.get_string().split("\t", 1)[1])
        filtered = Gtk.FilterListModel(model=store, filter=flt)
        selection = Gtk.SingleSelection(model=filtered, autoselect=False)
        factory = Gtk.SignalListItemFactory()

        def setup(_f, item):
            b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6)
            b.append(Gtk.Label(css_classes=["glyph"]))
            b.append(Gtk.Label(css_classes=["glyph-name"], ellipsize=3, max_width_chars=12))
            item.set_child(b)

        def bind(_f, item):
            glyph, name = item.get_item().get_string().split("\t", 1)
            b = item.get_child()
            b.get_first_child().set_label(glyph)
            b.get_last_child().set_label(name.split("-", 1)[-1])
            b.set_tooltip_text(name)
        factory.connect("setup", setup)
        factory.connect("bind", bind)
        grid = Gtk.GridView(model=selection, factory=factory, max_columns=8, min_columns=4, single_click_activate=True)

        def activate(_g, pos):
            glyph = filtered.get_item(pos).get_string().split("\t", 1)[0]
            self.set_field("icon", glyph)
            self.icon_glyph.set_label(glyph)
            dialog.close()
        grid.connect("activate", activate)
        scroll = Gtk.ScrolledWindow(child=grid, vexpand=True)
        box.append(scroll)

        def searched(entry):
            query["text"] = entry.get_text().lower().replace(" ", "_")
            flt.changed(Gtk.FilterChange.DIFFERENT)
        search.connect("search-changed", searched)
        dialog.set_child(box)
        dialog.present(self)

    def pick_file(self, title, mime_types, done):
        dialog = Gtk.FileDialog(title=title)
        flt = Gtk.FileFilter()
        for m in mime_types:
            flt.add_mime_type(m)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(flt)
        dialog.set_filters(filters)

        def finished(d, result):
            try:
                f = d.open_finish(result)
            except GLib.Error:
                return
            if f and f.get_path():
                done(f.get_path())
        dialog.open(self, None, finished)

    # ------------------------------------------------------------ look page
    def build_look_page(self):
        page = self.look_page
        self.clear_page(page)
        data = self.data
        themes = self.model.themes

        g = Adw.PreferencesGroup(title="Theme", description=f"Used by the “{self.profile}” profile.")
        self.add_group(page, g)
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=5, min_children_per_line=3,
                           row_spacing=10, column_spacing=10, homogeneous=True)
        current = data.get("theme", gd.DEFAULT_THEME)
        for name, theme in themes.items():
            pic = Gtk.Picture(can_shrink=False)
            pic.set_size_request(96, 96)
            pic.set_paintable(texture(gd.render_theme_key(name, theme, name == current).resize((96, 96))))
            b = Gtk.Button(child=pic, css_classes=["theme-tile"] + (["selected"] if name == current else []), tooltip_text=name)
            b.connect("clicked", lambda _b, n=name: self.set_theme(n))
            flow.append(b)
        g.add(flow)

        wp = Adw.PreferencesGroup(title="Theme from wallpaper",
                                  description="Builds a “wallpaper” theme from the wallpaper you have up: its most vivid "
                                              "colours for text and accents, a dark tint of it for the background.")
        self.add_group(page, wp)
        row = Adw.ActionRow(title="Generate now", use_markup=False, subtitle=gd.wallpaper_path(self.model.config.get("wallpaper_theme", {})) or "No wallpaper found — set the command in Settings")
        btn = Gtk.Button(label="Generate & use", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        btn.connect("clicked", lambda _b: self.generate_wallpaper_theme())
        row.add_suffix(btn)
        wp.add(row)
        auto = Adw.SwitchRow(title="Follow wallpaper changes", subtitle="Regenerate within ~10 s of a wallpaper change",
                             active=bool(self.model.config.get("wallpaper_theme", {}).get("auto")))
        auto.connect("notify::active", lambda r, _p: self.model.set("wallpaper_theme", "auto", r.get_active()))
        wp.add(auto)

        ov = Adw.PreferencesGroup(title="Colour tweaks", description="Change single colours of the theme for this profile only.")
        self.add_group(page, ov)
        overrides = data.get("theme_overrides", {})
        base = themes.get(current, gd.THEME_DEFAULTS)
        for field in THEME_COLOURS:
            row = Adw.ActionRow(title={"bg": "Screen background", "key_bg": "Key background", "fg": "Text and icons",
                                       "accent": "Highlight", "frame": "Screen frame", "dim": "Secondary text",
                                       "border": "Key borders"}[field],
                                subtitle="Tweaked" if field in overrides else "From theme")
            button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER,
                                           rgba=rgba(gd.colour(overrides.get(field, base.get(field, base["accent"])), base)))
            button.connect("notify::rgba", lambda b, _p, f=field, r=row: self.set_override(f, rgba_hex(b.get_rgba()), r))
            reset = Gtk.Button(icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"], tooltip_text="Back to the theme's colour")
            reset.connect("clicked", lambda _b, f=field: self.set_override(f, None))
            row.add_suffix(button)
            row.add_suffix(reset)
            ov.add(row)

        pg = Adw.PreferencesGroup(title="Pages")
        self.add_group(page, pg)
        names = [p["name"] for p in data["page"]]
        start = Adw.ComboRow(title="Start page", subtitle="Shown first, and where a right-dial press returns",
                             model=Gtk.StringList.new(names),
                             selected=names.index(data["start_page"]) if data.get("start_page") in names else 0)
        start.connect("notify::selected", lambda r, _p: self.set_profile_field("start_page", names[r.get_selected()]))
        pg.add(start)
        sp = Adw.SwitchRow(title="Settings page", subtitle="The generated last page: themes, brightness, profiles",
                           active=data.get("settings_page", True))
        sp.connect("notify::active", lambda r, _p: self.set_profile_field("settings_page", r.get_active()))
        pg.add(sp)

    def set_theme(self, name):
        self.data["theme"] = name
        self.changed_profile()
        self.build_look_page()

    def set_override(self, field, value, row=None):
        ov = self.data.setdefault("theme_overrides", {})
        if value is None:
            ov.pop(field, None)
            self.build_look_page()
        else:
            ov[field] = value
            if row:
                row.set_subtitle("Tweaked")
        if not ov:
            self.data.pop("theme_overrides", None)
        self.changed_profile()

    def set_profile_field(self, field, value):
        if field == "settings_page" and value:
            self.data.pop("settings_page", None)
        else:
            self.data[field] = value
        self.changed_profile()
        self.refresh_page_buttons()

    def generate_wallpaper_theme(self):
        settings = self.model.config.get("wallpaper_theme", {})
        if gd.write_wallpaper_theme(settings):
            self.model.themes = gd.all_themes()
            self.set_theme("wallpaper")
            self.toast("Theme generated from your wallpaper")
        else:
            self.toast("No wallpaper found — set the wallpaper command in Settings")

    # -------------------------------------------------------- settings page
    def build_settings_page(self):
        page = self.settings_page
        self.clear_page(page)
        cfg = self.model.config

        g = Adw.PreferencesGroup(title="Deck")
        self.add_group(page, g)
        bright = Adw.SpinRow.new_with_range(10, 100, 5)
        bright.set_title("Brightness")
        bright.set_value(cfg.get("brightness", 70))
        bright.connect("notify::value", lambda r, _p: self.debounce("brightness", lambda: self.model.set(None, "brightness", int(r.get_value()))))
        g.add(bright)
        step = Adw.SpinRow.new_with_range(1, 10, 1)
        step.set_title("Volume step")
        step.set_subtitle("Percent per click of the left dial")
        step.set_value(cfg.get("volume_step", 2))
        step.connect("notify::value", lambda r, _p: self.debounce("volume_step", lambda: self.model.set(None, "volume_step", int(r.get_value()))))
        g.add(step)
        clock = Adw.EntryRow(title="Clock format (strftime: %H:%M, %I:%M %p, …)", text=cfg.get("clock_format", "%H:%M"))
        clock.connect("changed", lambda r: self.debounce("clock", lambda: (self.model.set(None, "clock_format", r.get_text() or "%H:%M"), self.refresh_deck())))
        g.add(clock)
        names = list(self.model.profiles)
        start = Adw.ComboRow(title="Start profile", model=Gtk.StringList.new(names),
                             selected=names.index(cfg["start_profile"]) if cfg.get("start_profile") in names else 0)
        start.connect("notify::selected", lambda r, _p: self.model.set(None, "start_profile", names[r.get_selected()]))
        g.add(start)

        boot = cfg.get("boot", {})
        b = Adw.PreferencesGroup(title="Boot animation", description="Plays at login and whenever the keyboard is plugged in.")
        play = Gtk.Button(label="Play now", valign=Gtk.Align.CENTER, css_classes=["flat"])
        play.connect("clicked", lambda _b: self.replay_boot())
        b.set_header_suffix(play)
        self.add_group(page, b)
        en = Adw.SwitchRow(title="Enabled", active=boot.get("enabled", True))
        en.connect("notify::active", lambda r, _p: self.model.set("boot", "enabled", r.get_active()))
        b.add(en)
        speed = Adw.SpinRow.new_with_range(0.25, 2.0, 0.25)
        speed.set_digits(2)
        speed.set_title("Speed")
        speed.set_subtitle("1.0 is about 5 seconds; 0.5 is half speed")
        speed.set_value(boot.get("speed", 1.0))
        speed.connect("notify::value", lambda r, _p: self.debounce("speed", lambda: self.model.set("boot", "speed", round(r.get_value(), 2))))
        b.add(speed)
        title = Adw.EntryRow(title="Title", text=boot.get("title", gd.BOOT_TITLE))
        title.connect("changed", lambda r: self.debounce("title", lambda: self.model.set("boot", "title", r.get_text())))
        b.add(title)
        lines = Adw.EntryRow(title="Status lines: LABEL|STATUS, separated by ;",
                             text="; ".join(boot.get("lines", gd.BOOT_LINES)))
        lines.connect("changed", lambda r: self.debounce("lines", lambda: self.model.set(
            "boot", "lines", [x.strip() for x in r.get_text().split(";") if x.strip()][:6])))
        b.add(lines)
        for field, label, types in (("logo", "Logo (transparent PNG works best)", ["image/png", "image/webp"]),
                                    ("gif", "Your own animation instead (GIF/APNG/WebP)", ["image/gif", "image/png", "image/webp"])):
            row = Adw.ActionRow(title=label, subtitle=boot.get(field) or "None", use_markup=False)
            choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
            choose.connect("clicked", lambda _b, f=field, t=types, r=row: self.pick_file(
                "Choose file", t, lambda path, f=f, r=r: (self.model.set("boot", f, path), r.set_subtitle(path))))
            rm = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
            rm.connect("clicked", lambda _b, f=field, r=row: (self.model.set("boot", f, None, remove=True), r.set_subtitle("None")))
            row.add_suffix(choose)
            row.add_suffix(rm)
            b.add(row)

        a = Adw.PreferencesGroup(title="Auto-switch profiles",
                                 description="Switch profile by the focused window's class (a regular expression). "
                                             + ("Following window focus on " + {"hyprland": "Hyprland", "sway": "Sway", "i3": "i3", "niri": "niri",
                                                                                "x11": "X11"}[gd.focus_source()] + "." if gd.focus_source() else
                                                "Not available on this desktop: GNOME and KDE on Wayland don't expose the "
                                                "focused window. Works on Hyprland, Sway, i3, niri and X11 desktops."))
        add = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"], tooltip_text="Add rule")
        add.connect("clicked", lambda _b: self.add_rule())
        a.set_header_suffix(add)
        self.add_group(page, a)
        auto = cfg.get("auto_switch", {})
        fallback_names = ["(stay on current)"] + names
        fb = Adw.ComboRow(title="Other windows", subtitle="Profile when no rule matches", model=Gtk.StringList.new(fallback_names),
                          selected=fallback_names.index(auto["fallback"]) if auto.get("fallback") in names else 0)
        fb.connect("notify::selected", lambda r, _p: self.model.set(
            "auto_switch", "fallback", fallback_names[r.get_selected()], remove=r.get_selected() == 0))
        a.add(fb)
        for name, p in self.model.profiles.items():  # rules a profile brings along (add-ons), checked first
            for rule in p["data"].get("auto_switch", []):
                what = " + ".join(f"{field} {rule[field]}" for field in ("class", "title") if rule.get(field)) or "any window"
                a.add(Adw.ActionRow(title=f"{what} → {name}", use_markup=False,
                                    subtitle=f"From profiles/{name}.toml; checked before the rules below"))
        for i, rule in enumerate(auto.get("rules", [])):
            row = Adw.EntryRow(title=f"Window class → {rule.get('profile')}", text=rule.get("class", ""))
            prof = Gtk.DropDown(model=Gtk.StringList.new(names), valign=Gtk.Align.CENTER,
                                selected=names.index(rule["profile"]) if rule.get("profile") in names else 0)
            rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
            row.add_suffix(prof)
            row.add_suffix(rm)
            row.connect("changed", lambda r, i=i: self.debounce(f"rule{i}", lambda: self.edit_rule(i, "class", r.get_text())))
            prof.connect("notify::selected", lambda d, _p, i=i: self.edit_rule(i, "profile", names[d.get_selected()]))
            rm.connect("clicked", lambda _b, i=i: self.remove_rule(i))
            a.add(row)

        w = Adw.PreferencesGroup(title="Wallpaper theme")
        self.add_group(page, w)
        wt = cfg.get("wallpaper_theme", {})
        cmd = Adw.EntryRow(title="Wallpaper command (blank: detect noctalia, swww, hyprpaper, GNOME)", text=wt.get("command", ""))
        cmd.connect("changed", lambda r: self.debounce("wpcmd", lambda: self.model.set(
            "wallpaper_theme", "command", r.get_text(), remove=not r.get_text().strip())))
        w.add(cmd)
        for field, label in (("font", "Font"), ("clock_font", "Clock font")):
            row = Adw.EntryRow(title=label, text=wt.get(field, gd.THEME_DEFAULTS[field]))
            row.connect("changed", lambda r, f=field: self.debounce("wp" + f, lambda: self.model.set("wallpaper_theme", f, r.get_text())))
            w.add(row)

    def add_rule(self):
        rules = self.model.config.get("auto_switch", {}).get("rules", [])
        rules.append({"class": "^steam_app_", "profile": next(iter(self.model.profiles))})
        self.model.set("auto_switch", "rules", rules)
        self.build_settings_page()

    def edit_rule(self, i, field, value):
        rules = self.model.config.get("auto_switch", {}).get("rules", [])
        if i < len(rules):
            rules[i][field] = value
            self.model.set("auto_switch", "rules", rules)

    def remove_rule(self, i):
        rules = self.model.config.get("auto_switch", {}).get("rules", [])
        if i < len(rules):
            rules.pop(i)
            self.model.set("auto_switch", "rules", rules)
            self.build_settings_page()

    # ------------------------------------------------------ pages, profiles
    def add_page(self):
        def done(name):
            if name in [p["name"] for p in self.data["page"]] or name == "settings":
                return self.toast(f"There is already a page called “{name}”")
            self.data["page"].append({"name": name, "keys": [{} for _ in range(gd.KEYS)]})
            self.page = len(self.data["page"]) - 1
            self.after_structure_change()
        self.ask_text("New page", "Name for the page:", "", done)

    def rename_page(self):
        page = self.pages()[self.page]
        if page.get("settings"):
            return self.toast("The settings page is generated and can't be renamed")

        def done(name):
            old = page["name"]
            for p in self.data["page"]:
                for k in p["keys"]:
                    if k.get("page") == old:
                        k["page"] = name
            if self.data.get("start_page") == old:
                self.data["start_page"] = name
            page["name"] = name
            self.after_structure_change()
        self.ask_text("Rename page", "New name:", page["name"], done)

    def delete_page(self):
        page = self.pages()[self.page]
        if page.get("settings"):
            return self.toast("Turn the settings page off under Look instead")
        if len(self.data["page"]) == 1:
            return self.toast("A profile needs at least one page")

        def done():
            self.data["page"].remove(page)
            if self.data.get("start_page") == page["name"]:
                self.data.pop("start_page")
            self.page = max(0, self.page - 1)
            self.after_structure_change()
        self.confirm(f"Delete page “{page['name']}”?", "Its keys are removed too.", "Delete", done)

    def move_page(self, direction):
        pages = self.data["page"]
        j = self.page + direction
        if self.page < len(pages) and 0 <= j < len(pages):
            pages[self.page], pages[j] = pages[j], pages[self.page]
            self.page = j
            self.after_structure_change()

    def make_start_page(self):
        page = self.pages()[self.page]
        if not page.get("settings"):
            self.data["start_page"] = page["name"]
            self.changed_profile()
            self.toast(f"“{page['name']}” is now the start page")

    def after_structure_change(self):
        self.changed_profile()
        self.refresh_page_buttons()
        self.build_key_page()
        self.build_look_page()

    def new_profile(self, source=None):
        def done(name):
            name = name.lower().replace(" ", "-")
            if name in self.model.profiles:
                return self.toast(f"There is already a profile called “{name}”")
            path = os.path.join(gd.PROFILE_DIR, name + ".toml")
            if source:
                shutil.copy(self.model.profiles[source]["path"], path)
            else:
                gd.write_profile(path, {"theme": gd.DEFAULT_THEME, "page": [{"name": "main", "keys": []}]})
            self.model.load()
            self.profile, self.page, self.key = name, 0, 0
            self.refresh_all()
        self.ask_text("Duplicate profile" if source else "New profile", "Name for the profile:",
                      f"{source}-copy" if source else "", done)

    def duplicate_profile(self):
        self.new_profile(source=self.profile)

    def rename_profile(self):
        old = self.profile

        def done(name):
            name = name.lower().replace(" ", "-")
            if name in self.model.profiles:
                return self.toast(f"There is already a profile called “{name}”")
            self.flush()
            os.rename(self.model.profiles[old]["path"], os.path.join(gd.PROFILE_DIR, name + ".toml"))
            self.replace_profile_refs(old, name)
            self.model.load()
            self.profile = name
            self.refresh_all()
        self.ask_text("Rename profile", "New name:", old, done)

    def delete_profile(self):
        if len(self.model.profiles) == 1:
            return self.toast("You need at least one profile")
        name = self.profile

        def done():
            self.flush()
            os.remove(self.model.profiles[name]["path"])
            self.replace_profile_refs(name, None)
            self.model.load()
            self.profile, self.page, self.key = next(iter(self.model.profiles)), 0, 0
            self.refresh_all()
        self.confirm(f"Delete profile “{name}”?", "Its file is deleted; this can't be undone.", "Delete", done)

    def replace_profile_refs(self, old, new):
        """Point start_profile, auto-switch rules and profile keys at `new` (None: drop them)."""
        cfg = self.model.config
        if cfg.get("start_profile") == old:
            self.model.set(None, "start_profile", new, remove=new is None)
        auto = cfg.get("auto_switch", {})
        if auto.get("fallback") == old:
            self.model.set("auto_switch", "fallback", new, remove=new is None)
        if any(r.get("profile") == old for r in auto.get("rules", [])):
            rules = [dict(r, profile=new) if r.get("profile") == old else r for r in auto["rules"]
                     if new or r.get("profile") != old]
            self.model.set("auto_switch", "rules", rules)
        for pname, p in self.model.profiles.items():
            if pname == old:
                continue
            touched = False
            for page in p["data"]["page"]:
                for k in page["keys"]:
                    if k.get("profile") == old:
                        if new:
                            k["profile"] = new
                        else:
                            k.pop("profile")
                        touched = True
            if touched:
                self.model.save_profile(pname)

    def make_start_profile(self):
        self.model.set(None, "start_profile", self.profile)
        self.toast(f"The deck starts with “{self.profile}”")

    def on_profile_selected(self, drop, _p):
        names = list(self.model.profiles)
        i = drop.get_selected()
        if 0 <= i < len(names) and names[i] != self.profile:
            self.flush()
            self.profile, self.page, self.key = names[i], 0, 0
            self.refresh_all(keep_dropdown=True)

    # -------------------------------------------------------------- general
    def flush(self):
        """Run any pending debounced saves now."""
        for name, source in list(self.pending.items()):
            GLib.source_remove(source)
            self.pending.pop(name)
            if name.startswith("profile:") and name[8:] in self.model.profiles:
                self.model.save_profile(name[8:])

    def refresh_all(self, keep_dropdown=False):
        if not keep_dropdown:
            names = list(self.model.profiles)
            self.profile_list.splice(0, self.profile_list.get_n_items(), names)
            self.profile_drop.set_selected(names.index(self.profile))
        self.refresh_page_buttons()
        self.refresh_deck()
        self.build_key_page()
        self.build_look_page()
        self.build_settings_page()
        self.update_status()

    def show_error(self, text):
        self.banner.set_title(text)
        self.banner.set_revealed(True)

    def watch(self):
        """Pick up changes made elsewhere (the deck's settings page, a text editor)."""
        self.update_status()
        if not self.pending and gd.config_mtime() != self.model.mtime:
            try:
                self.model.load()
            except (OSError, tomllib.TOMLDecodeError) as e:
                self.show_error(f"Config error: {e}")
                return True
            self.banner.set_revealed(False)
            if self.profile not in self.model.profiles:
                self.profile = next(iter(self.model.profiles))
            self.refresh_all()
        return True

    def update_status(self):
        active = service_running()
        self.status.set_label("● deck running" if active else "○ service stopped")
        self.status.set_tooltip_text("The galleon-deck service is " + ("running" if active else "not running"))

    def replay_boot(self):
        self.flush()
        ok, err = restart_service()
        self.toast("Restarting the service — watch the deck" if ok else f"Restart failed: {err}")

    def restart_service(self):
        self.flush()
        ok, err = restart_service()
        self.toast("Service restarted" if ok else f"Restart failed: {err}")

    def open_folder(self):
        Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(gd.CONFIG_DIR), None)

    def about(self):
        about = Adw.AboutDialog(application_name="Galleon Deck", application_icon="input-keyboard",
                                developer_name="galleon-deck contributors", version="0.1.0",
                                comments="Stream Deck support for the Corsair Galleon 100 SD keyboard on Linux.",
                                license_type=Gtk.License.MIT_X11)
        about.present(self)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()

    def do_shutdown(self):
        win = self.props.active_window
        if win:
            win.flush()
        Adw.Application.do_shutdown(self)


def main():
    if not os.path.exists(gd.CONFIG_PATH):
        print(f"No config at {gd.CONFIG_PATH}; run the installer first.", file=sys.stderr)
        sys.exit(1)
    sys.exit(App().run(sys.argv))


if __name__ == "__main__":
    main()
