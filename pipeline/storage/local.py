from __future__ import annotations

from pathlib import Path

from .base import Storage


class LocalStorage(Storage):
    name = "local"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key.lstrip("/")).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise ValueError(f"key escapes storage root: {key}")
        return p

    def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return self.uri(key)

    def get(self, key: str) -> bytes:
        return self._path(self.key_from_uri(key)).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(self.key_from_uri(key)).exists()

    def list(self, prefix: str = "") -> list[str]:
        base = self._path(prefix) if prefix else self.root
        if not base.exists():
            return []
        return sorted(str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file())

    def delete(self, key: str) -> None:
        p = self._path(self.key_from_uri(key))
        if p.exists():
            p.unlink()

    def ping(self) -> None:
        probe = self.root / ".ping"
        probe.write_text("ok")
        probe.unlink()

    def uri(self, key: str) -> str:
        return f"local://{key.lstrip('/')}"

    def key_from_uri(self, uri: str) -> str:
        return uri[len("local://"):] if uri.startswith("local://") else uri
