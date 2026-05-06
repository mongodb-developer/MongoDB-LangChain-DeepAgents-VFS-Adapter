# Usage Guide

## Installation

```bash
pip install deepagents_mongodb_fs
```

Set your environment variables:

```bash
export OPENAI_API_KEY="sk-..."          # for the default embedding model
export AWS_ACCESS_KEY_ID="..."
export AWS_SECRET_ACCESS_KEY="..."
export AWS_DEFAULT_REGION="us-east-1"
```

---

## Basic Setup

```python
from deepagents_mongodb_fs import MongoFilesystemBackend

backend = MongoFilesystemBackend(
    s3_bucket_name="my-docs-bucket",
    mongodb_connection_string="mongodb+srv://user:pass@cluster.mongodb.net/",
)
# Constructor returns immediately. Index provisioning, initial sync,
# and the watcher start in a background daemon thread.
# Search operations block until the first sync completes.
```

---

## grep — Hybrid search over file contents

```python
# Natural-language or keyword search
result = backend.grep("authentication token flow")
for match in result.matches:
    print(f"{match.path}:{match.line}  {match.content[:100]}")

# Restrict to a path prefix
result = backend.grep("installation", path="docs/")

# Restrict to files matching a glob pattern
result = backend.grep("quarterly", glob="*.pdf")

# Combined
result = backend.grep("error handling", path="src/", glob="*.py")
```

**Cost note:** `grep` generates one embedding API call per query (negligible cost). The expensive part is the initial sync — at 100k 50-page docs, expect ~25M chunks, ~$500 in embedding costs with `text-embedding-3-small`.

---

## glob — Find files by name pattern

```python
# All PDFs in the bucket
result = backend.glob("*.pdf")
print(result.paths)

# All markdown files under docs/
result = backend.glob("*.md", path="docs/")

# All Python files
result = backend.glob("*.py")
```

---

## ls — List directory contents

```python
# Root level
result = backend.ls("")
for entry in result.entries:
    indicator = "/" if entry.is_dir else ""
    print(f"{entry.name}{indicator}")

# Subdirectory
result = backend.ls("docs/api/")
```

---

## read — Read file contents

```python
result = backend.read("docs/README.txt")
if result.error:
    print("Error:", result.error)
else:
    print(result.content[:500])

# With offset and limit (byte slice)
result = backend.read("large_file.txt", offset=1024, limit=4096)
```

---

## write — Write a file

```python
result = backend.write("docs/notes.txt", "My notes here.")
if result.success:
    print("Written successfully")
```

---

## edit — In-place edit (conditional write)

```python
# Replace first occurrence
result = backend.edit("docs/config.txt", old="debug=false", new="debug=true")

# Replace all occurrences
result = backend.edit("docs/config.txt", old="localhost", new="prod.host.com", replace_all=True)

if result.error and "E2008" in result.error:
    print("Concurrent modification detected — retry")
```

---

## upload_files / download_files

```python
# Upload multiple files
with open("report.pdf", "rb") as f:
    data = f.read()
result = backend.upload_files([("reports/2026-Q1.pdf", data)])

# Download multiple files
result = backend.download_files(["reports/2026-Q1.pdf", "docs/readme.txt"])
print(result.downloaded)   # list of successfully downloaded paths
print(result.failed)       # list of failures (if any)
```

---

## Watcher configuration

### Default: Polling (zero extra AWS setup)

```python
backend = MongoFilesystemBackend(
    s3_bucket_name="my-bucket",
    mongodb_connection_string="mongodb+srv://...",
    watcher="polling",   # default
)
```

The polling watcher checks S3 every 5 minutes (ETag diff). Suitable for development and low-churn buckets.

### Production: SQS event-driven

```python
backend = MongoFilesystemBackend(
    s3_bucket_name="my-bucket",
    mongodb_connection_string="mongodb+srv://...",
    watcher="sqs",
    sqs_queue_url="https://sqs.us-east-1.amazonaws.com/123456789/s3-events",
)
```

Requires S3 event notifications configured to send to the SQS queue. Latency is seconds rather than minutes.

**SQS IAM policy required:**

```json
{
  "Effect": "Allow",
  "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
  "Resource": "arn:aws:sqs:us-east-1:123456789:s3-events"
}
```

---

## Custom embedding model

```python
from langchain_openai import AzureOpenAIEmbeddings

backend = MongoFilesystemBackend(
    s3_bucket_name="my-bucket",
    mongodb_connection_string="mongodb+srv://...",
    embedding_model=AzureOpenAIEmbeddings(
        model="text-embedding-3-small",
        azure_endpoint="https://my-instance.openai.azure.com/",
        api_key="...",
    ),
    embedding_dimensions=1024,
)
```

Any `langchain_core.embeddings.Embeddings` implementation works here.

---

## Error handling

```python
result = backend.grep("query")
if result.error:
    if "E3001" in result.error:
        print("MongoDB connection failed — check your URI")
    elif "E4003" in result.error:
        print("Embedding rate limit — retry after backoff")
    else:
        print(result.error)
```

Use `debug=True` during local development to get full stack traces instead:

```python
backend = MongoFilesystemBackend(..., debug=True)
```

---

## Cost estimation for 100k documents

| Item | Estimate |
|---|---|
| Avg chunks per 50-page document | ~25 |
| Total chunks at 100k docs | ~2.5M |
| Tokens per chunk (512 max) | ~512 |
| Total tokens | ~1.28B |
| Cost at $0.02/1M tokens (text-embedding-3-small) | **~$25.60** |
| Atlas storage at 1024 dims × 4 bytes × 2.5M chunks | **~10 GB** |
| Atlas M10 cluster (with Vector Search) | ~$57/month |

Initial sync of 100k documents takes ~1–3 hours depending on network throughput and embedding API concurrency.
