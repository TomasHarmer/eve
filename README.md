<p align="center">
  <img src="assets/logo.svg" alt="eve" width="640">
</p>

<p align="center">
  <b>Finally, a good reason to talk to yourself.</b><br>
  A pocket push-to-talk stick that types what you say wherever your cursor is.
</p>

<p align="center">
  <a href="https://atech.dev"><img alt="Made with Atech" src="https://img.shields.io/badge/made%20with-Atech-7cc8ff?style=flat-square"></a>
  <img alt="Local first" src="https://img.shields.io/badge/speech--to--text-local%20first-bfe6ff?style=flat-square">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-e8f6ff?style=flat-square">
</p>

---

**eve** is a tiny USB-C voice stick. You hold its button and talk, and your
words appear in the window you were in: a terminal, an editor, a chat box. A
**tap** sends it with Enter, and a **double-tap** deletes it.

It was built for driving AI coding sessions (Claude Code and friends) while
walking around with the laptop open, so you don't have to stop and type. It
works in any app.

- **Live:** each phrase is typed as soon as you pause. You don't have to let go first.
- **Private by default:** speech recognition runs on your own computer with
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper), and works offline.
- **Fast when you're busy:** an optional cloud fallback (Groq or OpenAI) takes
  over only when your computer is too loaded to keep up.
- **Types in the right place:** text goes to the window you started in, even if
  you click somewhere else while it's still transcribing.
- **Hands-free send and undo:** tap to press Enter (or Tab to accept a suggestion), double-tap to delete.
- **Plug and go:** no drivers. It reconnects by itself, starts at login, and
  leaves your other dev boards alone.

## How it works

```
 ┌──────────── eve stick ────────────┐          ┌──────────── your computer ─────────────┐
 │  mic  ──▶  ESP32-S3  ◀──  button  │  USB-C   │  voicetype.py                          │
 │            streams 16 kHz audio   │ ───────▶ │   ├─ splits speech at pauses (VAD)     │
 │            while the button is    │          │   ├─ transcribes each phrase (Whisper) │
 │            held; tap = ⏎ / ⇥      │ ◀─────── │   └─ types it into your window         │
 └───────────────────────────────────┘  light   └────────────────────────────────────────┘
```

The stick only captures and streams audio. Transcription runs on your
computer, because a small microcontroller can't run a good speech model.

## Hardware

