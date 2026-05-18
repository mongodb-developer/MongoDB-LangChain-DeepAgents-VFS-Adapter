# Low-Level Design: deepagents_mongodb_fs

## Table of Contents

1. [Module Dependency Graph](#1-module-dependency-graph)
2. [Class Catalogue](#2-class-catalogue)
   - 2.1 [MongoFilesystemBackend](#21-mongofilesystembackend)
   - 2.2 [ObjectStoreBackend (ABC)](#22-objectstorebackend-abc)
   - 2.3 [S3Backend](#23-s3backend)
   - 2.4 [Chunker](#24-chunker)
   - 2.5 [Embedder](#25-embedder)
   - 2.6 [IndexManager](#26-indexmanager)
   - 2.7 [InitialSync](#27-initialsync)
   - 2.8 [SearchRouter](#28-searchrouter)
   - 2.9 [S3Watcher (ABC)](#29-s3watcher-abc)
   - 2.10 [PollingWatcher](#210-pollingwatcher)
   - 2.11 [SQSWatcher](#211-sqswatcher)
3. [Data Types & DTOs](#3-data-types--dtos)
4. [Error Taxonomy](#4-error-taxonomy)
5. [Threading Model](#5-threading-model)
6. [Algorithms](#6-algorithms)
   - 6.1 [Token-aware Chunking](#61-token-aware-chunking)
   - 6.2 [ETag-based Change Detection](#62-etag-based-change-detection)
   - 6.3 [Hybrid Search Pipeline Construction](#63-hybrid-search-pipeline-construction)
   - 6.4 [ls Aggregation Pipeline](#64-ls-aggregation-pipeline)
   - 6.5 [Watcher Diff Algorithm (Polling)](#65-watcher-diff-algorithm-polling)
   - 6.6 [SQS Message Processing](#66-sqs-message-processing)
   - 6.7 [OOXML Format Sniffing](#67-ooxml-format-sniffing)
7. [MongoDB Aggregation Pipelines](#7-mongodb-aggregation-pipelines)
8. [Configuration Constants](#8-configuration-constants)
9. [Sequence Diagrams](#9-sequence-diagrams)
10. [Error Boundary Mechanics](#10-error-boundary-mechanics)
11. [Index Definitions](#11-index-definitions)

---

## 1. Module Dependency Graph

```
backend.py (MongoFilesystemBackend)
  ├── backends/s3.py (S3Backend)
  │     └── backends/base.py (ObjectStoreBackend ABC)
  ├── chunker.py (Chunker)
  ├── embedder.py (Embedder)
  ├── index_manager.py (IndexManager)
  ├── sync.py (InitialSync)
  │     ├── backends/base.py
  │     ├── chunker.py
  │     ├── embedder.py
  │     └── dtypes.py (FileRecord, SyncReport)
  ├── search.py (SearchRouter)
  │     ├── embedder.py
  │     └── dtypes.py (GrepResult, GlobResult, LsResult, …)
  ├── watcher/base.py (S3Watcher ABC)
  │     ├── backends/base.py
  │     ├── chunker.py
  │     ├── embedder.py
  │     └── dtypes.py (FileRecord)
  ├── watcher/polling.py (PollingWatcher)
  │     └── watcher/base.py
  ├── watcher/sqs.py (SQSWatcher)
  │     └── watcher/base.py
  ├── dtypes.py  (all DTOs)
  └── errors.py  (AdapterError, ErrorCode, adapter_boundary)

External dependencies:
  pymongo, boto3/botocore, langchain_core, langchain_openai,
  langchain_aws, langchain_mongodb, tiktoken, tenacity,
  pypdf, python-docx, openpyxl, xlrd, python-pptx, olefile
```

---

## 2. Class Catalogue

### 2.1 MongoFilesystemBackend

**File:** `backend.py`  
**Purpose:** Public facade implementing DeepAgents `BackendProtocol`. Wires all components, manages the background init thread, and enforces the `@adapter_boundary` contract at every public method.

#### Constants

```python
_DB_NAME         = "deepagents_mongodb_fs"
_COLLECTION_NAME = "aws_mdb_resuts_chunks"
```

#### Attributes

| Attribute | Type | Description |
|---|---|---|
| `debug` | `bool` | Re-raise exceptions when True |
| `_prefix` | `str` | S3 key prefix filter |
| `_store` | `S3Backend` | Storage backend instance |
| `_embedder` | `Embedder` | Embedding model wrapper |
| `_chunker` | `Chunker` | Document chunker |
| `_col` | `pymongo.Collection` | MongoDB chunks collection |
| `_index_manager` | `IndexManager` | Atlas index provisioner |
| `_sync` | `InitialSync` | One-time sync engine |
| `_search` | `SearchRouter` | Query router |
| `_watcher` | `S3Watcher` | Incremental sync watcher |
| `_ready` | `threading.Event` | Gates search ops until first sync |
| `_init_thread` | `threading.Thread` | Background init daemon thread |

#### Methods

```python
def __init__(
    self,
    s3_bucket_name: str,                 # required
    mongodb_connection_string: str,      # required
    llm: Any = None,                     # reserved, unused
    embedding_model: Any = None,         # LangChain Embeddings; default: OpenAIEmbeddings
    embedding_dimensions: int = 1024,
    watcher: Literal["polling","sqs"] = "polling",
    sqs_queue_url: str | None = None,    # required if watcher="sqs"
    aws_region: str | None = None,
    s3_prefix: str = "",
    debug: bool = False,
) -> None
```

**Validation (synchronous, raises `AdapterError` immediately):**
- `not s3_bucket_name` → `E1001`
- `not mongodb_connection_string` → `E1001`
- `watcher == "sqs" and not sqs_queue_url` → `E1001`

**Watcher selection logic:**
```python
if watcher == "sqs":
    self._watcher = SQSWatcher(store, chunker, embedder, col, queue_url, region)
else:
    self._watcher = PollingWatcher(store, chunker, embedder, col, prefix)
```

**Background thread:**
```python
self._init_thread = threading.Thread(
    target=self._background_init,
    name="MongoFSInit",
    daemon=True,   # dies when main thread exits
)
self._init_thread.start()
```

```python
def _background_init(self) -> None:
    # Step 1: provision indexes (non-fatal on failure — logs, continues)
    try:
        self._index_manager.ensure_indexes()
        self._index_manager.wait_until_queryable()
    except Exception: ...

    # Step 2: initial sync (non-fatal)
    try:
        report = self._sync.run(prefix=self._prefix)
    except Exception: ...

    # Step 3: unblock search ops ALWAYS (even if steps 1/2 failed)
    self._ready.set()

    # Step 4: start watcher (non-fatal)
    try:
        self._watcher.start()
    except Exception: ...
```

**Public method signatures (all decorated with `@adapter_boundary`):**

```python
@adapter_boundary(ErrorCode.E5001_GREP_FAILED)
def grep(self, pattern: str, path: str = "", glob: str = "") -> GrepResult

@adapter_boundary(ErrorCode.E5002_GLOB_FAILED)
def glob(self, pattern: str, path: str = "") -> GlobResult

@adapter_boundary(ErrorCode.E5003_LS_FAILED)
def ls(self, path: str) -> LsResult

@adapter_boundary(ErrorCode.E2002_OBJECT_READ_FAILED)
def read(self, path: str, offset: int = 0, limit: int = -1) -> ReadResult

@adapter_boundary(ErrorCode.E2003_OBJECT_WRITE_FAILED)
def write(self, path: str, content: str) -> WriteResult

@adapter_boundary(ErrorCode.E2008_EDIT_CONFLICT)
def edit(self, path: str, old: str, new: str, replace_all: bool = False) -> EditResult

@adapter_boundary(ErrorCode.E2006_UPLOAD_FAILED)
def upload_files(self, files: list[tuple[str, bytes]]) -> UploadResult

@adapter_boundary(ErrorCode.E2007_DOWNLOAD_FAILED)
def download_files(self, paths: list[str]) -> DownloadResult

def stop(self) -> None   # calls self._watcher.stop()
def __enter__(self) -> "MongoFilesystemBackend"
def __exit__(self, *_) -> None   # calls stop()
```

**`read` implementation detail:**
```python
data = self._store.read(path, offset, limit)
return ReadResult(path=path, content=data.decode("utf-8", errors="replace"))
```

**`write` implementation detail:**
```python
self._store.write(path, content.encode("utf-8"))
return WriteResult(path=path, success=True)
```

---

### 2.2 ObjectStoreBackend (ABC)

**File:** `backends/base.py`  
**Purpose:** Storage-agnostic interface. Decouples all higher-level components from S3 specifics.

#### Abstract Methods

```python
@abstractmethod
def read(self, path: str, offset: int = 0, limit: int = -1) -> bytes

@abstractmethod
def write(self, path: str, content: bytes) -> None

@abstractmethod
def edit(self, path: str, old: str, new: str, replace_all: bool = False) -> None

@abstractmethod
def upload_files(self, files: list[tuple[str, bytes]]) -> list[str]

@abstractmethod
def download_files(self, paths: list[str]) -> list[tuple[str, bytes]]

@abstractmethod
def list_keys(self, prefix: str = "") -> Iterator[tuple[str, str]]
```

#### Concrete Helper (inherited by all subclasses)

```python
@staticmethod
def normalize_key(path: str) -> str:
    return str(PurePosixPath(path.replace("\\", "/")))
```

---

### 2.3 S3Backend

**File:** `backends/s3.py`  
**Inherits:** `ObjectStoreBackend`

#### Attributes

| Attribute | Type | Description |
|---|---|---|
| `_bucket` | `str` | Bucket name |
| `_client` | `boto3.S3Client` | boto3 S3 client |

#### Constructor

```python
def __init__(
    self,
    bucket_name: str,
    region_name: str | None = None,
    endpoint_url: str | None = None,   # for LocalStack / moto
    **boto_kwargs: Any,
) -> None:
    self._bucket = bucket_name
    self._client = boto3.client("s3", region_name=region_name,
                                endpoint_url=endpoint_url, **boto_kwargs)
    self._verify_bucket()   # raises E1002 if bucket unreachable
```

#### Method Detail

**`_verify_bucket()`**
```python
self._client.head_bucket(Bucket=self._bucket)
# ClientError code "404" or "NoSuchBucket" → E1002_INVALID_BUCKET
# Any other ClientError → E2002_OBJECT_READ_FAILED
```

**`_key(path)`** — normalizes path to S3 key (no leading slash)
```python
return self.normalize_key(path).lstrip("/")
```

**`read(path, offset=0, limit=-1) → bytes`**
```python
key = self._key(path)
range_header = {}
if offset > 0 or limit >= 0:
    end = "" if limit < 0 else str(offset + limit - 1)
    range_header["Range"] = f"bytes={offset}-{end}"
response = self._client.get_object(Bucket=self._bucket, Key=key, **range_header)
return response["Body"].read()
# NoSuchKey / 404 → E2001_OBJECT_NOT_FOUND
# Other ClientError → E2002_OBJECT_READ_FAILED
```

**`edit(path, old, new, replace_all=False)`** — CAS (Compare-and-Swap)
```python
# Phase 1: Read with ETag capture
head = self._client.head_object(Bucket=self._bucket, Key=key)
etag = head["ETag"]
body = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
text = body.decode("utf-8")

# Phase 2: Modify
updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)

# Phase 3: Conditional write
self._client.put_object(
    Bucket=self._bucket, Key=key,
    Body=updated.encode("utf-8"),
    **{"IfMatch": etag} if etag else {},
)
# PreconditionFailed → E2008_EDIT_CONFLICT (concurrent modification detected)
```

**`list_keys(prefix="") → Iterator[tuple[str, str]]`**
```python
paginator = self._client.get_paginator("list_objects_v2")
for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
    for obj in page.get("Contents", []):
        yield obj["Key"], obj.get("ETag", "").strip('"')
# ClientError → E2005_LIST_FAILED
```

**`get_etag(key) → str | None`** _(not in ABC; called via `getattr` in watcher)_
```python
head = self._client.head_object(Bucket=self._bucket, Key=key)
return head["ETag"].strip('"')
# ClientError → return None
```

---

### 2.4 Chunker

**File:** `chunker.py`

#### Constants

```python
_ENCODING            = "cl100k_base"   # tiktoken encoding
_DEFAULT_TOKEN_LIMIT = 512
_DEFAULT_OVERLAP     = 64
_OLE_MAGIC           = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # OLE2 magic bytes

_SUPPORTED_EXTENSIONS = {
    ".txt", ".md", ".rst", ".csv",
    ".pdf", ".docx",
    ".xlsx", ".xls",
    ".pptx", ".ppt",
}
```

#### Attributes

| Attribute | Type | Description |
|---|---|---|
| `_token_limit` | `int` | Max tokens per chunk |
| `_overlap` | `int` | Overlap tokens between consecutive chunks |
| `_enc` | `tiktoken.Encoding` | `cl100k_base` encoder instance |

#### Public Method

```python
def chunk(self, path: str, data: bytes) -> list[Chunk]:
    ext = PurePosixPath(path).suffix.lower()
    pages = self._extract(data, ext)        # list[tuple[int, str]]
    chunks: list[Chunk] = []
    chunk_index = 0
    for page_number, page_text in pages:
        for chunk_text, char_start, char_end, line_start in self._split(page_text):
            chunks.append(Chunk(
                source_path=path,
                chunk_index=chunk_index,
                content=chunk_text,
                page_number=page_number,
                char_start=char_start,
                char_end=char_end,
                line_start=line_start,
            ))
            chunk_index += 1
    return chunks
```

#### Format Extraction Dispatch

```python
def _extract(self, data: bytes, ext: str) -> list[tuple[int, str]]:
    # Extension-based dispatch (fast path)
    if ext in (".txt", ".md", ".rst", ".csv"):  → _extract_text()
    if ext == ".pdf":                            → _extract_pdf()
    if ext == ".docx":                           → _extract_docx()
    if ext == ".xlsx":                           → _extract_xlsx()
    if ext == ".xls":                            → _extract_xls()
    if ext == ".pptx":                           → _extract_pptx()
    if ext == ".ppt":                            → _extract_ppt()

    # Magic-byte fallback
    if data[:4] == b"%PDF":                      → _extract_pdf()
    if data[:2] == b"PK":                        → _sniff_ooxml() → dispatch
    if data[:8] == _OLE_MAGIC:                   → _extract_xls() or _extract_ppt()
    else:                                        → _extract_text() (with warning)
```

#### Per-Format Extractor Return Shapes

| Format | Pages | Page unit | Library |
|---|---|---|---|
| txt/md/rst/csv | `[(0, full_text)]` | Whole file | Built-in |
| pdf | `[(i, page_text) for i in range(n_pages)]` | PDF page | pypdf |
| docx | `[(0, joined_paragraphs)]` | Whole doc | python-docx |
| xlsx | `[(i, tsv_rows) for i, sheet in enumerate(sheets)]` | Worksheet | openpyxl |
| xls | `[(i, tsv_rows) for i, sheet in enumerate(sheets)]` | Worksheet | xlrd |
| pptx | `[(i, shapes+notes) for i, slide in enumerate(slides)]` | Slide | python-pptx |
| ppt | `[(0, scanned_text)]` | Whole file (best-effort) | olefile |

#### PDF Extraction Detail

```python
reader = PdfReader(io.BytesIO(data), strict=False)
for i, page in enumerate(reader.pages):
    text = page.extract_text(extraction_mode="layout") or ""
    if not text.strip():
        text = page.extract_text() or ""   # plain fallback
    pages.append((i, text))
```

`strict=False` tolerates malformed float tokens in reportlab/graphics-heavy PDFs. Layout mode preserves column order and table structure; plain mode is the fallback.

#### XLSX Extraction Detail

```python
wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
for page_number, sheet in enumerate(wb.worksheets):
    lines = [f"# Sheet: {sheet.title}"]
    for row in sheet.iter_rows(values_only=True):
        cells = ["" if v is None else str(v) for v in row]
        if any(c.strip() for c in cells):
            lines.append("\t".join(cells))
    pages.append((page_number, "\n".join(lines)))
```

Rows rendered as TSV lines. Header prepended as `# Sheet: <name>`.

#### Legacy .ppt Extraction Detail

Uses `olefile` to open the OLE2 container, reads the `PowerPoint Document` stream, then applies `_scan_ole_strings()`:

```python
@staticmethod
def _scan_ole_strings(blob: bytes, min_len: int = 4) -> str:
    # Pass 1: UTF-16LE runs — pairs of (printable_ascii, 0x00)
    for i in range(len(blob)):
        scan for runs of (byte in 0x20-0x7E, next_byte == 0x00)
        emit if run length >= min_len

    # Pass 2: ASCII runs — bytes in range 0x20-0x7E + tab/LF/CR
    scan blob for printable ASCII runs >= min_len

    return "\n".join(extracted_strings)
```

#### Token-Aware Splitter

```python
def _split(self, text: str) -> list[tuple[str, int, int, int]]:
    tokens = self._enc.encode(text)
    results = []
    start = 0
    while start < len(tokens):
        end = min(start + self._token_limit, len(tokens))
        chunk_tokens = tokens[start:end]
        chunk_text = self._enc.decode(chunk_tokens)
        char_start = self._token_offset(text, tokens[:start])
        char_end   = char_start + len(chunk_text)
        line_start = text[:char_start].count("\n")
        results.append((chunk_text, char_start, char_end, line_start))
        if end == len(tokens):
            break
        start += self._token_limit - self._overlap   # advance with overlap
    return results

def _token_offset(self, text: str, prefix_tokens: Sequence[int]) -> int:
    if not prefix_tokens:
        return 0
    return len(self._enc.decode(list(prefix_tokens)))
```

**Sliding window visualization:**
```
tokens:  [0 ........... 511 | 448 ........... 959 | 896 ........... eof]
           chunk 0 (512t)      chunk 1 (512t)        chunk 2
           |<-- overlap=64 -->|<-- overlap=64 -->|
```

---

### 2.5 Embedder

**File:** `embedder.py`

#### Constants

```python
_DEFAULT_PROVIDER       = "bedrock"
_DEFAULT_MODEL          = "text-embedding-3-small"
_BEDROCK_DEFAULT_MODEL  = "amazon.titan-embed-text-v2:0"
_DEFAULT_DIMENSIONS     = 1024
_DEFAULT_BATCH_SIZE     = 100
```

#### Attributes

| Attribute | Type | Description |
|---|---|---|
| `_dimensions` | `int` | Expected vector dimensionality |
| `_batch_size` | `int` | Texts per API call |
| `_model` | `langchain_core.embeddings.Embeddings` | Provider model instance |

#### Constructor Resolution Logic

```python
if model is not None:
    self._model = model   # injected directly (testing / custom providers)
else:
    provider = os.getenv("EMBEDDING_PROVIDER", "bedrock").lower()
    resolved_name = (
        model_name
        or os.getenv("EMBEDDING_MODEL")
        or ("text-embedding-3-small" if provider == "openai"
            else "amazon.titan-embed-text-v2:0")
    )
    self._model = self._build_model(provider, resolved_name, dimensions)
```

#### `_build_model(provider, model_name, dimensions) → Embeddings`

```python
if provider == "openai":
    from langchain_openai import OpenAIEmbeddings
    return OpenAIEmbeddings(model=model_name, dimensions=dimensions)

if provider == "bedrock":
    from langchain_aws import BedrockEmbeddings
    region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    return BedrockEmbeddings(model_id=model_name, region_name=region)

raise AdapterError(E1001, f"Unknown EMBEDDING_PROVIDER '{provider}'")
```

#### `embed_batch(chunks) → list[list[float]]`

```python
texts = [c.content for c in chunks]
vectors = []
for i in range(0, len(texts), self._batch_size):    # batch=100
    batch = texts[i : i + self._batch_size]
    batch_vectors = self._embed_with_retry(batch)
    vectors.extend(batch_vectors)

# Dimension validation
if vectors and len(vectors[0]) != self._dimensions:
    raise AdapterError(E4002, f"Expected {self._dimensions} dims, got {len(vectors[0])}")
return vectors
```

#### `_embed_with_retry(texts) → list[list[float]]`

```python
delays = [1, 2, 4, 8, 16]     # seconds (total wait: up to 31s)
for delay in delays:
    try:
        return self._model.embed_documents(texts)
    except Exception as exc:
        if "rate" in str(exc).lower() or "429" in str(exc):
            time.sleep(delay)           # rate limit: retry
        else:
            raise AdapterError(E4001, str(exc))   # non-retryable: fail fast
raise AdapterError(E4003, str(last_exc))           # exhausted all retries
```

---

### 2.6 IndexManager

**File:** `index_manager.py`

#### Constants

```python
_DEFAULT_WAIT_SECONDS = 600   # 10 minutes
```

#### Attributes

| Attribute | Type | Description |
|---|---|---|
| `_col` | `pymongo.Collection` | Target collection |
| `_dims` | `int` | Vector dimensionality |
| `_timeout` | `int` | Max seconds to wait for index readiness |
| `_is_atlas` | `bool \| None` | Atlas detection result (lazy cache) |

#### `ensure_indexes()`

```python
self._ensure_native_index()        # always
if self._check_atlas():
    self._ensure_vector_search_index()
    self._ensure_fulltext_search_index()
else:
    logger.warning("Non-Atlas MongoDB — skipping Atlas indexes")
```

#### `_check_atlas() → bool`

```python
if self._is_atlas is not None:
    return self._is_atlas
try:
    self._col.list_search_indexes()   # Atlas-only API
    self._is_atlas = True
except (AttributeError, TypeError, OperationFailure):
    # mongomock → TypeError; community Mongo → OperationFailure
    self._is_atlas = False
return self._is_atlas
```

#### `_ensure_native_index()`

```python
self._col.create_index(
    [("source_path", 1), ("chunk_index", 1)],
    unique=True,
    name="source_path_chunk_index_unique",
)
# OperationFailure with "already exists" → silently skip
# Other OperationFailure → E1004
```

#### `_ensure_vector_search_index()`

```python
index_name = "vector_search_embedding"
existing = {idx["name"] for idx in self._col.list_search_indexes()}
if index_name in existing:
    return   # already exists

create_vector_search_index(
    collection=self._col,
    index_name=index_name,
    path="embedding",
    dimensions=self._dims,
    similarity="cosine",
    filters=["source_path", "filename"],   # filterable fields for $vectorSearch
)
```

#### `_ensure_fulltext_search_index()` — with migration support

```python
index_name = "fulltext_search_content_filename"
definition = {
    "mappings": {
        "dynamic": False,
        "fields": {
            "content":     {"type": "string", "analyzer": "lucene.standard"},
            "filename":    {"type": "string", "analyzer": "lucene.keyword"},
            "source_path": {"type": "string", "analyzer": "lucene.keyword"},
        },
    }
}
existing = {idx["name"]: idx for idx in self._col.list_search_indexes()}

if index_name not in existing:
    self._col.create_search_index({"name": index_name, "type": "search",
                                   "definition": definition})
    return

# Migration check: verify filename uses lucene.keyword and source_path exists
current_fields = existing[index_name]["latestDefinition"]["mappings"]["fields"]
correct = (
    current_fields.get("filename", {}).get("analyzer") == "lucene.keyword"
    and "source_path" in current_fields
)
if not correct:
    self._col.update_search_index(index_name, definition)
```

#### `wait_until_queryable(timeout=None) → bool`

```python
deadline = time.monotonic() + (timeout or self._timeout)
poll_interval = 10   # seconds between checks
while time.monotonic() < deadline:
    if self._indexes_queryable():
        return True
    time.sleep(poll_interval)
return False   # timeout
```

#### `_indexes_queryable() → bool`

```python
states = {
    idx["name"]: (idx.get("queryable", False), idx.get("status", ""))
    for idx in self._col.list_search_indexes()
}
vector_ok   = states.get("vector_search_embedding",          (False,"")) == (True, "READY")
fulltext_ok = states.get("fulltext_search_content_filename", (False,"")) == (True, "READY")
return vector_ok and fulltext_ok
```

Checks both `queryable=True` AND `status="READY"`. During an index update, `queryable` stays `True` (old definition active), but `status` is `"UPDATING"`. Waiting for `READY` ensures the new definition is live.

---

### 2.7 InitialSync

**File:** `sync.py`

#### Constants

```python
_DOWNLOAD_WORKERS = 16    # ThreadPoolExecutor size
_UPSERT_BATCH     = 500   # bulk_write batch size
```

#### Attributes

| Attribute | Type | Description |
|---|---|---|
| `_store` | `ObjectStoreBackend` | Storage backend |
| `_chunker` | `Chunker` | Document chunker |
| `_embedder` | `Embedder` | Embedding model |
| `_col` | `pymongo.Collection` | Target collection |
| `_workers` | `int` | Concurrent download threads |

#### `run(prefix="", dry_run=False) → SyncReport`

```python
keys_etags = list(self._store.list_keys(prefix))   # [(key, etag), ...]
report.seen = len(keys_etags)

to_process = self._filter_changed(keys_etags)       # ETag diff
report.skipped = report.seen - len(to_process)

if dry_run:
    return report   # skip embedding + upsert

raw_objects = self._download_all(to_process)        # concurrent download

pending_records: list[FileRecord] = []
for key, etag, data in raw_objects:
    chunks  = self._chunker.chunk(key, data)
    vectors = self._embedder.embed_batch(chunks)
    filename = PurePosixPath(key).name
    for chunk, vector in zip(chunks, vectors):
        pending_records.append(FileRecord(...))
    if len(pending_records) >= _UPSERT_BATCH:       # flush batch
        self._upsert(pending_records)
        pending_records = []

if pending_records:                                 # flush remainder
    self._upsert(pending_records)

return report
```

#### `_filter_changed(keys_etags) → list[tuple[str, str]]`

```python
all_keys = [k for k, _ in keys_etags]

# Aggregate: one document per FILE (not per chunk) to minimize data transfer
stored = {}
for doc in self._col.aggregate([
    {"$match": {"source_path": {"$in": all_keys}}},
    {"$group": {"_id": "$source_path", "etag": {"$first": "$etag"}}},
]):
    stored[doc["_id"]] = doc["etag"]

return [(key, etag) for key, etag in keys_etags if stored.get(key) != etag]
```

#### `_download_all(to_process) → list[tuple[str, str, bytes]]`

```python
with ThreadPoolExecutor(max_workers=self._workers) as pool:
    future_map = {
        pool.submit(self._store.read, key): (key, etag)
        for key, etag in to_process
    }
    for future in as_completed(future_map):
        key, etag = future_map[future]
        try:
            data = future.result()
            results.append((key, etag, data))
        except Exception:
            logger.warning("Download failed for key '%s'", key)
return results
```

#### `_upsert(records) → None`

```python
ops = [
    UpdateOne(
        {"source_path": r.source_path, "chunk_index": r.chunk_index},
        {"$set": r.to_dict()},
        upsert=True,
    )
    for r in records
]
self._col.bulk_write(ops, ordered=False)   # ordered=False: continue past failures
# Exception → E3003_UPSERT_FAILED
```

---

### 2.8 SearchRouter

**File:** `search.py`

#### Constants

```python
_DEFAULT_GREP_LIMIT = 20
_DEFAULT_GLOB_LIMIT = 200
_DEFAULT_LS_LIMIT   = 1000
```

#### Attributes

| Attribute | Type | Description |
|---|---|---|
| `_col` | `pymongo.Collection` | Target collection |
| `_embedder` | `Embedder` | Embedding model |
| `_grep_limit` | `int` | Max grep results |
| `_glob_limit` | `int` | Max glob results |
| `_atlas` | `bool \| None` | Atlas availability cache (lazy) |

#### `ls(path) → LsResult`

```python
prefix = path.rstrip("/")
if prefix:
    prefix = prefix + "/"
escaped = re.escape(prefix)

pipeline = [
    {"$match": {"source_path": {"$regex": f"^{escaped}"}} if prefix else {}},
    {
        "$group": {
            "_id": {
                "$arrayElemAt": [
                    {"$split": [{"$substr": ["$source_path", len(prefix), -1]}, "/"]},
                    0,
                ]
            },
            "full_paths": {"$addToSet": "$source_path"},
        }
    },
    {"$sort": {"_id": 1}},
    {"$limit": _DEFAULT_LS_LIMIT},
]

docs = list(self._col.aggregate(pipeline))

for doc in docs:
    segment = doc["_id"]
    full_paths = doc["full_paths"]
    # is_dir: true if any stored path has more segments after this segment
    is_dir = any(
        p[len(prefix):].count("/") > 0 or not p[len(prefix):].endswith(segment)
        for p in full_paths
    )
    entries.append(LsEntry(name=segment, is_dir=is_dir))

return LsResult(path=path, entries=entries)
```

#### `glob(pattern, path="") → GlobResult`

```python
if self._is_atlas_available():
    return self._glob_atlas(pattern, path)
return self._glob_regex(pattern, path)
```

**`_glob_atlas(pattern, path)`:**
```python
pipeline = [
    {
        "$search": {
            "index": "fulltext_search_content_filename",
            "wildcard": {
                "query": pattern,
                "path": "source_path",
                "allowAnalyzedField": True,   # required: source_path uses lucene.keyword
            },
        }
    },
    # Path prefix filter AFTER $search (wildcard doesn't support pre-filter)
    *([{"$match": {"source_path": {"$regex": f"^{re.escape(path)}"}}}] if path else []),
    {"$group": {"_id": "$source_path"}},
    {"$sort": {"_id": 1}},
    {"$limit": self._glob_limit},
]
```

**`_glob_regex(pattern, path)`:**
```python
cursor = self._col.find(
    {"source_path": {"$regex": f"^{re.escape(path)}"}} if path else {},
    {"source_path": 1, "filename": 1, "_id": 0}
)
# Python fnmatch filter; deduplicate by seen set; cap at self._glob_limit
results = [sp for sp in cursor if sp not in seen and fnmatch.fnmatch(sp, pattern)]
```

#### `grep(pattern, path="", glob="") → GrepResult`

```python
if self._is_atlas_available():
    return self._grep_hybrid(pattern, path, glob)
return self._grep_regex(pattern, path, glob)
```

**`_grep_hybrid(pattern, path, glob)`:**
```python
# 1. Embed query
try:
    dummy_chunk = Chunk(source_path="", chunk_index=0, content=pattern)
    query_vector = self._embedder.embed_batch([dummy_chunk])[0]
except AdapterError:
    query_vector = None   # fallback to full-text only

# 2. Build path/glob filter for post-search $match
path_filter = []
if path:
    path_filter.append({"source_path": {"$regex": f"^{re.escape(path)}"}})
if glob:
    path_filter.append({"filename": {"$regex": fnmatch.translate(glob)}})
pre_filter = {"$and": path_filter} if path_filter else {}

# 3. Build pipeline
if query_vector is not None:
    vs_stage = vector_search_stage(
        query_vector, "embedding", "vector_search_embedding",
        top_k=self._grep_limit * 2,
        filter=None,              # $vectorSearch filter ≠ $regex; apply post-search
        oversampling_factor=5,    # numCandidates = top_k * 5
    )
    pipeline = [
        {
            "$rankFusion": {
                "input": {
                    "pipelines": {
                        "fulltext": [
                            {"$search": {"index": "fulltext_search_content_filename",
                                         "text": {"query": pattern, "path": "content"}}},
                            *([{"$match": pre_filter}] if pre_filter else []),
                        ],
                        "vector": [
                            vs_stage,
                            *([{"$match": pre_filter}] if pre_filter else []),
                        ],
                    }
                },
                "combination": {"weights": {"fulltext": 0.5, "vector": 0.5}},
            }
        },
        {"$limit": self._grep_limit},
        {"$project": {"source_path": 1, "line_start": 1, "content": 1, "_id": 0}},
    ]
else:
    # Full-text only (embedder unavailable)
    pipeline = [
        *text_search_stage(pattern, "content",
                           "fulltext_search_content_filename",
                           limit=self._grep_limit,
                           filter=pre_filter or None),
        {"$project": {"source_path": 1, "line_start": 1, "content": 1, "score": 1, "_id": 0}},
    ]

docs = list(self._col.aggregate(pipeline))
return self._dedupe_to_grep_result(docs)
```

**`_grep_regex(pattern, path, glob)`:**
```python
query = {"content": {"$regex": pattern, "$options": "i"}}
if path:
    query["source_path"] = {"$regex": f"^{re.escape(path)}"}
if glob:
    query["filename"] = {"$regex": fnmatch.translate(glob)}
cursor = self._col.find(
    query,
    {"source_path": 1, "line_start": 1, "content": 1, "_id": 0}
).limit(self._grep_limit)
```

**`_dedupe_to_grep_result(docs) → GrepResult`** (static)
```python
seen: set[tuple[str, int]] = set()
matches: list[GrepMatch] = []
for doc in docs:
    key = (doc["source_path"], doc["line_start"])
    if key in seen:
        continue
    seen.add(key)
    matches.append(GrepMatch(
        path=doc["source_path"],
        line=doc["line_start"],
        content=doc["content"],
        score=doc.get("score", 0.0),
    ))
return GrepResult(matches=matches)
```

**`_is_atlas_available() → bool`** (lazy, cached after first call)
```python
if self._atlas is not None:
    return self._atlas
try:
    self._col.list_search_indexes()   # Atlas-only method
    self._atlas = True
except (AttributeError, Exception):
    self._atlas = False
return self._atlas
```

---

### 2.9 S3Watcher (ABC)

**File:** `watcher/base.py`

#### Attributes

| Attribute | Type | Description |
|---|---|---|
| `_store` | `ObjectStoreBackend` | Storage backend |
| `_chunker` | `Chunker` | Document chunker |
| `_embedder` | `Embedder` | Embedding model |
| `_col` | `pymongo.Collection` | Target collection |

#### Abstract Methods

```python
@abstractmethod
def start(self) -> None

@abstractmethod
def stop(self) -> None
```

#### Shared Callbacks

```python
def on_created(self, key: str) -> None:
    self._ingest(key)

def on_updated(self, key: str) -> None:
    self._col.delete_many({"source_path": key})   # purge stale chunks first
    self._ingest(key)

def on_deleted(self, key: str) -> None:
    result = self._col.delete_many({"source_path": key})
    logger.info("Deleted %d chunks for key '%s'", result.deleted_count, key)
```

#### Shared Ingestion Pipeline

```python
def _ingest(self, key: str) -> None:
    data = self._store.read(key)   # AdapterError → log and return
    etag = getattr(self._store, "get_etag", lambda k: "")(key) or ""

    chunks  = self._chunker.chunk(key, data)
    vectors = self._embedder.embed_batch(chunks)
    filename = PurePosixPath(key).name

    ops = [
        UpdateOne(
            {"source_path": chunk.source_path, "chunk_index": chunk.chunk_index},
            {"$set": FileRecord(...).to_dict()},
            upsert=True,
        )
        for chunk, vector in zip(chunks, vectors)
    ]
    self._col.bulk_write(ops, ordered=False)
```

`get_etag` is accessed via `getattr` so the watcher base class does not hardcode a dependency on `S3Backend`. Any `ObjectStoreBackend` that does not expose `get_etag` will simply store an empty ETag.

---

### 2.10 PollingWatcher

**File:** `watcher/polling.py`

#### Constants

```python
_DEFAULT_INTERVAL = 300   # 5 minutes
```

#### Attributes (beyond base)

| Attribute | Type | Description |
|---|---|---|
| `_interval` | `int` | Poll interval in seconds |
| `_prefix` | `str` | S3 key prefix to watch |
| `_stop_event` | `threading.Event` | Signals loop to exit |
| `_thread` | `Thread \| None` | Background thread handle |
| `_state` | `dict[str, str]` | `{key: etag}` snapshot of last poll |

#### `start()`

```python
if self._thread and self._thread.is_alive():
    return   # idempotent
self._stop_event.clear()
self._thread = threading.Thread(target=self._loop, name="PollingWatcher", daemon=True)
self._thread.start()
```

#### `stop()`

```python
self._stop_event.set()
if self._thread:
    self._thread.join(timeout=self._interval + 10)
```

#### `_loop()`

```python
while not self._stop_event.wait(timeout=self._interval):
    self._poll()
```

`Event.wait(timeout)` blocks for `timeout` seconds and returns `True` if the event was set (→ stop), `False` on timeout (→ poll). This eliminates `time.sleep` + `is_set()` polling patterns.

#### `_poll()`

```python
current = dict(self._store.list_keys(self._prefix))   # {key: etag}
prev = self._state

for key, etag in current.items():
    if key not in prev:
        self.on_created(key)
    elif prev[key] != etag:
        self.on_updated(key)

for key in prev:
    if key not in current:
        self.on_deleted(key)

self._state = current
```

---

### 2.11 SQSWatcher

**File:** `watcher/sqs.py`

#### Constants

```python
_VISIBILITY_TIMEOUT = 120   # seconds (message invisible while being processed)
_LONG_POLL_WAIT     = 20    # seconds (SQS long-poll max)
_MAX_MESSAGES       = 10    # per ReceiveMessage call
```

#### Attributes (beyond base)

| Attribute | Type | Description |
|---|---|---|
| `_queue_url` | `str` | Full SQS queue URL |
| `_visibility_timeout` | `int` | Message visibility timeout |
| `_stop_event` | `threading.Event` | Signals loop to exit |
| `_thread` | `Thread \| None` | Background thread handle |
| `_sqs` | `boto3.SQSClient` | SQS client |

#### `_loop()`

```python
while not self._stop_event.is_set():
    try:
        self._receive_and_process()
    except Exception:
        logger.error(...)   # continue loop; don't crash
```

#### `_receive_and_process()`

```python
response = self._sqs.receive_message(
    QueueUrl=self._queue_url,
    MaxNumberOfMessages=_MAX_MESSAGES,     # up to 10 per call
    WaitTimeSeconds=_LONG_POLL_WAIT,       # long-poll (reduces empty responses)
    VisibilityTimeout=self._visibility_timeout,
)
for msg in response.get("Messages", []):
    receipt = msg["ReceiptHandle"]
    try:
        self._process_message(msg["Body"])
        self._sqs.delete_message(QueueUrl=self._queue_url, ReceiptHandle=receipt)
    except Exception:
        logger.warning("...will retry")   # message becomes visible again after timeout
```

#### `_process_message(body)`

```python
envelope = json.loads(body)

# SNS wrapping: {"Message": "<json_string>"}
if "Message" in envelope:
    inner = json.loads(envelope["Message"])
else:
    inner = envelope

records = inner.get("Records", [])

for record in records:
    event_name = record.get("eventName", "")
    key = record["s3"]["object"]["key"]

    if event_name.startswith("ObjectCreated"):
        self.on_created(key)
    elif event_name.startswith("ObjectRemoved"):
        self.on_deleted(key)
    # Unknown events are logged and skipped
```

---

## 3. Data Types & DTOs

**File:** `dtypes.py`

### Internal Types

```python
@dataclass(frozen=True)
class Chunk:
    source_path:  str
    chunk_index:  int
    content:      str
    page_number:  int = 0
    char_start:   int = 0
    char_end:     int = 0
    line_start:   int = 0
    metadata:     dict[str, Any] = field(default_factory=dict)

@dataclass
class FileRecord:
    source_path:  str
    chunk_index:  int
    content:      str
    embedding:    list[float]
    page_number:  int
    char_start:   int
    char_end:     int
    line_start:   int
    etag:         str
    filename:     str
    metadata:     dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]: ...   # serializes to MongoDB document

@dataclass(frozen=True)
class SearchHit:
    source_path:  str
    chunk_index:  int
    content:      str
    line_start:   int
    score:        float = 0.0
```

### Public DTOs (returned by `MongoFilesystemBackend`)

```python
@dataclass
class LsEntry:
    name:    str
    is_dir:  bool
    size:    int = 0

@dataclass
class LsResult:
    path:     str
    entries:  list[LsEntry] = field(default_factory=list)
    error:    str | None = None

@dataclass
class ReadResult:
    path:     str
    content:  str = ""
    error:    str | None = None

@dataclass
class WriteResult:
    path:     str
    success:  bool = True
    error:    str | None = None

@dataclass
class EditResult:
    path:     str
    success:  bool = True
    error:    str | None = None

@dataclass
class GrepMatch:
    path:     str
    line:     int
    content:  str
    score:    float = 0.0

@dataclass
class GrepResult:
    matches:  list[GrepMatch] = field(default_factory=list)
    error:    str | None = None

@dataclass
class GlobResult:
    paths:    list[str] = field(default_factory=list)
    error:    str | None = None

@dataclass
class UploadResult:
    uploaded: list[str] = field(default_factory=list)
    failed:   list[str] = field(default_factory=list)
    error:    str | None = None

@dataclass
class DownloadResult:
    downloaded: list[str] = field(default_factory=list)
    failed:     list[str] = field(default_factory=list)
    error:      str | None = None

@dataclass
class SyncReport:
    seen:      int = 0
    processed: int = 0
    skipped:   int = 0
    failed:    int = 0
    error:     str | None = None
```

---

## 4. Error Taxonomy

**File:** `errors.py`

### `AdapterError(Exception)`

```python
class AdapterError(Exception):
    def __init__(self, code: ErrorCode, message: str,
                 cause: BaseException | None = None) -> None:
        self.code    = code
        self.message = message
        self.cause   = cause
        super().__init__(f"[{code.value}] {message}")
```

### `ErrorCode` Enum

| Code | Value | Meaning |
|---|---|---|
| `E1001_MISSING_CONFIG` | "E1001" | Required config param missing |
| `E1002_INVALID_BUCKET` | "E1002" | S3 bucket not found/accessible |
| `E1003_INVALID_MONGO_URI` | "E1003" | Bad/unreachable MongoDB URI |
| `E1004_INDEX_PROVISION_FAILED` | "E1004" | Atlas index creation failure |
| `E1005_INITIAL_SYNC_FAILED` | "E1005" | Initial S3→Mongo sync failure |
| `E1006_WATCHER_START_FAILED` | "E1006" | Watcher thread couldn't start |
| `E2001_OBJECT_NOT_FOUND` | "E2001" | S3 key does not exist |
| `E2002_OBJECT_READ_FAILED` | "E2002" | S3 GetObject failed |
| `E2003_OBJECT_WRITE_FAILED` | "E2003" | S3 PutObject failed |
| `E2004_OBJECT_DELETE_FAILED` | "E2004" | S3 DeleteObject failed |
| `E2005_LIST_FAILED` | "E2005" | S3 ListObjects failed |
| `E2006_UPLOAD_FAILED` | "E2006" | One or more uploads failed |
| `E2007_DOWNLOAD_FAILED` | "E2007" | One or more downloads failed |
| `E2008_EDIT_CONFLICT` | "E2008" | Concurrent edit (ETag mismatch) |
| `E3001_CONNECTION_FAILED` | "E3001" | MongoDB unreachable |
| `E3002_QUERY_FAILED` | "E3002" | MongoDB query error |
| `E3003_UPSERT_FAILED` | "E3003" | MongoDB bulk_write failure |
| `E3004_DELETE_FAILED` | "E3004" | MongoDB delete failure |
| `E3005_INDEX_NOT_READY` | "E3005" | Atlas index not yet queryable |
| `E4001_EMBEDDING_API_FAILED` | "E4001" | Embedding API returned error |
| `E4002_EMBEDDING_DIMENSION_MISMATCH` | "E4002" | Vector size ≠ configured dims |
| `E4003_EMBEDDING_RATE_LIMITED` | "E4003" | Rate limit exhausted (all retries) |
| `E5001_GREP_FAILED` | "E5001" | grep operation failed |
| `E5002_GLOB_FAILED` | "E5002" | glob operation failed |
| `E5003_LS_FAILED` | "E5003" | ls operation failed |
| `E5004_VECTOR_SEARCH_UNAVAILABLE` | "E5004" | Vector Search not on this cluster |
| `E6001_SQS_RECEIVE_FAILED` | "E6001" | SQS ReceiveMessage failed |
| `E6002_EVENT_PARSE_FAILED` | "E6002" | S3 event JSON unparseable |
| `E6003_WATCHER_CRASHED` | "E6003" | Watcher background thread crash |
| `E9001_INTERNAL_ERROR` | "E9001" | Unexpected internal error |
| `E9002_CHUNKER_FAILED` | "E9002" | Chunker extraction error |
| `E9003_FORMAT_UNSUPPORTED` | "E9003" | File format not supported |

---

## 5. Threading Model

```
Main Thread
  │
  └─ MongoFilesystemBackend.__init__()
       │
       ├─ spawns: MongoFSInit (daemon=True)
       │     ├─ ensure_indexes()
       │     ├─ wait_until_queryable()   ← polls every 10s, up to 600s
       │     ├─ InitialSync.run()        ← sequential after downloads
       │     │    └─ ThreadPoolExecutor (16 workers)  ← download pool (dies after run())
       │     ├─ _ready.set()
       │     └─ watcher.start()
       │          └─ spawns: PollingWatcher OR SQSWatcher (daemon=True)
       │               └─ loops until _stop_event.set()
       │
       └─ returns immediately ← constructor non-blocking
```

### Synchronization Points

| Mechanism | Where | Purpose |
|---|---|---|
| `threading.Event._ready` | `MongoFilesystemBackend` | Gates `grep`/`glob`/`ls` until first sync |
| `threading.Event._stop_event` | `PollingWatcher`, `SQSWatcher` | Signals watcher thread to exit |
| `ThreadPoolExecutor` | `InitialSync._download_all()` | Bounded concurrency for S3 downloads |
| `daemon=True` on all threads | Everywhere | All background threads die with the process |

### Thread Safety Notes

- `SearchRouter._atlas` is written once lazily and then only read; no lock needed for a bool in CPython (GIL).
- `PollingWatcher._state` (`dict[str, str]`) is written only by the watcher thread and read only by the watcher thread; no external readers → no lock needed.
- `MongoFilesystemBackend._ready` is `set()` once and `wait()`ed by multiple callers; `threading.Event` is thread-safe.

---

## 6. Algorithms

### 6.1 Token-aware Chunking

**Input:** `text: str`, `token_limit=512`, `overlap=64`

```
tokens = cl100k_base.encode(text)   # [int, ...]

chunks = []
start = 0
while start < len(tokens):
    end = min(start + token_limit, len(tokens))
    chunk_tokens = tokens[start:end]
    chunk_text   = decode(chunk_tokens)

    # Compute character offset by decoding the prefix before this chunk
    char_start = len(decode(tokens[:start]))
    char_end   = char_start + len(chunk_text)
    line_start = text[:char_start].count("\n")

    chunks.append((chunk_text, char_start, char_end, line_start))

    if end == len(tokens): break
    start += token_limit - overlap   # e.g. 512 - 64 = 448
```

**Complexity:** O(n²) in token count due to `decode(tokens[:start])` at each iteration. For typical documents (< 50K tokens) this is negligible. Large documents (e.g., 500-page PDFs) are processed page-by-page, so worst-case per call is bounded by one page's token count.

### 6.2 ETag-based Change Detection

**Input:** `keys_etags: list[(key, etag)]` from S3

```
# Step 1: Batch MongoDB lookup — one doc per file (not per chunk)
stored_etags = aggregate([
    $match: {source_path: {$in: [all keys]}},
    $group: {_id: "$source_path", etag: {$first: "$etag"}},
])
→ {source_path → etag}

# Step 2: Filter
to_process = [
    (key, etag)
    for (key, etag) in keys_etags
    if stored_etags.get(key) != etag   # None (new) or different (changed)
]
```

**Properties:**
- Idempotent: safe to call repeatedly with same data → skips all
- O(files) MongoDB data transfer, not O(chunks)
- Handles: new files (not in `stored`), changed files (etag differs), deleted files (present in `stored` but not in S3 list → handled separately by watcher)

### 6.3 Hybrid Search Pipeline Construction

**Tier selection logic:**

```
if _is_atlas_available():
    query_vector = embedder.embed_batch([query_chunk])
    if query_vector is not None:
        use $rankFusion (hybrid)
    else:
        use text_search_stage (full-text only)
else:
    use $regex (fallback)
```

**`$rankFusion` parameters:**
- `numCandidates` for vector = `grep_limit * 2 * oversampling_factor` = `20 * 2 * 5 = 200`
- `top_k` for vector stage = `grep_limit * 2 = 40`
- Final `$limit` = `grep_limit = 20`
- Weights: `fulltext: 0.5, vector: 0.5`

**Why `oversampling_factor=5`?**  
ANN (Approximate Nearest Neighbor) search examines `numCandidates` vectors to find the closest `top_k`. A higher ratio improves recall at the cost of latency. Factor 5 is a standard trade-off for cosine similarity at this scale.

### 6.4 ls Aggregation Pipeline

**Input:** `path = "docs/architecture/"`

**Step-by-step:**

```
prefix = "docs/architecture/"
escaped = "docs/architecture/"

Pipeline:
  1. $match: {source_path: {$regex: "^docs/architecture/"}}
     → all chunks under that prefix

  2. $group:
       _id: $arrayElemAt[
               $split[$substr[$source_path, len("docs/architecture/"), -1], "/"],
               0
            ]
       full_paths: $addToSet($source_path)
     → groups by next path segment after the prefix

     Example: "docs/architecture/overview.pdf" → segment = "overview.pdf"
              "docs/architecture/patterns/dp.md" → segment = "patterns"

  3. $sort: {_id: 1}  → alphabetical

  4. $limit: 1000

Post-aggregation is_dir check:
  for each (segment, full_paths):
      is_dir = any(
          path[len(prefix):].count("/") > 0
          or not path[len(prefix):].endswith(segment)
          for path in full_paths
      )
```

### 6.5 Watcher Diff Algorithm (Polling)

```
current_state = dict(list_keys(prefix))   # {key: etag}
prev_state    = self._state               # {key: etag} from last poll

# Created or updated
for key, etag in current_state.items():
    if key not in prev_state:
        on_created(key)
    elif prev_state[key] != etag:
        on_updated(key)   # delete_many({source_path: key}) + _ingest(key)

# Deleted
for key in prev_state:
    if key not in current_state:
        on_deleted(key)   # delete_many({source_path: key})

self._state = current_state   # snapshot for next diff
```

**Space complexity:** O(N keys) for `_state`. For a bucket with 1M objects, this is ~100 MB of key strings — acceptable for a background process but worth noting for very large buckets.

### 6.6 SQS Message Processing

```
Message body (one of two shapes):

  Shape A (direct S3 notification):
    {
      "Records": [
        {"eventName": "ObjectCreated:Put", "s3": {"object": {"key": "path/to/file.pdf"}}},
        ...
      ]
    }

  Shape B (SNS-wrapped):
    {
      "Message": "<JSON string of Shape A>"
    }

Processing:
  1. json.loads(body) → envelope
  2. if "Message" in envelope: inner = json.loads(envelope["Message"])
     else: inner = envelope
  3. for record in inner["Records"]:
       event_name = record["eventName"]
       key = record["s3"]["object"]["key"]
       if event_name.startswith("ObjectCreated"): on_created(key)
       elif event_name.startswith("ObjectRemoved"): on_deleted(key)
  4. delete_message(receipt_handle)   # only on success
     (on exception: message becomes visible again after _visibility_timeout=120s)
```

### 6.7 OOXML Format Sniffing

ZIP archives may be `.docx`, `.xlsx`, or `.pptx` — all use the same `PK` magic bytes.

```
with ZipFile(io.BytesIO(data)) as zf:
    names = zf.namelist()

if any(n.startswith("xl/")   for n in names): return "xlsx"
if any(n.startswith("ppt/")  for n in names): return "pptx"
if any(n.startswith("word/") for n in names): return "docx"
return None   # unrecognized; log and attempt docx extraction
```

---

## 7. MongoDB Aggregation Pipelines

### Hybrid grep ($rankFusion)

```javascript
[
  {
    "$rankFusion": {
      "input": {
        "pipelines": {
          "fulltext": [
            {
              "$search": {
                "index": "fulltext_search_content_filename",
                "text": { "query": "<pattern>", "path": "content" }
              }
            },
            { "$match": { "source_path": { "$regex": "^<path>" } } }  // optional
          ],
          "vector": [
            {
              "$vectorSearch": {
                "index": "vector_search_embedding",
                "path": "embedding",
                "queryVector": [0.012, -0.034, ...],
                "numCandidates": 200,
                "limit": 40
              }
            },
            { "$match": { "source_path": { "$regex": "^<path>" } } }  // optional
          ]
        }
      },
      "combination": { "weights": { "fulltext": 0.5, "vector": 0.5 } }
    }
  },
  { "$limit": 20 },
  { "$project": { "source_path": 1, "line_start": 1, "content": 1, "_id": 0 } }
]
```

### Full-text grep (embedder unavailable)

Built by `langchain_mongodb.pipelines.text_search_stage`:
```javascript
[
  {
    "$search": {
      "index": "fulltext_search_content_filename",
      "text": { "query": "<pattern>", "path": "content" }
    }
  },
  { "$limit": 20 },
  { "$project": { "source_path": 1, "line_start": 1, "content": 1, "score": 1, "_id": 0 } }
]
```

### Regex grep (non-Atlas)

```javascript
db.chunks.find(
  {
    "content": { "$regex": "<pattern>", "$options": "i" },
    "source_path": { "$regex": "^<path>" },   // optional
    "filename": { "$regex": "<glob_translated>" }  // optional
  },
  { "source_path": 1, "line_start": 1, "content": 1, "_id": 0 }
).limit(20)
```

### glob (Atlas)

```javascript
[
  {
    "$search": {
      "index": "fulltext_search_content_filename",
      "wildcard": {
        "query": "*.pdf",
        "path": "source_path",
        "allowAnalyzedField": true
      }
    }
  },
  { "$match": { "source_path": { "$regex": "^<path>" } } },  // optional
  { "$group": { "_id": "$source_path" } },
  { "$sort": { "_id": 1 } },
  { "$limit": 200 }
]
```

### ls

```javascript
[
  { "$match": { "source_path": { "$regex": "^docs/architecture/" } } },
  {
    "$group": {
      "_id": {
        "$arrayElemAt": [
          { "$split": [{ "$substr": ["$source_path", 18, -1] }, "/"] },
          0
        ]
      },
      "full_paths": { "$addToSet": "$source_path" }
    }
  },
  { "$sort": { "_id": 1 } },
  { "$limit": 1000 }
]
```

### ETag change detection

```javascript
db.chunks.aggregate([
  { "$match": { "source_path": { "$in": ["key1", "key2", ...] } } },
  { "$group": { "_id": "$source_path", "etag": { "$first": "$etag" } } }
])
```

### Bulk upsert (InitialSync / Watcher)

```python
UpdateOne(
    filter={"source_path": key, "chunk_index": idx},
    update={"$set": {
        "source_path": key,
        "chunk_index": idx,
        "content": "...",
        "embedding": [...],
        "page_number": 0,
        "char_start": 0,
        "char_end": 500,
        "line_start": 42,
        "etag": '"abc123"',
        "filename": "file.pdf",
        "metadata": {},
    }},
    upsert=True,
)
# Sent as bulk_write([...], ordered=False)
```

---

## 8. Configuration Constants

| Constant | File | Value | Purpose |
|---|---|---|---|
| `_DB_NAME` | `backend.py` | `"deepagents_mongodb_fs"` | MongoDB database name |
| `_COLLECTION_NAME` | `backend.py` | `"aws_mdb_resuts_chunks"` | Collection name (intentional typo) |
| `_ENCODING` | `chunker.py` | `"cl100k_base"` | tiktoken encoding |
| `_DEFAULT_TOKEN_LIMIT` | `chunker.py` | `512` | Tokens per chunk |
| `_DEFAULT_OVERLAP` | `chunker.py` | `64` | Overlap tokens between chunks |
| `_OLE_MAGIC` | `chunker.py` | `b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"` | OLE2 magic bytes |
| `_DEFAULT_PROVIDER` | `embedder.py` | `"bedrock"` | Default embedding provider |
| `_DEFAULT_MODEL` | `embedder.py` | `"text-embedding-3-small"` | OpenAI default model |
| `_BEDROCK_DEFAULT_MODEL` | `embedder.py` | `"amazon.titan-embed-text-v2:0"` | Bedrock default model |
| `_DEFAULT_DIMENSIONS` | `embedder.py` | `1024` | Embedding vector dimensions |
| `_DEFAULT_BATCH_SIZE` | `embedder.py` | `100` | Texts per embedding API call |
| `_DEFAULT_WAIT_SECONDS` | `index_manager.py` | `600` | Atlas index readiness timeout |
| `_DOWNLOAD_WORKERS` | `sync.py` | `16` | S3 download thread pool size |
| `_UPSERT_BATCH` | `sync.py` | `500` | MongoDB bulk_write batch size |
| `_DEFAULT_GREP_LIMIT` | `search.py` | `20` | Max grep results |
| `_DEFAULT_GLOB_LIMIT` | `search.py` | `200` | Max glob results |
| `_DEFAULT_LS_LIMIT` | `search.py` | `1000` | Max ls entries |
| `_DEFAULT_INTERVAL` | `watcher/polling.py` | `300` | Poll interval (seconds) |
| `_VISIBILITY_TIMEOUT` | `watcher/sqs.py` | `120` | SQS message visibility timeout |
| `_LONG_POLL_WAIT` | `watcher/sqs.py` | `20` | SQS long-poll wait (seconds) |
| `_MAX_MESSAGES` | `watcher/sqs.py` | `10` | Max messages per SQS receive call |

---

## 9. Sequence Diagrams

### Startup Sequence

```
Caller              MongoFilesystemBackend     MongoFSInit Thread
  │                         │                        │
  │─── __init__() ─────────►│                        │
  │                         │── spawn(daemon) ───────►│
  │◄── returns immediately ─│                         │
  │                         │                    ensure_indexes()
  │                         │                    wait_until_queryable()
  │                         │                         │── poll every 10s ─┐
  │                         │                         │◄──────────────────┘
  │                         │                    InitialSync.run()
  │                         │                         │
  │                         │                    _ready.set()
  │                         │                    watcher.start()
  │                         │                         │── spawn(daemon) ──► WatcherThread
```

### grep() Sequence (Happy Path)

```
Caller      MongoFilesystemBackend   SearchRouter   Embedder         MongoDB
  │                  │                    │             │               │
  │── grep(pat) ────►│                    │             │               │
  │                  │── _wait_ready() ──►│             │               │
  │                  │   (blocks if not ready)          │               │
  │                  │── grep(pat,p,g) ──►│             │               │
  │                  │                    │─ embed([pat])►             │
  │                  │                    │◄─ [vector] ─┤             │
  │                  │                    │── aggregate($rankFusion) ─►│
  │                  │                    │◄─ [{source_path,line,content}] ─┤
  │                  │                    │── _dedupe_to_grep_result() │
  │◄── GrepResult ───│◄── GrepResult ─────│             │               │
```

### edit() Sequence (Conflict Detected)

```
Caller          MongoFilesystemBackend         S3Backend                S3
  │                      │                         │                     │
  │── edit(p,old,new) ──►│                         │                     │
  │                      │── edit(p,old,new) ──────►│                     │
  │                      │                          │── HeadObject ──────►│
  │                      │                          │◄── etag="abc" ──────│
  │                      │                          │── GetObject ────────►│
  │                      │                          │◄── body ────────────│
  │                      │                          │── PutObject(IfMatch="abc") ──►│
  │                      │          [concurrent writer modified it]        │
  │                      │                          │◄── 412 PreconditionFailed ────│
  │                      │                          │── raise E2008_EDIT_CONFLICT   │
  │◄── EditResult(       │◄── AdapterError caught   │                     │
  │     success=False,   │    by @adapter_boundary  │                     │
  │     error="E2008…")  │                          │                     │
```

### Watcher Ingestion Sequence (PollingWatcher)

```
PollingWatcher      S3Backend      Chunker     Embedder       MongoDB
     │                  │             │            │              │
     │── _poll() ───────│             │            │              │
     │── list_keys() ──►│             │            │              │
     │◄── {k:e} ────────│             │            │              │
     │   diff with self._state        │            │              │
     │── on_updated("k") →            │            │              │
     │   delete_many({source_path:"k"}) ───────────────────────►│
     │── _ingest("k") ──│             │            │              │
     │── read("k") ─────►│             │            │              │
     │◄── bytes ─────────│             │            │              │
     │── chunk("k",bytes) ─────────►│             │              │
     │◄── [Chunk,...] ──────────────│             │              │
     │── embed_batch([Chunk,...]) ──────────────►│              │
     │◄── [[vector],...] ───────────────────────│              │
     │── bulk_write([UpdateOne,...]) ──────────────────────────►│
     │── self._state = current_state │            │              │
```

---

## 10. Error Boundary Mechanics

**File:** `errors.py`

```python
def adapter_boundary(code: ErrorCode = ErrorCode.E9001_INTERNAL_ERROR):
    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            correlation_id = str(uuid.uuid4())[:8]   # 8-char hex prefix
            try:
                return fn(self, *args, **kwargs)

            except AdapterError as exc:
                logger.error("[%s] AdapterError %s in %s: %s",
                             correlation_id, exc.code.value,
                             fn.__qualname__, exc.message,
                             exc_info=exc.cause)
                if getattr(self, "debug", False):
                    raise   # full traceback in dev
                return _make_error_dto(fn, args, kwargs, exc.code, exc.message)

            except Exception as exc:
                tb = traceback.format_exc()
                logger.error("[%s] Unhandled %s in %s:\n%s",
                             correlation_id, type(exc).__name__,
                             fn.__qualname__, tb)
                if getattr(self, "debug", False):
                    raise
                return _make_error_dto(fn, args, kwargs, code, str(exc))
        return wrapper
    return decorator
```

**`_make_error_dto(fn, args, kwargs, code, detail)`** maps method names to zero-value DTOs:

```python
_return_map = {
    "ls":             LsResult(path=args[1] or "",  error=user_message(code, detail)),
    "read":           ReadResult(path=args[1] or "", error=user_message(code, detail)),
    "write":          WriteResult(path=args[1] or "", success=False, error=...),
    "edit":           EditResult(path=args[1] or "",  success=False, error=...),
    "grep":           GrepResult(error=user_message(code, detail)),
    "glob":           GlobResult(error=user_message(code, detail)),
    "upload_files":   UploadResult(error=user_message(code, detail)),
    "download_files": DownloadResult(error=user_message(code, detail)),
}
return _return_map.get(fn.__name__, None)
```

**`user_message(code, detail="")`:**
```python
base = _CODE_MESSAGES.get(code, "An error occurred.")
return f"[{code.value}] {base}" + (f" Detail: {detail}" if detail else "")
# e.g. "[E2008] Edit conflict: the object was modified concurrently. Detail: ..."
```

---

## 11. Index Definitions

### Index 1: Native Compound (BSON)

```python
collection.create_index(
    [("source_path", ASCENDING), ("chunk_index", ASCENDING)],
    unique=True,
    name="source_path_chunk_index_unique",
)
```

- Ensures upserts by `(source_path, chunk_index)` are idempotent.
- Supports efficient `$match` on `source_path` (prefix of the compound key).
- Works on all MongoDB versions (community, Atlas, mongomock).

### Index 2: Atlas Vector Search

```python
create_vector_search_index(
    collection=collection,
    index_name="vector_search_embedding",
    path="embedding",
    dimensions=1024,
    similarity="cosine",
    filters=["source_path", "filename"],
)
```

**Atlas definition (JSON):**
```json
{
  "name": "vector_search_embedding",
  "type": "vectorSearch",
  "definition": {
    "fields": [
      {
        "type": "vector",
        "path": "embedding",
        "numDimensions": 1024,
        "similarity": "cosine"
      },
      { "type": "filter", "path": "source_path" },
      { "type": "filter", "path": "filename" }
    ]
  }
}
```

Filter fields allow `$vectorSearch` to pre-filter on `source_path` and `filename` using equality operators (`$eq`, `$in`). Regex filters are **not** supported in `$vectorSearch` pre-filter — they are applied as a `$match` stage after the search.

### Index 3: Atlas Full-Text Search

```json
{
  "name": "fulltext_search_content_filename",
  "type": "search",
  "definition": {
    "mappings": {
      "dynamic": false,
      "fields": {
        "content": {
          "type": "string",
          "analyzer": "lucene.standard"
        },
        "filename": {
          "type": "string",
          "analyzer": "lucene.keyword"
        },
        "source_path": {
          "type": "string",
          "analyzer": "lucene.keyword"
        }
      }
    }
  }
}
```

**Analyzer choices:**
- `content` → `lucene.standard`: tokenizes, lowercases, removes stop words. Suitable for BM25 text search over document content.
- `filename` → `lucene.keyword`: treats the entire value as a single token. Required for glob wildcard queries (`*.pdf`) via `$search.wildcard` with `allowAnalyzedField: true`.
- `source_path` → `lucene.keyword`: same reason — full path must be preserved as one token for prefix/wildcard matching.

**Migration logic:** `_ensure_fulltext_search_index()` checks whether an existing index has `filename.analyzer == "lucene.keyword"` and `source_path` present. If not (legacy definition), it calls `update_search_index()` to migrate in place without dropping and recreating.
