#!/usr/bin/env python3
"""
voicetype — push-to-talk dictation from the Atech Tiny voice stick.

Hold the stick's button and talk. Each phrase is typed into the window you
started in as soon as you pause (X11, via xdotool); you don't have to let go
first. Transcription runs locally with faster-whisper, with an optional cloud
fallback (~/.config/eve/cloud.env) for when the computer is busy. The stick
connects over USB or Bluetooth LE: --link auto (default), usb or ble.

    host/.venv/bin/python host/voicetype.py                   # default: base.en, live
    host/.venv/bin/python host/voicetype.py --model small.en  # more accurate, ~3x slower
    host/.venv/bin/python host/voicetype.py --enter           # press Enter on release
                                                              # (or tap the button: Enter / Tab;
                                                              #  triple-tap, then N taps: window N)
    host/.venv/bin/python host/voicetype.py --no-live         # type everything on release

See ../firmware/src/main.cpp for the wire protocol.
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass

import numpy as np
import serial
from serial.tools import list_ports

SAMPLE_RATE = 16000
BYTES_PER_SEC = SAMPLE_RATE * 2
MAGIC = b"\xa5\x5a"
ESPRESSIF_VID = 0x303A
MAX_PAYLOAD = 4096
PACKET_TYPES = b"SDEANTHCM"
STALL_SECONDS = 3.0  # a recording with no packets this long is finalized anyway

CLEAR_LINES = 6  # Ctrl+U presses when clearing a prompt; each clears one line

DEFAULT_PROMPT = (
    "Dictation for a coding assistant in a terminal. Claude, Claude Code, git, "
    "GitHub, Python, TypeScript, JSON, API, CLI, README, npm, pip, ESP32, Atech, "
    "PCB, LED, USB-C, GPIO, I2C, firmware, antenna, module, KiCad, FreeCAD, power."
)

log = logging.getLogger("voicetype")


# ---------------------------------------------------------------- serial link
DEVICE_FILE = os.path.expanduser("~/.config/eve/device")   # eve USB serial numbers, one per line


def known_serials() -> set[str]:
    try:
        with open(DEVICE_FILE) as f:
            return {line.strip() for line in f if line.strip()}
    except OSError:
        return set()


def remember_serial(sn: str) -> None:
    os.makedirs(os.path.dirname(DEVICE_FILE), exist_ok=True)
    with open(DEVICE_FILE, "a") as f:
        f.write(sn + "\n")


def probe(device: str) -> bool:
    """Ask the device on `device` whether it is eve ('?' -> 'H' packet)."""
    try:
        s = open_port(device)
    except (serial.SerialException, OSError):
        return False            # busy (e.g. a browser is flashing it) or gone
    try:
        s.write_timeout = 0.5   # a board that isn't reading must not hang us
        s.reset_input_buffer()
        buf = bytearray()
        # eve can take a moment after the port opens before it reads, so ask
        # a few times.
        deadline = time.time() + 2.0
        next_ask = 0.0
        while time.time() < deadline:
            if time.time() >= next_ask:
                try:
                    s.write(b"?")
                except serial.SerialTimeoutException:
                    pass
                next_ask = time.time() + 0.3
            buf += s.read(256)
            if any(t == "H" and p == b"eve" for t, p in parse_packets(buf)):
                return True
        return False
    except (serial.SerialException, OSError):
        return False
    finally:
        s.reset_output_buffer()   # drop unsent bytes so close() doesn't wait on them
        s.close()


def find_port(not_eve: set) -> str | None:
    """Find eve among the connected ESP32 boards.

    Once any eve is known (~/.config/eve/device), only known eve boards are
    ever opened; to add another eve board, put its USB serial number on a new
    line in that file. Until then each board is asked once whether it is eve;
    boards that say no are left alone for the rest of the session."""
    ports = [p for p in list_ports.comports() if p.vid == ESPRESSIF_VID]
    known = known_serials()
    if known:
        # Never open any other board: a flasher (atech.dev, PlatformIO)
        # resets boards mid-flash, and opening the port then breaks it.
        return next((p.device for p in ports if p.serial_number in known), None)
    for p in ports:
        key = p.serial_number or p.device
        if key in not_eve:
            continue
        if probe(p.device):
            if p.serial_number:
                remember_serial(p.serial_number)
                log.info("found eve (USB serial %s); other boards will be left alone",
                         p.serial_number)
            return p.device
        not_eve.add(key)
        log.info("%s (%s) is not eve; leaving it alone", p.device, p.product or "?")
    return None


def open_port(device: str) -> serial.Serial:
    s = serial.Serial()
    s.port = device
    s.baudrate = 115200
    s.timeout = 0.1
    s.exclusive = True   # nothing else (e.g. a browser) can open eve's port while we hold it
    # On the ESP32-S3's USB-Serial-JTAG, DTR low + RTS high resets the chip.
    # Linux raises both lines on open; letting pyserial drop DTR first would
    # pass through that state and reboot eve. So open with both high, then
    # drop RTS before DTR.
    s.open()
    try:
        s.rts = False
        s.dtr = False
    except OSError:
        pass   # not a USB serial port (e.g. a virtual one): no control lines
    return s


# ------------------------------------------------------------ Bluetooth LE link
EVE_SVC_UUID = "7e5e0001-6576-6500-8000-00805f9b34fb"
EVE_TX_UUID = "7e5e0002-6576-6500-8000-00805f9b34fb"   # device -> host, notify
EVE_RX_UUID = "7e5e0003-6576-6500-8000-00805f9b34fb"   # host -> device, write

_ble_loop = None


def ble_loop():
    """One asyncio loop, on its own thread, for all Bluetooth work."""
    global _ble_loop
    if _ble_loop is None:
        import asyncio
        _ble_loop = asyncio.new_event_loop()
        threading.Thread(target=_ble_loop.run_forever, daemon=True, name="ble").start()
    return _ble_loop


def ble_available() -> bool:
    try:
        import bleak  # noqa: F401
        return True
    except ImportError:
        return False


BLE_FILE = os.path.expanduser("~/.config/eve/ble")   # address of the last eve seen over Bluetooth


def _ble_still_connected(addr: str) -> bool:
    """A connected stick doesn't advertise, so a scan can't see it. That
    happens when the system's Bluetooth kept the link of an eve that was
    stopped without closing it."""
    try:
        r = subprocess.run(["bluetoothctl", "info", addr], capture_output=True, text=True, timeout=3)
        return "Connected: yes" in r.stdout
    except Exception:
        return False


def find_ble(timeout: float = 4.0):
    """Find eve over Bluetooth: ("ble", BLEDevice), or None."""
    import asyncio
    from bleak import BleakScanner

    try:
        last = open(BLE_FILE).read().strip()
    except OSError:
        last = ""
    if last and _ble_still_connected(last):
        # Drop the stale link so the stick advertises again and the scan finds it.
        log.info("releasing a Bluetooth link to eve left over from before")
        subprocess.run(["bluetoothctl", "disconnect", last], capture_output=True, timeout=10)
        time.sleep(1)

    async def scan():
        return await BleakScanner.find_device_by_filter(
            lambda d, ad: EVE_SVC_UUID in [u.lower() for u in ad.service_uuids], timeout=timeout)
    try:
        dev = asyncio.run_coroutine_threadsafe(scan(), ble_loop()).result(timeout + 5)
    except Exception as e:
        log.debug("Bluetooth scan failed (%s)", e)
        return None
    if dev is None:
        return None
    if dev.address != last:
        os.makedirs(os.path.dirname(BLE_FILE), exist_ok=True)
        with open(BLE_FILE, "w") as f:
            f.write(dev.address + "\n")
    return ("ble", dev)


class BlePort:
    """eve over Bluetooth LE, with just enough of serial.Serial's interface
    (read, write, is_open, close) for the rest of the app to use it unchanged.
    Notifications are one ordered byte stream carrying the usual packets."""

    def __init__(self, device):
        import asyncio
        from bleak import BleakClient
        self.name = f"Bluetooth {device.address}"
        self.buf = bytearray()
        self.cond = threading.Condition()
        self.connected = False

        def gone(_client):
            with self.cond:
                self.connected = False
                self.cond.notify_all()

        def on_data(_char, data: bytearray):
            with self.cond:
                self.buf += data
                self.cond.notify_all()

        async def connect():
            self.client = BleakClient(device, disconnected_callback=gone)
            await self.client.connect(timeout=10)
            await self.client.start_notify(EVE_TX_UUID, on_data)
        try:
            asyncio.run_coroutine_threadsafe(connect(), ble_loop()).result(20)
        except Exception as e:
            raise serial.SerialException(f"Bluetooth connect failed: {e}") from e
        self.connected = True

    @property
    def is_open(self) -> bool:
        return self.connected

    def read(self, n: int) -> bytes:
        with self.cond:
            if not self.buf and self.connected:
                self.cond.wait(0.1)
            if not self.buf and not self.connected:
                raise serial.SerialException("Bluetooth link lost")
            out = bytes(self.buf[:n])
            del self.buf[:n]
            return out

    def write(self, data: bytes) -> None:
        import asyncio
        if self.connected:
            asyncio.run_coroutine_threadsafe(
                self.client.write_gatt_char(EVE_RX_UUID, data, response=False), ble_loop())

    def close(self) -> None:
        import asyncio
        if getattr(self, "client", None) is not None:
            try:
                asyncio.run_coroutine_threadsafe(self.client.disconnect(), ble_loop()).result(5)
            except Exception:
                pass
        self.connected = False


# IMA ADPCM, as the stick sends audio over Bluetooth ('C' packets).
_IMA_STEP = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
    253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
    3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
    11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767]
_IMA_INDEX = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]


def adpcm_decode(payload: bytes) -> bytes:
    """One 'C' block -> int16 LE PCM. Each block carries its own start state."""
    if len(payload) < 5:
        return b""
    pred, index, n = struct.unpack_from("<hBH", payload)
    index = min(max(index, 0), 88)
    data = payload[5:]
    n = min(n, len(data) * 2)
    out = [0] * n
    for i in range(n):
        code = (data[i >> 1] >> 4) if i & 1 else (data[i >> 1] & 0x0F)
        step = _IMA_STEP[index]
        delta = step >> 3
        if code & 4:
            delta += step
        if code & 2:
            delta += step >> 1
        if code & 1:
            delta += step >> 2
        pred = pred - delta if code & 8 else pred + delta
        pred = 32767 if pred > 32767 else (-32768 if pred < -32768 else pred)
        index = min(max(index + _IMA_INDEX[code], 0), 88)
        out[i] = pred
    return struct.pack(f"<{n}h", *out)


def parse_packets(buf: bytearray):
    """Yield (type, payload) from buf, consuming what it parses. Non-packet
    bytes (driver log lines) are logged at debug level and discarded."""
    while True:
        i = buf.find(MAGIC)
        if i < 0:
            junk_end = len(buf) - 1 if buf.endswith(MAGIC[:1]) else len(buf)
            if junk_end:
                _log_junk(buf[:junk_end])
                del buf[:junk_end]
            return
        if i:
            _log_junk(buf[:i])
            del buf[:i]
        if len(buf) < 5:
            return
        ptype = buf[2]
        (length,) = struct.unpack_from("<H", buf, 3)
        if ptype not in PACKET_TYPES or length > MAX_PAYLOAD:
            del buf[:1]  # false magic; resync
            continue
        if len(buf) < 5 + length:
            return
        payload = bytes(buf[5 : 5 + length])
        del buf[: 5 + length]
        yield chr(ptype), payload


def _log_junk(b: bytes) -> None:
    text = b.decode("utf-8", "replace").strip()
    if text:
        log.debug("device: %s", text)


# -------------------------------------------------------------- transcription
class LocalWhisper:
    def __init__(self, model: str, prompt: str | None, threads: int):
        from faster_whisper import WhisperModel

        log.info("loading whisper model %r (first run downloads it)...", model)
        t0 = time.time()
        opts = dict(device="cpu", compute_type="int8", cpu_threads=threads)
        try:
            # Use the downloaded copy without touching the network, so eve
            # starts the same with or without internet.
            self.model = WhisperModel(model, local_files_only=True, **opts)
        except Exception:
            log.info("model not downloaded yet; fetching it (needs internet, once)")
            self.model = WhisperModel(model, **opts)
        self.prompt = prompt
        log.info("model ready in %.1fs", time.time() - t0)

    def transcribe(self, audio: np.ndarray, context: str = "") -> str:
        # Feeding the words already typed in this dictation keeps phrases
        # consistent (capitalisation, punctuation, names) across the splits.
        prompt = " ".join(p for p in (self.prompt, context[-200:]) if p) or None
        segments, _info = self.model.transcribe(
            audio,
            language="en",
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=prompt,
        )
        return " ".join(s.text.strip() for s in segments).strip()


# Cloud speech services with an OpenAI-style /audio/transcriptions endpoint.
CLOUD_PROVIDERS = {
    "groq":   ("https://api.groq.com/openai/v1/audio/transcriptions", "whisper-large-v3-turbo", "GROQ_API_KEY"),
    "openai": ("https://api.openai.com/v1/audio/transcriptions", "gpt-4o-mini-transcribe", "OPENAI_API_KEY"),
}
CLOUD_FILE = os.path.expanduser("~/.config/eve/cloud.env")


def load_cloud_config() -> tuple[str, str] | None:
    """(provider, api_key) from ~/.config/eve/cloud.env, else None (cloud off).

    The file holds two lines:  provider=groq   and   key=<your API key>"""
    conf = {}
    try:
        with open(CLOUD_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    conf[k.strip().lower()] = v.strip().strip("'\"")
    except OSError:
        return None
    provider = conf.get("provider", "groq").lower()
    if provider not in CLOUD_PROVIDERS:
        log.warning("cloud.env: unknown provider %r (use groq or openai)", provider)
        return None
    key = conf.get("key") or os.environ.get(CLOUD_PROVIDERS[provider][2], "")
    return (provider, key) if key else None


class CloudWhisper:
    TIMEOUT = 4.0   # a healthy request takes ~0.3 s; past this it's stuck, retry

    def __init__(self, provider: str, key: str, prompt: str | None):
        from urllib.parse import urlsplit
        self.url, self.model, _ = CLOUD_PROVIDERS[provider]
        self.provider, self.key, self.prompt = provider, key, prompt
        u = urlsplit(self.url)
        self.host, self.path = u.netloc, u.path
        self.idle: list = []          # kept-alive HTTPS connections, saves a TLS handshake
        self.idle_lock = threading.Lock()

    def _body(self, audio: np.ndarray, context: str) -> tuple[bytes, str]:
        import io
        import uuid
        wav = io.BytesIO()
        with wave.open(wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        prompt = " ".join(p for p in (self.prompt, context[-200:]) if p)
        fields = {"model": self.model, "language": "en", "response_format": "json"}
        if prompt:
            fields["prompt"] = prompt
        b = uuid.uuid4().hex
        body = bytearray()
        for k, v in fields.items():
            body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        body += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="eve.wav"\r\n'
                 "Content-Type: audio/wav\r\n\r\n").encode()
        body += wav.getvalue() + f"\r\n--{b}--\r\n".encode()
        return bytes(body), b

    def _post(self, body: bytes, boundary: str) -> str:
        import http.client
        import json
        with self.idle_lock:
            conn = self.idle.pop() if self.idle else None
        reused = conn is not None
        if conn is None:
            conn = http.client.HTTPSConnection(self.host, timeout=self.TIMEOUT)
        try:
            conn.request("POST", self.path, body=body, headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "eve-voicetype",
            })
            r = conn.getresponse()
            data = r.read()
        except (http.client.HTTPException, ConnectionError) as e:
            conn.close()
            if reused:   # the server closed the idle connection; that's normal, go again
                return self._post(body, boundary)
            raise OSError(str(e)) from e
        except Exception:
            conn.close()
            raise
        if r.status != 200:
            conn.close()
            raise OSError(f"HTTP {r.status}: {data[:200].decode(errors='replace')}")
        with self.idle_lock:
            self.idle.append(conn)
        return json.loads(data).get("text", "").strip()

    def transcribe(self, audio: np.ndarray, context: str = "") -> str:
        body, boundary = self._body(audio, context)
        try:
            return self._post(body, boundary)
        except Exception as e:
            if isinstance(e, OSError) and str(e).startswith("HTTP 4"):
                raise   # bad key, bad request: retrying won't help
            log.info("cloud request failed (%s); retrying once", e)
            return self._post(body, boundary)


class Hybrid:
    """Local first. If the local model hasn't answered within `wait` seconds,
    or is still busy with an earlier phrase, ask the cloud and use whichever
    answer arrives first. Falls back to local if the cloud fails.

    The local model's recent speed is tracked, so when it's been taking longer
    than `wait` anyway (a busy CPU), the cloud is asked straight away rather
    than after a wasted wait."""

    def __init__(self, local: LocalWhisper, cloud: CloudWhisper | None, wait: float):
        self.local, self.cloud, self.wait = local, cloud, wait
        self.local_free = threading.Lock()
        self.local_secs: float | None = None   # recent local transcription time (moving average)

    def _note_local(self, secs: float) -> None:
        self.local_secs = secs if self.local_secs is None else 0.7 * self.local_secs + 0.3 * secs

    def transcribe(self, audio: np.ndarray, context: str = "") -> tuple[str, str]:
        if self.cloud is None:
            with self.local_free:
                return self.local.transcribe(audio, context), "local"
        answers: queue.Queue = queue.Queue()   # (source, text or None on failure)
        started = self.local_free.acquire(blocking=False)
        local_pending = started
        if started:
            def run_local():
                t0 = time.time()
                try:
                    answers.put(("local", self.local.transcribe(audio, context)))
                    self._note_local(time.time() - t0)
                except Exception as e:
                    log.warning("local transcription failed (%s)", e)
                    answers.put(("local", None))
                finally:
                    self.local_free.release()
            threading.Thread(target=run_local, daemon=True).start()
            slow = self.local_secs is not None and self.local_secs > self.wait
            if not slow:
                try:
                    src, text = answers.get(timeout=self.wait)
                    if text is not None:
                        return text, "local"
                    local_pending = False
                except queue.Empty:
                    pass
        why = "busy" if not started else ("slow" if self.local_secs is None or self.local_secs > self.wait else "late")

        def run_cloud():
            try:
                answers.put(("cloud", self.cloud.transcribe(audio, context)))
            except Exception as e:
                log.warning("cloud transcription failed (%s); using local", e)
                answers.put(("cloud", None))
        threading.Thread(target=run_cloud, daemon=True).start()

        pending = {"cloud"} | ({"local"} if local_pending else set())
        while pending:
            src, text = answers.get()
            pending.discard(src)
            if text is not None:
                return text, ("local" if src == "local" else f"{self.cloud.provider}, local {why}")
        # Both failed, or the cloud failed while local was busy with an earlier phrase.
        if started:
            return "", "local"
        with self.local_free:
            return self.local.transcribe(audio, context), "local"


def prepare_audio(pcm: bytes) -> np.ndarray:
    a = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    if a.size == 0:
        return a
    a -= a.mean()  # MEMS mics carry a small DC offset
    peak = float(np.abs(a).max())
    if peak > 1e-5:
        a *= min(0.9 / peak, 50.0)  # the mic is quiet at arm's length; normalize
    return a


VAD_GAIN = 4.0  # fixed: speech from this mic peaks near 0.25 FS; x4 keeps it unclipped


def vad_audio(pcm: bytes) -> np.ndarray:
    """Audio for voice detection: fixed gain, NOT peak-normalized. Normalizing a
    buffer of pure room noise blows the noise up until the VAD calls it speech."""
    a = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    if a.size:
        a -= a.mean()
    return np.clip(a * VAD_GAIN, -1.0, 1.0)


# Whisper's well-known outputs for near-silence.
HALLUCINATIONS = {"", "you", "thank you", "thanks for watching", "thank you for watching", "bye"}


def is_hallucination(text: str) -> bool:
    return text.lower().strip(" .!?,") in HALLUCINATIONS


# --------------------------------------------------------- live phrase splitter
class PhraseSplitter:
    """Cuts a growing recording into phrases at pauses, using Silero VAD (ships
    with faster-whisper). Each phrase can be transcribed while you keep talking."""

    CHECK_EVERY = 0.3   # seconds of new audio between VAD passes
    MAX_PHRASE = 20.0   # force a cut so one phrase never nears Whisper's 30 s window
    KEEP_LEAD = 0.5     # silence kept before speech so its onset isn't clipped

    def __init__(self, pause: float):
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        self._vad = get_speech_timestamps
        self.pause = pause
        self.opts = VadOptions(min_silence_duration_ms=int(pause * 1000), speech_pad_ms=150)
        self.reset()

    def reset(self) -> None:
        self.pending = bytearray()
        self._unchecked = 0

    def feed(self, payload: bytes) -> bytes | None:
        """Add audio; return a finished phrase (raw PCM) if one just ended."""
        self.pending += payload
        self._unchecked += len(payload)
        if self._unchecked < self.CHECK_EVERY * BYTES_PER_SEC:
            return None
        self._unchecked = 0

        audio = vad_audio(bytes(self.pending))
        n = audio.size
        speech = self._vad(audio, self.opts)
        if not speech:
            # Only silence so far: drop it, keeping a little lead-in.
            keep = int(self.KEEP_LEAD * SAMPLE_RATE)
            if n > keep:
                del self.pending[: (n - keep) * 2]
            return None

        end = speech[-1]["end"]
        if n - end >= self.pause * SAMPLE_RATE:
            return self._cut(end)
        if n >= self.MAX_PHRASE * SAMPLE_RATE:
            # No pause long enough: cut at the last gap the VAD saw, or here.
            return self._cut(speech[-2]["end"] if len(speech) > 1 else n)
        return None

    def flush(self) -> bytes:
        rest = bytes(self.pending)
        self.reset()
        return rest

    def _cut(self, at_sample: int) -> bytes:
        at = at_sample * 2
        phrase = bytes(self.pending[:at])
        del self.pending[:at]
        return phrase


# ---------------------------------------------------------------------- typing
def type_text(text: str) -> None:
    # A few words per xdotool call, checking before each that the target
    # window still has focus: a long phrase takes a second or two to type,
    # and you may click away in the middle of it.
    for chunk in re.findall(r".{1,24}(?:\s+|$)|\S+", text, re.S):
        in_window.ensure()
        subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "4", "--", chunk], check=True)


def press_key(key: str, repeat: int = 1) -> None:
    in_window.ensure()
    subprocess.run(["xdotool", "key", "--clearmodifiers", "--repeat", str(repeat), "--repeat-delay", "2", key],
                   check=True)


def active_window() -> str | None:
    try:
        r = subprocess.run(["xdotool", "getactivewindow"], capture_output=True, text=True, timeout=2)
        return r.stdout.strip() or None
    except Exception:
        return None


def list_windows() -> list[tuple[str, str]]:
    """App windows (id, title), oldest first: the window manager's own
    _NET_CLIENT_LIST order, so a window keeps its number until it closes.
    Skips the desktop, docks, and anything kept off the taskbar."""
    try:
        r = subprocess.run(["xprop", "-root", "_NET_CLIENT_LIST"], capture_output=True, text=True, timeout=2)
        ids = re.findall(r"0x[0-9a-fA-F]+", r.stdout.split("#", 1)[-1])
    except Exception:
        return []
    wins = []
    for w in ids:
        try:
            r = subprocess.run(["xprop", "-id", w, "_NET_WM_WINDOW_TYPE", "_NET_WM_STATE"],
                               capture_output=True, text=True, timeout=2)
        except Exception:
            continue
        props = r.stdout
        untyped = "_NET_WM_WINDOW_TYPE:  not found" in props   # no type set means normal
        if not (untyped or "_NET_WM_WINDOW_TYPE_NORMAL" in props) or "SKIP_TASKBAR" in props:
            continue
        wid = str(int(w, 16))   # decimal, like xdotool's ids
        wins.append((wid, window_name(wid, 60) or "(untitled)"))
    return wins


def window_name(w: str | None, width: int = 40) -> str:
    try:
        r = subprocess.run(["xdotool", "getwindowname", w], capture_output=True, text=True, timeout=2)
        return r.stdout.strip()[:width]
    except Exception:
        return "?"


class WindowGone(Exception):
    pass


class in_window:
    """Run the typing inside `with in_window(w):` to send it to window w even
    if focus has moved on since: switch to w, type, then switch back to the
    window you'd moved to. type_text and press_key re-check focus before each
    piece, so a click elsewhere mid-phrase is undone too."""

    target: str | None = None   # window the current `with` block types into

    def __init__(self, window: str | None):
        self.window = window
        self.back: str | None = None

    @staticmethod
    def _activate(w: str) -> bool:
        try:
            r = subprocess.run(["xdotool", "windowactivate", "--sync", w], capture_output=True, timeout=3)
        except subprocess.TimeoutExpired:
            return False
        return r.returncode == 0 and active_window() == w

    @classmethod
    def ensure(cls) -> None:
        w = cls.target
        if not w:
            return
        cur = active_window()
        if cur == w:
            return
        log.info("focus moved to %s (%s); switching back to %s (%s) to type", cur, window_name(cur), w, window_name(w))
        if not cls._activate(w):
            raise WindowGone(w)
        if cls._current and cur:
            cls._current.back = cur   # where you went; return there afterwards
        time.sleep(0.05)   # let the window settle its keyboard focus

    _current: "in_window | None" = None

    def __enter__(self):
        in_window.target, in_window._current = self.window, self
        return self

    def __exit__(self, *exc):
        in_window.target, in_window._current = None, None
        # Return only if eve still holds the focus: if you've clicked somewhere
        # else again since, stay there. --sync so the next action sees the
        # real focus, not one still changing.
        if self.back and active_window() == self.window:
            try:
                subprocess.run(["xdotool", "windowactivate", "--sync", self.back],
                               capture_output=True, timeout=3)
            except subprocess.TimeoutExpired:
                pass
        return False


# ------------------------------------------------------------------------ main
@dataclass
class Job:
    dictation: int   # which button-hold this audio belongs to
    pcm: bytes
    final: bool      # last piece of the dictation (button released)
    kind: str = "audio"   # or "tap" / "double" / "multi": button gestures, handled in order
    taps: int = 0         # for "multi": how many taps
    window: str | None = None  # X window that had focus when the button was pressed


class App:
    def __init__(self, args):
        self.args = args
        prompt = None if args.no_prompt else args.prompt
        local = LocalWhisper(args.model, prompt, args.threads)
        conf = None if args.no_cloud else load_cloud_config()
        cloud = CloudWhisper(conf[0], conf[1], prompt) if conf else None
        if cloud:
            log.info("cloud fallback on (%s) for phrases slower than %.1fs", cloud.provider, args.cloud_after)
        self.engine = Hybrid(local, cloud, args.cloud_after)
        self.splitter = None if args.no_live else PhraseSplitter(args.pause)
        self.jobs: queue.Queue[Job] = queue.Queue()
        self._carry: Job | None = None
        self.unsent = 0   # characters eve has typed that no Enter has sent yet
        self.tabbed = False  # eve pressed Tab (accepted a suggestion) that no Enter has sent yet
        self.text_window: str | None = None  # where that unsent text / Tab went
        self.picker: list[tuple[str, str]] = []   # (window, title) while the window picker is open
        self.picker_until = 0.0
        self.picker_note: str | None = None      # notification id, to replace it
        self.port: serial.Serial | None = None
        self.port_lock = threading.Lock()

    def tell_device(self, code: bytes) -> None:
        with self.port_lock:
            try:
                if self.port and self.port.is_open:
                    self.port.write(code)
            except serial.SerialException:
                pass

    def heartbeat(self) -> None:
        """Tell the stick every second that the app is connected; while these
        keep coming, its light pulses softly."""
        while True:
            self.tell_device(b"P")
            time.sleep(1.0)

    # -- worker: transcribe + type in order, off the serial thread
    def worker(self) -> None:
        current = -1
        context = ""      # text typed so far in the current dictation
        typed_any = False
        while True:
            job = self._next_job()
            if job.kind in ("tap", "double", "multi"):
                # Queued behind any text still being typed, so it acts after it.
                taps = {"tap": 1, "double": 2}.get(job.kind, job.taps)
                if self.picker and time.time() < self.picker_until:
                    self.pick_window(taps)
                elif job.kind == "multi":
                    if taps == 3:
                        self.open_picker()
                    else:
                        log.info("%d taps: nothing to do (triple-tap opens the window picker)", taps)
                else:
                    self.picker = []
                    self.gesture(job.kind, job.window)
                continue
            self.picker = []   # talking closes the window picker
            if job.dictation != current:
                current, context, typed_any = job.dictation, "", False
            try:
                text = self.transcribe(job, context)
                if text:
                    text += " "   # phrases and dictations need a space between them
                    if self.args.dry_run:
                        print(text, end="" if not job.final else "\n", flush=True)
                    else:
                        if self.unsent and self.text_window != job.window:
                            self.unsent = 0   # earlier unsent text was in another window
                        with in_window(job.window):
                            type_text(text)
                        self.text_window = job.window
                    context += text
                    typed_any = True
                    self.unsent += len(text)
                if job.final:
                    if self.args.dry_run and not text:
                        print(flush=True)
                    if self.args.enter and typed_any and not self.args.dry_run:
                        with in_window(job.window):
                            press_key("Return")
                        self.unsent = 0
                        self.tabbed = False
                    self.tell_device(b"K" if typed_any else b"I")
            except WindowGone:
                log.warning("the window you were dictating into is gone; dropped the text")
                self.tell_device(b"X")
            except Exception:
                log.exception("transcription failed")
                self.tell_device(b"X")

    # -- window picker: triple-tap shows your windows numbered; then tap N times
    PICKER_SECONDS = 8.0

    def _notify(self, title: str, body: str = "") -> None:
        cmd = ["notify-send", "-a", "eve", "-p", "-u", "low", title, body]
        if self.picker_note:
            cmd[1:1] = ["-r", self.picker_note]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            self.picker_note = r.stdout.strip() or self.picker_note
        except Exception:
            pass

    def open_picker(self) -> None:
        wins = list_windows()
        if not wins:
            log.info("triple tap -> window picker: no windows found")
            self.tell_device(b"X")
            return
        self.picker, self.picker_until = wins, time.time() + self.PICKER_SECONDS
        lines = [f"{i}  {'●' if w == active_window() else ' '} {t}" for i, (w, t) in enumerate(wins, 1)]
        log.info("triple tap -> window picker:\n  %s", "\n  ".join(lines))
        if self.args.dry_run:
            print("[window picker]", *lines, sep="\n", flush=True)
        else:
            self._notify("Tap the window number", "\n".join(lines[:15]))

    def pick_window(self, n: int) -> None:
        wins, self.picker = self.picker, []
        if not 1 <= n <= len(wins):
            log.info("window picker: no window %d", n)
            self._notify(f"No window {n}")
            self.tell_device(b"X")
            return
        w, title = wins[n - 1]
        log.info("window picker -> %d: %s", n, title)
        if self.args.dry_run:
            print(f"[window {n}: {title}]", flush=True)
            return
        try:
            r = subprocess.run(["xdotool", "windowactivate", "--sync", w], capture_output=True, timeout=3)
            ok = r.returncode == 0
        except subprocess.TimeoutExpired:
            ok = False
        if not ok:
            self._notify(f"Couldn't switch to window {n}", title)
            self.tell_device(b"X")
            return
        # You've moved on: taps now act on the window you picked, not on
        # text eve left waiting in the old one.
        self.unsent, self.tabbed, self.text_window = 0, False, None
        self._notify(f"→ {n}  {title}")
        self.tell_device(b"K")

    def gesture(self, kind: str, tap_window: str | None = None) -> None:
        """Single tap: Enter if eve has typed unsent text, else Tab (accepts
        Claude Code's suggestion). Double tap: delete eve's unsent text, or
        clear the whole prompt if a Tab put a suggestion there.

        Enter and delete go to the window holding eve's unsent text, even if
        you've clicked somewhere else since; a fresh Tab goes to the window
        that had focus when you tapped."""
        waiting = bool(self.unsent) or self.tabbed
        window = self.text_window if waiting else tap_window
        try:
            with in_window(None if self.args.dry_run else window):
                self._gesture(kind)
            if kind == "tap" and self.tabbed:
                self.text_window = window
        except WindowGone:
            log.warning("the window eve typed into is gone; ignoring the %s", kind)
            self.unsent, self.tabbed = 0, False
            self.tell_device(b"X")
        except Exception:
            log.exception("%s failed", kind)
            self.tell_device(b"X")

    def _gesture(self, kind: str) -> None:
        if kind == "tap":
            # A Tab fills in Claude Code's suggestion, so after one there is
            # text waiting too: the next tap sends it.
            send = bool(self.unsent) or self.tabbed
            key = "Return" if send else "Tab"
            log.info("tap -> %s", "Enter" if send else "Tab")
            if self.args.dry_run:
                print(f"[{key}]", flush=True)
            else:
                press_key(key)
            self.unsent = 0
            self.tabbed = not send
        elif self.tabbed:
            # eve can't know how long the accepted suggestion is, so clear
            # the whole prompt the documented Claude Code way: Ctrl+E to the
            # line end, then Ctrl+U (delete to line start) repeated, which
            # also clears multi-line input. Not Esc Esc: on an empty prompt
            # that opens the rewind menu.
            log.info("double tap -> clear the prompt (suggestion%s)",
                     f" + {self.unsent} dictated characters" if self.unsent else "")
            if self.args.dry_run:
                print("[clear prompt]", flush=True)
            else:
                press_key("ctrl+e")
                press_key("ctrl+u", repeat=CLEAR_LINES)
            self.unsent = 0
            self.tabbed = False
        elif self.unsent:
            log.info("double tap -> delete %d characters", self.unsent)
            if self.args.dry_run:
                print(f"[delete {self.unsent}]", flush=True)
            else:
                press_key("BackSpace", repeat=self.unsent)
            self.unsent = 0
        else:
            log.info("double tap -> nothing to delete")

    def _next_job(self) -> Job:
        """Next job, merged with any phrases of the same dictation already waiting.
        Each Whisper call has a fixed cost however short the audio, so when
        phrases arrive faster than they're transcribed, one call for all of
        them keeps the lag from building up."""
        job = self._carry or self.jobs.get()
        self._carry = None
        while not job.final:
            try:
                nxt = self.jobs.get_nowait()
            except queue.Empty:
                break
            if nxt.kind != "audio" or nxt.dictation != job.dictation:
                self._carry = nxt
                break
            job = Job(job.dictation, job.pcm + nxt.pcm, nxt.final, window=job.window)
        return job

    def transcribe(self, job: Job, context: str) -> str:
        seconds = len(job.pcm) / BYTES_PER_SEC
        if seconds < 0.3:
            return ""
        if job.final:
            self.tell_device(b"B")
        if self.args.save_wav:
            save_wav(self.args.save_wav, job.pcm)
        t0 = time.time()
        text, source = self.engine.transcribe(prepare_audio(job.pcm), context)
        dt = time.time() - t0
        if is_hallucination(text):
            log.info("%.1fs audio -> (nothing heard) [%.1fs %s]", seconds, dt, source)
            return ""
        log.info("%.1fs audio -> %r [%.1fs %s]%s", seconds, text, dt, source, "" if job.final else " (live)")
        return text

    # -- serial loop with automatic reconnect
    def run(self) -> None:
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.heartbeat, daemon=True).start()
        link = "usb" if self.args.port else self.args.link
        self.use_ble = link != "usb" and ble_available()
        if link == "ble" and not self.use_ble:
            sys.exit("Bluetooth needs the bleak package: host/.venv/bin/pip install bleak")
        log.info("link: %s", {"auto": "USB, or Bluetooth when unplugged", "usb": "USB only",
                              "ble": "Bluetooth only"}[link] + ("" if self.use_ble or link == "usb"
                                                                 else " (no bleak: USB only)"))
        waiting_logged = False
        not_eve: set = set()
        while True:
            device = self.args.port or (None if link == "ble" else find_port(not_eve))
            if not device and self.use_ble:
                device = find_ble()
            if not device:
                if not waiting_logged:
                    log.info("waiting for the voice stick (%s)...",
                             "Bluetooth" if link == "ble" else
                             "USB or Bluetooth" if self.use_ble else "plug it in")
                    waiting_logged = True
                time.sleep(1)
                continue
            try:
                with self.port_lock:
                    self.port = open_port(device) if isinstance(device, str) else BlePort(device[1])
                where = device if isinstance(device, str) else self.port.name
                log.info("connected on %s — hold the button and talk", where)
                waiting_logged = False
                self.read_loop()
            except serial.SerialException as e:
                log.warning("device disconnected (%s)", e)
            finally:
                with self.port_lock:
                    if self.port:
                        try:
                            self.port.close()
                        except Exception:
                            pass
                    self.port = None
            time.sleep(1)

    def read_loop(self) -> None:
        buf = bytearray()
        rec: bytearray | None = None   # whole recording, used by --no-live
        dictation = 0
        window = None      # focused window when the current dictation started
        last_packet = time.time()

        def finish():
            nonlocal rec
            pcm = self.splitter.flush() if self.splitter else bytes(rec)
            self.jobs.put(Job(dictation, pcm, final=True, window=window))
            rec = None

        while True:
            data = self.port.read(4096)
            if data:
                buf += data
                for ptype, payload in parse_packets(buf):
                    last_packet = time.time()
                    if ptype == "C":   # compressed audio (Bluetooth)
                        ptype, payload = "D", adpcm_decode(payload)
                    if ptype == "S":
                        if rec is not None:
                            log.warning("new recording before the last one ended; finishing it")
                            finish()
                        dictation += 1
                        window = active_window()
                        rec = bytearray()
                        if self.splitter:
                            self.splitter.reset()
                        log.info("recording...")
                    elif ptype == "D" and rec is not None:
                        if self.splitter:
                            phrase = self.splitter.feed(payload)
                            if phrase:
                                self.jobs.put(Job(dictation, phrase, final=False, window=window))
                        else:
                            rec += payload
                            if len(rec) > self.args.max_seconds * BYTES_PER_SEC:
                                log.warning("hit %ss cap, transcribing what we have", self.args.max_seconds)
                                finish()
                    elif ptype == "A" and rec is not None:
                        rec = None   # it was only a tap; nothing to transcribe
                        if self.splitter:
                            self.splitter.reset()
                    elif ptype == "T":
                        self.jobs.put(Job(-1, b"", True, kind="tap", window=active_window()))
                    elif ptype == "N":
                        self.jobs.put(Job(-1, b"", True, kind="double", window=active_window()))
                    elif ptype == "M" and payload:
                        self.jobs.put(Job(-1, b"", True, kind="multi", taps=payload[0], window=active_window()))
                    elif ptype == "E" and rec is not None:
                        dropped = struct.unpack("<I", payload)[0] if len(payload) == 4 else 0
                        if dropped:
                            log.warning("device dropped %d samples (link too slow?)", dropped)
                        finish()
            elif rec is not None and time.time() - last_packet > STALL_SECONDS:
                log.warning("stream stalled; transcribing what arrived")
                finish()


def save_wav(path: str, pcm: bytes) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default=os.environ.get("VOICETYPE_MODEL", "base.en"),
                    help="faster-whisper model: tiny.en, base.en (default), small.en (more accurate, ~3x slower), medium.en")
    ap.add_argument("--port", help="serial device (default: find eve among the connected boards)")
    ap.add_argument("--link", choices=("auto", "usb", "ble"), default=os.environ.get("EVE_LINK", "auto"),
                    help="how to reach the stick: auto (default: USB when plugged in, else Bluetooth), "
                         "usb (USB only), ble (Bluetooth only, even when plugged in)")
    ap.add_argument("--forget-device", action="store_true",
                    help="forget which boards are eve (~/.config/eve/device) and search again")
    ap.add_argument("--enter", action="store_true", help="press Enter when you release the button (sends it to Claude)")
    ap.add_argument("--no-live", action="store_true", help="type everything at once on release instead of phrase by phrase")
    ap.add_argument("--pause", type=float, default=0.4,
                    help="seconds of silence that end a phrase in live mode (default 0.4)")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT, help="vocabulary hint for whisper")
    ap.add_argument("--no-prompt", action="store_true", help="disable the vocabulary hint")
    ap.add_argument("--cloud-after", type=float, default=1.0,
                    help="seconds to wait for local transcription before asking the cloud (default 1; skipped when local has lately been slower than this)")
    ap.add_argument("--no-cloud", action="store_true", help="never use the cloud, even if cloud.env is set up")
    ap.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    ap.add_argument("--max-seconds", type=int, default=120, help="cap for one dictation in --no-live mode")
    ap.add_argument("--dry-run", action="store_true", help="print text instead of typing it")
    ap.add_argument("--save-wav", metavar="PATH", help="save each recording (debugging the mic)")
    ap.add_argument("-v", "--verbose", action="store_true", help="also show device log lines")
    args = ap.parse_args()
    if args.forget_device:
        try:
            os.remove(DEVICE_FILE)
        except FileNotFoundError:
            pass

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "huggingface_hub", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if not args.dry_run and not shutil.which("xdotool"):
        sys.exit("xdotool is not installed: sudo apt install xdotool")
    if not os.environ.get("DISPLAY") and not args.dry_run:
        sys.exit("DISPLAY is not set — run this inside your X11 session")

    import signal
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    app = App(args)
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        # Release the Bluetooth link, or the system keeps it and the stick
        # stops advertising until it times out.
        if isinstance(app.port, BlePort):
            app.port.close()


if __name__ == "__main__":
    main()