eve is built from [**Atech**](https://atech.dev) plug-together modules, with
no soldering:

| Part | Where it goes |
|---|---|
| Atech **Tiny** board (ESP32-S3, 3 ports) | — |
| Atech **Microphone** module (ICS-43434, I²S) | ports 1 + 2 |
| Atech **Button** module (V2.0 for the RGB status light) | port 3 |
| Atech **USB-C** module | port 4 |
| A USB-C cable that carries **data** | — |

> On our units the microphone answered on the opposite port lines from the board
> docs, and the firmware's pins match that. If you get silence, run the wiring
> diagnostic (see Troubleshooting).

## Setup

eve runs on Linux. How it types depends on your desktop, and it detects that
when it starts:

| Desktop | Types with | Switches back to the window you started in |
|---|---|---|
| X11 (any window manager) | `xdotool` | yes, with `xdotool` |
| Hyprland | `wtype` | yes, with `hyprctl` |
| Sway | `wtype` | yes, with `swaymsg` (untested) |
| Other wlroots Wayland compositors | `wtype` | no: types wherever the focus is |
| GNOME or KDE on Wayland | not supported yet: log in to an X11 session for now | |

### Set up with a coding agent

You can follow the steps below yourself, or paste this into a coding agent
(Claude Code, Cursor, Codex, ...) running in a terminal on the computer you'll
use eve on. It gives the agent the context it needs and has it check with you
before anything that needs sudo or changes your system.

```text
Help me set up eve, a push-to-talk dictation stick, on this Linux computer.
The code is at https://github.com/TomasHarmer/eve: clone it if it isn't here
yet, and read README.md before you start.

How eve works:
- The stick (an Atech Tiny ESP32-S3 board with microphone, button and USB-C
  modules) only records. While its button is held, it streams 16 kHz audio over
  USB serial (/dev/ttyACM*). The wire protocol is at the top of
  firmware/src/main.cpp.
- host/voicetype.py is the desktop app. It finds the stick (it asks each ESP32
  board whether it's eve and saves eve's USB serial number in
  ~/.config/eve/device), splits speech into phrases at pauses, transcribes them
  locally with faster-whisper, and types each one into the window that had
  focus when the button was pressed. A tap presses Enter (or Tab), a
  double-tap deletes what eve typed.
- host/desktop.py decides how to type on this desktop: xdotool on X11,
  wtype + hyprctl on Hyprland, wtype + swaymsg on Sway, wtype alone on other
  wlroots compositors. GNOME and KDE on Wayland are not supported yet.
- host/install.sh installs the typing tools for the detected desktop, adds the
  user to the serial-port group (dialout or uucp), and creates host/.venv.
- Optional cloud fallback: ~/.config/eve/cloud.env (provider=groq or openai,
  key=...). Only phrases that local transcription is slow on are sent.

Steps:
1. Run `python3 host/desktop.py` and tell me which desktop it found. If it
   says "not supported", stop and explain my options (for now, an X11 session).
2. Run `host/install.sh --dry-run`, show me what it would do, then run
   `host/install.sh` once I agree. It uses sudo; I'll type the password. If it
   added me to a group, tell me to log out and back in before testing.
3. Ask me whether the stick is already flashed. If not: flashing needs
   PlatformIO (the Atech drivers eve uses are in firmware/lib/atech). Help me
   install it if `pio` isn't found, then run `cd firmware && pio run -t upload`. The button light blinks green once when
   it's done; if not, I replug the stick. If the upload can't connect, I hold
   the board's BOOT button while plugging it in and you try again.
4. Test with `host/.venv/bin/python host/voicetype.py --dry-run`, which prints
   the text instead of typing it. It runs until stopped, so start it in the
   background and read its output. The first run downloads the speech model
   (base.en is about 150 MB, small.en about 480 MB). Ask me to hold the button
   and say something, and check what it heard. Then run it without --dry-run
   and have me dictate into a text editor.
5. Ask me whether eve should start at login. If so, run
   `host/install-autostart.sh --model small.en`. If it prints a line to add to
   my compositor's config, show it to me and ask before adding it.
6. Ask me whether I want the cloud fallback. If so, tell me how to create
   ~/.config/eve/cloud.env myself (chmod 600). Don't ask me to paste the API
   key into this chat.

Rules:
- Ask before using sudo, before editing config files outside this repo, and
  before flashing.
- Only one program can use the stick's serial port at a time. Stop eve
  (`pkill -f host/voicetype.py`) before flashing or starting another copy.
- eve has to run inside my desktop session. If it can't find my desktop (for
  example, because you're connected over SSH), ask me to start it from a
  terminal on the desktop.
- When something fails, check the Troubleshooting section of README.md and
  the log first. The first log lines name the detected desktop; when eve
  runs from autostart, the log is ~/.cache/voicetype.log. Don't change the
  code to work around a setup problem without asking me.
```

### 1. Install the desktop app

```bash
git clone https://github.com/TomasHarmer/eve && cd eve
host/install.sh
```

The script detects your desktop and installs the typing tools it needs
(with apt, pacman, dnf or zypper). It also adds you to the serial-port group
if needed (`dialout` or `uucp`; log out and back in afterwards), and sets up
the Python environment in `host/.venv`. Run `host/install.sh --dry-run` first
to see what it would do, or `python3 host/desktop.py` to see only what it
detected.

### 2. Flash the stick

You need [PlatformIO](https://platformio.org). The Atech drivers eve uses are
included in `firmware/lib/atech`.

```bash
cd firmware
pio run -t upload
```

When it's done the button light should blink green once. If it doesn't, unplug
the stick and plug it back in: the board sometimes waits in its bootloader
after a flash. If the upload can't connect at all, hold the board's **BOOT**
button while you plug it in, then upload again.

### 3. Run it

```bash
host/.venv/bin/python host/voicetype.py --model small.en
```

The first run downloads the speech model once (`small.en` is about 480 MB;
the default `base.en` is about 150 MB). After that it works offline. Hold the
button and talk.

To start it automatically every time you log in:

```bash
host/install-autostart.sh --model small.en
```

(or `host/install.sh --autostart --model small.en` in one go). This writes a
launcher, `~/.config/eve/start.sh`, and an XDG autostart entry that runs it.
GNOME, KDE and XFCE run that entry. Hyprland and Sway only do when started
through a session manager such as `uwsm`. When none is running, the script
prints the line to add to your compositor's config instead.

It logs to `~/.cache/voicetype.log`, which shows each phrase, how long it took
and whether it was transcribed locally or in the cloud. To stop or restart it:

```bash
pkill -f host/voicetype.py        # stop
tail -f ~/.cache/voicetype.log    # watch it
```

### 4. Optional: cloud fallback

On a busy laptop (a browser, CAD and a build all at once), local transcription
can slow down to several seconds per phrase. eve can hand those phrases to a
cloud speech service instead:

- Every phrase is still transcribed locally first.
- If the local result isn't ready within **1 second**, or the local model is
  still busy with an earlier phrase, that phrase is also sent to the cloud, and
  whichever answer arrives first is typed.
- If the cloud is unreachable, eve carries on locally.

[Groq](https://console.groq.com) is the fastest choice (about half a second per
phrase, with a free tier). Create an API key there, then:

```bash
mkdir -p ~/.config/eve
install -m 600 /dev/null ~/.config/eve/cloud.env
nano ~/.config/eve/cloud.env
```

```
provider=groq        # or openai
key=YOUR_API_KEY
```

Restart eve. The log should say `cloud fallback on (groq)`, and from then on it
marks each phrase `local` or `groq`. Only phrases that fall back are sent, and
without this file no audio leaves your computer.

## Using it

| You do | eve does |
|---|---|
| **Hold** the button and talk | types each phrase as you pause |
| **Let go** | finishes and types the last phrase |
| **Tap** | presses **Enter** to send what eve typed. With nothing waiting, presses **Tab** to accept Claude Code's suggestion, and the next tap sends it |
| **Double-tap** | deletes the text eve typed that hasn't been sent yet. After a Tab, clears the whole prompt, suggestion included |

**Text goes where you started.** eve remembers the window that had focus when
you pressed the button. If you click somewhere else while it's still
transcribing, it briefly switches back to that window to type (and to press
Enter or delete for a tap), then returns you to where you were. So you can talk,
tap to send, and move on straight away, even in the middle of a long phrase.
This works per window: tabs inside one terminal or browser window look like a
single window, so text goes to whichever tab is showing.

A tap waits about a third of a second to make sure it isn't a double-tap.
eve only tracks text it typed itself: if you type or edit by hand in between,
a tap still sees "nothing waiting", and a double-tap deletes only as many
characters as eve typed. Clearing after a Tab uses Claude Code's Ctrl+U, so it
clears everything in the prompt.

The button light shows what's happening:

| Light | Meaning |
|---|---|
| green blink at power-up | firmware booted |
| **red** | listening |
| **amber** | finishing the last phrase |
| green flash | typed |
| blue flash | tap (Enter or Tab) |
| violet flash | double-tap (delete) |
| red flash | something went wrong (see the log) |

### Other boards on the same computer

eve's app never grabs a board that isn't eve. The first time, it asks each
connected ESP32 board whether it's eve and saves eve's USB serial number in
`~/.config/eve/device`. From then on it only ever opens that board, so other
boards stay free for atech.dev, PlatformIO or a serial monitor, even while
they reset during a flash. It also holds eve's port exclusively, so a browser
can't knock it offline by opening the wrong port.

In a browser's port picker eve shows up as "USB JTAG/serial debug unit". If
you're flashing another board, pick that board's entry instead.

To use **a second eve stick**, add its serial number as a new line in
`~/.config/eve/device` (`udevadm info -q property /dev/ttyACM0 | grep SERIAL_SHORT`
shows it), or run once with `--forget-device` while only the new stick is
plugged in.

## Options

| Flag | What it does |
|---|---|
| `--model small.en` | more accurate, especially on technical words. About 3× slower than the default `base.en`, which the cloud fallback makes up for |
| `--cloud-after 1` | seconds to wait for local transcription before asking the cloud. Default `1`. `0` sends every phrase to the cloud straight away |
| `--no-cloud` | never use the cloud, even if `cloud.env` is set up |
| `--pause 0.4` | how long a pause (seconds) ends a phrase. Default `0.4` |
| `--enter` | also press Enter every time you let go |
| `--no-live` | type everything at once when you let go |
| `--prompt "..."` | replace the vocabulary hint (words Whisper should expect) |
| `--forget-device` | forget which boards are eve and look again |
| `--port /dev/ttyACM0` | use this port instead of finding eve automatically |
| `--dry-run` | print the text instead of typing it |
| `--desktop hyprland` | type the way this desktop needs instead of detecting it (`hyprland`, `sway`, `wayland`, `x11`) |
| `--save-wav last.wav` | keep the last recording, for checking the mic |
| `-v` | also show the stick's log lines |

## Troubleshooting

**"waiting for the voice stick" forever.** The cable is probably charge-only,
or the firmware isn't flashed. `ls /dev/ttyACM*` should show a device. If it
does, check the log for a line saying the board "is not eve". If you reflashed
eve onto a different board, see *Other boards on the same computer*.

**`error -71` in `journalctl -k`.** The laptop sees the stick but can't talk to
it, which means a bad data contact. Try another cable, then reseat the USB-C
module.

**Upload fails with "port is busy".** eve's app is holding the stick. Stop it
(`pkill -f host/voicetype.py`), flash, then start it again.

**Text is empty.** Record with `--save-wav last.wav` and play it with `aplay`.
If it's silent, run the wiring diagnostic:

```bash
cd firmware && pio run -e diag -t upload && pio device monitor
```

It tries every way the mic could be connected and reports which one is live.
Put those pins into `MIC_*` in `firmware/src/main.cpp`.

**Slow.** Local transcription time depends on how busy your CPU is. Laptops
also throttle on a low battery or in power-saver mode. Turn on the cloud
fallback, or use the faster `base.en` model.

**Nothing gets typed.** The first lines of the log say which desktop eve
detected (`Hyprland: types with wtype, switches windows with hyprctl`). If it
names the wrong one, eve was started outside your desktop session, without its
environment (`WAYLAND_DISPLAY`, `HYPRLAND_INSTANCE_SIGNATURE`, ...). Start it
from your compositor's config, as above, or force one with `--desktop`.

**Wrong words.** Use `--model small.en` and the cloud fallback. If a word keeps
coming out wrong (a product or project name), add it to the vocabulary hint
with `--prompt`.

## Project layout

```
firmware/            PlatformIO project for the stick (ESP32-S3, Arduino)
  src/main.cpp       recording, button gestures, USB wire protocol
  diag/diag.cpp      microphone wiring diagnostic
  lib/atech/         the Atech microphone and button drivers
host/
  voicetype.py       desktop app: finding eve, VAD phrase splitting,
                     local + cloud Whisper, typing into the right window
  desktop.py         per-desktop typing and window switching
  install.sh         installs what your desktop needs, plus the venv
  install-autostart.sh
assets/logo.svg
```

The USB protocol is small and documented at the top of `firmware/src/main.cpp`.
It's a good starting point for a different front end, such as a macOS typer.

### Adding a desktop

Everything desktop-specific lives in `host/desktop.py`. A desktop is a
**keyboard**, which sends text and keys (`Xdotool`, `Wtype`), plus a
**window tracker**, which finds the focused window and focuses one by id
(`XdotoolWindows`, `HyprlandWindows`, `SwayWindows`). To support another one:

1. Write the parts it's missing. A `Windows` subclass implements `active()`,
   `title(w)` and `focus(w)`, and lists the commands it uses in `tools`.
   `focus` should return only once the focus has actually moved.
2. Add a `Desktop(...)` line to `DESKTOPS`, with a `matches` function that
   recognizes the session from its environment. The list is checked in order,
   so put it before any more general entry it overlaps with (for example,
   before plain `wayland`).
3. If a tool's package name differs from its command, give it in
   `Tool(..., packages={"apt": "..."})`. Tools that ship with the desktop
   itself are `bundled=True`.

`install.sh` and `voicetype.py --desktop` pick the new entry up from there.
Check it with `python3 host/desktop.py --desktop yourname`.

**GNOME and KDE on Wayland** are the obvious next ones. Neither lets ordinary
apps use the virtual-keyboard protocol wtype relies on, so they need a
different keyboard. One option is a uinput tool such as `dotool`, which works
on any desktop but needs access to `/dev/uinput` and must be told the
keyboard layout. The other is the RemoteDesktop portal, which needs a one-time
permission prompt. Switching windows would need KWin scripting on KDE and a
Shell extension on GNOME.

### Adding modules

eve uses two Atech modules, the microphone and the button, and
`firmware/lib/atech` has only their drivers. To add another module (a speaker,
a display, a sensor), get it from the [**Atech SDK**](https://atech.dev/docs#own-agent),
which has the drivers and API for every Atech module:

```bash
pip install atech
```

`atech guide` explains how to use the SDK, including with a coding agent.
Copy the module's driver files into `firmware/lib/atech`, and its `lib_deps`
from its `module.yaml` into `firmware/platformio.ini`. The SDK also installs
PlatformIO.

## Credits

Made with [**Atech**](https://atech.dev) hardware modules. Speech recognition by
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) and OpenAI's Whisper
models, with Silero VAD for phrase detection. The logo is set in
[Montserrat](https://github.com/JulietaUla/Montserrat) ExtraLight (SIL Open Font License).

## License

[MIT](LICENSE)
