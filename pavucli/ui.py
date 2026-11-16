"""curses user interface."""

from __future__ import annotations

import curses
import threading
import time
import unicodedata
from dataclasses import dataclass, field

from . import __version__, pulse
from .pulse import CARD, SINK, SINK_INPUT, SOURCE, SOURCE_OUTPUT, Card, Item, PulseError

TABS = [
    ("Playback", "Play", SINK_INPUT),
    ("Recording", "Rec", SOURCE_OUTPUT),
    ("Output Devices", "Out", SINK),
    ("Input Devices", "In", SOURCE),
    ("Configuration", "Cfg", CARD),
]
TAB_NAMES = {"playback": 0, "recording": 1, "output": 2, "input": 3, "config": 4}

EMPTY_TEXT = {
    SINK_INPUT: "No application is playing audio.",
    SOURCE_OUTPUT: "No application is recording audio.",
    SINK: "No output devices.",
    SOURCE: "No input devices.",
    CARD: "No sound cards.",
}

HELP = [
    ("Navigation", ""),
    ("Tab  Shift-Tab", "next / previous tab"),
    ("F1 … F5", "jump to tab"),
    ("↑ ↓  k j", "select item"),
    ("Home End  g G", "first / last item"),
    ("Volume", ""),
    ("← →  h l", "volume -/+ step"),
    ("H L  Shift-← →", "volume -/+ 1 %"),
    ("PgDn PgUp", "volume -/+ 10 %"),
    ("1 … 9, 0", "set 10 % … 90 %, 100 %"),
    ("m", "toggle mute"),
    ("[ ]", "balance left / right"),
    ("b  =", "center balance"),
    ("c  Space", "lock / unlock channels"),
    ("Devices & streams", ""),
    ("Enter", "move stream / port / profile"),
    ("p", "choose port"),
    ("d", "set as default device"),
    ("M", "show / hide monitor sources"),
    ("Other", ""),
    ("mouse", "click / wheel on bars"),
    ("?", "this help"),
    ("q  Esc", "quit"),
]

# colour pair ids
(
    C_HEAD,
    C_TAB_ON,
    C_TAB_OFF,
    C_OK,
    C_OVER,
    C_MUTED,
    C_DIM,
    C_ACCENT,
    C_ERR,
    C_WARN,
    C_POPUP,
) = range(1, 12)


@dataclass
class Options:
    tab: int = 0
    step: int = 5
    max_volume: int = 150
    ascii: bool = False
    mouse: bool = True
    color: bool = True


@dataclass
class Row:
    obj: Item | Card
    channel: int | None = None

    @property
    def key(self):
        return (*self.obj.key, self.channel)


@dataclass
class Line:
    segments: list[tuple[str, int]]
    row: int | None = None  # index into rows
    bar: tuple[int, int] | None = None  # (x0, width) of a clickable volume bar
    block: int = 0  # index of the object block this line belongs to


@dataclass
class Glyphs:
    full: str
    partial: list[str]
    empty: str
    mark100: str
    sel: str
    sub_sel: str
    knob: str
    track: str
    center: str
    hline: str
    ellipsis: str
    star: str
    arrow: str


UNICODE = Glyphs(
    full="█",
    partial=["", "▏", "▎", "▍", "▌", "▋", "▊", "▉"],
    empty="─",
    mark100="┊",
    sel="▶",
    sub_sel="▸",
    knob="●",
    track="─",
    center="┼",
    hline="─",
    ellipsis="…",
    star="★",
    arrow="→",
)
ASCII = Glyphs(
    full="#",
    partial=[""] * 8,
    empty="-",
    mark100="|",
    sel=">",
    sub_sel=">",
    knob="o",
    track="-",
    center="+",
    hline="-",
    ellipsis="~",
    star="*",
    arrow="->",
)


# --------------------------------------------------------------------------
# text width helpers (wide CJK / emoji characters take two cells)
# --------------------------------------------------------------------------


def cwidth(ch: str) -> int:
    if unicodedata.combining(ch) or ch in "​‍️":
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def swidth(s: str) -> int:
    return sum(cwidth(c) for c in s)


def trunc(s: str, width: int, ellipsis: str = "…") -> str:
    if width <= 0:
        return ""
    if swidth(s) <= width:
        return s
    out, used = [], 0
    for ch in s:
        w = cwidth(ch)
        if used + w > width - swidth(ellipsis):
            break
        out.append(ch)
        used += w
    return "".join(out) + ellipsis


