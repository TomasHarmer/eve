#!/usr/bin/env python3
"""
Desktop integration for eve: how to type text and press keys, and how to find
and refocus the window a dictation started in.

A desktop is a pair of parts:

    Keyboard   sends text and keys          (xdotool, wtype, ...)
    Windows    finds and focuses windows    (xdotool, hyprctl, swaymsg, ...)

DESKTOPS lists the supported desktops in the order they're detected. To add
one, write the Keyboard and/or Windows class it needs (each lists the tools it
uses) and add a Desktop line; eve and host/install.sh both pick it up from
here. Standard library only: install.sh runs this before the venv exists.

    python3 host/desktop.py                   # what's detected, what's missing
    python3 host/desktop.py --packages apt    # missing packages, for an installer
    python3 host/desktop.py --name            # e.g. "hyprland"
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator, Mapping


def _run(cmd: list[str], timeout: float = 2) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


@dataclass(frozen=True)
class Tool:
    """A command a part needs. `packages` maps a package manager to the
    package's name where it isn't simply the command's name; bundled tools
    come with the desktop itself and aren't installed separately."""
    command: str
    packages: Mapping[str, str] = field(default_factory=dict)
    bundled: bool = False

    def package(self, manager: str) -> str:
        return self.packages.get(manager, self.command)

    def present(self) -> bool:
        return shutil.which(self.command) is not None


# ------------------------------------------------------------------ keyboards
class Keyboard:
    tools: tuple[Tool, ...] = ()

    def type(self, text: str) -> None:
        raise NotImplementedError

    def key(self, key: str, repeat: int = 1) -> None:
        """key is xdotool-style: "Return", "BackSpace", "ctrl+u"."""
        raise NotImplementedError


class Xdotool(Keyboard):
    """X11 (XTEST)."""
    tools = (Tool("xdotool"),)

    def type(self, text: str) -> None:
        subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "4", "--", text], check=True)

    def key(self, key: str, repeat: int = 1) -> None:
        subprocess.run(["xdotool", "key", "--clearmodifiers", "--repeat", str(repeat), "--repeat-delay", "2", key],
                       check=True)


class Wtype(Keyboard):
    """Wayland virtual-keyboard protocol (Hyprland, Sway, other wlroots
    compositors; not GNOME or KDE)."""
    tools = (Tool("wtype"),)

    def type(self, text: str) -> None:
        subprocess.run(["wtype", "-d", "4", "--", text], check=True)

    def key(self, key: str, repeat: int = 1) -> None:
        *mods, name = key.split("+")   # "ctrl+u" -> wtype -M ctrl -k u -m ctrl
        cmd = ["wtype", "-d", "2"]
        for m in mods:
            cmd += ["-M", m]
        cmd += ["-k", name] * repeat
        for m in reversed(mods):
            cmd += ["-m", m]
        subprocess.run(cmd, check=True)


# -------------------------------------------------------------------- windows
class Windows:
    """No window tracking: eve types wherever the focus is."""
    tools: tuple[Tool, ...] = ()
    tracks = False

    def active(self) -> str | None:
        return None

    def title(self, w: str) -> str:
        return "?"

    def focus(self, w: str) -> None:
        """Focus window w and return once it has the focus (or gave up)."""

    def _wait_for(self, w: str, seconds: float = 0.5) -> None:
        end = time.monotonic() + seconds
        while self.active() != w and time.monotonic() < end:
            time.sleep(0.025)


class XdotoolWindows(Windows):
    tools = (Tool("xdotool"),)
    tracks = True

    def active(self) -> str | None:
        r = _run(["xdotool", "getactivewindow"])
        return (r.stdout.strip() or None) if r else None

    def title(self, w: str) -> str:
        r = _run(["xdotool", "getwindowname", w])
        return r.stdout.strip() if r else "?"

    def focus(self, w: str) -> None:
        _run(["xdotool", "windowactivate", "--sync", w], timeout=3)


class HyprlandWindows(Windows):
    """Windows are identified by their address, e.g. 0x562fc855b840."""
    tools = (Tool("hyprctl", bundled=True),)
    tracks = True

    def _json(self, what: str):
        r = _run(["hyprctl", "-j", what])
        try:
            return json.loads(r.stdout) if r and r.returncode == 0 else None
        except ValueError:
            return None

    def active(self) -> str | None:
        w = self._json("activewindow")
        return w.get("address") if isinstance(w, dict) else None

    def title(self, w: str) -> str:
        for c in self._json("clients") or []:
            if c.get("address") == w:
                return c.get("title", "?")
        return "?"

    def focus(self, w: str) -> None:
        # Hyprland 0.55+ takes Lua dispatches; older versions the classic form.
        for cmd in (["hyprctl", "dispatch", f'hl.dsp.focus({{ window = "address:{w}" }})'],
                    ["hyprctl", "dispatch", "focuswindow", f"address:{w}"]):
            r = _run(cmd, timeout=3)
            if r and r.stdout.strip() == "ok":
                self._wait_for(w)
                return


class SwayWindows(Windows):
    """Windows are identified by their con_id."""
    tools = (Tool("swaymsg", bundled=True),)
    tracks = True

    def _nodes(self) -> Iterator[dict]:
        r = _run(["swaymsg", "-r", "-t", "get_tree"])
        try:
            stack = [json.loads(r.stdout)] if r and r.returncode == 0 else []
        except ValueError:
            return
        while stack:
            n = stack.pop()
            yield n
            stack += n.get("nodes", []) + n.get("floating_nodes", [])

    def active(self) -> str | None:
        for n in self._nodes():
            if n.get("focused") and n.get("type") in ("con", "floating_con"):
                return str(n["id"])
        return None

    def title(self, w: str) -> str:
        for n in self._nodes():
            if str(n.get("id")) == w:
                return n.get("name") or "?"
        return "?"

    def focus(self, w: str) -> None:
        r = _run(["swaymsg", "-r", f"[con_id={w}]", "focus"], timeout=3)
        try:
            ok = bool(r) and all(x.get("success") for x in json.loads(r.stdout))
        except (ValueError, AttributeError, TypeError):
            ok = False
        if ok:
            self._wait_for(w)


