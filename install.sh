#!/usr/bin/env bash
# Install galleon-deck for the current user.
#
#   ./install.sh            install (copies files; asks for sudo once, for the udev rule)
#   ./install.sh --dev      run from this checkout instead of copying (for development)
#   ./install.sh --no-deps  don't install distro packages
#   ./install.sh --no-root  don't run anything as root; print those commands instead
#
# Works on Arch/CachyOS/Manjaro, Debian/Ubuntu/Mint/Pop, Fedora, and openSUSE;
# on others, install the packages listed under "other" below yourself.
set -euo pipefail

REPO=$(cd "$(dirname "$0")" && pwd)
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
CONF=${XDG_CONFIG_HOME:-$HOME/.config}
BIN=$HOME/.local/bin
DEV=0 DEPS=1 ROOT=1
for arg in "$@"; do
    case $arg in
        --dev) DEV=1 ;;
        --no-deps) DEPS=0 ;;
        --no-root) ROOT=0 ;;
        -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

say() { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; }
as_root() {
    if [ "$ROOT" = 1 ]; then sudo "$@"; else echo "    sudo $*"; fi
}

# ------------------------------------------------------------ dependencies
if [ "$DEPS" = 1 ]; then
    if command -v pacman >/dev/null; then
        pkgs=(python python-pillow python-evdev python-gobject gtk4 libadwaita python-fonttools
              ttf-nerd-fonts-symbols ttf-jetbrains-mono playerctl libnotify)
        say "Installing packages with pacman: ${pkgs[*]}"
        as_root pacman -S --needed "${pkgs[@]}"
    elif command -v apt-get >/dev/null; then
        pkgs=(python3 python3-pil python3-evdev python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-fonttools
              fonts-jetbrains-mono playerctl libnotify-bin curl unzip)
        python3 -c 'import tomllib' 2>/dev/null || pkgs+=(python3-tomli)
        say "Installing packages with apt: ${pkgs[*]}"
        as_root apt-get install -y "${pkgs[@]}"
    elif command -v dnf >/dev/null; then
        pkgs=(python3 python3-pillow python3-evdev python3-gobject gtk4 libadwaita python3-fonttools
              jetbrains-mono-fonts playerctl libnotify curl unzip)
        say "Installing packages with dnf: ${pkgs[*]}"
        as_root dnf install -y "${pkgs[@]}"
    elif command -v zypper >/dev/null; then
        pkgs=(python3 python3-Pillow python3-evdev python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-4_0
              typelib-1_0-Adw-1 python3-fonttools jetbrains-mono-fonts playerctl libnotify-tools curl unzip)
        say "Installing packages with zypper: ${pkgs[*]}"
        as_root zypper install -y "${pkgs[@]}"
    else
        warn "Unknown package manager. Install these yourself (names vary by distro):"
        warn "  Python 3.10+ (plus tomli on 3.10), Pillow, python-evdev, PyGObject, GTK 4, libadwaita 1.5+,"
        warn "  fontTools (optional, for icon search), playerctl and notify-send (optional)"
    fi
fi

# The icon font: most distros don't package the Nerd Font symbols.
if ! fc-list 2>/dev/null | grep -q "Symbols Nerd Font"; then
    say "Installing Symbols Nerd Font (key icons) to $DATA/fonts"
    tmp=$(mktemp -d)
    curl -fsSL -o "$tmp/s.zip" https://github.com/ryanoasis/nerd-fonts/releases/latest/download/NerdFontsSymbolsOnly.zip \
        && mkdir -p "$DATA/fonts" && unzip -o -q "$tmp/s.zip" '*.ttf' -d "$DATA/fonts/SymbolsNerdFont" \
        && fc-cache -f "$DATA/fonts" >/dev/null || warn "Couldn't download the icon font; key icons will be blank"
    rm -rf "$tmp"
fi
# The default theme's font (optional: anything else falls back cleanly).
if ! fc-list 2>/dev/null | grep -qi "Quantico"; then
    say "Installing the Quantico font (default theme) to $DATA/fonts"
    mkdir -p "$DATA/fonts/Quantico"
    for style in Regular Bold; do
        curl -fsSL -o "$DATA/fonts/Quantico/Quantico-$style.ttf" \
            "https://github.com/google/fonts/raw/main/ofl/quantico/Quantico-$style.ttf" || true
    done
    fc-cache -f "$DATA/fonts" >/dev/null || true