def channel_label(name: str) -> str:
    special = {"mono": "M", "lfe": "LFE", "subwoofer": "LFE"}
    if name in special:
        return special[name]
    if name.startswith("aux"):
        return "A" + name[3:]
    return "".join(part[0] for part in name.split("-") if part).upper()


# --------------------------------------------------------------------------
# the application
# --------------------------------------------------------------------------


class App:
    def __init__(self, scr, opts: Options):
        self.scr = scr
        self.opts = opts
        self.g = ASCII if opts.ascii else UNICODE
        self.tab = opts.tab
        self.snap = pulse.Snapshot()
        self.sel_key: list[tuple | None] = [None] * len(TABS)
        self.sel_idx = [0] * len(TABS)
        self.scroll = [0] * len(TABS)
        self.unlocked: set[tuple[str, str]] = set()
        self.show_monitors = False
        self.status = ""
        self.status_err = False
        self.status_until = 0.0
        self.lines: list[Line] = []
        self.rows: list[Row] = []
        self.tab_ranges: list[tuple[int, int, int]] = []
        self.body_top = 3
        self.event = threading.Event()
        self.event.set()
        self.last_refresh = 0.0
        self.running = True
        self.dirty = True
        self.sub = pulse.Subscriber(self.event)

    # ------------------------------------------------------------ helpers

    @property
    def max_raw(self) -> int:
        return pulse.from_pct(self.opts.max_volume)

    def flash(self, msg: str, err: bool = False, secs: float = 4.0) -> None:
        self.status = msg
        self.status_err = err
        self.status_until = time.monotonic() + secs
        self.dirty = True

    def attr(self, pair: int, extra: int = 0) -> int:
        if self.opts.color and curses.has_colors():
            return curses.color_pair(pair) | extra
        fallback = {C_TAB_ON: curses.A_REVERSE, C_HEAD: curses.A_REVERSE, C_POPUP: 0}
        return fallback.get(pair, 0) | extra

    def put(self, win, y: int, x: int, text: str, attr: int = 0, maxw: int | None = None) -> int:
        """Draw clipped text; returns the number of cells written."""
        h, w = win.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return 0
        room = w - x if maxw is None else min(maxw, w - x)
        text = trunc(text, room, "") if swidth(text) > room else text
        try:
            win.addstr(y, x, text, attr)
        except curses.error:
            pass  # writing the bottom-right cell raises; harmless
        return swidth(text)

    def objects(self, tab: int) -> list:
        kind = TABS[tab][2]
        objs = self.snap.items(kind)
        if kind == SOURCE and not self.show_monitors:
            objs = [o for o in objs if not o.is_monitor]
        return objs

    def build_rows(self, tab: int) -> list[Row]:
        rows = []
        for obj in self.objects(tab):
            rows.append(Row(obj))
            if isinstance(obj, Item) and obj.key in self.unlocked and len(obj.channels) > 1:
                rows.extend(Row(obj, i) for i in range(len(obj.channels)))
        return rows

    def current(self) -> Row | None:
        rows = self.build_rows(self.tab)
        idx = self.resolve_selection(self.tab, rows)
        return rows[idx] if rows else None

    def resolve_selection(self, tab: int, rows: list[Row]) -> int:
        if not rows:
            self.sel_idx[tab] = 0
            return 0
        key = self.sel_key[tab]
        if key is not None:
            for i, r in enumerate(rows):
                if r.key == key:
                    self.sel_idx[tab] = i
                    return i
            # channel row vanished (channels locked again): fall back to its item
            for i, r in enumerate(rows):
                if r.key[:2] == key[:2]:
                    self.sel_idx[tab] = i
                    self.sel_key[tab] = r.key
                    return i
        idx = max(0, min(self.sel_idx[tab], len(rows) - 1))
        self.sel_idx[tab] = idx
        self.sel_key[tab] = rows[idx].key
        return idx

    def select(self, idx: int) -> None:
        rows = self.build_rows(self.tab)
        if not rows:
            return
        idx = max(0, min(idx, len(rows) - 1))
        self.sel_idx[self.tab] = idx
        self.sel_key[self.tab] = rows[idx].key
        self.dirty = True

    def refresh(self) -> None:
        self.event.clear()
        self.last_refresh = time.monotonic()
        try:
            self.snap = pulse.fetch()
        except PulseError as exc:
            self.flash(f"Cannot talk to the sound server: {exc}", err=True, secs=3600)
        else:
            if self.status_err and self.status.startswith("Cannot talk"):
                self.status_until = 0
        self.dirty = True

    def do(self, fn, *args) -> bool:
        try:
            fn(*args)
        except PulseError as exc:
            self.flash(str(exc), err=True)
            return False
        self.event.set()
        self.dirty = True
        return True

    # ------------------------------------------------------------ actions

    def change_volume(self, delta: float | None = None, absolute: float | None = None, row: Row | None = None):
        row = row or self.current()
        if row is None or not isinstance(row.obj, Item):
            return
        item = row.obj
        if not item.volumes:
            self.flash("This stream has no adjustable volume.")
            return
        vols = list(item.volumes)
        cur = item.volume if row.channel is None else vols[row.channel]
        if absolute is not None:
            target = pulse.from_pct(absolute)
        else:
            target = pulse.from_pct(pulse.pct(cur) + delta)
            if delta > 0 and target > self.max_raw:
                target = max(self.max_raw, cur)  # never jump down when already above max
        target = max(0, target)
        if row.channel is None:
            new = pulse.scale_volumes(vols, target)
        else:
            vols[row.channel] = target
            new = vols
        if new != item.volumes and self.do(pulse.set_volume, item, new):
            item.volumes = new  # optimistic, so repeated keys accumulate

    def toggle_mute(self):
        row = self.current()
        if row and isinstance(row.obj, Item) and self.do(pulse.set_mute, row.obj, not row.obj.mute):
            row.obj.mute = not row.obj.mute

    def change_balance(self, delta: float | None):
        row = self.current()
        if row is None or not isinstance(row.obj, Item):
            return
        item = row.obj
        bal = item.balance
        if bal is None:
            self.flash("Balance needs a left and a right channel.")
            return
        target = 0.0 if delta is None else round((bal + delta) * 20) / 20
        new = pulse.set_balance(item.channels, item.volumes, target)
        if new != item.volumes and self.do(pulse.set_volume, item, new):
            item.volumes = new

    def toggle_lock(self):
        row = self.current()
        if row is None or not isinstance(row.obj, Item):
            return
        item = row.obj
        if len(item.channels) < 2:
            self.flash("Only one channel.")
            return
        if item.key in self.unlocked:
            self.unlocked.discard(item.key)
            self.sel_key[self.tab] = (*item.key, None)
        else:
            self.unlocked.add(item.key)
        self.dirty = True

    def set_default(self):
        row = self.current()
        if row is None or not isinstance(row.obj, Item) or row.obj.kind not in (SINK, SOURCE):
            self.flash("Select an output or input device to make it the default.")
            return
        if self.do(pulse.set_default, row.obj):
            self.flash(f"Default {'output' if row.obj.kind == SINK else 'input'}: {row.obj.title}")

    def choose_port(self):
        row = self.current()
        if row is None or not isinstance(row.obj, Item) or row.obj.kind not in (SINK, SOURCE):
            return
        item = row.obj
        if not item.ports:
            self.flash("This device has no ports to choose from.")
            return
        opts = [(p.description, p.name, "" if p.available else "unplugged") for p in item.ports]
        choice = self.pick(f"Port of {item.title}", opts, item.active_port)
        if choice is not None and choice != item.active_port:
            self.do(pulse.set_port, item, choice)

    def move_stream(self):
        row = self.current()
        if row is None or not isinstance(row.obj, Item):
            return
        item = row.obj
        dev_kind = SINK if item.kind == SINK_INPUT else SOURCE
        devices = self.snap.sinks if dev_kind == SINK else self.snap.sources
        opts = [
            (d.title, d.index, "default" if d.is_default else ("monitor" if d.is_monitor else ""))
            for d in devices
        ]
        verb = "Play" if dev_kind == SINK else "Record"
        choice = self.pick(f"{verb} {item.title} on", opts, item.parent)
        if choice is not None and choice != item.parent:
            target = self.snap.device(dev_kind, choice)
            if target:
                self.do(pulse.move_stream, item, target)

    def choose_profile(self):
        row = self.current()
        if row is None or not isinstance(row.obj, Card):
            return
        card = row.obj
        opts = [(p.description, p.name, "" if p.available else "unavailable") for p in card.profiles]
        choice = self.pick(f"Profile of {card.title}", opts, card.active_profile)
        if choice is not None and choice != card.active_profile:
            self.do(pulse.set_card_profile, card, choice)

    def activate(self):
        kind = TABS[self.tab][2]
        if kind in (SINK_INPUT, SOURCE_OUTPUT):
            self.move_stream()
        elif kind in (SINK, SOURCE):
            self.choose_port()
        else:
            self.choose_profile()

    def switch_tab(self, tab: int):
        self.tab = tab % len(TABS)
        self.dirty = True

    # ------------------------------------------------------------ popups

    def pick(self, title: str, options: list[tuple[str, object, str]], current=None):
        """Modal list picker; returns the chosen value or None."""
        if not options:
            return None
        idx = next((i for i, o in enumerate(options) if o[1] == current), 0)
        top = 0
        while True:
            H, W = self.scr.getmaxyx()
            label_w = max(swidth(o[0]) + (swidth(o[2]) + 3 if o[2] else 0) for o in options)
            w = max(30, min(W - 4, max(label_w + 8, swidth(title) + 6)))
            h = max(5, min(H - 2, len(options) + 4))
            visible = h - 4
            if idx < top:
                top = idx
            elif idx >= top + visible:
                top = idx - visible + 1
            y0, x0 = max(0, (H - h) // 2), max(0, (W - w) // 2)
            try:
                win = curses.newwin(h, w, y0, x0)
            except curses.error:
                return None
            win.keypad(True)
            win.bkgd(" ", self.attr(C_POPUP))
            win.erase()
            win.border()
            self.put(win, 0, 2, f" {trunc(title, w - 6, self.g.ellipsis)} ", self.attr(C_POPUP, curses.A_BOLD))
            for i in range(visible):
                n = top + i
                if n >= len(options):
                    break
                label, value, note = options[n]
                selected = n == idx
                a = self.attr(C_POPUP, curses.A_REVERSE if selected else 0)
                mark = self.g.knob if value == current else " "
                text = f" {mark} {label}"
                x = 1
                self.put(win, 2 + i, x, " " * (w - 2), a)
                x += self.put(win, 2 + i, x, trunc(text, w - 4 - (swidth(note) + 2 if note else 0), self.g.ellipsis), a)
                if note:
                    self.put(win, 2 + i, w - 2 - swidth(note) - 1, note, a | curses.A_DIM)
            self.put(win, h - 1, 2, " ⏎ select · Esc cancel " if not self.opts.ascii else " Enter select, Esc cancel ", self.attr(C_POPUP, curses.A_DIM))
            win.refresh()
            try:
                key = win.get_wch()
            except curses.error:
                continue
            if key in (curses.KEY_UP, "k"):
                idx = (idx - 1) % len(options)
            elif key in (curses.KEY_DOWN, "j", "\t"):
                idx = (idx + 1) % len(options)
            elif key in (curses.KEY_HOME, "g"):
                idx = 0
            elif key in (curses.KEY_END, "G"):
                idx = len(options) - 1
            elif key in ("\n", "\r", curses.KEY_ENTER, " "):
                self.dirty = True
                return options[idx][1]
            elif key in ("\x1b", "q", curses.KEY_LEFT, "h"):
                self.dirty = True
                return None
            elif key == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bstate = curses.getmouse()
                except curses.error:
                    continue
                if win.enclose(my, mx) and 2 <= my - y0 < 2 + visible:
                    n = top + my - y0 - 2
                    if n < len(options):
                        idx = n
                        if bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED):
                            self.dirty = True
                            return options[idx][1]
                elif bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED):
                    self.dirty = True
                    return None
            elif key == curses.KEY_RESIZE:
                self.draw()

    def show_help(self):
        H, W = self.scr.getmaxyx()
        kw = max(swidth(k) for k, d in HELP if d)
        dw = max(swidth(d) for _, d in HELP)
        col_w = kw + dw + 4
        ncols = 1 if len(HELP) + 2 <= H - 2 or 2 * col_w + 4 > W - 2 else 2
        per_col = -(-len(HELP) // ncols)
        w = min(W - 2, ncols * col_w + 3)
        h = min(H - 2, per_col + 3)
        try:
            win = curses.newwin(h, w, max(0, (H - h) // 2), max(0, (W - w) // 2))
        except curses.error:
            return
        win.bkgd(" ", self.attr(C_POPUP))
        win.erase()
        win.border()
        self.put(win, 0, 2, f" pavucli {__version__} - keys ", self.attr(C_POPUP, curses.A_BOLD))
        for n, (k, d) in enumerate(HELP):
            col, i = divmod(n, per_col)
            y, x = 1 + i + (1 if h > per_col + 2 else 0), 2 + col * col_w
            if y >= h - 1:
                continue
            if not d:
                self.put(win, y, x, k, self.attr(C_POPUP, curses.A_BOLD | curses.A_UNDERLINE), w - x - 1)
            else:
                self.put(win, y, x + 1, k, self.attr(C_POPUP, curses.A_BOLD), w - x - 2)
                self.put(win, y, x + kw + 3, d, self.attr(C_POPUP), max(0, w - x - kw - 4))
        win.refresh()
        win.keypad(True)
        win.timeout(-1)
        try:
            win.get_wch()  # any key (or click) closes the help
        except curses.error:
            pass
        self.dirty = True

    # ------------------------------------------------------------ drawing

    def bar(self, volumes_value: int, width: int, muted: bool) -> list[tuple[str, int]]:
        g = self.g
        max_raw = self.max_raw or 1
        fill = min(volumes_value / max_raw, 1.0) * width
        full = int(fill)
        frac = int((fill - full) * 8)
        mark = int(round(width * pulse.VOLUME_NORM / max_raw)) if self.opts.max_volume > 100 else -1
        cells: list[tuple[str, int]] = []
        for i in range(width):
            over = self.opts.max_volume > 100 and i >= mark
            fill_attr = self.attr(C_MUTED) if muted else self.attr(C_OVER if over else C_OK)
            if i < full:
                cells.append((g.full, fill_attr))
            elif i == full and frac and g.partial[frac]:
                cells.append((g.partial[frac], fill_attr))
            elif i == mark:
                cells.append((g.mark100, self.attr(C_DIM)))
            else:
                cells.append((g.empty, self.attr(C_DIM)))
        # merge runs of equal attributes
        merged: list[tuple[str, int]] = []
        for ch, a in cells:
            if merged and merged[-1][1] == a:
                merged[-1] = (merged[-1][0] + ch, a)
            else:
                merged.append((ch, a))
        return merged

    def balance_widget(self, bal: float, width: int = 11) -> list[tuple[str, int]]:
        g = self.g
        n = width - 2
        pos = round((bal + 1) / 2 * (n - 1))
        mid = (n - 1) // 2
        track = []
        for i in range(n):
            if i == pos:
                track.append((g.knob, self.attr(C_ACCENT, curses.A_BOLD)))
            elif i == mid:
                track.append((g.center, self.attr(C_DIM)))
            else:
                track.append((g.track, self.attr(C_DIM)))
        return [("L", self.attr(C_DIM)), *track, ("R", self.attr(C_DIM))]

    def volume_line(
        self, value: int, muted: bool, width: int, bal: float | None, row: int, block: int, label: str = ""
    ) -> Line:
        """One volume bar. All bars share the same geometry so the 100 % marks line up;
        the right-hand slot shows the balance slider or, for channel rows, the channel name."""
        indent = 5
        pct_text = f"{pulse.pct(value):>4}%"
        db = pulse.volume_to_db(value)
        db_text = f"{db:>+6.1f}dB" if db != float("-inf") else "  -inf dB"
        show_db = width >= 70
        show_slot = width >= 50
        tail = len(pct_text) + (len(db_text) + 1 if show_db else 0) + (13 if show_slot else 0)
        bw = max(5, width - indent - tail - 2)
        segs: list[tuple[str, int]] = [(" " * indent, 0)]
        segs += self.bar(value, bw, muted)
        segs.append((" " + pct_text, curses.A_BOLD if not muted else self.attr(C_MUTED)))
        if show_db:
            segs.append((" " + db_text, self.attr(C_DIM)))
        if show_slot and bal is not None:
            segs.append(("  ", 0))
            segs += self.balance_widget(bal)
        elif show_slot and label:
            segs.append(("  " + label, self.attr(C_DIM)))
        return Line(segs, row=row, bar=(indent, bw), block=block)

    def item_lines(self, obj, width: int, rows: list[Row], sel: int, block: int) -> list[Line]:
        g = self.g
        lines: list[Line] = []
        row_idx = next(i for i, r in enumerate(rows) if r.obj is obj and r.channel is None)
        selected = rows[sel].obj is obj if rows else False
        whole_sel = selected and rows[sel].channel is None

        # --- line 1: title + flags
        marker = g.sel if whole_sel else " "
        title_attr = curses.A_BOLD | (self.attr(C_ACCENT) if selected else 0)
        right: list[tuple[str, int]] = []
        if isinstance(obj, Item):
            if obj.mute:
                right.append(("MUTED", self.attr(C_ERR, curses.A_BOLD)))
            if obj.is_default:
                right.append((f"{g.star} default", self.attr(C_WARN, curses.A_BOLD)))
            if obj.kind in (SINK_INPUT, SOURCE_OUTPUT):
                dev = self.snap.device(SINK if obj.kind == SINK_INPUT else SOURCE, obj.parent)
                if obj.corked:
                    right.append(("paused", self.attr(C_DIM)))
                right.append((f"{g.arrow} {dev.title if dev else '?'}", self.attr(C_DIM)))
            elif obj.ports and obj.active_port:
                right.append((obj.port_description or "", self.attr(C_DIM)))
            if obj.key in self.unlocked:
                right.append(("channels unlocked", self.attr(C_DIM)))
        else:
            n = len(obj.profiles)
            right.append((f"{n} profile{'s' if n != 1 else ''}", self.attr(C_DIM)))

        right_w = sum(swidth(t) for t, _ in right) + 2 * max(0, len(right) - 1)
        avail = width - 3 - 1
        if right_w > avail // 2:
            # keep the most important flags only
            while right and right_w > avail // 2:
                right.pop()
                right_w = sum(swidth(t) for t, _ in right) + 2 * max(0, len(right) - 1)
        left_room = avail - right_w - (2 if right else 0)
        title = trunc(obj.title, left_room, g.ellipsis)
        segs = [(f" {marker} ", self.attr(C_ACCENT, curses.A_BOLD)), (title, title_attr)]
        used = 3 + swidth(title)
        if isinstance(obj, Item) and obj.subtitle and left_room - swidth(title) > 6:
            sub = trunc(obj.subtitle, left_room - swidth(title) - 3, g.ellipsis)
            segs.append(("  " + sub, self.attr(C_DIM)))
            used += 2 + swidth(sub)
        if right:
            gap = width - used - right_w - 1
            segs.append((" " * max(1, gap), 0))
            for i, (t, a) in enumerate(right):
                if i:
                    segs.append(("  ", 0))
                segs.append((t, a))
        lines.append(Line(segs, row=row_idx, block=block))

        # --- line 2+: volume or profile
        if isinstance(obj, Card):
            lines.append(
                Line(
                    [("     Profile: ", self.attr(C_DIM)), (trunc(obj.profile_description, width - 15, g.ellipsis), 0)],
                    row=row_idx,
                    block=block,
                )
            )
            return lines
        if not obj.volumes:
            lines.append(Line([("     (no volume control)", self.attr(C_DIM))], row=row_idx, block=block))
            return lines
        lines.append(self.volume_line(obj.volume, obj.mute, width, obj.balance, row_idx, block))
        if obj.key in self.unlocked and len(obj.channels) > 1:
            for ch, (cname, vol) in enumerate(zip(obj.channels, obj.volumes)):
                r = row_idx + 1 + ch
                ln = self.volume_line(vol, obj.mute, width, None, r, block, label=trunc(cname, 11, g.ellipsis))
                if sel == r:
                    ln.segments[0] = ("   " + g.sub_sel + " ", self.attr(C_ACCENT, curses.A_BOLD))
                lines.append(ln)
        return lines

    def draw(self):
        scr = self.scr
        scr.erase()
        H, W = scr.getmaxyx()
        if H < 7 or W < 32:
            self.put(scr, 0, 0, "Terminal too small", self.attr(C_ERR))
            scr.refresh()
            return
        g = self.g

        # header
        head = f" pavucli {__version__} "
        self.put(scr, 0, 0, " " * W, self.attr(C_HEAD))
        x = self.put(scr, 0, 0, head, self.attr(C_HEAD, curses.A_BOLD))
        self.put(scr, 0, x, f"{g.hline} {self.snap.server}", self.attr(C_HEAD), W - x - 9)
        self.put(scr, 0, W - 8, " ? help ", self.attr(C_HEAD))

        # tabs
        counts = [len(self.objects(i)) for i in range(len(TABS))]
        long_w = sum(len(t[0]) + len(str(c)) + 5 for t, c in zip(TABS, counts))
        x = 1
        self.tab_ranges = []
        for i, (name, short, _) in enumerate(TABS):
            label = f" {name if long_w <= W - 2 else short} "
            if i < 4:
                label = label[:-1] + f" {counts[i]} "
            a = self.attr(C_TAB_ON, curses.A_BOLD) if i == self.tab else self.attr(C_TAB_OFF)
            w = self.put(scr, 1, x, label, a)
            self.tab_ranges.append((x, x + w, i))
            x += w + 1
        self.put(scr, 2, 0, g.hline * W, self.attr(C_DIM))

        # body
        self.body_top = 3
        body_h = H - self.body_top - 1
        rows = self.build_rows(self.tab)
        self.rows = rows
        sel = self.resolve_selection(self.tab, rows)
        lines: list[Line] = []
        for b, obj in enumerate(self.objects(self.tab)):
            if b:
                lines.append(Line([], block=b))
            lines.extend(self.item_lines(obj, W, rows, sel, b))
        self.lines = lines

        if not lines:
            msg = EMPTY_TEXT[TABS[self.tab][2]]
            self.put(scr, self.body_top + body_h // 2, max(0, (W - len(msg)) // 2), msg, self.attr(C_DIM))
            self.scroll[self.tab] = 0
        else:
            sel_lines = [i for i, ln in enumerate(lines) if ln.row == sel]
            blk = lines[sel_lines[0]].block if sel_lines else 0
            blk_lines = [i for i, ln in enumerate(lines) if ln.block == blk and ln.segments]
            top = self.scroll[self.tab]
            first, last = blk_lines[0], blk_lines[-1]
            if last - first + 1 > body_h:  # block larger than screen: follow the row
                first, last = sel_lines[0], sel_lines[-1]
            if first < top:
                top = first
            elif last >= top + body_h:
                top = last - body_h + 1
            top = max(0, min(top, max(0, len(lines) - body_h)))
            self.scroll[self.tab] = top
            for y, ln in enumerate(lines[top : top + body_h]):
                cx = 0
                for text, a in ln.segments:
                    if cx >= W:
                        break
                    cx += self.put(scr, self.body_top + y, cx, text, a)
            if top > 0:
                self.put(scr, self.body_top, W - 2, "▲" if not self.opts.ascii else "^", self.attr(C_DIM))
            if top + body_h < len(lines):
                self.put(scr, H - 2, W - 2, "▼" if not self.opts.ascii else "v", self.attr(C_DIM))

        # status line
        if self.status and time.monotonic() < self.status_until:
            self.put(scr, H - 1, 0, " " + self.status, self.attr(C_ERR if self.status_err else C_ACCENT, curses.A_BOLD), W - 1)
        else:
            kind = TABS[self.tab][2]
            if kind in (SINK_INPUT, SOURCE_OUTPUT):
                hint = "←→ volume  m mute  [ ] balance  c channels  ⏎ move to device  ? help  q quit"
            elif kind in (SINK, SOURCE):
                hint = "←→ volume  m mute  [ ] balance  c channels  d default  ⏎ port"
                hint += "  M monitors" if kind == SOURCE else ""
                hint += "  ? help  q quit"
            else:
                hint = "↑↓ select  ⏎ change profile  Tab next tab  ? help  q quit"
            if self.opts.ascii:
                hint = hint.replace("←→", "<- ->").replace("↑↓", "up/down").replace("⏎", "Enter")
            self.put(scr, H - 1, 0, " " + hint, self.attr(C_DIM), W - 1)
        scr.refresh()
        self.dirty = False

    # ------------------------------------------------------------ input

    def on_mouse(self):
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return
        wheel_up = bstate & getattr(curses, "BUTTON4_PRESSED", 0)
        wheel_down = bstate & getattr(curses, "BUTTON5_PRESSED", 0)
        click = bstate & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED)
        if my == 1 and click:
            for x0, x1, tab in self.tab_ranges:
                if x0 <= mx < x1:
                    self.switch_tab(tab)
            return
        li = self.scroll[self.tab] + my - self.body_top
        if my < self.body_top or not (0 <= li < len(self.lines)):
            if wheel_up or wheel_down:  # wheel outside body -> selected item
                self.change_volume(self.opts.step if wheel_up else -self.opts.step)
            return
        line = self.lines[li]
        if line.row is None:
            return
        self.select(line.row)
        row = self.rows[line.row] if line.row < len(self.rows) else None
        if wheel_up or wheel_down:
            self.change_volume(self.opts.step if wheel_up else -self.opts.step, row=row)
        elif click and line.bar:
            x0, bw = line.bar
            if x0 <= mx < x0 + bw:
                value = (mx - x0 + 1) / bw * self.opts.max_volume
                self.change_volume(absolute=round(value), row=row)

    def handle(self, key) -> None:
        step = self.opts.step
        rows_len = len(self.build_rows(self.tab))
        cur = self.sel_idx[self.tab]
        f_keys = {curses.KEY_F1 + i: i for i in range(len(TABS))}

        if key in ("q", "Q", "\x1b"):
            self.running = False
        elif key == curses.KEY_RESIZE:
            self.dirty = True
        elif key == "\t":
            self.switch_tab(self.tab + 1)
        elif key == curses.KEY_BTAB:
            self.switch_tab(self.tab - 1)
        elif key in f_keys:
            self.switch_tab(f_keys[key])
        elif key in (curses.KEY_UP, "k"):
            self.select(cur - 1)
        elif key in (curses.KEY_DOWN, "j"):
            self.select(cur + 1)
        elif key in (curses.KEY_HOME, "g"):
            self.select(0)
        elif key in (curses.KEY_END, "G"):
            self.select(rows_len - 1)
        elif key in (curses.KEY_LEFT, "h"):
            self.change_volume(-step)
        elif key in (curses.KEY_RIGHT, "l"):
            self.change_volume(step)
        elif key in (curses.KEY_SLEFT, "H"):
            self.change_volume(-1)
        elif key in (curses.KEY_SRIGHT, "L"):
            self.change_volume(1)
        elif key == curses.KEY_NPAGE:
            self.change_volume(-10)
        elif key == curses.KEY_PPAGE:
            self.change_volume(10)
        elif isinstance(key, str) and key.isdigit() and len(key) == 1:
            self.change_volume(absolute=100 if key == "0" else int(key) * 10)
        elif key == "m":
            self.toggle_mute()
        elif key == "[":
            self.change_balance(-0.05)
        elif key == "]":
            self.change_balance(0.05)
        elif key in ("b", "="):
            self.change_balance(None)
        elif key in ("c", " "):
            self.toggle_lock()
        elif key == "d":
            self.set_default()
        elif key == "p":
            self.choose_port()
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self.activate()
        elif key == "M":
            self.show_monitors = not self.show_monitors
            self.flash("Monitor sources " + ("shown" if self.show_monitors else "hidden"))
        elif key == "r":
            self.event.set()
        elif key == "?":
            self.draw()
            self.show_help()
        elif key == curses.KEY_MOUSE:
            self.on_mouse()

    def run(self) -> None:
        self.sub.start()
        try:
            while self.running:
                now = time.monotonic()
                if self.event.is_set() and now - self.last_refresh >= 0.04:
                    self.refresh()
                if self.status and now >= self.status_until and self.status_until:
                    self.status_until = 0
                    self.dirty = True
                if self.dirty:
                    self.draw()
                try:
                    key = self.scr.get_wch()
                except curses.error:
                    continue  # timeout
                except KeyboardInterrupt:
                    break
                self.handle(key)
                self.dirty = True
        finally:
            self.sub.stop()


def init_colors(opts: Options) -> None:
    if not (opts.color and curses.has_colors()):
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    pairs = {
        C_HEAD: (curses.COLOR_BLACK, curses.COLOR_CYAN),
        C_TAB_ON: (curses.COLOR_BLACK, curses.COLOR_CYAN),
        C_TAB_OFF: (-1 if bg == -1 else curses.COLOR_WHITE, bg),
        C_OK: (curses.COLOR_GREEN, bg),
        C_OVER: (curses.COLOR_RED, bg),
        C_MUTED: (8 if curses.COLORS > 8 else curses.COLOR_WHITE, bg),
        C_DIM: (8 if curses.COLORS > 8 else curses.COLOR_WHITE, bg),
        C_ACCENT: (curses.COLOR_CYAN, bg),
        C_ERR: (curses.COLOR_RED, bg),
        C_WARN: (curses.COLOR_YELLOW, bg),
        C_POPUP: (curses.COLOR_WHITE, curses.COLOR_BLUE),
    }
    for pid, (fg, b) in pairs.items():
        try:
            curses.init_pair(pid, fg, b)
        except (curses.error, ValueError):
            curses.init_pair(pid, curses.COLOR_WHITE, curses.COLOR_BLACK)


def run(scr, opts: Options) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    scr.keypad(True)
    scr.timeout(50)
    init_colors(opts)
    if opts.mouse:
        mask = curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED
        mask |= getattr(curses, "BUTTON4_PRESSED", 0) | getattr(curses, "BUTTON5_PRESSED", 0)
        curses.mousemask(mask)
        curses.mouseinterval(0)
    App(scr, opts).run()
