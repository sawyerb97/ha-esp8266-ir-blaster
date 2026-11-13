"""Flipper-IRDB (.ir) parsing and conversion to firmware raw timings.

Protocol encoders are ported from the Flipper Zero firmware
(lib/infrared/encoder_decoder) so generated timings match what a Flipper
transmits for the same file. Everything is converted to raw mark/space
microseconds and sent through the firmware's /json "raw" type, which avoids
any differences in how the firmware's named protocols pack bits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ArduinoJson doc on the device is 4096 bytes (~16 B per array slot).
MAX_RAW_LEN = 230
FRAME_GAP_US = 10000


class UnsupportedSignal(Exception):
    """Signal can't be converted."""


@dataclass
class Signal:
    name: str
    type: str  # "parsed" | "raw"
    protocol: str = ""
    address: int = 0
    command: int = 0
    frequency: int = 38000
    duty_cycle: float = 0.33
    data: list[int] | None = None


def _le_bytes(value: str) -> int:
    parts = value.split()
    return int.from_bytes(bytes(int(p, 16) for p in parts), "little") if parts else 0


def parse_ir_file(text: str) -> list[Signal]:
    """Parse a Flipper 'IR signals file'."""
    signals: list[Signal] = []
    cur: dict[str, str] = {}

    def flush() -> None:
        if not cur.get("name") or not cur.get("type"):
            return
        t = cur["type"].strip().lower()
        try:
            if t == "parsed":
                signals.append(
                    Signal(
                        name=cur["name"].strip(),
                        type="parsed",
                        protocol=cur.get("protocol", "").strip(),
                        address=_le_bytes(cur.get("address", "")),
                        command=_le_bytes(cur.get("command", "")),
                    )
                )
            elif t == "raw":
                signals.append(
                    Signal(
                        name=cur["name"].strip(),
                        type="raw",
                        frequency=int(float(cur.get("frequency", "38000"))),
                        duty_cycle=float(cur.get("duty_cycle", "0.33")),
                        data=[abs(int(float(x))) for x in cur.get("data", "").split()],
                    )
                )
        except (ValueError, KeyError):
            pass  # skip malformed entries

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            if line.startswith("#"):
                flush()
                cur = {}
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        if key == "name" and "name" in cur:
            flush()
            cur = {}
        cur[key] = val.strip()
    flush()
    return signals


# --- helpers ---------------------------------------------------------------

def _bits_lsb(data: int, n: int) -> list[int]:
    return [(data >> i) & 1 for i in range(n)]


def _pdwm(bits: list[int], one: tuple[int, int], zero: tuple[int, int], pwm: bool) -> list[int]:
    """Pulse-distance/width encoding as Flipper does it.

    pwm (equal spaces, e.g. SIRC): mark(value), space ... last bit has no trailing space.
    distance (equal marks, e.g. NEC): mark, space(value) ... then a final stop mark.
    """
    out: list[int] = []
    for i, b in enumerate(bits):
        mark, space = one if b else zero
        out.append(mark)
        if pwm and i == len(bits) - 1:
            break
        out.append(space)
    if not pwm:
        out.append(one[0])
    return out


def _manchester(seq: list[tuple[bool, int]]) -> list[int]:
    """Merge (level, duration) pairs into mark/space list starting with a mark."""
    merged: list[list[Any]] = []
    for level, dur in seq:
        if merged and merged[-1][0] == level:
            merged[-1][1] += dur
        else:
            merged.append([level, dur])
    while merged and not merged[0][0]:
        merged.pop(0)
    while merged and not merged[-1][0]:
        merged.pop()
    return [d for _, d in merged]


def _frame(preamble: tuple[int, int] | None, body: list[int]) -> list[int]:
    return (list(preamble) if preamble else []) + body


def _join(frames: list[list[int]], gaps: list[int]) -> list[int]:
    """Concatenate frames (each ends with a mark), inserting gap spaces."""
    out: list[int] = []
    for i, f in enumerate(frames):
        if i:
            out.append(gaps[i - 1])
        out.extend(f)
    return out


