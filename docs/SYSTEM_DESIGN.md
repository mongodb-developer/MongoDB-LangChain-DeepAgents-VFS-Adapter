# High-Level System Design: deepagents_mongodb_fs

## Table of Contents

1. [Overview](#1-overview)
2. [Goals & Non-Goals](#2-goals--non-goals)
3. [Architecture](#3-architecture)
4. [Component Breakdown](#4-component-breakdown)
5. [Data Model](#5-data-model)
6. [Data Flow](#6-data-flow)
7. [Search Strategy](#7-search-strategy)
8. [Synchronization Strategy](#8-synchronization-strategy)
9. [Error Handling](#9-error-handling)
10. [Configuration & Integration Points](#10-configuration--integration-points)
11. [Design Decisions & Trade-offs](#11-design-decisions--trade-offs)
12. [Scalability & Cost](#12-scalability--cost)
13. [Testing Strategy](#13-testing-strategy)
14. [Known Limitations](#14-known-limitations)

---

## 1. Overview

`deepagents_mongodb_fs` is a **MongoDB Atlas-backed virtual filesystem adapter for LangChain DeepAgents**. It gives AI agents a filesystem-like interface (`grep`, `glob`, `ls`, `read`, `write`, `edit`) over documents stored in S3, while providing hybrid semantic + keyword search powered by MongoDB Atlas Vector Search and Full-Text Search.

### Problem Statement

AI agents operating over large document corpora need to locate, read, and modify files efficiently. Raw S3 ListObjects + GetObject is too slow and has no semantic search capability. Purely vector-based search loses keyword precision. This adapter bridges both worlds: files live in S3 (cheap, durable, scalable), while a synchronized MongoDB index provides fast, hybrid-ranked search.

### Core Value Proposition

| Capability | How It's Delivered |
|---|---|
| Semantic search | MongoDB Atlas Vector Search (cosine similarity, 1024-dim embeddings) |
| Keyword search | MongoDB Atlas Full-Text Search (Lucene BM25) |
| Hybrid ranking | `$rankFusion` with Reciprocal Rank Fusion (server-side, single round-trip) |
| Filesystem operations | Direct S3 read/write/edit with ETag-based optimistic locking |
| Real-time sync | Pluggable watchers (polling or SQS-driven) keep MongoDB index fresh |
| Graceful degradation | Falls back from hybrid → full-text → regex on non-Atlas clusters |

---

## 2. Goals & Non-Goals

### Goals

- Implement the DeepAgents `BackendProtocol` so any DeepAgent subagent can call filesystem operations transparently.
- Maintain a MongoDB index synchronized with S3 content in near-real-time.
- Support hybrid semantic + keyword search with a single MongoDB aggregation pipeline.
- Handle 10+ document formats (PDF, DOCX, XLSX, PPTX, Markdown, plain text, etc.).
- Provide a non-blocking startup: the constructor returns immediately; initialization runs in a background thread.
- Return stable, typed error DTOs at all public API boundaries — no raw exceptions leak to callers.
- Work on non-Atlas MongoDB deployments (community, mongomock) with automatic fallback to regex search.

### Non-Goals

- General-purpose database or file storage system.
- Multi-bucket or multi-region federation.
- Authentication / authorization (delegated to IAM, boto3 credential chain, and MongoDB URI).
- Document transformation or post-processing beyond chunking and embedding.
- Streaming large files (current design reads full objects into memory).

---

## 3. Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                         DeepAgent                                    │
│        grep / glob / ls / read / write / edit / upload / download    │
└──────────────────────────────┬───────────────────────────────────────┘
                               │  BackendProtocol
                               ▼
         ┌─────────────────────────────────────────┐
         │        MongoFilesystemBackend            │
         │              (backend.py)                │
         │  ┌───────────────────────────────────┐  │
         │  │  @adapter_boundary (error.py)     │  │
         │  │  Non-blocking constructor          │  │
         │  │  _background_init() daemon thread  │  │
         │  └───────────────────────────────────┘  │
         └────────────┬──────────────┬─────────────┘
                      │              │
          ┌───────────┘              └─────────────┐
          ▼                                        ▼
  ┌───────────────────┐                 ┌──────────────────────┐
  │   SearchRouter    │                 │      S3Backend       │
  │   (search.py)     │                 │   (backends/s3.py)   │
  │                   │                 │                      │
  │  grep (hybrid)    │                 │  read (range-based)  │
  │  glob (wildcard)  │                 │  write (PUT)         │
  │  ls (aggregation) │                 │  edit (ETag CAS)     │
  └────────┬──────────┘                 │  upload / download   │
           │                            │  list_keys           │
           │                            └──────────┬───────────┘
           │                                       │
           ▼                                       ▼
  ┌─────────────────────────────┐        ┌─────────────────────┐
  │     MongoDB Atlas           │        │    AWS S3           │
  │  chunks collection          │        │  (object storage)   │
  │  - Vector Search index      │        └─────────────────────┘
  │  - Full-Text Search index   │                 ▲
  │  - Native compound index    │                 │
  └─────────────────────────────┘                 │
                                                  │
  Background Synchronization:                     │
  ┌───────────────────────────────────────────────┴──────┐
  │              InitialSync (sync.py)                   │
  │   list_keys → filter by ETag → download → chunk     │
  │                → embed → bulk_write (upsert)         │
  └───────────────────────────────────────────────────────┘
                    ▲                   ▲
                    │                   │
  ┌─────────────────┤     ┌─────────────┤
  │ PollingWatcher  │     │  SQSWatcher │
  │ (polling.py)    │     │  (sqs.py)   │
  │ Fixed interval  │     │  SQS events │
  └─────────────────┘     └─────────────┘
```

### Startup Sequence

```
MongoFilesystemBackend.__init__()  ← returns immediately (non-blocking)
       │
       └── spawns _background_init() daemon thread
                │
                ├── IndexManager.ensure_indexes()   ← idempotent index provisioning
                ├── IndexManager.wait_until_queryable()  ← polls until Atlas indexes ready
                ├── InitialSync.run()               ← ETag-filtered S3 → MongoDB sync
                └── _ready.set() + watcher.start()  ← unblocks search ops
```

Search methods (`grep`, `glob`, `ls`) call `_wait_ready()` and block until the background thread sets `_ready`. Storage methods (`read`, `write`, `edit`) bypass this gate and are available immediately.

---

## 4. Component Breakdown

### 4.1 MongoFilesystemBackend (`backend.py`)

The public facade. Implements `BackendProtocol` from DeepAgents. Wires all internal components together and routes calls:

- **Search ops** → `SearchRouter`
- **Storage ops** → `S3Backend`
- **All public methods** wrapped with `@adapter_boundary` (catches exceptions, returns error DTOs)

### 4.2 SearchRouter (`search.py`)

Handles all read-only filesystem queries against MongoDB:

| Method | MongoDB Mechanism |
|---|---|
| `ls(path)` | Aggregation: regex filter + group by path segment |
| `glob(pattern, path)` | Atlas `$search` wildcard operator; Python `fnmatch` fallback |
| `grep(pattern, path, glob)` | `$rankFusion` hybrid search (vector + full-text); full-text fallback; regex fallback |

### 4.3 S3Backend (`backends/s3.py`)

Concrete implementation of `ObjectStoreBackend` ABC using boto3:

- `read(path, offset, limit)` — Range-header GET for efficient slicing
- `write(path, content)` — Simple PUT
- `edit(path, old, new)` — Read → replace → conditional PUT with `If-Match` ETag (optimistic lock)
- `list_keys(prefix)` — Paginated ListObjects yielding `(key, etag)` tuples

### 4.4 Chunker (`chunker.py`)

Format-aware document splitter:

- **Format detection**: extension-first, magic-byte fallback
- **Supported formats**: txt, md, rst, csv, PDF, DOCX, XLSX, XLS, PPTX, PPT
- **Chunking strategy**: tiktoken `cl100k_base`, 512 tokens/chunk, 64-token overlap
- **Output**: `Chunk` dataclasses carrying `source_path`, `chunk_index`, `page_number`, `char_start`, `char_end`, `line_start`

### 4.5 Embedder (`embedder.py`)

Embedding API abstraction with retry logic:

- **Providers**: OpenAI (`text-embedding-3-small`, 1024 dims) or AWS Bedrock (`amazon.titan-embed-text-v2:0`)
- **Batch size**: 100 texts per API call (configurable)
- **Retry**: Exponential backoff (1 → 2 → 4 → 8 → 16 s) for rate-limit errors (HTTP 429)
- **Validation**: Checks returned vector dimensionality matches configured value

### 4.6 IndexManager (`index_manager.py`)

Idempotent provisioning of three MongoDB indexes:

| Index | Type | Fields | Purpose |
|---|---|---|---|
| Native compound | BSON unique | `(source_path, chunk_index)` | Deduplication, efficient upsert |
| Vector Search | Atlas knnVector | `embedding` (1024 dims, cosine) | Semantic search |
| Full-Text Search | Atlas Lucene | `content`, `filename` | Keyword search |

- Lazy Atlas detection via `list_search_indexes()` (Atlas-only API)
- `wait_until_queryable()` polls index status every 10 s (timeout: 600 s)
- Gracefully skips Atlas index creation on non-Atlas clusters

### 4.7 InitialSync (`sync.py`)

One-time, ETag-idempotent sync of S3 → MongoDB:

1. List all S3 keys and ETags under the configured prefix
2. Query MongoDB for stored ETags; skip unchanged files
3. Download changed files concurrently (16-thread `ThreadPoolExecutor`)
4. Chunk → embed → upsert sequentially (API-rate-limited)
5. Upsert in 500-record bulk batches via `bulk_write([UpdateOne(..., upsert=True)])`

### 4.8 Watchers (`watcher/`)

Incremental sync after initial load:

**PollingWatcher** (`polling.py`)
- Diffs current vs. previous ETag snapshots on a fixed interval (default: 5 min)
- Zero AWS infrastructure required
- Suitable for development or low-churn workloads

**SQSWatcher** (`sqs.py`)
- Long-polls an SQS queue (20 s wait, 120 s visibility timeout)
- Parses S3 event JSON (handles SNS-wrapped messages)
- Near-real-time sync (latency: seconds vs. minutes)
- Requires S3 event notifications → SNS → SQS setup

Both watchers share the same ingestion pipeline in `S3Watcher` base class: download → chunk → embed → bulk upsert.

### 4.9 Error Handling (`errors.py`)

Centralized error taxonomy:

| Range | Category |
|---|---|
| E1xxx | Configuration / initialization |
| E2xxx | S3 / object store |
| E3xxx | MongoDB |
| E4xxx | Embedding API |
| E5xxx | Search operations |
| E6xxx | Watcher |
| E9xxx | Internal / unexpected |

The `@adapter_boundary` decorator on every public method catches exceptions, maps them to `ErrorCode` values, returns typed error DTOs, and logs with a correlation ID. When `debug=True`, it re-raises for local development.

---

## 5. Data Model

### MongoDB Document (`chunks` collection)

```json
{
  "_id": "<ObjectId>",
  "source_path": "docs/architecture/overview.pdf",
  "filename": "overview.pdf",
  "chunk_index": 3,
  "page_number": 2,
  "char_start": 1024,
  "char_end": 3096,
  "line_start": 42,
  "content": "...chunk text (up to 512 tokens)...",
  "embedding": [0.012, -0.034, ...],  // 1024 floats
  "etag": "\"d41d8cd98f00b204e9800998ecf8427e\""
}
```

### Key Design Choices

- **`line_start`** is preserved from chunking to support DeepAgents-compatible `GrepMatch.line` (agents navigate to exact source lines).
- **`etag`** on each document enables incremental sync: only re-ingest files whose ETag changed.
- **Unique index on `(source_path, chunk_index)`** ensures upserts are idempotent and prevents duplicate chunks on re-ingestion.

---

## 6. Data Flow

### 6.1 Initial Ingestion

```
S3 (list_keys)
    │
    ├─ (key, etag) stream
    │
    ▼
InitialSync._filter_changed()
    │ query MongoDB for stored etags
    │ skip unchanged files
    ▼
S3Backend.read() [16 concurrent workers]
    │
    ▼
Chunker.chunk(key, bytes)
    │  format detection → extract text by page → split by tokens
    │
    ▼
Embedder.embed_batch(chunks)
    │  batch=100 → OpenAI/Bedrock API → vectors [1024 dims]
    │
    ▼
Collection.bulk_write([UpdateOne({source_path, chunk_index}, $set {...}, upsert=True)])
    │  batched in 500-record groups
    ▼
MongoDB Atlas
```

### 6.2 grep("kubernetes networking") Flow

```
MongoFilesystemBackend.grep(pattern, path, glob)
    │
    ├─ _wait_ready()  ← blocks if initial sync not complete
    │
    └─ SearchRouter.grep(pattern, path, glob)
           │
           ├─ [Atlas + embedder available]
           │       Embedder.embed_batch([pattern]) → query_vector
           │       │
           │       └─ MongoDB aggregation:
           │             $rankFusion:
           │               fulltext pipeline:  $search text operator (BM25)
           │               vector pipeline:    $vectorSearch (cosine, ANN)
           │             $match {source_path, filename filters}
           │             $project {source_path, line_start, content, score}
           │
           ├─ [Atlas available, embedder failed]
           │       $search text operator only
           │
           └─ [Non-Atlas MongoDB]
                   $regex case-insensitive on content
           │
           ▼
    SearchRouter._dedupe_to_grep_result(docs)
           dedup by (source_path, line_start)
           │
           ▼
    GrepResult([GrepMatch(path, line, content, score), ...])
```

### 6.3 edit("file.txt", "old text", "new text") Flow

```
MongoFilesystemBackend.edit(path, old, new)
    │
    └─ S3Backend.edit(path, old, new)
           │
           ├─ HeadObject(path) → etag
           ├─ GetObject(path) → bytes → decode → text
           ├─ text.replace(old, new, 1) → updated_text
           │
           └─ PutObject(path, updated_text, If-Match=etag)
                  ├─ Success        → EditResult(success=True)
                  └─ 412 Precondition Failed → AdapterError(E2008_EDIT_CONFLICT)
```

### 6.4 Incremental Sync (PollingWatcher)

```
Every 5 min:
    S3Backend.list_keys() → {key: etag} snapshot
    diff(previous_state, current_state)
        ├─ new keys      → on_created(key)
        ├─ changed ETags → on_updated(key): delete_many + _ingest(key)
        └─ removed keys  → on_deleted(key): delete_many
```

---

## 7. Search Strategy

The system implements a three-tier search strategy with automatic selection:

### Tier 1: Hybrid Search (Atlas + Embedder)

```
$rankFusion (MongoDB 8.1+)
├── Full-Text pipeline:
│     $search { text: { query: pattern, path: "content" } }
│     → BM25 scored documents
│
└── Vector pipeline:
      $vectorSearch {
        queryVector: embed(pattern),
        path: "embedding",
        numCandidates: 150,
        limit: 50,
        filter: { source_path, filename }
      }
      → cosine similarity scored documents

Combined with Reciprocal Rank Fusion (50/50 weights)
Server-side aggregation — single MongoDB round-trip
```

**Why RRF?** BM25 and cosine similarity produce incomparable score scales. RRF normalizes by rank position rather than raw score, combining them fairly without manual weight tuning.

### Tier 2: Full-Text Only (Atlas, Embedder unavailable)

```
$search { text: { query: pattern, path: ["content", "filename"] } }
```

### Tier 3: Regex Fallback (Non-Atlas MongoDB)

```
$match { content: { $regex: pattern, $options: "i" } }
```

Tier selection is automatic and transparent to the caller.

### Path and Glob Filtering

Vector Search (`$vectorSearch`) supports only equality filters (`$eq`, `$in`), not regex. Path/glob filters are therefore applied as `$match` stages after the search pipelines, not inside them.

---

## 8. Synchronization Strategy

### ETag-Based Idempotency

Every chunk document stores the S3 ETag of its source file. On re-sync:

1. `list_keys()` yields current `(key, etag)` pairs from S3.
2. `_filter_changed()` queries MongoDB for stored ETags.
3. Only files whose ETag has changed are re-ingested.
4. Deleted files are removed from MongoDB via `delete_many`.

This makes sync restarts safe and efficient: re-running `InitialSync` on an unchanged bucket is a no-op (just two queries).

### Watcher Selection

| | PollingWatcher | SQSWatcher |
|---|---|---|
| Latency | ~polling interval (default 5 min) | Near-real-time (seconds) |
| Infrastructure | None | S3 → SNS → SQS pipeline |
| Use case | Development, low-churn | Production, high-churn |
| Cost | Periodic S3 ListObjects | SQS per-message pricing |

### Concurrent Download, Sequential Embedding

Initial sync downloads files concurrently (16 workers, I/O bound) but embeds sequentially (API rate-limit bound). This keeps throughput high without overwhelming embedding API quotas.

---

## 9. Error Handling

### Error Boundary Pattern

Every public method on `MongoFilesystemBackend` is decorated with `@adapter_boundary`:

```python
@adapter_boundary
def grep(self, pattern, path, glob):
    ...
```

The decorator:
1. Catches any exception raised inside the method.
2. Maps it to the nearest `ErrorCode` enum value.
3. Logs the error with a randomly generated correlation ID for tracing.
4. Returns a typed result DTO with `error: str` populated (never raises).
5. If `debug=True`, re-raises after logging for local development.

### Result DTOs

All public methods return typed dataclasses:

```python
@dataclass(frozen=True)
class GrepResult:
    matches: list[GrepMatch]
    error: str | None = None
```

Callers check `result.error` rather than catching exceptions, enabling clean agent code.

### Stable Error Codes

Error codes are versioned enums (e.g., `E2001_KEY_NOT_FOUND`, `E5001_GREP_FAILED`). Downstream systems can match on codes without parsing error messages, and codes will not change across patch versions.

---

## 10. Configuration & Integration Points

### Constructor Parameters

| Parameter | Type | Required | Default | Purpose |
|---|---|---|---|---|
| `s3_bucket_name` | `str` | Yes | — | S3 bucket to sync |
| `mongodb_connection_string` | `str` | Yes | — | Atlas or compatible URI |
| `embedding_model` | `Embeddings` | No | OpenAIEmbeddings | LangChain embedding instance |
| `embedding_dimensions` | `int` | No | 1024 | Vector dimensionality |
| `watcher` | `str` | No | `"polling"` | `"polling"` or `"sqs"` |
| `sqs_queue_url` | `str` | If sqs | None | Full SQS queue URL |
| `aws_region` | `str` | No | env | AWS region |
| `s3_prefix` | `str` | No | `""` | S3 key prefix filter |
| `debug` | `bool` | No | `False` | Re-raise on error |

### Environment Variables

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | OpenAI API key |
| `AWS_DEFAULT_REGION` | AWS region |
| `AWS_ACCESS_KEY_ID` | AWS credentials |
| `AWS_SECRET_ACCESS_KEY` | AWS credentials |

### External Dependencies

| System | Role | Client |
|---|---|---|
| MongoDB Atlas | Search index storage | pymongo |
| AWS S3 | Document storage | boto3 |
| AWS SQS | Event delivery (optional) | boto3 |
| OpenAI / Bedrock | Embedding API | LangChain |
| DeepAgents | Agent framework | `BackendProtocol` |
| LangChain MongoDB | Atlas pipeline helpers | `langchain_mongodb` |

---

## 11. Design Decisions & Trade-offs

### Non-blocking Constructor

**Decision**: Spawn a background daemon thread for index provisioning and initial sync; gate search ops on a `threading.Event`.

**Why**: Atlas index creation and a large initial sync can take minutes. A blocking constructor would make integration tests slow, deployment startup fragile, and agents non-responsive during warmup.

**Trade-off**: Agents calling `grep` before `_ready` is set will block. This is intentional: returning empty results would be silently wrong.

### Server-Side Reciprocal Rank Fusion

**Decision**: Use MongoDB `$rankFusion` (Atlas 8.1+) rather than client-side score combination.

**Why**: Eliminates a second MongoDB round-trip; BM25 and cosine scores are incomparable, so rank-based fusion is more correct than score normalization; keeps the aggregation pipeline composable.

**Trade-off**: Requires MongoDB Atlas 8.1+. Pre-8.1 clusters fall back to full-text only.

### ETag-Based Optimistic Locking for Edits

**Decision**: Read object ETag, then `PutObject` with `If-Match` header.

**Why**: Prevents lost updates when two agents edit the same file concurrently. The losing writer gets `E2008_EDIT_CONFLICT` and can retry.

**Trade-off**: Two S3 round-trips per edit (HeadObject + PutObject). Acceptable since edits are rare compared to reads.

### ObjectStoreBackend ABC

**Decision**: Introduce an abstract base class for storage operations rather than depending directly on boto3.

**Why**: Enables testing with moto (S3 mock) without needing a real bucket. Also opens the door to future backends (Azure Blob, GCS, local disk) without changing higher-level logic.

**Trade-off**: One extra abstraction layer. The current implementation only supports S3, so the abstraction is speculative.

### Chunking with Token Overlap

**Decision**: 512-token chunks with 64-token overlap using tiktoken.

**Why**: Overlap preserves semantic context across chunk boundaries so that phrases split by a chunk boundary still appear in at least one chunk's context window. Token-based splitting is consistent across languages and avoids character-count heuristics.

**Trade-off**: ~12.5% storage overhead from duplicated tokens. Deduplication at retrieval time uses `(source_path, line_start)`.

---

## 12. Scalability & Cost

### MongoDB Storage Estimate

| Metric | Estimate |
|---|---|
| Embedding vector size | 1024 dims × 4 bytes = 4 KB/chunk |
| Avg chunks per document | ~25 (at 512 tokens/chunk, 50 pages avg) |
| 100k documents | ~2.5M chunks → ~10 GB embeddings |
| Atlas M10 cluster | ~$57/month |

### Embedding API Cost

| Provider | Model | Price | 100k docs (2.5M chunks × 512 tokens) |
|---|---|---|---|
| OpenAI | text-embedding-3-small | $0.02 / 1M tokens | ~$25.60 |
| Bedrock | Titan Embed v2 | $0.02 / 1M tokens | ~$25.60 |

### Initial Sync Time (100k docs)

- Download (16 workers, 100 Mbps): ~10 min
- Embedding (batch=100, 10 rps): ~60-90 min
- Upsert (500-batch bulk): ~5 min

### Scalability Limits

- **MongoDB Atlas**: Atlas Vector Search scales to hundreds of millions of vectors via replica sets and sharding.
- **S3**: No practical object count limit.
- **Embedding API**: Rate-limited by provider; horizontal scaling requires multiple Embedder instances (not currently supported).
- **Watcher**: Single-threaded polling/SQS loop; high-churn workloads (>100 changes/min) should prefer SQSWatcher.

---

## 13. Testing Strategy

### Unit Tests (`tests/unit/`)

No real AWS or MongoDB required:
- **S3**: Mocked via [moto](https://github.com/getmoto/moto)
- **MongoDB**: Mocked via [mongomock](https://github.com/mongomock/mongomock)
- **Coverage**: Chunker, Embedder, S3Backend, IndexManager, errors, SearchRouter

### Integration Tests (`tests/integration/`)

- Moto + mongomock with [testcontainers](https://testcontainers.com/)
- Covers: InitialSync, watcher callbacks, search operations, error propagation

### End-to-End Tests (`tests/e2e/`)

- Real Atlas cluster + real S3 bucket (requires credentials)
- Full stack validation including `$rankFusion` and `$vectorSearch`
- Optional MongoDB command logging via `MONGO_LOG=1` (redacts large vectors)
- 20/20 real_e2e tests passing (as of 2026-05-04)

---

## 14. Known Limitations

1. **`llm` parameter is unused**: The constructor accepts an `llm` parameter reserved for future LLM-based operations but does not currently use it.

2. **Full file reads for editing**: `edit()` reads the entire S3 object into memory. Large files (>1 GB) may cause memory pressure.

3. **Single-threaded ingestion in watcher**: The watcher ingestion loop (`_ingest`) is sequential. High-velocity S3 change streams may lag.

4. **Path filter applied post-vector search**: Because `$vectorSearch` does not support regex path filters, glob/path scoping is done as a `$match` after vector search, which may return and discard irrelevant results.

5. **Atlas 8.1+ required for `$rankFusion`**: Clusters on earlier Atlas versions fall back to full-text search only. The fallback is silent and automatic.

6. **No streaming reads**: `read(path)` loads the full object into memory before returning. The `offset`/`limit` parameters use S3 Range headers to avoid unnecessary data transfer, but the requested range is still fully buffered.
