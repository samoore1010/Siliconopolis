"""S3-compatible storage (Railway Buckets, AWS S3, R2, MinIO ...)."""

from __future__ import annotations

from .base import Storage


class S3Storage(Storage):
    name = "s3"

    def __init__(self, *, endpoint: str, bucket: str, region: str, access_key_id: str, secret_access_key: str):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint or None,
            region_name=region or "auto",
            aws_access_key_id=access_key_id or None,
            aws_secret_access_key=secret_access_key or None,
            # Railway buckets use virtual-hosted style; older ones need path style.
            # TODO(owner): if the bucket Credentials tab says "path-style", set addressing_style="path".
            config=Config(s3={"addressing_style": "auto"}, retries={"max_attempts": 5, "mode": "standard"}),
        )

    def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        key = key.lstrip("/")
        self.client.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType=content_type or self.guess_type(key)
        )
        return self.uri(key)

    def get(self, key: str) -> bytes:
        obj = self.client.get_object(Bucket=self.bucket, Key=self.key_from_uri(key))
        return obj["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self.key_from_uri(key))
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def list(self, prefix: str = "") -> list[str]:
        keys: list[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix.lstrip("/")):
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return keys

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self.key_from_uri(key))

    def ping(self) -> None:
        self.client.list_objects_v2(Bucket=self.bucket, MaxKeys=1)

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key.lstrip('/')}"

    def key_from_uri(self, uri: str) -> str:
        prefix = f"s3://{self.bucket}/"
        return uri[len(prefix):] if uri.startswith(prefix) else uri.lstrip("/")
