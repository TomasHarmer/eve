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
- **USB or Bluetooth:** plug it in, or use it wirelessly over Bluetooth LE.
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
| For wireless use: a USB-C power bank or charger | — |

> On our units the microphone answered on the opposite port lines from the board
> docs, and the firmware's pins match that. If you get silence, run the wiring
> diagnostic (see Troubleshooting).

## Setup

Tested on Linux with X11. Typing uses `xdotool`, which doesn't work on Wayland.

### 1. Install the desktop app

```bash
git clone https://github.com/TomasHarmer/eve && cd eve
sudo apt install xdotool
python3 -m venv host/.venv
host/.venv/bin/pip install -r host/requirements.txt
```

### 2. Flash the stick

You need [PlatformIO](https://platformio.org) and the Atech driver library.
Get the driver library through [atech.dev](https://atech.dev).

```bash
export ATECH_LIB=/path/to/atech/firmware/esp32
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
- eve keeps track of how fast your computer has been lately. While local
  transcription is taking longer than that second anyway, it asks the cloud
  straight away instead of waiting first.
- A cloud request that hangs is retried once after 4 seconds. If the cloud is
  unreachable, eve carries on locally.

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

### Bluetooth

eve also works over Bluetooth LE, so the stick doesn't need a cable to your
computer: power it from a USB-C power bank or charger instead. It carries the
same button gestures and light as USB, with the audio compressed 4:1, and
dictation is as fast as over USB.

You don't need to pair it. Choose how eve reaches the stick with `--link`:

| Mode | What it does |
|---|---|
| `--link auto` | the default. USB when the stick is plugged into the computer, Bluetooth when it isn't. Switches by itself when you plug or unplug it |
| `--link usb` | USB only. eve never uses Bluetooth |
| `--link ble` | Bluetooth only, even while the stick is plugged in (handy for trying it out) |

```bash
host/.venv/bin/python host/voicetype.py --model small.en --link ble
host/install-autostart.sh --model small.en --link auto   # or set it for login
```

Your computer's Bluetooth must be on for `auto` and `ble`.

> There's no pairing yet: while eve isn't connected, any nearby Bluetooth
> device could connect to the stick and hear what it records. Only one
> connection is allowed at a time, so once eve is connected nobody else can.

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
| `--cloud-after 1` | seconds to wait for local transcription before asking the cloud. Default `1`, skipped while local has been slower than that. `0` sends every phrase to the cloud straight away |
| `--no-cloud` | never use the cloud, even if `cloud.env` is set up |
| `--pause 0.4` | how long a pause (seconds) ends a phrase. Default `0.4` |
| `--enter` | also press Enter every time you let go |
| `--no-live` | type everything at once when you let go |
| `--prompt "..."` | replace the vocabulary hint (words Whisper should expect) |
| `--forget-device` | forget which boards are eve and look again |
| `--link auto` | `auto` (default): USB when plugged in, else Bluetooth. `usb`: USB only. `ble`: Bluetooth only |
| `--port /dev/ttyACM0` | use this port instead of finding eve automatically |
| `--dry-run` | print the text instead of typing it |
| `--save-wav last.wav` | keep the last recording, for checking the mic |
| `-v` | also show the stick's log lines |

## Troubleshooting

**Bluetooth doesn't connect.** Check that the computer's Bluetooth is on
(`bluetoothctl show` says `Powered: yes`), and that the stick was flashed with
the current firmware. Watch the log for `connected on Bluetooth`.

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

**Wrong words.** Use `--model small.en` and the cloud fallback. If a word keeps
coming out wrong (a product or project name), add it to the vocabulary hint
with `--prompt`.

## Project layout

```
firmware/            PlatformIO project for the stick (ESP32-S3, Arduino)
  src/main.cpp       recording, button gestures, USB wire protocol
  diag/diag.cpp      microphone wiring diagnostic
host/
  voicetype.py       desktop app: finding eve, VAD phrase splitting,
                     local + cloud Whisper, typing into the right window
  install-autostart.sh
assets/logo.svg
```

The USB and Bluetooth protocol is small and documented at the top of `firmware/src/main.cpp`.
It's a good starting point for a different front end, such as a macOS or
Wayland typer.

## Credits

Made with [**Atech**](https://atech.dev) hardware modules. Speech recognition by
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) and OpenAI's Whisper
models, with Silero VAD for phrase detection. The logo is set in
[Montserrat](https://github.com/JulietaUla/Montserrat) ExtraLight (SIL Open Font License).

## License

[MIT](LICENSE)
