"""Storage interface: local filesystem for dev, S3-compatible (Railway bucket) for staging/prod."""

from __future__ import annotations

import mimetypes
from abc import ABC, abstractmethod
from functools import lru_cache

from pipeline.config import get_settings


class Storage(ABC):
    name: str = "base"

    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        """Store bytes under key, return a storage URI (local path or s3://bucket/key)."""

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def list(self, prefix: str = "") -> list[str]: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def ping(self) -> None:
        """Raise if the backend is unreachable."""

    def uri(self, key: str) -> str:
        return key

    def key_from_uri(self, uri: str) -> str:
        return uri

    @staticmethod
    def guess_type(key: str) -> str:
        return mimetypes.guess_type(key)[0] or "application/octet-stream"


@lru_cache(maxsize=1)
def get_storage() -> Storage:
    settings = get_settings()
    if settings.storage_backend == "s3":
        from .s3 import S3Storage

        return S3Storage(
            endpoint=settings.s3_endpoint,
            bucket=settings.s3_bucket,
            region=settings.s3_region,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
        )
    from .local import LocalStorage

    return LocalStorage(settings.resolve(settings.storage_dir))


def reset_storage_cache() -> None:
    get_storage.cache_clear()
