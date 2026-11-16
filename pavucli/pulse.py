"""Thin backend around ``pactl`` (uses its JSON output, libpulse >= 16).

Talking to the server through ``pactl`` keeps pavucli free of third-party
Python dependencies and works the same on PulseAudio and PipeWire
(pipewire-pulse).
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import threading
from dataclasses import dataclass, field

VOLUME_NORM = 65536  # PA_VOLUME_NORM, i.e. 100 %

SINK = "sink"
SOURCE = "source"
SINK_INPUT = "sink-input"
SOURCE_OUTPUT = "source-output"
CARD = "card"


def _make_env() -> dict[str, str]:
    """Environment for pactl: keep the user's language, force '.' decimals.

    Some pactl versions print locale-formatted floats inside their JSON
    output, which would break parsing with e.g. de_DE.
    """
    env = dict(os.environ)
    lc_all = env.pop("LC_ALL", None)
    if lc_all:
        env.setdefault("LANG", lc_all)
        env["LC_CTYPE"] = lc_all
    env["LC_NUMERIC"] = "C"
    return env


_ENV = _make_env()


class PulseError(RuntimeError):
    """Raised when pactl fails or the server cannot be reached."""


def pactl(*args: str, json_out: bool = False):
    cmd = ["pactl", *(("-f", "json") if json_out else ()), *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            env=_ENV,
        )
    except FileNotFoundError as exc:
        raise PulseError("pactl not found - install libpulse (Arch) / pulseaudio-utils (Debian)") from exc
    except subprocess.TimeoutExpired as exc:
        raise PulseError(f"pactl timed out ({' '.join(args)})") from exc
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip() or f"pactl exited with {proc.returncode}"
        raise PulseError(msg.splitlines()[-1])
    if json_out:
        try:
            return json.loads(proc.stdout or "null")
        except json.JSONDecodeError as exc:
            raise PulseError(f"cannot parse pactl output ({exc}); pactl >= 16 is required") from exc
    return proc.stdout


# --------------------------------------------------------------------------
# Volume math (pure functions, unit tested)
# --------------------------------------------------------------------------


def channel_side(name: str, index: int, count: int) -> int:
    """-1 for left channels, 1 for right channels, 0 for center/LFE/mono."""
    if "left" in name:
        return -1
    if "right" in name:
        return 1
    # PipeWire apps often use aux0/aux1 for plain stereo
    if count == 2 and name.startswith("aux"):
        return -1 if index == 0 else 1
    return 0


def _sides(channels: list[str]) -> list[int]:
    return [channel_side(c, i, len(channels)) for i, c in enumerate(channels)]


def _side_avgs(channels: list[str], volumes: list[int]) -> tuple[float, float] | None:
    sides = _sides(channels)
    left = [v for v, s in zip(volumes, sides) if s < 0]
    right = [v for v, s in zip(volumes, sides) if s > 0]
    if not left or not right:
        return None
    return sum(left) / len(left), sum(right) / len(right)


def get_balance(channels: list[str], volumes: list[int]) -> float | None:
    """Balance in [-1, 1] (negative = left), same definition as libpulse.

    Returns None when the channel map has no left/right pair.
    """
    avgs = _side_avgs(channels, volumes)
    if avgs is None:
        return None
    left, right = avgs
    if left == right:
        return 0.0
    if left > right:
        return -1.0 + right / left
    return 1.0 - left / right


def set_balance(channels: list[str], volumes: list[int], balance: float) -> list[int]:
    """Return new per-channel volumes with the given balance applied.

    The louder side keeps its level, the other side is attenuated,
    center channels are untouched (mirrors pa_cvolume_set_balance).
    """
    avgs = _side_avgs(channels, volumes)
    if avgs is None:
        return list(volumes)
    balance = max(-1.0, min(1.0, balance))
    left, right = avgs
    top = max(left, right)
    if balance <= 0:
        new_left, new_right = top, top * (1.0 + balance)
    else:
        new_left, new_right = top * (1.0 - balance), top
    out = []
    for v, side in zip(volumes, _sides(channels)):
        if side < 0:
            out.append(round(v * new_left / left) if left else round(new_left))
        elif side > 0:
            out.append(round(v * new_right / right) if right else round(new_right))
        else:
            out.append(v)
    return out


def scale_volumes(volumes: list[int], level: int) -> list[int]:
    """Set the loudest channel to ``level`` while keeping channel ratios."""
    level = max(0, int(level))
    top = max(volumes, default=0)
    if top == 0:
        return [level] * len(volumes)
    return [round(v * level / top) for v in volumes]


def volume_to_db(value: int) -> float:
    """Software volume -> dB using PulseAudio's cubic mapping."""
    if value <= 0:
        return float("-inf")
    return 60.0 * math.log10(value / VOLUME_NORM)


def pct(value: int) -> int:
    return round(value * 100 / VOLUME_NORM)


def from_pct(percent: float) -> int:
    return max(0, round(percent * VOLUME_NORM / 100))


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class Port:
    name: str
    description: str
    available: bool = True


