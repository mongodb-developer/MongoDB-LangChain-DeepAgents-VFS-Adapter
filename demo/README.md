# Demo Package — `deepagents_mongodb_fs`

Materials for the **product check-in**. Two audiences are showcased:

1. **Developer Experience (DX)** — what it feels like to integrate the library
2. **Consumer Experience (CX)** — what it feels like for a DeepAgent (and its end-user) to navigate an S3 corpus through us

Plus a **test-results** section so the PM can see proof, not promises.

---

## Contents

| File | Purpose |
|---|---|
| `slides.md` | Marp deck — opens the meeting (problem, architecture, what we ship today, what's next) |
| `demo.ipynb` | Live notebook — DX section then CX section, runnable against real Atlas + S3 |
| `talk_track.md` | Presenter notes, timing, fallback plans if live demo breaks |
| `build_corpus.py` | Generates ~50 mixed-format files (txt / md / pdf / docx) into `corpus/` |
| `seed_corpus.py` | Uploads `corpus/` to your S3 bucket under a chosen prefix |
| `run_tests.sh` | Runs `pytest` (unit + integration) with coverage and saves a summary artifact |
| `corpus/` | Generated demo files (gitignored — regenerate locally) |
| `artifacts/` | Test summary, coverage XML excerpt, mongo command log excerpt — for the slides |

---

## Prereqs

- Python ≥ 3.10, project installed in editable mode (`pip install -e ".[dev,openai]"` from repo root)
- A **MongoDB Atlas** cluster (any tier with Search + Vector Search enabled — M0 free tier works)
- An **S3 bucket** you can write to
- An **OpenAI API key** (default embedder) or Bedrock credentials
- `jupyter` (`pip install jupyter`) and, optionally, [Marp CLI](https://github.com/marp-team/marp-cli) for rendering slides

Set these in your shell **before** launching jupyter:

```bash
export MONGODB_URI="mongodb+srv://<user>:<pw>@<cluster>.mongodb.net/?retryWrites=true&w=majority"
export S3_BUCKET_NAME="your-demo-bucket"
export S3_PREFIX="demo/"                         # optional, isolates demo data
export AWS_REGION="us-east-1"
export OPENAI_API_KEY="sk-..."
```

---

## Run order (T-1 day to demo)

```bash
# 1. Generate the demo corpus (~50 files, mixed formats)
python demo/build_corpus.py

# 2. Upload to S3 under your prefix
python demo/seed_corpus.py

# 3. Refresh the test artifacts that the slides reference
bash demo/run_tests.sh

# 4. Render slides (optional)
marp demo/slides.md --pdf -o demo/artifacts/slides.pdf

# 5. Smoke-test the notebook end-to-end
jupyter nbconvert --to notebook --execute demo/demo.ipynb \
    --output demo.executed.ipynb --output-dir demo/artifacts/
```

If step 5 is green, you're ready.

## Run order (demo day)

```bash
jupyter lab demo/demo.ipynb
# open slides.pdf in a second window
```

Follow `talk_track.md` for cues.

---

## Cleanup

The notebook writes a few files under your S3 `$S3_PREFIX` and several
documents into the Atlas `deepagents_mongodb_fs.chunks` collection. To reset
between rehearsals:

```bash
aws s3 rm "s3://$S3_BUCKET_NAME/$S3_PREFIX" --recursive
# then in mongosh:
#   use deepagents_mongodb_fs
#   db.chunks.deleteMany({})
```
