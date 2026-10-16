from __future__ import annotations

from functools import lru_cache

from pipeline.config import get_settings


@lru_cache(maxsize=16)
def load_prompt(name: str) -> str:
    settings = get_settings()
    path = settings.resolve(settings.prompts_dir) / f"{name}.md"
    return path.read_text(encoding="utf-8").strip()
