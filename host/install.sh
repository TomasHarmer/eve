#!/usr/bin/env bash
# Set up eve's desktop app: the typing tools your desktop needs (see
# host/desktop.py), access to the stick's serial port, and a Python venv.
#
#   host/install.sh                          # install
#   host/install.sh --dry-run                # only show what it would do
#   host/install.sh --autostart [flags...]   # also start eve at login, with these
#                                            # voicetype.py flags (e.g. --model small.en)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DRY=0
AUTOSTART=0
AUTOSTART_FLAGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1 ;;
        --autostart) AUTOSTART=1; shift; AUTOSTART_FLAGS=("$@"); break ;;
        -h|--help) sed -n '2,8s/^# \{0,1\}//p' "$0"; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
run()  { echo "+ $*"; [ "$DRY" = 1 ] || "$@"; }

# ------------------------------------------------------------ package manager
PM=""
for c in apt-get pacman dnf zypper; do
    if command -v "$c" >/dev/null; then PM="${c%-get}"; break; fi
done

install_packages() {
    [ $# -gt 0 ] || return 0
    case "$PM" in
        apt)    run sudo apt-get install -y "$@" ;;
        pacman) run sudo pacman -S --needed --noconfirm "$@" ;;
        dnf)    run sudo dnf install -y "$@" ;;
        zypper) run sudo zypper install -y "$@" ;;
        *)      warn "No supported package manager found (apt, pacman, dnf, zypper)."
                warn "Install these yourself, then run this again: $*"
                exit 1 ;;
    esac
}

if ! command -v python3 >/dev/null; then
    say "Installing Python"
    case "$PM" in pacman) install_packages python ;; *) install_packages python3 ;; esac
fi

# -------------------------------------------------------------- typing tools
say "Desktop"
UNSUPPORTED=0
python3 "$HERE/desktop.py" || true
if packages="$(python3 "$HERE/desktop.py" --packages "${PM:-unknown}")"; then
    if [ -n "$packages" ]; then
        say "Installing typing tools: $packages"
        # shellcheck disable=SC2086  # one word per package
        install_packages $packages
    fi
else
    UNSUPPORTED=1
    warn "eve can't type on this desktop; setting up the rest anyway (--dry-run still works)."
fi

# --------------------------------------------------------------- serial port
# /dev/ttyACM* belongs to a group (dialout on Debian/Ubuntu/Fedora, uucp on
# Arch); you need to be in it to talk to the stick.
port_group=""
for dev in /dev/ttyACM*; do
    [ -e "$dev" ] && { port_group="$(stat -c %G "$dev")"; break; }
done
if [ -z "$port_group" ]; then
    for g in dialout uucp; do
        getent group "$g" >/dev/null && { port_group="$g"; break; }
    done
fi
RELOGIN=0
if [ -n "$port_group" ] && [ "$port_group" != root ]; then
    if ! id -nG "$USER" | tr ' ' '\n' | grep -qx "$port_group"; then
        say "Giving $USER access to the serial port (group $port_group)"
        run sudo usermod -aG "$port_group" "$USER"
        RELOGIN=1
    elif ! id -nG | tr ' ' '\n' | grep -qx "$port_group"; then
        RELOGIN=1   # added earlier, but this session predates it
    fi
fi

# ---------------------------------------------------------------- Python venv
say "Python environment (host/.venv)"
if [ ! -x "$HERE/.venv/bin/python" ]; then
    if [ "$PM" = apt ] && ! python3 -c 'import ensurepip' 2>/dev/null; then
        install_packages python3-venv   # Debian/Ubuntu ship venv separately
    fi
    run python3 -m venv "$HERE/.venv"
fi
run "$HERE/.venv/bin/pip" install --quiet --upgrade pip
run "$HERE/.venv/bin/pip" install --quiet -r "$HERE/requirements.txt"

# ------------------------------------------------------------------ autostart
if [ "$AUTOSTART" = 1 ]; then
    say "Autostart"
    run "$HERE/install-autostart.sh" "${AUTOSTART_FLAGS[@]}"
fi

# ---------------------------------------------------------------------- done
say "Done"
[ "$DRY" = 1 ] && echo "(dry run: nothing was changed)"
[ "$UNSUPPORTED" = 1 ] && warn "Typing won't work on this desktop: see the Desktop line above."
[ "$RELOGIN" = 1 ] && warn "Log out and back in so the serial-port group takes effect."
echo "Run it:  $HERE/.venv/bin/python $HERE/voicetype.py --model small.en"
[ "$AUTOSTART" = 1 ] || echo "Start at login:  $HERE/install-autostart.sh --model small.en"
