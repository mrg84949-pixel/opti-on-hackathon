"""Parse organization service price_label strings into minor currency units."""
from __future__ import annotations

import re

_DIGITS_RE = re.compile(r"\d+")


def parse_price_label_to_minor(label: str | None) -> int | None:
    """Extract amount from price_label; treat as major KZT and return minor (*100)."""
    if label is None:
        return None
    text = label.strip()
    if not text:
        return None
    parts = _DIGITS_RE.findall(text.replace("\u00a0", " "))
    if not parts:
        return None
    try:
        major = int("".join(parts))
    except ValueError:
        return None
    if major <= 0:
        return None
    return major * 100
