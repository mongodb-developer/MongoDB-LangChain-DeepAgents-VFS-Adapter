"""S3 backend implementation using boto3."""
from __future__ import annotations

import logging
from typing import Any, Iterator

import boto3
from botocore.exceptions import ClientError

from deepagents_mongodb_fs.backends.base import ObjectStoreBackend
from deepagents_mongodb_fs.errors import AdapterError, ErrorCode
from pprint import pprint

logger = logging.getLogger(__name__)


class S3Backend(ObjectStoreBackend):
    """ObjectStoreBackend backed by an AWS S3 bucket.

    Args:
        bucket_name: Name of the S3 bucket.
        region_name: AWS region (defaults to AWS_DEFAULT_REGION env var).
        endpoint_url: Override endpoint for local testing (e.g. LocalStack).
        **boto_kwargs: Forwarded verbatim to ``boto3.client``.
    """

    def __init__(
        self,
        bucket_name: str,
        region_name: str | None = None,
        endpoint_url: str | None = None,
        **boto_kwargs: Any,
    ) -> None:
        self._bucket = bucket_name
        self._client: Any = boto3.client(
            "s3",
            region_name=region_name,
            endpoint_url=endpoint_url,
            **boto_kwargs,
        )
        self._verify_bucket()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _verify_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError as exc:
            pprint(exc)
            code = exc.response["Error"]["Code"]
            if code in ("404", "NoSuchBucket"):
                raise AdapterError(ErrorCode.E1002_INVALID_BUCKET, f"Bucket '{self._bucket}' not found") from exc
            raise AdapterError(ErrorCode.E2002_OBJECT_READ_FAILED, str(exc)) from exc

    def _key(self, path: str) -> str:
        return self.normalize_key(path).lstrip("/")

    # ------------------------------------------------------------------
    # ObjectStoreBackend implementation
    # ------------------------------------------------------------------

    def read(self, path: str, offset: int = 0, limit: int = -1) -> bytes:
        key = self._key(path)
        range_header: dict[str, str] = {}
        if offset > 0 or limit >= 0:
            end = "" if limit < 0 else str(offset + limit - 1)
            range_header["Range"] = f"bytes={offset}-{end}"
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key, **range_header)
            return response["Body"].read()
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("NoSuchKey", "404"):
                raise AdapterError(ErrorCode.E2001_OBJECT_NOT_FOUND, f"Key '{key}' not found") from exc
            raise AdapterError(ErrorCode.E2002_OBJECT_READ_FAILED, str(exc)) from exc

    def write(self, path: str, content: bytes) -> None:
        key = self._key(path)
        try:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=content)
        except ClientError as exc:
            raise AdapterError(ErrorCode.E2003_OBJECT_WRITE_FAILED, str(exc)) from exc

    def edit(self, path: str, old: str, new: str, replace_all: bool = False) -> None:
        key = self._key(path)
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=key)
            etag = head["ETag"]
            body = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("NoSuchKey", "404"):
                raise AdapterError(ErrorCode.E2001_OBJECT_NOT_FOUND, f"Key '{key}' not found") from exc
            raise AdapterError(ErrorCode.E2002_OBJECT_READ_FAILED, str(exc)) from exc

        text = body.decode("utf-8")
        if replace_all:
            updated = text.replace(old, new)
        else:
            updated = text.replace(old, new, 1)

        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=updated.encode("utf-8"),
                # Conditional write: reject if object changed since we read it
                # (boto3 passes unknown params through; real S3 honours If-Match)
                **{"IfMatch": etag} if etag else {},
            )
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "PreconditionFailed":
                raise AdapterError(ErrorCode.E2008_EDIT_CONFLICT, "Concurrent modification detected") from exc
            raise AdapterError(ErrorCode.E2003_OBJECT_WRITE_FAILED, str(exc)) from exc

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[str]:
        uploaded: list[str] = []
        failed: list[str] = []
        for path, content in files:
            try:
                self.write(path, content)
                uploaded.append(path)
            except AdapterError:
                logger.warning("Upload failed for path: %s", path)
                failed.append(path)
        if failed:
            raise AdapterError(
                ErrorCode.E2006_UPLOAD_FAILED,
                f"Failed to upload {len(failed)} file(s): {failed}",
            )
        return uploaded

    def download_files(self, paths: list[str]) -> list[tuple[str, bytes]]:
        results: list[tuple[str, bytes]] = []
        failed: list[str] = []
        for path in paths:
            try:
                results.append((path, self.read(path)))
            except AdapterError:
                logger.warning("Download failed for path: %s", path)
                failed.append(path)
        if failed:
            raise AdapterError(
                ErrorCode.E2007_DOWNLOAD_FAILED,
                f"Failed to download {len(failed)} file(s): {failed}",
            )
        return results

    def list_keys(self, prefix: str = "") -> Iterator[tuple[str, str]]:
        paginator = self._client.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    yield obj["Key"], obj.get("ETag", "").strip('"')
        except ClientError as exc:
            raise AdapterError(ErrorCode.E2005_LIST_FAILED, str(exc)) from exc

    def get_etag(self, key: str) -> str | None:
        """Return the current ETag for *key*, or None if not found."""
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=key)
            return head["ETag"].strip('"')
        except ClientError:
            return None

    def get_size(self, path: str) -> int:
        """Return the object's size in bytes via a HEAD request (no body fetch)."""
        key = self._key(path)
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=key)
            return int(head["ContentLength"])
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("NoSuchKey", "404"):
                raise AdapterError(ErrorCode.E2001_OBJECT_NOT_FOUND, f"Key '{key}' not found") from exc
            raise AdapterError(ErrorCode.E2002_OBJECT_READ_FAILED, str(exc)) from exc
