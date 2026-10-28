"""URL slug generation.

Slugs are derived from human-readable names so that ``/products/belgian-dark``
stays meaningful and stable. Transliteration is intentionally conservative: the
name is normalised to ASCII, lowercased, and anything that is not a letter or
digit collapses into a single hyphen.
"""
from __future__ import annotations

import re
import unicodedata

MAX_SLUG_LENGTH = 200

_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Return a lowercase, hyphen-separated slug for ``value``.

    >>> slugify("Belgian Dark  Chunk!")
    'belgian-dark-chunk'
    >>> slugify("Crème Brûlée")
    'creme-brulee'
    >>> slugify("   ")
    ''
    """
    normalised = unicodedata.normalize("NFKD", value)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii").lower()
    slug = _NON_SLUG.sub("-", ascii_only).strip("-")
    if len(slug) > MAX_SLUG_LENGTH:
        # Cut on a hyphen boundary so the slug never ends mid-word.
        slug = slug[:MAX_SLUG_LENGTH].rsplit("-", 1)[0] or slug[:MAX_SLUG_LENGTH]
    return slug


def with_suffix(base: str, index: int) -> str:
    """Return ``base`` disambiguated with ``-index``, respecting the length cap."""
    suffix = f"-{index}"
    trimmed = base[: MAX_SLUG_LENGTH - len(suffix)].rstrip("-")
    return f"{trimmed or 'item'}{suffix}"


__all__ = ["MAX_SLUG_LENGTH", "slugify", "with_suffix"]
