"""IP-safety blocklist: brands, trademarks, characters, celebrities, named artists, 'in the style of'."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pipeline.config import get_settings

# Phrases that are risky regardless of the specific name that follows.
BUILTIN_PATTERNS = [
    r"\bin the style of\b",
    r"\binspired by [A-Z][a-z]+ [A-Z][a-z]+\b",  # "inspired by Firstname Lastname"
    r"\bofficial\b",
    r"\blicensed\b",
    r"\btrademark(ed)?\b",
    r"\b(tm|®|™)\b",
]


class Blocklist:
    def __init__(self, terms: list[str], patterns: list[str] | None = None):
        self.terms = sorted({t.strip().lower() for t in terms if t.strip() and not t.strip().startswith("#")})
        self._term_re = (
            re.compile(r"(?<!\w)(" + "|".join(re.escape(t) for t in self.terms) + r")(?!\w)", re.IGNORECASE)
            if self.terms
            else None
        )
        self._patterns = [re.compile(p, re.IGNORECASE) for p in (patterns if patterns is not None else BUILTIN_PATTERNS)]

    @classmethod
    def from_file(cls, path: Path) -> Blocklist:
        terms = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        return cls(terms)

    def check(self, text: str | None) -> list[str]:
        """Return the list of matched blocked terms/patterns (empty = clean)."""
        if not text:
            return []
        hits: list[str] = []
        if self._term_re:
            hits.extend(m.group(1).lower() for m in self._term_re.finditer(text))
        for pat in self._patterns:
            for m in pat.finditer(text):
                hits.append(m.group(0).lower())
        # dedupe, keep order
        seen: set[str] = set()
        return [h for h in hits if not (h in seen or seen.add(h))]

    def check_many(self, *texts: str | None) -> list[str]:
        hits: list[str] = []
        for t in texts:
            if isinstance(t, (list, tuple)):
                t = " ".join(str(x) for x in t)
            hits.extend(self.check(t))
        seen: set[str] = set()
        return [h for h in hits if not (h in seen or seen.add(h))]

    def is_clean(self, *texts: str | None) -> bool:
        return not self.check_many(*texts)


@lru_cache(maxsize=1)
def get_blocklist() -> Blocklist:
    settings = get_settings()
    return Blocklist.from_file(settings.resolve(settings.blocklist_file))


def reset_blocklist_cache() -> None:
    get_blocklist.cache_clear()
