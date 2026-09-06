"""Authentication failure policy for Twitch recording startup."""

import re


_TWITCH_AUTH_REJECTION_PATTERNS = (
    re.compile(
        r"\bauthorization\b.{0,80}\btoken\b.{0,40}\binvalid\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:twitch\s+)?oauth\b.{0,80}\btoken\b.{0,40}\binvalid\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\binvalid\b.{0,40}\b(?:twitch\s+)?oauth\b.{0,40}\btoken\b",
        re.IGNORECASE | re.DOTALL,
    ),
)


def is_twitch_auth_rejection(output: str) -> bool:
    """Return whether sanitized Streamlink output proves Twitch rejected auth."""
    if any(pattern.search(output) for pattern in _TWITCH_AUTH_REJECTION_PATTERNS):
        return True
    return any(
        re.search(r"\b401\s+unauthorized\b", line, re.IGNORECASE)
        and not re.search(r"\bproxy|proxy\.|tunnel|connect\b", line, re.IGNORECASE)
        for line in output.splitlines()
    )