@dataclass
class Item:
    """A sink, source, sink-input or source-output."""

    kind: str
    index: int
    name: str
    title: str
    subtitle: str = ""
    channels: list[str] = field(default_factory=list)
    volumes: list[int] = field(default_factory=list)
    mute: bool = False
    is_default: bool = False
    is_monitor: bool = False
    ports: list[Port] = field(default_factory=list)
    active_port: str | None = None
    parent: int | None = None  # sink/source index a stream is connected to
    corked: bool = False
    state: str = ""

    @property
    def ident(self) -> str:
        """Identifier accepted by pactl."""
        if self.kind in (SINK, SOURCE):
            return self.name
        return str(self.index)

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.ident)

    @property
    def volume(self) -> int:
        return max(self.volumes, default=0)

    @property
    def balance(self) -> float | None:
        return get_balance(self.channels, self.volumes)

    @property
    def port_description(self) -> str | None:
        for p in self.ports:
            if p.name == self.active_port:
                return p.description
        return self.active_port


@dataclass
class Profile:
    name: str
    description: str
    available: bool = True
    priority: int = 0


@dataclass
class Card:
    index: int
    name: str
    title: str
    profiles: list[Profile] = field(default_factory=list)
    active_profile: str | None = None
    kind: str = CARD

    @property
    def ident(self) -> str:
        return self.name

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.name)

    @property
    def profile_description(self) -> str:
        for p in self.profiles:
            if p.name == self.active_profile:
                return p.description
        return self.active_profile or "-"


@dataclass
class Snapshot:
    server: str = ""
    default_sink: str = ""
    default_source: str = ""
    sinks: list[Item] = field(default_factory=list)
    sources: list[Item] = field(default_factory=list)
    sink_inputs: list[Item] = field(default_factory=list)
    source_outputs: list[Item] = field(default_factory=list)
    cards: list[Card] = field(default_factory=list)

    def items(self, kind: str) -> list:
        return {
            SINK: self.sinks,
            SOURCE: self.sources,
            SINK_INPUT: self.sink_inputs,
            SOURCE_OUTPUT: self.source_outputs,
            CARD: self.cards,
        }[kind]

    def device(self, kind: str, index: int | None) -> Item | None:
        for dev in self.sinks if kind == SINK else self.sources:
            if dev.index == index:
                return dev
        return None


def _parse_volume(obj: dict) -> tuple[list[str], list[int]]:
    vol = obj.get("volume") or {}
    cmap = [c for c in str(obj.get("channel_map") or "").split(",") if c]
    if not cmap or any(c not in vol for c in cmap):
        cmap = list(vol.keys())
    volumes = []
    for c in cmap:
        v = vol.get(c) or {}
        try:
            volumes.append(int(v.get("value", 0)))
        except (TypeError, ValueError):
            volumes.append(0)
    return cmap, volumes


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_device(kind: str, d: dict, default_name: str) -> Item:
    channels, volumes = _parse_volume(d)
    props = d.get("properties") or {}
    is_monitor = False
    if kind == SOURCE:
        # pactl prints a source's "monitor of sink" under various keys
        mon = d.get("monitor_of_sink", d.get("monitor_source"))
        is_monitor = (
            props.get("device.class") == "monitor"
            or (mon not in (None, "", "n/a"))
            or str(d.get("name", "")).endswith(".monitor")
        )
    ports = [
        Port(
            name=p.get("name", ""),
            description=p.get("description") or p.get("name", ""),
            available=p.get("availability") != "not available",
        )
        for p in d.get("ports") or []
    ]
    return Item(
        kind=kind,
        index=_int(d.get("index"), -1),
        name=d.get("name", ""),
        title=d.get("description") or d.get("name", "?"),
        subtitle=props.get("device.product.name") or "",
        channels=channels,
        volumes=volumes,
        mute=bool(d.get("mute")),
        is_default=d.get("name") == default_name,
        is_monitor=is_monitor,
        ports=ports if not is_monitor else [],
        active_port=d.get("active_port") if not is_monitor else None,
        state=str(d.get("state") or ""),
    )


def _parse_stream(kind: str, s: dict) -> Item:
    channels, volumes = _parse_volume(s)
    props = s.get("properties") or {}
    title = (
        props.get("application.name")
        or props.get("node.description")
        or props.get("node.name")
        or props.get("application.process.binary")
        or f"Stream #{s.get('index')}"
    )
    subtitle = props.get("media.name") or ""
    if subtitle == title:
        subtitle = ""
    parent = _int(s.get("sink" if kind == SINK_INPUT else "source"))
    return Item(
        kind=kind,
        index=_int(s.get("index"), -1),
        name=str(s.get("index")),
        title=title,
        subtitle=subtitle,
        channels=channels,
        volumes=volumes,
        mute=bool(s.get("mute")),
        parent=parent,
        corked=bool(s.get("corked")),
    )


