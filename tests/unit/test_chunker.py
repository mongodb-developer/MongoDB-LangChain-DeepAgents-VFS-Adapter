"""Unit tests for the Chunker module."""
from __future__ import annotations

import pytest

from deepagents_mongodb_fs.chunker import Chunker
from deepagents_mongodb_fs.dtypes import Chunk
from deepagents_mongodb_fs.errors import AdapterError, ErrorCode


@pytest.mark.unit
class TestChunkerPlainText:
    def test_basic_chunk_returns_list(self, chunker, sample_text_bytes):
        chunks = chunker.chunk("docs/sample.txt", sample_text_bytes)
        assert isinstance(chunks, list)
        assert len(chunks) > 0

    def test_chunk_type(self, chunker, sample_text_bytes):
        chunks = chunker.chunk("docs/sample.txt", sample_text_bytes)
        for c in chunks:
            assert isinstance(c, Chunk)

    def test_source_path_preserved(self, chunker, sample_text_bytes):
        path = "docs/my_file.txt"
        chunks = chunker.chunk(path, sample_text_bytes)
        for c in chunks:
            assert c.source_path == path

    def test_chunk_indices_sequential(self, chunker, sample_text_bytes):
        chunks = chunker.chunk("sample.txt", sample_text_bytes)
        for i, c in enumerate(chunks):
            assert c.chunk_index == i

    def test_content_non_empty(self, chunker, sample_text_bytes):
        chunks = chunker.chunk("sample.txt", sample_text_bytes)
        for c in chunks:
            assert c.content.strip() != ""

    def test_line_start_non_negative(self, chunker, sample_text_bytes):
        chunks = chunker.chunk("sample.txt", sample_text_bytes)
        for c in chunks:
            assert c.line_start >= 0

    def test_char_offsets_ordered(self, chunker, sample_text_bytes):
        chunks = chunker.chunk("sample.txt", sample_text_bytes)
        for c in chunks:
            assert c.char_start <= c.char_end

    def test_token_limit_respected(self):
        chunker = Chunker(token_limit=10, overlap=2)
        # 100 tokens of text
        text = ("hello world " * 50).encode("utf-8")
        chunks = chunker.chunk("big.txt", text)
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        for c in chunks:
            token_count = len(enc.encode(c.content))
            assert token_count <= 12  # small margin for boundary rounding

    def test_empty_file_returns_no_chunks(self, chunker):
        chunks = chunker.chunk("empty.txt", b"")
        assert chunks == []

    def test_md_extension_treated_as_text(self, chunker):
        data = b"# Heading\n\nSome markdown content."
        chunks = chunker.chunk("README.md", data)
        assert len(chunks) > 0

    def test_unknown_extension_falls_back_to_text(self, chunker):
        data = b"Some plain text in a weird extension."
        chunks = chunker.chunk("file.xyz", data)
        assert len(chunks) > 0


@pytest.mark.unit
class TestChunkerPDF:
    def test_pdf_extraction(self, chunker, sample_pdf_bytes):
        chunks = chunker.chunk("report.pdf", sample_pdf_bytes)
        # A minimal PDF may have 0 or 1 chunks depending on content
        assert isinstance(chunks, list)

    def test_pdf_page_number_set(self, chunker, sample_pdf_bytes):
        chunks = chunker.chunk("report.pdf", sample_pdf_bytes)
        for c in chunks:
            assert c.page_number >= 0


@pytest.mark.unit
class TestChunkerMagicBytes:
    def test_pdf_magic_bytes_detected(self, chunker, sample_pdf_bytes):
        # Rename to unknown extension — should still parse as PDF
        chunks = chunker.chunk("report.dat", sample_pdf_bytes)
        assert isinstance(chunks, list)


@pytest.mark.unit
class TestChunkerErrors:
    def test_unsupported_format_still_falls_back(self, chunker):
        # We expect graceful fallback, not an exception, for unknown text-like content
        data = b"Some content"
        chunks = chunker.chunk("file.abc123", data)
        assert isinstance(chunks, list)
