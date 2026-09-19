#!/usr/bin/env bash
# Remove galleon-deck. Your config and add-ons are kept unless you pass --purge.
set -euo pipefail
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
CONF=${XDG_CONFIG_HOME:-$HOME/.config}
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

if systemctl --user show-environment >/dev/null 2>&1; then
    systemctl --user disable --now galleon-deck.service 2>/dev/null || true
    rm -f "$CONF/systemd/user/galleon-deck.service"
    systemctl --user daemon-reload
fi
pid="${XDG_RUNTIME_DIR:-/tmp}/galleon-deck.pid"
[ -f "$pid" ] && kill "$(cat "$pid")" 2>/dev/null || true

rm -f "$HOME/.local/bin/galleon-deck" "$HOME/.local/bin/galleon-deck-config" "$HOME/.local/bin/galleon-addon"
rm -rf "$DATA/galleon-deck"
rm -f "$DATA/applications/io.github.galleondeck.Config.desktop" "$CONF/autostart/galleon-deck-autostart.desktop"
[ "$PURGE" = 1 ] && rm -rf "$CONF/galleon-deck" "$DATA/galleon-deck-addons" "${XDG_STATE_HOME:-$HOME/.local/state}/galleon-deck"

echo "Removing the udev rule and uinput autoload needs root:"
sudo rm -f /etc/udev/rules.d/71-galleon-deck.rules /etc/modules-load.d/galleon-deck.conf
sudo udevadm control --reload
echo "Done.$([ "$PURGE" = 0 ] && echo " Your config is still in $CONF/galleon-deck.")"
