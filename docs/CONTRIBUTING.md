# Contributing Guide

## Development setup

```bash
git clone https://github.com/anujpanchal57/deepagents_mongodb_fs
cd deepagents_mongodb_fs
pip install -e ".[dev]"
```

Run the test suite:

```bash
pytest -m unit            # fast, no external services
pytest -m integration     # requires moto + mongomock (auto-installed)
pytest -m e2e             # full stack (requires real Atlas + S3)
```

---

## Adding a new object store backend

The adapter isolates all object-store specifics behind `ObjectStoreBackend`. Adding Azure Blob Storage (or GCS, local disk, etc.) requires implementing exactly one class and one optional watcher.

### Step 1 — Implement `ObjectStoreBackend`

Create `src/deepagents_mongodb_fs/backends/azure_blob.py`:

```python
from azure.storage.blob import BlobServiceClient
from deepagents_mongodb_fs.backends.base import ObjectStoreBackend
from deepagents_mongodb_fs.errors import AdapterError, ErrorCode
from typing import Iterator

class AzureBlobBackend(ObjectStoreBackend):
    def __init__(self, connection_string: str, container_name: str) -> None:
        self._client = BlobServiceClient.from_connection_string(connection_string)
        self._container = container_name
        self._container_client = self._client.get_container_client(container_name)

    def read(self, path: str, offset: int = 0, limit: int = -1) -> bytes:
        blob = self._container_client.get_blob_client(blob=self.normalize_key(path))
        try:
            data = blob.download_blob().readall()
        except Exception as exc:
            if "BlobNotFound" in str(exc):
                raise AdapterError(ErrorCode.E2001_OBJECT_NOT_FOUND, str(exc)) from exc
            raise AdapterError(ErrorCode.E2002_OBJECT_READ_FAILED, str(exc)) from exc
        if offset or limit >= 0:
            end = None if limit < 0 else offset + limit
            data = data[offset:end]
        return data

    def write(self, path: str, content: bytes) -> None:
        blob = self._container_client.get_blob_client(blob=self.normalize_key(path))
        try:
            blob.upload_blob(content, overwrite=True)
        except Exception as exc:
            raise AdapterError(ErrorCode.E2003_OBJECT_WRITE_FAILED, str(exc)) from exc

    def edit(self, path: str, old: str, new: str, replace_all: bool = False) -> None:
        data = self.read(path)
        text = data.decode("utf-8")
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        self.write(path, updated.encode("utf-8"))

    def upload_files(self, files):
        uploaded = []
        for path, content in files:
            self.write(path, content)
            uploaded.append(path)
        return uploaded

    def download_files(self, paths):
        return [(p, self.read(p)) for p in paths]

    def list_keys(self, prefix: str = "") -> Iterator[tuple[str, str]]:
        for blob in self._container_client.list_blobs(name_starts_with=prefix):
            etag = blob.etag or ""
            yield blob.name, etag.strip('"')
```

### Step 2 — (Optional) Implement a watcher

If Azure Blob Storage Event Grid / Service Bus is available, extend `S3Watcher`:

```python
from deepagents_mongodb_fs.watcher.base import S3Watcher
import threading

class AzureBlobWatcher(S3Watcher):
    def __init__(self, store, chunker, embedder, collection, event_hub_connection_string, ...):
        super().__init__(store, chunker, embedder, collection)
        # set up Azure Event Hub consumer here
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self):
        while not self._stop.is_set():
            # receive events and call self.on_created / self.on_updated / self.on_deleted
            pass
```

### Step 3 — Wire it into `MongoFilesystemBackend`

In `backend.py`, the constructor accepts `ObjectStoreBackend` instances. Until the package officially supports Azure, callers can inject it directly:

```python
from deepagents_mongodb_fs.backends.azure_blob import AzureBlobBackend
from deepagents_mongodb_fs.backend import MongoFilesystemBackend

# Temporarily override the store after construction:
backend = MongoFilesystemBackend.__new__(MongoFilesystemBackend)
backend._store = AzureBlobBackend(connection_string="...", container_name="my-container")
# ... wire remaining components
```

A cleaner approach (coming in v0.2.0) will accept a `store: ObjectStoreBackend` constructor parameter directly.

### Step 4 — Add tests

Mirror the test structure in `tests/unit/test_s3_backend.py` but use the Azure SDK's mock library (`azure-storage-blob` ships with a `MockBlobService`).

### Step 5 — Submit a PR

- Add your backend to `src/deepagents_mongodb_fs/backends/__init__.py`
- Add the dependency to `pyproject.toml` as an optional extra (e.g. `azure = ["azure-storage-blob>=12.0"]`)
- Update `CONTRIBUTING.md` and `README.md`
- Ensure CI passes on Linux + macOS + Windows

---

## Code style

- Ruff for linting: `ruff check src/ tests/`
- MyPy for type checking: `mypy src/deepagents_mongodb_fs/`
- No comments unless the WHY is non-obvious
- Every public class and method must have a docstring

## Commit convention

```
feat: add AzureBlobBackend
fix: handle empty page_text in PDF extractor
chore: bump pypdf to 4.3.0
test: add PollingWatcher integration tests
```
