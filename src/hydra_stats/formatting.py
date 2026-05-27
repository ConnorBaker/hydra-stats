"""Duration parsing + human-readable formatters."""

# pyright: strict

import argparse
import datetime as dt
import re

_DUR_UNITS: dict[str, int] = {"s": 1, "m": 60, "h": 3600, "d": 86_400, "w": 7 * 86_400}
_DUR_RE = re.compile(r"^(\d+)([smhdw])?$")


def parse_duration(spec: str) -> int:
    """Parse '30d' / '12h' / '1w' / '3600s' or a bare integer (seconds)."""
    m = _DUR_RE.match(spec.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(f"invalid duration {spec!r}: expected <number>[s|m|h|d|w]")
    return int(m.group(1)) * _DUR_UNITS[m.group(2) or "s"]


def human_duration(seconds: float | None) -> str:
    """Format seconds as '42s' / '5m07s' / '2h03m'."""
    if seconds is None or seconds < 0:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m{s:02d}s"
    h, rem = divmod(int(seconds), 3600)
    return f"{h}h{rem // 60:02d}m"


_SIZE_UNITS = (("B", 1024), ("KiB", 1024**2), ("MiB", 1024**3), ("GiB", 1024**4))


def human_size(n: float | None) -> str:
    """Format bytes with binary-suffix labels (B/KiB/MiB/GiB/TiB)."""
    if n is None or n < 0:
        return "-"
    for unit, threshold in _SIZE_UNITS:
        if n < threshold:
            scale = max(threshold // 1024, 1)
            return f"{n:.0f}B" if unit == "B" else f"{n / scale:.1f}{unit}"
    return f"{n / 1024**4:.1f}TiB"


def human_time(ts: int | None) -> str:
    if ts is None:
        return "-"
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
