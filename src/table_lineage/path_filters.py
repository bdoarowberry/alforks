from __future__ import annotations

from pathlib import Path
import re


DATE_LIKE_TOKEN = re.compile(
    r"(?:_|-)(?:"
    r"(?:19|20)[0-9]{4,12}(?![0-9])|"
    r"[0-9]{4}(?:19|20)[0-9]{2}(?![0-9])|"
    r"(?:19|20)[0-9]{2}[-_][0-9]{1,2}[-_][0-9]{1,2}(?![0-9])|"
    r"[0-9]{1,2}[-_][0-9]{1,2}[-_](?:19|20)[0-9]{2}(?![0-9])"
    r")",
    re.ASCII,
)


def is_dated_snapshot(path: Path) -> bool:
    """Return whether the filename stem contains a recognized date-like token."""
    return DATE_LIKE_TOKEN.search(path.stem) is not None
