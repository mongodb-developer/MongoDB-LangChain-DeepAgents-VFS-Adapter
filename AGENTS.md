# AGENTS.md

Guidance for AI coding agents working in this repository.

## Project overview

`deepagents_mongodb_fs` is a **MongoDB Atlas-backed filesystem search adapter for LangChain DeepAgents**. It implements DeepAgents' `BackendProtocol`:

- `grep`, `glob`, and `ls` are routed through **MongoDB Atlas** (vector search + full-text search + hybrid `$rankFusion`).
- `read`, `write`, `edit`, `upload_files`, and `download_files` are forwarded directly to **AWS S3**.

- **Package name (PyPI):** `deepagents_mongodb_fs`
- **GitHub repo:** `mongodb-developer/MongoDB-LangChain-DeepAgents-VFS-Adapter`
- **Language:** Python 3.10+
- **Build backend:** hatchling

## Repository layout

| Path | Purpose |
|---|---|
| `src/deepagents_mongodb_fs/` | Library source |
| `src/deepagents_mongodb_fs/backend.py` | `MongoFilesystemBackend` — main entry point |
| `src/deepagents_mongodb_fs/backends/` | Storage backends (`base.py`, `s3.py`) |
| `src/deepagents_mongodb_fs/watcher/` | Sync watchers (`polling.py`, `sqs.py`) |
| `src/deepagents_mongodb_fs/search.py` | `$rankFusion` hybrid search router |
| `src/deepagents_mongodb_fs/chunker.py` | Multi-format text extraction + chunking |
| `src/deepagents_mongodb_fs/embedder.py` | Embedding provider resolution (Bedrock/OpenAI) |
| `tests/unit`, `tests/integration`, `tests/e2e` | Test suites by marker |
| `docs/` | Architecture, usage, error codes, design docs |
| `demo/` | Corpus build/seed scripts and notebook |

## Setup

```bash
pip install -e ".[dev]"
```

## Build and test commands

```bash
# Build distributions
python -m build
python -m twine check dist/*

# Lint and type-check
ruff check src tests
mypy src

# Tests (by marker)
pytest -m unit            # fast, no external services
pytest -m integration     # moto + mongomock (auto-installed via dev extras)
pytest -m e2e             # full stack, moto + mongomock (no real credentials)
pytest -m real_e2e        # requires real Atlas + S3 + embedding provider creds
pytest -m watcher_e2e     # PollingWatcher E2E; subset of real_e2e
```

For `real_e2e`, copy `.env.e2e.template` to `.env.e2e`, fill in credentials, then:

```bash
set -a && source .env.e2e && set +a
pytest -m real_e2e -v
```

## Conventions

- Line length 100 (`ruff`), target Python 3.10. Lint rules: `E, F, I, UP, B, SIM`.
- `mypy` runs in `strict` mode — keep type annotations complete.
- Every public method returns a DTO; errors are surfaced via a stable `ErrorCode` in the `error` field, never raw stack traces. See `docs/ERROR_CODES.md`.
- Use `debug=True` only for local development (re-raises exceptions with traceback).

## Releasing

Releases publish to PyPI via **Trusted Publishing (OIDC)** in `.github/workflows/release.yml`:

- Push a tag `vX.Y.Z` to publish to TestPyPI then PyPI.
- Or run the workflow manually (`workflow_dispatch`) for a TestPyPI dry-run.
- No API tokens are stored; pending publishers must be configured on PyPI/TestPyPI.

## More documentation

- `README.md` — quickstart and configuration
- `docs/SYSTEM_DESIGN.md`, `docs/LOW_LEVEL_DESIGN.md` — architecture
- `docs/API_REFERENCE.md`, `docs/USAGE.md`, `docs/DEEPAGENTS_USAGE.md` — usage
- `docs/CONTRIBUTING.md` — contributing (incl. adding a new storage backend)