fi

python3 - <<'EOF' || { warn "Missing Python modules (see above); install them and re-run."; exit 1; }
import importlib, sys
missing = []
for mod in ("PIL", "evdev"):
    try:
        importlib.import_module(mod)
    except ImportError:
        missing.append(mod)
try:
    import tomllib  # noqa: F401
except ImportError:
    try:
        import tomli  # noqa: F401
    except ImportError:
        missing.append("tomli (Python 3.10)")
if missing:
    print("missing:", ", ".join(missing), file=sys.stderr)
    sys.exit(1)
try:
    import gi
    gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
    from gi.repository import Adw
    if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 5):
        print("note: libadwaita 1.5+ is needed for the configurator app; the service works without it")
except (ImportError, ValueError):
    print("note: GTK 4 / libadwaita not found; the configurator app won't start, the service works without it")
EOF

# ------------------------------------------------------------------ files
say "Installing galleon-deck$([ "$DEV" = 1 ] && echo " (dev: running from $REPO)")"
mkdir -p "$BIN" "$DATA/applications"
if [ "$DEV" = 1 ]; then
    ln -sfn "$REPO/bin/galleon-deck" "$BIN/galleon-deck"
    ln -sfn "$REPO/bin/galleon-deck-config" "$BIN/galleon-deck-config"
else
    mkdir -p "$DATA/galleon-deck"
    install -m644 "$REPO"/src/*.py "$DATA/galleon-deck/"
    rm -f "$BIN/galleon-deck" "$BIN/galleon-deck-config"
    install -m755 "$REPO/bin/galleon-deck" "$REPO/bin/galleon-deck-config" "$BIN/"
fi
install -m644 "$REPO/data/applications/io.github.galleondeck.Config.desktop" "$DATA/applications/"
command -v update-desktop-database >/dev/null && update-desktop-database "$DATA/applications" 2>/dev/null || true

if [ ! -e "$CONF/galleon-deck/config.toml" ]; then
    say "Creating a starter config in $CONF/galleon-deck"
    mkdir -p "$CONF/galleon-deck"
    cp -r "$REPO/examples/." "$CONF/galleon-deck/"
else
    say "Keeping your existing config in $CONF/galleon-deck"
fi

# ------------------------------------------------------------------- root
say "Device access (udev rule + uinput module) needs root once:"
as_root install -m644 "$REPO/data/udev/71-galleon-deck.rules" /etc/udev/rules.d/71-galleon-deck.rules
as_root rm -f /etc/udev/rules.d/71-galleon-deck-hidraw.rules   # older name of the same rule
as_root sh -c 'echo uinput > /etc/modules-load.d/galleon-deck.conf; modprobe uinput'
as_root udevadm control --reload
as_root udevadm trigger --subsystem-match=hidraw --subsystem-match=misc

# ---------------------------------------------------------------- service
if systemctl --user show-environment >/dev/null 2>&1; then
    say "Starting the systemd user service"
    mkdir -p "$CONF/systemd/user"
    install -m644 "$REPO/data/systemd/galleon-deck.service" "$CONF/systemd/user/"
    rm -f "$CONF/autostart/galleon-deck-autostart.desktop"
    systemctl --user daemon-reload
    systemctl --user enable galleon-deck.service
    systemctl --user restart galleon-deck.service
else
    say "No systemd user session: starting through XDG autostart"
    mkdir -p "$CONF/autostart"
    install -m644 "$REPO/data/applications/galleon-deck-autostart.desktop" "$CONF/autostart/"
    [ -f "${XDG_RUNTIME_DIR:-/tmp}/galleon-deck.pid" ] && kill "$(cat "${XDG_RUNTIME_DIR:-/tmp}/galleon-deck.pid")" 2>/dev/null || true
    nohup "$BIN/galleon-deck" >/dev/null 2>&1 &
fi

case ":$PATH:" in *":$BIN:"*) ;; *) warn "$BIN isn't on your PATH; add it so 'galleon-deck-config' runs from a terminal." ;; esac
say "Done. Open \"Galleon Deck\" from your app menu (or run galleon-deck-config) to customize the deck."
