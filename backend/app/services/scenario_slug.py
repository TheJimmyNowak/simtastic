from __future__ import annotations

import re
import unicodedata

_POLISH_ASCII = str.maketrans(
    {
        "ą": "a",
        "ć": "c",
        "ę": "e",
        "ł": "l",
        "ń": "n",
        "ó": "o",
        "ś": "s",
        "ź": "z",
        "ż": "z",
        "Ą": "a",
        "Ć": "c",
        "Ę": "e",
        "Ł": "l",
        "Ń": "n",
        "Ó": "o",
        "Ś": "s",
        "Ź": "z",
        "Ż": "z",
    }
)


def slugify_scenario_id(name: str, taken: set[str] | None = None) -> str:
    """Filesystem-safe scenario id from display name (ASCII, lowercase, dashes)."""
    normalized = unicodedata.normalize("NFKD", name.strip().translate(_POLISH_ASCII))
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")
    if not slug:
        slug = "scenario"
    slug = slug[:80]
    if not taken:
        return slug
    if slug not in taken:
        return slug
    n = 2
    while True:
        candidate = f"{slug}-{n}"
        if candidate not in taken:
            return candidate
        n += 1
