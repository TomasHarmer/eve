#!/bin/sh
# Start eve automatically when you log in to your desktop (X11).
# Usage: host/install-autostart.sh [extra voicetype.py flags, e.g. --model small.en]
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.config/autostart" "$HOME/.cache"
cat > "$HOME/.config/autostart/eve.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=eve
Comment=Push-to-talk dictation from the eve voice stick
Exec=sh -c '"$HERE/.venv/bin/python" "$HERE/voicetype.py" $* >> "\$HOME/.cache/voicetype.log" 2>&1'
X-GNOME-Autostart-enabled=true
NoDisplay=true
DESKTOP
echo "Installed ~/.config/autostart/eve.desktop — eve will start at your next login."
echo "Log: ~/.cache/voicetype.log"
