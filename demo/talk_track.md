# Demo Talk Track — Product Check-in

**Target length:** 20 minutes (15 demo + 5 Q&A buffer).
**Slot allocation:**

| Section | Time | Material |
|---|---|---|
| Opening — problem + what we ship | 3 min | `slides.md` pp. 1–4 |
| Why `$rankFusion` (the bet) | 2 min | `slides.md` p. 5 |
| **Developer Experience demo** | 4 min | `demo.ipynb` §1 |
| **Consumer Experience demo** | 5 min | `demo.ipynb` §2 |
| Test results + roadmap + asks | 3 min | `slides.md` pp. 10–13 + `artifacts/` |
| Q&A | balance | — |

---

## Opening (3 min) — `slides.md` p. 1–4

Land three points before any code:

1. **The pain.** Agents need to navigate S3 corpora; S3 has no search. Naïve indexing per agent run is wasteful.
2. **Our bet.** Keep S3 as source of truth. Make Atlas the *searchable mirror*. Keep them in sync with a watcher. Use **hybrid search** because no single retrieval signal wins all queries.
3. **What "done" looks like today.** A drop-in `BackendProtocol` implementation, ~10 lines to integrate, three test tiers green.

Cue line: *"We're at late-beta. The architecture is settled, the surface is settled, the tests are green. Today I want to show you what an engineer sees integrating us, what a DeepAgent feels using us, and what we still need to ship before 0.1.0 leaves the dock."*

---

## Why `$rankFusion` (2 min) — p. 5

This slide exists because every PM will ask *"why not just embeddings?"* Pre-empt it.

- BM25 wins on exact identifiers / rare strings.
- Vectors win on paraphrase.
- We've all seen demos where each fails in its weak case — the **hero demo in §2 of the notebook is designed to make this visceral**.

---

## Developer Experience demo (4 min) — `demo.ipynb` §1

Run cells **1.1 → 1.6** in order.

| Cell | Beat |
|---|---|
| 1.1 Imports + env check | "Two env vars and an API key. That's the config surface." |
| 1.2 Construct the backend | **Highlight that this returns instantly.** Look at the wall-clock printed below the cell. The sync is already running in the daemon thread. |
| 1.3 Watch `_ready` flip | This is what gates the search ops. Engineers don't need to think about it — they just call `grep` and it blocks until sync is done. |
| 1.4 Forced error → clean DTO | Run the bad-bucket cell. Show: no stack trace, structured `error="E2xxx ..."`. Pull up `docs/ERROR_CODES.md` in a second tab if a stakeholder pushes on it. |
| 1.5 Swap embedder (OpenAI ↔ Bedrock) | Don't actually swap live; show the one-line change. Sell: "any LangChain `Embeddings` works." |
| 1.6 Docs tour | 30-second pass over `USAGE.md` / `API_REFERENCE.md`. |

**If something breaks:** the notebook has a `MOCK_MODE=1` flag at the top that pivots to mongomock + moto. You lose the hybrid-search hero moment but keep the DX story.

---

## Consumer Experience demo (5 min) — `demo.ipynb` §2

This is the part the PM will remember. Three beats.

### Beat 1 — `ls` / `glob` (45 sec)
- `backend.ls("docs/")` — show structure.
- `backend.glob("**/*.pdf")` — show multi-format ingestion.

### Beat 2 — The hybrid `grep` hero demo (3 min)
Three queries on the same corpus:

1. **Exact ID query:** `"ACME-AUTH-7421"` — lexical wins. Note that pure-vector would dilute this.
2. **Paraphrase query:** `"how do we handle session expiry and silent re-auth"` — vector wins. The word "rotation" never appears in the most relevant docs.
3. **Mixed query:** `"ACME-AUTH-7421 session expiry"` — hybrid wins. Show the runbooks (which carry *both* signals) ranking above the single-signal docs.

The notebook prints all three results side by side. **Talking point:** *"Same call, same code path, three different queries — and the user never had to choose a retrieval mode."*

### Beat 3 — Live sync (1 min)
- Write a new file to S3 via `backend.write(...)`.
- Wait for the polling tick (interval is dropped to 10s for the demo).
- Re-run `grep` on a phrase only in that new file. Hit.

**Talking point:** *"S3 is still the source of truth. The agent doesn't need to know we have an index — it just needs the index to be fresh."*

---

## Test results + roadmap + asks (3 min) — slides 10–13

- Open `demo/artifacts/test_summary.txt` in a terminal pane. Pytest summary fits on one screen.
- Coverage headline from `coverage_summary.txt`.
- If `RUN_REAL_E2E=1` was set the night before, show `mongo_e2e_excerpt.log` — *"every Mongo command our backend issues in production is observable and redacted."*
- Pivot to slide 12 (roadmap, Wikipedia tiers).
- Land slide 13 (asks): PyPI sign-off, prioritization between perf / new object stores / agent-LLM integration, design-partner ask.

---

## Q&A — common questions, pre-baked answers

**"What if Atlas goes down?"**
We degrade to text-only via `text_search_stage`, then to plain `$regex` on non-Atlas Mongo. The DeepAgent keeps working; search quality drops gracefully. Same code path is exercised in CI on mongomock.

**"What about content updates / deletes?"**
ETag-diffed in both initial sync and the polling watcher. Deletes on the watcher trigger a `delete_many` on the chunks collection before any re-upsert.

**"Concurrency on `edit`?"**
S3 conditional write via `IfMatch: <etag>`. Lost-update conflicts surface as `E2008_EDIT_CONFLICT` — DTO has an `error` field, never raises into agent code.

**"Why MongoDB and not Postgres + pgvector?"**
We needed `$rankFusion` (RRF) as a server-side primitive over both lexical and vector signals, plus a hosted offering for the watcher / multi-region story. Could be revisited, but the gain isn't large enough to justify two storage engines.

**"How much does this cost to run?"**
Two cost centers: Atlas (storage + search index) and the embedding provider. We have a perf milestone planned on Wikipedia to put numbers on both — that's slide 12 and one of today's asks.

**"What's the minimum bar to ship 0.1.0 to PyPI?"**
Perf harness landed + numbers published + the two minor code smells cleaned up. ~1 sprint.