def _is_hidden_stream(s: dict) -> bool:
    props = s.get("properties") or {}
    # pavucontrol / other mixers' level meters
    if props.get("media.name") == "Peak detect":
        return True
    return props.get("application.id") == "org.PulseAudio.pavucontrol" and "peak" in str(
        props.get("media.name", "")
    ).lower()


def _parse_card(c: dict) -> Card:
    props = c.get("properties") or {}
    profiles_raw = c.get("profiles") or {}
    if isinstance(profiles_raw, dict):
        items = profiles_raw.items()
    else:  # defensive: list of dicts
        items = [(p.get("name", ""), p) for p in profiles_raw]
    profiles = [
        Profile(
            name=name,
            description=p.get("description") or name,
            available=bool(p.get("available", True)),
            priority=_int(p.get("priority"), 0),
        )
        for name, p in items
    ]
    profiles.sort(key=lambda p: -p.priority)
    return Card(
        index=_int(c.get("index"), -1),
        name=c.get("name", ""),
        title=props.get("device.description") or props.get("alsa.card_name") or c.get("name", "?"),
        profiles=profiles,
        active_profile=c.get("active_profile"),
    )


def fetch() -> Snapshot:
    info = pactl("info", json_out=True) or {}
    default_sink = info.get("default_sink_name") or ""
    default_source = info.get("default_source_name") or ""
    snap = Snapshot(
        server=f"{info.get('server_name', '?')}",
        default_sink=default_sink,
        default_source=default_source,
    )
    snap.sinks = [_parse_device(SINK, d, default_sink) for d in pactl("list", "sinks", json_out=True) or []]
    snap.sources = [
        _parse_device(SOURCE, d, default_source) for d in pactl("list", "sources", json_out=True) or []
    ]
    snap.sink_inputs = [
        _parse_stream(SINK_INPUT, s)
        for s in pactl("list", "sink-inputs", json_out=True) or []
        if not _is_hidden_stream(s)
    ]
    snap.source_outputs = [
        _parse_stream(SOURCE_OUTPUT, s)
        for s in pactl("list", "source-outputs", json_out=True) or []
        if not _is_hidden_stream(s)
    ]
    snap.cards = [_parse_card(c) for c in pactl("list", "cards", json_out=True) or []]
    for lst in (snap.sinks, snap.sources, snap.sink_inputs, snap.source_outputs, snap.cards):
        lst.sort(key=lambda x: x.index)
    return snap


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


def set_volume(item: Item, volumes: list[int]) -> None:
    pactl(f"set-{item.kind}-volume", item.ident, *(str(int(v)) for v in volumes))


def set_mute(item: Item, mute: bool) -> None:
    pactl(f"set-{item.kind}-mute", item.ident, "1" if mute else "0")


def set_default(item: Item) -> None:
    pactl(f"set-default-{item.kind}", item.name)


def set_port(item: Item, port: str) -> None:
    pactl(f"set-{item.kind}-port", item.ident, port)


def move_stream(item: Item, target: Item) -> None:
    pactl(f"move-{item.kind}", item.ident, target.name)


def set_card_profile(card: Card, profile: str) -> None:
    pactl("set-card-profile", card.name, profile)


# --------------------------------------------------------------------------
# Change notifications
# --------------------------------------------------------------------------


class Subscriber(threading.Thread):
    """Runs ``pactl subscribe`` and sets ``event`` whenever something changes."""

    _RELEVANT = ("sink", "source", "card", "server")  # also matches sink-input/source-output

    def __init__(self, event: threading.Event):
        super().__init__(name="pactl-subscribe", daemon=True)
        self.event = event
        self._stop_flag = threading.Event()
        self._proc: subprocess.Popen | None = None

    def run(self) -> None:
        while not self._stop_flag.is_set():
            try:
                self._proc = subprocess.Popen(
                    ["pactl", "subscribe"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=_ENV,
                )
            except OSError:
                self._stop_flag.wait(2)
                continue
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                # "Event 'change' on sink-input #42" - ignore client/module
                # churn (every pactl call we make creates/removes a client).
                target = line.rsplit(" on ", 1)[-1]
                if target.startswith(self._RELEVANT):
                    self.event.set()
            self._proc.wait()
            if self._stop_flag.is_set():
                break
            # server went away (restart?) - poll again shortly
            self.event.set()
            self._stop_flag.wait(1)

    def stop(self) -> None:
        self._stop_flag.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._proc.kill()


__all__ = [
    "VOLUME_NORM",
    "SINK",
    "SOURCE",
    "SINK_INPUT",
    "SOURCE_OUTPUT",
    "CARD",
    "PulseError",
    "Item",
    "Card",
    "Port",
    "Profile",
    "Snapshot",
    "fetch",
    "get_balance",
    "set_balance",
    "scale_volumes",
    "volume_to_db",
    "pct",
    "from_pct",
    "set_volume",
    "set_mute",
    "set_default",
    "set_port",
    "move_stream",
    "set_card_profile",
    "Subscriber",
]
