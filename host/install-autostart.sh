#!/bin/sh
# Start eve automatically when you log in to your desktop.
# Usage: host/install-autostart.sh [extra voicetype.py flags, e.g. --model small.en]
#
# Writes a launcher (~/.config/eve/start.sh) and an XDG autostart entry for it.
# GNOME, KDE and XFCE run that entry; compositors such as Hyprland and Sway
# only do under a session manager like uwsm, so there it also says how to
# start the launcher from the compositor's config.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
LAUNCHER="$HOME/.config/eve/start.sh"
mkdir -p "$HOME/.config/eve" "$HOME/.config/autostart" "$HOME/.cache"

# Quote each flag for the launcher, so ones with spaces (--prompt "...") survive.
flags=""
for a in "$@"; do
    flags="$flags '$(printf '%s' "$a" | sed "s/'/'\\\\''/g")'"
done

cat > "$LAUNCHER" <<LAUNCH
#!/bin/sh
# Starts eve. Written by $HERE/install-autostart.sh; run that again to change it.
exec "$HERE/.venv/bin/python" "$HERE/voicetype.py"$flags >> "\$HOME/.cache/voicetype.log" 2>&1
LAUNCH
chmod +x "$LAUNCHER"

cat > "$HOME/.config/autostart/eve.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=eve
Comment=Push-to-talk dictation from the eve voice stick
Exec=$LAUNCHER
X-GNOME-Autostart-enabled=true
NoDisplay=true
DESKTOP

echo "Installed $LAUNCHER and ~/.config/autostart/eve.desktop."
echo "Log: ~/.cache/voicetype.log"

if ! systemctl --user is-active -q xdg-desktop-autostart.target 2>/dev/null; then
    hint="$(python3 "$HERE/desktop.py" --autostart "$LAUNCHER" 2>/dev/null || true)"
    if [ -n "$hint" ]; then
        echo
        echo "Your desktop may not run autostart entries (no uwsm or similar session manager"
        echo "seems to be running). To start eve at login, $hint"
        exit 0
    fi
fi
echo "eve will start at your next login."
