#!/usr/bin/env python3
"""Generate a small, curated demo corpus (~50 files, mixed formats).

The corpus is designed to make the hybrid `grep` story land:

- Several files contain a *rare exact identifier* (``ACME-AUTH-7421``) that
  vector search alone tends to miss → lexical / FTS wins.
- Several files describe the *concept* of token rotation, session expiry, and
  refresh flows without ever using the exact phrase the agent will query →
  vector search wins.
- A handful of files contain *both* the exact identifier and conceptual
  language → ``$rankFusion`` (RRF over the two) wins versus either alone.
- A small set of "noise" files on unrelated topics (cooking, weather, travel)
  is included to verify ranking actually filters them out.

File formats covered: ``.txt`` ``.md`` ``.pdf`` ``.docx``. Output goes to
``demo/corpus/`` relative to this script.

Run:    python demo/build_corpus.py
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from docx import Document  # python-docx
    from pypdf import PdfWriter
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
except ImportError as exc:  # pragma: no cover - dev-time helper
    print(
        f"Missing dependency: {exc.name}.\n"
        "Install with:  pip install python-docx pypdf reportlab",
        file=sys.stderr,
    )
    sys.exit(1)


HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus"

# --- Content fragments -------------------------------------------------------

RARE_ID = "ACME-AUTH-7421"

EXACT_ONLY = [
    f"Incident report {RARE_ID}: rotation key was leaked via a misconfigured "
    "logging sink. No customer credentials were exposed.",
    f"Change request {RARE_ID} was approved by the security review board on "
    "the 12th and merged the following Tuesday.",
    f"Run ID {RARE_ID} appears in three audit traces; cross-reference with "
    "the SIEM dashboard before closing the ticket.",
    f"Postmortem header: title=`{RARE_ID}`, severity=`sev-2`, status=`resolved`.",
]

CONCEPT_ONLY = [
    "The refresh credential lifecycle was redesigned last quarter. Short-lived "
    "bearer material is now issued at sign-in and silently re-issued from a "
    "longer-lived sibling credential without prompting the user. Inactive "
    "sessions are torn down server-side after a configurable idle window.",
    "Our session management story relies on rotating short-lived secrets. The "
    "client never sees the long-lived seed; only the derived per-request "
    "material crosses the wire. Expiry is enforced at the gateway, not in "
    "the client.",
    "When a user comes back from sleep, the SDK transparently swaps the stale "
    "credential for a fresh one using the long-lived companion artifact. The "
    "exchange happens in the background and is observable only via telemetry.",
    "Background: we used to keep a single long-lived bearer in local storage. "
    "We replaced that with a paired short / long credential design so a "
    "compromised short credential expires before it can be replayed.",
    "Idle session teardown is governed by a separate timer than active "
    "session expiry. Operators tune both via the platform console; defaults "
    "are 15 minutes idle, 8 hours active.",
]

BOTH = [
    f"Runbook for {RARE_ID}: the rotation of short-lived bearer credentials "
    "stalled because the long-lived companion artifact had been revoked. "
    "Re-issue the companion artifact, then bounce the gateway pods.",
    f"Design doc excerpt — incident {RARE_ID} prompted us to revisit the "
    "refresh credential lifecycle. The short / long credential pairing now "
    "fails closed when the long-lived seed is missing.",
    f"Operator note: alarm `{RARE_ID}` fires when the rotation of session "
    "material backs up. Inspect the silent re-issue path before paging the "
    "on-call.",
]

NOISE = [
    ("recipe_carbonara.md", "# Spaghetti Carbonara\n\n- Guanciale, eggs, pecorino, pepper.\n"),
    ("weather_seattle.txt", "Seattle averages 152 rainy days per year; the wettest months are November through January.\n"),
    ("travel_kyoto.md", "# Kyoto in autumn\n\nMaple peak is typically mid-November near Tofuku-ji and Eikando.\n"),
    ("garden_tomatoes.txt", "Determinate tomato varieties set their fruit in a short window; indeterminate keep producing until frost.\n"),
    ("music_jazz.md", "# Hard bop\n\nA reaction to cool jazz; rooted in gospel and blues phrasing.\n"),
    ("bike_maintenance.txt", "Chain wear is measured with a 12-inch ruler across rivet pins; replace at 0.75% elongation.\n"),
    ("origami_crane.md", "# Crane fold\n\nBegin with a bird base; sink the head and tail.\n"),
    ("astronomy_eclipse.txt", "Total solar eclipses recur at a given location roughly every 360 years on average.\n"),
    ("photography_tips.md", "# Golden hour\n\nLow-angle sun softens shadows and warms midtones.\n"),
    ("history_silk_road.txt", "The Silk Road was a network, not a single route; maritime branches were often busier than overland ones.\n"),
]


# --- Writers -----------------------------------------------------------------

def write_txt(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def write_md(path: Path, title: str, body: str) -> None:
    path.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")


def write_docx(path: Path, title: str, body: str) -> None:
    doc = Document()
    doc.add_heading(title, level=1)
    for para in body.split("\n\n"):
        doc.add_paragraph(para)
    doc.save(str(path))


def write_pdf(path: Path, title: str, body: str) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter
    text = c.beginText(50, height - 72)
    text.setFont("Helvetica-Bold", 14)
    text.textLine(title)
    text.setFont("Helvetica", 11)
    text.textLine("")
    for line in body.split("\n"):
        # naive wrap at ~95 chars
        while len(line) > 95:
            text.textLine(line[:95])
            line = line[95:]
        text.textLine(line)
    c.drawText(text)
    c.showPage()
    c.save()


# --- Build plan --------------------------------------------------------------

def build() -> None:
    if CORPUS.exists():
        for p in CORPUS.iterdir():
            if p.is_file():
                p.unlink()
    CORPUS.mkdir(parents=True, exist_ok=True)

    (CORPUS / "docs").mkdir(exist_ok=True)
    (CORPUS / "runbooks").mkdir(exist_ok=True)
    (CORPUS / "noise").mkdir(exist_ok=True)

    n = 0

    # Exact-identifier files (lexical wins) — spread across formats
    for i, body in enumerate(EXACT_ONLY):
        fmt = ["txt", "md", "pdf", "docx"][i % 4]
        title = f"Audit note {i + 1}"
        path = CORPUS / "docs" / f"audit_{i + 1}.{fmt}"
        if fmt == "txt":
            write_txt(path, body)
        elif fmt == "md":
            write_md(path, title, body)
        elif fmt == "pdf":
            write_pdf(path, title, body)
        else:
            write_docx(path, title, body)
        n += 1

    # Concept-only files (vector wins) — spread across formats
    for i, body in enumerate(CONCEPT_ONLY):
        fmt = ["md", "pdf", "docx", "txt", "md"][i % 5]
        title = f"Design note {i + 1}"
        path = CORPUS / "docs" / f"design_{i + 1}.{fmt}"
        if fmt == "txt":
            write_txt(path, body)
        elif fmt == "md":
            write_md(path, title, body)
        elif fmt == "pdf":
            write_pdf(path, title, body)
        else:
            write_docx(path, title, body)
        n += 1

    # Both signals (hybrid wins) — runbooks
    for i, body in enumerate(BOTH):
        fmt = ["md", "docx", "pdf"][i % 3]
        title = f"Runbook {i + 1}"
        path = CORPUS / "runbooks" / f"runbook_{i + 1}.{fmt}"
        if fmt == "md":
            write_md(path, title, body)
        elif fmt == "pdf":
            write_pdf(path, title, body)
        else:
            write_docx(path, title, body)
        n += 1

    # Noise files (must NOT rank for our query)
    for name, body in NOISE:
        write_txt(CORPUS / "noise" / name, body) if name.endswith(".txt") else write_md(
            CORPUS / "noise" / name, name.removesuffix(".md").replace("_", " ").title(), body
        )
        n += 1

    # Padding files so we land at ~50 total — short generic notes
    pad_topics = [
        "kubernetes pod scheduling",
        "postgres vacuum tuning",
        "graphql schema stitching",
        "kafka consumer groups",
        "redis eviction policies",
        "TLS certificate pinning",
        "OAuth 2.0 device flow",
        "WebSocket back-pressure",
        "feature flag rollout",
        "gRPC deadline propagation",
        "S3 multipart uploads",
        "DynamoDB hot partitions",
        "CDN cache invalidation",
        "ElasticSearch shard sizing",
        "Lambda cold start mitigation",
        "Terraform state locking",
        "Helm chart values overrides",
        "Prometheus recording rules",
        "OpenTelemetry context propagation",
        "ArgoCD sync waves",
        "Vault transit secrets engine",
        "Envoy circuit breakers",
        "Istio traffic shifting",
        "Cilium network policies",
        "etcd compaction",
    ]
    needed = max(0, 50 - n)
    for i in range(needed):
        topic = pad_topics[i % len(pad_topics)]
        body = (
            f"Brief note on {topic}. This file is intentionally generic and "
            "exists to give the search index realistic background volume. It "
            f"does not discuss {RARE_ID} or refresh-credential lifecycles."
        )
        fmt = ["txt", "md"][i % 2]
        path = CORPUS / "docs" / f"note_{i + 1:02d}.{fmt}"
        if fmt == "txt":
            write_txt(path, body)
        else:
            write_md(path, topic.title(), body)
        n += 1

    print(f"Wrote {n} files under {CORPUS}")


if __name__ == "__main__":
    build()