# ------------------------------------------------------------------- desktops
@dataclass
class Desktop:
    name: str                                   # for --desktop
    label: str                                  # for people
    matches: Callable[[Mapping[str, str]], bool]
    keyboard: Keyboard | None
    windows: Windows = field(default_factory=Windows)
    unsupported: str | None = None              # why eve can't type here
    # How to run a command at login from this desktop's own config, for
    # desktops that may not run XDG autostart entries (~/.config/autostart).
    autostart: Callable[[str], str] | None = None

    @property
    def tools(self) -> list[Tool]:
        seen: dict[str, Tool] = {}
        for t in (self.keyboard.tools if self.keyboard else ()) + self.windows.tools:
            seen.setdefault(t.command, t)
        return list(seen.values())

    def missing(self) -> list[Tool]:
        return [t for t in self.tools if not t.present()]

    def describe(self) -> str:
        if self.unsupported:
            return f"{self.label}: not supported yet ({self.unsupported})"
        how = f"types with {self.keyboard.tools[0].command}"
        if self.windows.tracks:
            how += f", switches windows with {self.windows.tools[0].command}"
        else:
            how += "; can't switch windows, so it types wherever the focus is"
        return f"{self.label}: {how}"


def _desktop_is(env: Mapping[str, str], *names: str) -> bool:
    current = env.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
    return any(n in current for n in names)


def _hyprland_autostart(cmd: str) -> str:
    lua = os.path.expanduser("~/.config/hypr/hyprland.lua")
    if os.path.exists(lua):
        return f'add to {lua}:\n    hl.on("hyprland.start", function() hl.exec_cmd("{cmd}") end)'
    return f"add to ~/.config/hypr/hyprland.conf:\n    exec-once = {cmd}"


_NO_VKBD = "it doesn't offer the virtual-keyboard protocol wtype uses; log in to an X11 session for now"

DESKTOPS: list[Desktop] = [
    Desktop("hyprland", "Hyprland", lambda e: bool(e.get("HYPRLAND_INSTANCE_SIGNATURE")),
            Wtype(), HyprlandWindows(), autostart=_hyprland_autostart),
    Desktop("sway", "Sway", lambda e: bool(e.get("SWAYSOCK")),
            Wtype(), SwayWindows(), autostart=lambda cmd: f"add to ~/.config/sway/config:\n    exec {cmd}"),
    Desktop("gnome-wayland", "GNOME on Wayland",
            lambda e: bool(e.get("WAYLAND_DISPLAY")) and _desktop_is(e, "gnome"),
            None, unsupported=_NO_VKBD),
    Desktop("kde-wayland", "KDE Plasma on Wayland",
            lambda e: bool(e.get("WAYLAND_DISPLAY")) and _desktop_is(e, "kde"),
            None, unsupported=_NO_VKBD),
    Desktop("wayland", "Wayland (wlroots)", lambda e: bool(e.get("WAYLAND_DISPLAY")),
            Wtype()),
    Desktop("x11", "X11", lambda e: bool(e.get("DISPLAY")),
            Xdotool(), XdotoolWindows()),
]


def by_name(name: str) -> Desktop:
    for d in DESKTOPS:
        if d.name == name:
            return d
    raise KeyError(f"unknown desktop {name!r}; choose from: {', '.join(d.name for d in DESKTOPS)}")


def detect(env: Mapping[str, str] | None = None) -> Desktop | None:
    """The desktop this session runs, from its environment. EVE_DESKTOP
    (a name from DESKTOPS) overrides the detection."""
    env = os.environ if env is None else env
    if env.get("EVE_DESKTOP"):
        return by_name(env["EVE_DESKTOP"])
    return next((d for d in DESKTOPS if d.matches(env)), None)


def main() -> int:
    ap = argparse.ArgumentParser(description="Show how eve will type on this desktop.")
    ap.add_argument("--desktop", choices=[d.name for d in DESKTOPS], help="use this desktop instead of detecting it")
    ap.add_argument("--packages", metavar="MANAGER",
                    help="print the missing packages to install with MANAGER (apt, pacman, dnf, zypper)")
    ap.add_argument("--name", action="store_true", help="print the detected desktop's name")
    ap.add_argument("--autostart", metavar="CMD",
                    help="print how to run CMD at login from this desktop's config, if it has a way")
    args = ap.parse_args()
    try:
        d = by_name(args.desktop) if args.desktop else detect()
    except KeyError as e:
        print(e.args[0], file=sys.stderr)
        return 2
    if d is None:
        print("no desktop session found (neither WAYLAND_DISPLAY nor DISPLAY is set)", file=sys.stderr)
        return 1
    if args.name:
        print(d.name)
        return 0
    if args.autostart:
        if d.autostart:
            print(d.autostart(args.autostart))
        return 0
    if args.packages:
        for t in d.missing():
            if t.bundled:
                print(f"{t.command} is missing, though it normally comes with {d.label}", file=sys.stderr)
        print(" ".join(t.package(args.packages) for t in d.missing() if not t.bundled))
        return 1 if d.unsupported else 0
    print(d.describe())
    for t in d.tools:
        print(f"  {t.command:10} {'ok' if t.present() else 'MISSING'}")
    return 1 if d.unsupported or d.missing() else 0


if __name__ == "__main__":
    sys.exit(main())
