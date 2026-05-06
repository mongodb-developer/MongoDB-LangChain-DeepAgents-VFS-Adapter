# Error Code Reference

All errors surfaced by `MongoFilesystemBackend` use a stable `ErrorCode`. The error code appears in the `error` field of the returned DTO as `[EXXXX] Human-readable message. Detail: <cause>`.

No raw exceptions, driver errors, or stack traces are ever returned to the caller. Use `debug=True` during development to re-raise the original exception with a full traceback.

---

## E1xxx — Configuration / Initialization

| Code | Name | Meaning | Likely Cause | Remediation |
|---|---|---|---|---|
| E1001 | MISSING_CONFIG | Required configuration parameter is missing. | Constructor called without `s3_bucket_name` or `mongodb_connection_string`. | Provide all required parameters. |
| E1002 | INVALID_BUCKET | S3 bucket does not exist or is not accessible. | Bucket name typo, or IAM role lacks `s3:HeadBucket`. | Verify bucket exists and IAM policy includes `s3:HeadBucket`, `s3:GetObject`, `s3:PutObject`, `s3:ListBucket`. |
| E1003 | INVALID_MONGO_URI | MongoDB connection string is invalid or cluster unreachable. | Wrong URI, expired credentials, network firewall. | Test URI with `pymongo.MongoClient(uri).admin.command('ping')`. |
| E1004 | INDEX_PROVISION_FAILED | Failed to create required Atlas search indexes. | Insufficient Atlas cluster tier (Vector Search requires M10+), or permission issue. | Use Atlas M10+ for production. Check Atlas project roles. |
| E1005 | INITIAL_SYNC_FAILED | Initial S3 → MongoDB sync did not complete. | S3 or embedding API unreachable during startup. | Check logs for the specific sub-error; restarting will resume from last ETag checkpoint. |
| E1006 | WATCHER_START_FAILED | Background watcher could not start. | Invalid SQS queue URL, or polling interval of 0. | Verify SQS URL and IAM permissions. |

---

## E2xxx — Object Store

| Code | Name | Meaning | Likely Cause | Remediation |
|---|---|---|---|---|
| E2001 | OBJECT_NOT_FOUND | Requested object does not exist in the bucket. | Key typo, or object was deleted between listing and fetching. | Check the key with `backend.ls()` or AWS Console. |
| E2002 | OBJECT_READ_FAILED | Failed to read object from the object store. | Network error, IAM `s3:GetObject` missing. | Check IAM policy and VPC routing. |
| E2003 | OBJECT_WRITE_FAILED | Failed to write object. | IAM `s3:PutObject` missing, or bucket policy blocks. | Verify write permissions. |
| E2004 | OBJECT_DELETE_FAILED | Failed to delete object. | IAM `s3:DeleteObject` missing. | Add delete permission if needed. |
| E2005 | LIST_FAILED | Failed to list objects in the bucket. | IAM `s3:ListBucket` missing. | Add `s3:ListBucket` to the IAM policy. |
| E2006 | UPLOAD_FAILED | One or more file uploads failed. | Partial network failure; check `UploadResult.failed` for which keys failed. | Retry failed keys individually. |
| E2007 | DOWNLOAD_FAILED | One or more file downloads failed. | Object deleted mid-download, or network timeout. | Retry or check `DownloadResult.failed`. |
| E2008 | EDIT_CONFLICT | Edit conflict — object was modified concurrently. | Another process wrote the same key between your read and write. | Re-read the object and apply your edit again. |

---

## E3xxx — MongoDB

| Code | Name | Meaning | Likely Cause | Remediation |
|---|---|---|---|---|
| E3001 | CONNECTION_FAILED | Could not connect to MongoDB. | Wrong URI, cluster paused, firewall. | Test connection string independently. |
| E3002 | QUERY_FAILED | MongoDB query failed. | Malformed aggregation pipeline, or Atlas Search index temporarily unavailable. | Check Atlas cluster status; retry after a few seconds. |
| E3003 | UPSERT_FAILED | MongoDB bulk upsert operation failed. | Atlas cluster under high load, or write concern timeout. | Reduce batch size or retry. |
| E3004 | DELETE_FAILED | MongoDB delete operation failed. | Cluster write unavailable. | Check Atlas cluster status. |
| E3005 | INDEX_NOT_READY | Atlas search index is not yet queryable. | Index just created; still building. | Wait for `IndexManager.wait_until_queryable()` — can take up to 10 min on first provision. |

---

## E4xxx — Embedding

| Code | Name | Meaning | Likely Cause | Remediation |
|---|---|---|---|---|
| E4001 | EMBEDDING_API_FAILED | The embedding API returned an error. | Invalid API key, model unavailable, or server error. | Check `OPENAI_API_KEY`. Test with `OpenAI().embeddings.create(...)` directly. |
| E4002 | EMBEDDING_DIMENSION_MISMATCH | Returned vector size doesn't match configured dimensions. | Model changed, or `embedding_dimensions` misconfigured. | Ensure `embedding_dimensions` matches the model's output size. |
| E4003 | EMBEDDING_RATE_LIMITED | Embedding API rate limit exceeded after all retries. | Too many concurrent requests. | Reduce `batch_size`, add org-level rate-limit increase, or use a different embedding provider. |

---

## E5xxx — Search

| Code | Name | Meaning | Likely Cause | Remediation |
|---|---|---|---|---|
| E5001 | GREP_FAILED | The grep search operation failed. | Atlas cluster unavailable, or malformed query. | Check Atlas status; retry. |
| E5002 | GLOB_FAILED | The glob search operation failed. | Atlas Full-Text index unavailable, or invalid pattern. | Verify index status; test pattern with `fnmatch.fnmatch`. |
| E5003 | LS_FAILED | The ls operation failed. | MongoDB aggregation error. | Check cluster connectivity. |
| E5004 | VECTOR_SEARCH_UNAVAILABLE | Atlas Vector Search not available on this cluster. | Using Atlas M0 or community MongoDB. | Upgrade to Atlas M10+. |

---

## E6xxx — Watcher

| Code | Name | Meaning | Likely Cause | Remediation |
|---|---|---|---|---|
| E6001 | SQS_RECEIVE_FAILED | Failed to receive messages from SQS. | IAM missing `sqs:ReceiveMessage`, or invalid queue URL. | Verify queue URL and IAM permissions. |
| E6002 | EVENT_PARSE_FAILED | Could not parse an S3 event notification from SQS. | Unexpected event shape (e.g. test notification from AWS). | Check SQS message body format; ensure S3 event notifications are properly configured. |
| E6003 | WATCHER_CRASHED | Background watcher thread crashed. | Unhandled exception in the watcher loop. | Check application logs for the underlying error; restart the backend. |

---

## E9xxx — Internal / Unexpected

| Code | Name | Meaning | Likely Cause | Remediation |
|---|---|---|---|---|
| E9001 | INTERNAL_ERROR | An unexpected internal error occurred. | Bug in the adapter. | Enable `debug=True`, capture the traceback, and file a GitHub issue. |
| E9002 | CHUNKER_FAILED | The document chunker encountered an error. | Corrupt file, or encoding issue. | Verify the file is valid and try a different format. |
| E9003 | FORMAT_UNSUPPORTED | File format is not supported by the chunker. | `.xls`, `.zip`, `.pptx`, or other unsupported type. | Convert to `.pdf`, `.docx`, or `.txt` before uploading, or implement a custom extractor. |
