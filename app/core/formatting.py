"""Human-readable sizes and speeds."""

from __future__ import annotations

_UNITS = ("B", "KB", "MB", "GB", "TB")


def human_size(num_bytes: float) -> str:
    value = float(num_bytes)
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} {_UNITS[-1]}"


def human_speed(bytes_per_sec: float) -> str:
    return f"{human_size(bytes_per_sec)}/s"