def _rev(value: int, width: int) -> int:
    return int(f"{value & ((1 << width) - 1):0{width}b}"[::-1], 2)


# --- protocol encoders (return (timings, khz, duty%)) ---------------------

def _nec(sig: Signal) -> tuple[list[int], int, int]:
    p, a, c = sig.protocol.lower(), sig.address, sig.command
    if p == "nec":
        data = (a & 0xFF) | ((~a & 0xFF) << 8) | ((c & 0xFF) << 16) | ((~c & 0xFF) << 24)
        n = 32
    elif p == "necext":
        data = (a & 0xFFFF) | ((c & 0xFFFF) << 16)
        n = 32
    elif p == "nec42":
        data = (a & 0x1FFF) | ((~a & 0x1FFF) << 13) | ((c & 0xFF) << 26) | ((~c & 0xFF) << 34)
        n = 42
    else:  # nec42ext
        data = (a & 0x3FFFFFF) | ((c & 0xFFFF) << 26)
        n = 42
    body = _pdwm(_bits_lsb(data, n), (560, 1690), (560, 560), pwm=False)
    return _frame((9000, 4500), body), 38, 33


def _samsung32(sig: Signal) -> tuple[list[int], int, int]:
    a, c = sig.address & 0xFF, sig.command & 0xFF
    data = a | (a << 8) | (c << 16) | ((~c & 0xFF) << 24)
    body = _pdwm(_bits_lsb(data, 32), (550, 1650), (550, 550), pwm=False)
    return _frame((4500, 4500), body), 38, 33


def _sirc(sig: Signal) -> tuple[list[int], int, int]:
    p = sig.protocol.lower()
    abits, n = {"sirc": (0x1F, 12), "sirc15": (0xFF, 15), "sirc20": (0x1FFF, 20)}[p]
    data = (sig.command & 0x7F) | ((sig.address & abits) << 7)
    frame = _frame((2400, 600), _pdwm(_bits_lsb(data, n), (1200, 600), (600, 600), pwm=True))
    gap = max(45000 - sum(frame), 600)
    return _join([frame] * 3, [gap, gap]), 40, 33


def _rc5(sig: Signal) -> tuple[list[int], int, int]:
    x = sig.protocol.lower() == "rc5x"
    cmd = sig.command & 0x7F
    field = 0 if (x or cmd > 0x3F) else 1
    bits = [1, field, 0] + [(sig.address >> i) & 1 for i in range(4, -1, -1)] + [(cmd >> i) & 1 for i in range(5, -1, -1)]
    seq: list[tuple[bool, int]] = []
    for b in bits:  # RC5: 1 = space->mark, 0 = mark->space
        seq += [(False, 889), (True, 889)] if b else [(True, 889), (False, 889)]
    return _manchester(seq), 36, 33


def _rc6(sig: Signal) -> tuple[list[int], int, int]:
    seq: list[tuple[bool, int]] = [(True, 2666), (False, 889)]
    bits = [1, 0, 0, 0]
    for b in bits:  # RC6: 1 = mark->space, 0 = space->mark
        seq += [(True, 444), (False, 444)] if b else [(False, 444), (True, 444)]
    seq += [(False, 888), (True, 888)]  # toggle bit = 0, double width
    for b in [(sig.address >> i) & 1 for i in range(7, -1, -1)] + [(sig.command >> i) & 1 for i in range(7, -1, -1)]:
        seq += [(True, 444), (False, 444)] if b else [(False, 444), (True, 444)]
    return _manchester(seq), 36, 33


