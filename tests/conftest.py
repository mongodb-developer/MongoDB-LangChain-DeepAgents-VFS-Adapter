"""Shared pytest fixtures for unit, integration, and e2e tests."""
from __future__ import annotations

import io
import os
import textwrap
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

# ---------------------------------------------------------------------------
# Fixture data paths
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# AWS / S3 fixtures (moto)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def aws_credentials():
    """Fake AWS creds so moto doesn't hit real AWS."""
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    yield
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SECURITY_TOKEN",
               "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION"):
        os.environ.pop(k, None)


@pytest.fixture(scope="function")
def s3_bucket(aws_credentials):
    """Create a moto-mocked S3 bucket and yield its name."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        yield "test-bucket"


@pytest.fixture(scope="function")
def s3_backend(s3_bucket):
    """Return an S3Backend pointed at the moto-mocked bucket."""
    from deepagents_mongodb_fs.backends.s3 import S3Backend
    with mock_aws():
        yield S3Backend(bucket_name=s3_bucket, region_name="us-east-1")


@pytest.fixture(scope="function")
def sqs_queue(aws_credentials):
    """Create a moto-mocked SQS queue and yield its URL."""
    with mock_aws():
        client = boto3.client("sqs", region_name="us-east-1")
        response = client.create_queue(QueueName="test-queue")
        yield response["QueueUrl"]


# ---------------------------------------------------------------------------
# MongoDB fixtures (mongomock)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def mongo_collection():
    """Return a mongomock Collection for the chunk store."""
    mongomock = pytest.importorskip("mongomock")
    client = mongomock.MongoClient()
    return client["deepagents_mongodb_fs"]["chunks"]


# ---------------------------------------------------------------------------
# Embedder mock
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_embedder():
    """Embedder that returns deterministic zero vectors."""
    from deepagents_mongodb_fs.embedder import Embedder
    from deepagents_mongodb_fs.dtypes import Chunk

    embedder = MagicMock(spec=Embedder)
    embedder._dimensions = 1024

    def _embed(chunks):
        return [[0.0] * 1024 for _ in chunks]

    embedder.embed_batch.side_effect = _embed
    return embedder


# ---------------------------------------------------------------------------
# Chunker fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def chunker():
    from deepagents_mongodb_fs.chunker import Chunker
    return Chunker(token_limit=128, overlap=16)


# ---------------------------------------------------------------------------
# Sample text fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_text_bytes() -> bytes:
    return textwrap.dedent("""\
        Line 1: The quick brown fox jumps over the lazy dog.
        Line 2: MongoDB Atlas provides vector search capabilities.
        Line 3: LangChain DeepAgents can use filesystem backends.
        Line 4: S3 is a reliable object store.
        Line 5: Chunking preserves context across splits.
    """).encode("utf-8")


@pytest.fixture
def sample_pdf_bytes() -> bytes:
    """Create a minimal valid PDF in-memory using pypdf if available."""
    try:
        from reportlab.pdfgen import canvas as rl_canvas
        buf = io.BytesIO()
        c = rl_canvas.Canvas(buf)
        c.drawString(100, 750, "Hello PDF World")
        c.save()
        return buf.getvalue()
    except ImportError:
        # Return a minimal hand-crafted PDF (1 page, empty text)
        return (
            b"%PDF-1.4\n1 0 obj<</Type /Catalog /Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type /Pages /Kids [3 0 R] /Count 1>>endobj\n"
            b"3 0 obj<</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]>>endobj\n"
            b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
            b"0000000058 00000 n \n0000000115 00000 n \n"
            b"trailer<</Size 4 /Root 1 0 R>>\nstartxref\n190\n%%EOF"
        )
