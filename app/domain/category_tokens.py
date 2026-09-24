"""Stable short identifiers for dynamic category values at service boundaries."""

from hashlib import sha256

_CATEGORY_TOKEN_LENGTH = 20


def category_token(category: str) -> str:
    """Project canonical category text to a bounded, reproducible selection key.

    Keyboards use this short value in callback payloads; the feedback service
    resolves it against current categories while holding SQLite's writer lock.
    """
    return sha256(category.encode("utf-8")).hexdigest()[:_CATEGORY_TOKEN_LENGTH]