def _kaseikyo(sig: Signal) -> tuple[list[int], int, int]:
    a, c = sig.address, sig.command
    ident = (a >> 24) & 3
    vendor = (a >> 8) & 0xFFFF
    g1, g2 = (a >> 4) & 0xF, a & 0xF
    d = [vendor & 0xFF, vendor >> 8, 0, 0, 0, 0]
    vp = d[0] ^ d[1]
    vp = (vp & 0xF) ^ (vp >> 4)
    d[2] = (vp & 0xF) | (g1 << 4)
    d[3] = (g2 & 0xF) | ((c & 0xF) << 4)
    d[4] = ((ident << 6) | (c >> 4)) & 0xFF
    d[5] = d[2] ^ d[3] ^ d[4]
    data = int.from_bytes(bytes(d), "little")
    u = 432
    body = _pdwm(_bits_lsb(data, 48), (u, 3 * u), (u, u), pwm=False)
    return _frame((8 * u, 4 * u), body), 38, 33


def _rca(sig: Signal) -> tuple[list[int], int, int]:
    a, c = sig.address & 0xFF, sig.command & 0xFF
    data = (a & 0xF) | (c << 4) | ((~a & 0xF) << 12) | ((~c & 0xFF) << 16)
    body = _pdwm(_bits_lsb(data, 24), (500, 2000), (500, 1000), pwm=False)
    return _frame((4000, 4000), body), 38, 33


def _pioneer(sig: Signal) -> tuple[list[int], int, int]:
    a, c = sig.address & 0xFF, sig.command & 0xFF
    data = a | ((~a & 0xFF) << 8) | (c << 16) | ((~c & 0xFF) << 24)
    frame = _frame((8500, 4225), _pdwm(_bits_lsb(data, 33), (500, 1500), (500, 500), pwm=False))
    return _join([frame, frame], [26000]), 40, 33


ENCODERS = {
    "nec": _nec, "necext": _nec, "nec42": _nec, "nec42ext": _nec,
    "samsung32": _samsung32,
    "sirc": _sirc, "sirc15": _sirc, "sirc20": _sirc,
    "rc5": _rc5, "rc5x": _rc5,
    "rc6": _rc6,
    "kaseikyo": _kaseikyo,
    "rca": _rca,
    "pioneer": _pioneer,
}


def _trim(raw: list[int]) -> list[int]:
    """Fit the device buffer: keep whole frames (split at long gaps) up to MAX_RAW_LEN."""
    if len(raw) % 2 == 0:
        raw = raw[:-1]  # must end on a mark
    if len(raw) <= MAX_RAW_LEN:
        return raw
    cut = None
    for i in range(1, min(len(raw), MAX_RAW_LEN), 2):  # odd indexes are spaces
        if raw[i] >= FRAME_GAP_US:
            cut = i
    if cut is None:
        raise UnsupportedSignal(f"raw signal too long for device ({len(raw)} timings)")
    return raw[:cut]


def to_firmware(sig: Signal) -> list[dict[str, Any]]:
    """Convert a Signal into a firmware /json command list."""
    if sig.type == "raw":
        if not sig.data:
            raise UnsupportedSignal("empty raw signal")
        raw = _trim(sig.data)
        khz = max(1, round(sig.frequency / 1000))
        duty = max(1, min(100, round(sig.duty_cycle * 100)))
    else:
        enc = ENCODERS.get(sig.protocol.lower())
        if enc is None:
            raise UnsupportedSignal(f"protocol {sig.protocol} not supported")
        raw, khz, duty = enc(sig)
        raw = _trim(raw)
    return [{"type": "raw", "data": raw, "khz": khz, "duty": duty}]


def convert_file(text: str) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Return ({button_name: firmware_json}, [skipped descriptions])."""
    codes: dict[str, list[dict[str, Any]]] = {}
    skipped: list[str] = []
    for sig in parse_ir_file(text):
        try:
            fw = to_firmware(sig)
        except UnsupportedSignal as err:
            skipped.append(f"{sig.name}: {err}")
            continue
        name, n = sig.name, 2
        while name in codes:
            name, n = f"{sig.name}_{n}", n + 1
        codes[name] = fw
    return codes, skipped
