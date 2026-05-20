"""MongoFilesystemBackend — public entry point implementing BackendProtocol.

Construction is non-blocking: index provisioning, initial sync, and the
background watcher start in a daemon thread so the constructor returns
immediately. Searches block until the first sync completes.

Usage::

    from deepagents_mongodb_fs import MongoFilesystemBackend

    backend = MongoFilesystemBackend(
        s3_bucket_name="my-bucket",
        mongodb_connection_string="mongodb+srv://...",
    )
    result = backend.grep("authentication flow", path="docs/")
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Literal

import pymongo
from pymongo.collection import Collection
from pymongo.driver_info import DriverInfo

from deepagents_mongodb_fs.backends.s3 import S3Backend
from deepagents_mongodb_fs.chunker import Chunker
from deepagents_mongodb_fs.dtypes import (
    DownloadResult,
    EditResult,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    UploadResult,
    WriteResult,
)
from deepagents_mongodb_fs.embedder import Embedder
from deepagents_mongodb_fs.errors import (
    AdapterError,
    ErrorCode,
    adapter_boundary,
)
from deepagents_mongodb_fs.index_manager import IndexManager
from deepagents_mongodb_fs.search import SearchRouter
from deepagents_mongodb_fs.sync import InitialSync
from deepagents_mongodb_fs.watcher import PollingWatcher, SQSWatcher, S3Watcher

logger = logging.getLogger(__name__)

try:
    from importlib.metadata import version as get_version
    _VERSION = get_version("deepagents_mongodb_fs")
except Exception:
    _VERSION = None

_DRIVER_INFO = DriverInfo(name="DeepAgents-MongoDB-FS", version=_VERSION)

_DB_NAME = "deepagents_mongodb_fs"
_COLLECTION_NAME = "demo_chunks"

WatcherType = Literal["polling", "sqs"]


class MongoFilesystemBackend:
    """DeepAgents BackendProtocol implementation powered by MongoDB Atlas.

    Args:
        s3_bucket_name: Name of the S3 bucket that is the source of truth.
        mongodb_connection_string: Atlas (or compatible) connection string.
        llm: Optional LangChain LLM instance (reserved for future use).
        embedding_model: LangChain Embeddings instance. Defaults to
            ``OpenAIEmbeddings(model='text-embedding-3-small', dimensions=1024)``.
        embedding_dimensions: Embedding vector size (default 1024).
        watcher: ``"polling"`` (default) or ``"sqs"``.
        sqs_queue_url: Required when ``watcher="sqs"``.
        aws_region: AWS region for S3 and SQS clients.
        s3_prefix: Only sync/watch objects under this S3 prefix.
        debug: If True, re-raise exceptions after logging (local dev only).
    """

    def __init__(
        self,
        s3_bucket_name: str,
        mongodb_connection_string: str,
        llm: Any = None,
        embedding_model: Any = None,
        embedding_dimensions: int = 1024,
        watcher: WatcherType = "polling",
        sqs_queue_url: str | None = None,
        aws_region: str | None = None,
        s3_prefix: str = "",
        debug: bool = False,
    ) -> None:
        if not s3_bucket_name:
            raise AdapterError(ErrorCode.E1001_MISSING_CONFIG, "s3_bucket_name is required")
        if not mongodb_connection_string:
            raise AdapterError(ErrorCode.E1001_MISSING_CONFIG, "mongodb_connection_string is required")
        if watcher == "sqs" and not sqs_queue_url:
            raise AdapterError(ErrorCode.E1001_MISSING_CONFIG, "sqs_queue_url is required when watcher='sqs'")

        self.debug = debug
        self._prefix = s3_prefix

        # Build internal components
        self._store = S3Backend(bucket_name=s3_bucket_name, region_name=aws_region)
        self._embedder = Embedder(model=embedding_model, dimensions=embedding_dimensions)
        self._chunker = Chunker()

        mongo_client = pymongo.MongoClient(mongodb_connection_string, driver=_DRIVER_INFO)
        self._col: Collection = mongo_client[_DB_NAME][_COLLECTION_NAME]  # type: ignore[type-arg]

        self._index_manager = IndexManager(self._col, embedding_dimensions=embedding_dimensions)
        self._sync = InitialSync(self._store, self._chunker, self._embedder, self._col)
        self._search = SearchRouter(self._col, self._embedder)

        # Build watcher
        if watcher == "sqs":
            self._watcher: S3Watcher = SQSWatcher(
                store=self._store,
                chunker=self._chunker,
                embedder=self._embedder,
                collection=self._col,
                queue_url=sqs_queue_url,  # type: ignore[arg-type]
                region_name=aws_region,
            )
        else:
            self._watcher = PollingWatcher(
                store=self._store,
                chunker=self._chunker,
                embedder=self._embedder,
                collection=self._col,
                prefix=s3_prefix,
            )

        # Gate that blocks grep/glob/ls until the first sync completes
        self._ready = threading.Event()

        # Background init thread
        self._init_thread = threading.Thread(
            target=self._background_init, name="MongoFSInit", daemon=True
        )
        self._init_thread.start()

    # ------------------------------------------------------------------
    # Background initialization
    # ------------------------------------------------------------------

    def _background_init(self) -> None:
        try:
            logger.info("Provisioning indexes…")
            self._index_manager.ensure_indexes()
            self._index_manager.wait_until_queryable()
        except Exception as exc:
            logger.error("Index provisioning failed: %s", exc)

        try:
            logger.info("Running initial sync…")
            report = self._sync.run(prefix=self._prefix)
            logger.info("Initial sync complete: %s", report)
        except Exception as exc:
            logger.error("Initial sync failed: %s", exc)

        self._ready.set()

        try:
            logger.info("Starting watcher…")
            self._watcher.start()
        except Exception as exc:
            logger.error("Watcher start failed: %s", exc)

    def _wait_ready(self) -> None:
        """Block until at least one sync pass has completed."""
        self._ready.wait()

    # ------------------------------------------------------------------
    # Search operations (route through SearchRouter after sync)
    # ------------------------------------------------------------------

    @adapter_boundary(ErrorCode.E5001_GREP_FAILED)
    def grep(self, pattern: str, path: str = "", glob: str = "") -> GrepResult:
        """Search file contents using hybrid MongoDB search.

        Args:
            pattern: Natural-language or keyword query.
            path: Restrict search to this path prefix.
            glob: Restrict to filenames matching this glob pattern.

        Returns:
            GrepResult with ranked GrepMatch entries.
        """
        self._wait_ready()
        return self._search.grep(pattern, path, glob)

    @adapter_boundary(ErrorCode.E5002_GLOB_FAILED)
    def glob(self, pattern: str, path: str = "") -> GlobResult:
        """Find files whose names match *pattern*.

        Args:
            pattern: Glob pattern (e.g. ``"*.pdf"``).
            path: Restrict to this path prefix.

        Returns:
            GlobResult with matching file paths.
        """
        self._wait_ready()
        return self._search.glob(pattern, path)

    @adapter_boundary(ErrorCode.E5003_LS_FAILED)
    def ls(self, path: str) -> LsResult:
        """List immediate children of *path*.

        Args:
            path: Virtual directory path (e.g. ``"docs/"``).

        Returns:
            LsResult with file and directory entries.
        """
        self._wait_ready()
        return self._search.ls(path)

    # ------------------------------------------------------------------
    # Pass-through operations (go directly to the object store)
    # ------------------------------------------------------------------

    @adapter_boundary(ErrorCode.E2002_OBJECT_READ_FAILED)
    def read(self, path: str, offset: int = 0, limit: int = -1) -> ReadResult:
        """Read raw bytes from the object store.

        Args:
            path: Object key.
            offset: Byte offset to start from.
            limit: Maximum bytes to return (-1 = all).

        Returns:
            ReadResult with the file content as a decoded string.
        """
        data = self._store.read(path, offset, limit)
        return ReadResult(path=path, content=data.decode("utf-8", errors="replace"))

    @adapter_boundary(ErrorCode.E2003_OBJECT_WRITE_FAILED)
    def write(self, path: str, content: str) -> WriteResult:
        """Write *content* to *path* in the object store.

        Args:
            path: Object key.
            content: String content to write.

        Returns:
            WriteResult indicating success.
        """
        self._store.write(path, content.encode("utf-8"))
        return WriteResult(path=path, success=True)

    @adapter_boundary(ErrorCode.E2008_EDIT_CONFLICT)
    def edit(self, path: str, old: str, new: str, replace_all: bool = False) -> EditResult:
        """Edit *path* by replacing *old* with *new*.

        Performs a conditional read-modify-write with ETag verification.

        Args:
            path: Object key.
            old: Substring to find.
            new: Replacement string.
            replace_all: Replace every occurrence if True, else only the first.

        Returns:
            EditResult indicating success.
        """
        self._store.edit(path, old, new, replace_all)
        return EditResult(path=path, success=True)

    @adapter_boundary(ErrorCode.E2006_UPLOAD_FAILED)
    def upload_files(self, files: list[tuple[str, bytes]]) -> UploadResult:
        """Upload multiple files to the object store.

        Args:
            files: List of ``(path, bytes)`` tuples.

        Returns:
            UploadResult with lists of uploaded and failed paths.
        """
        uploaded = self._store.upload_files(files)
        return UploadResult(uploaded=uploaded)

    @adapter_boundary(ErrorCode.E2007_DOWNLOAD_FAILED)
    def download_files(self, paths: list[str]) -> DownloadResult:
        """Download multiple files from the object store.

        Args:
            paths: List of object keys to download.

        Returns:
            DownloadResult with ``(path, bytes)`` tuples for successful downloads.
        """
        results = self._store.download_files(paths)
        return DownloadResult(downloaded=[r[0] for r in results])

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Gracefully stop the background watcher."""
        self._watcher.stop()

    def __enter__(self) -> "MongoFilesystemBackend":
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop()
