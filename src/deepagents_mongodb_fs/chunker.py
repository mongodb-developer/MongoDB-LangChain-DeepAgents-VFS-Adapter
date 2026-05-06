"""Document chunker: bytes → list[Chunk].

Supports .txt, .pdf (.docx detection via python-docx. Format is detected by
file extension first, then magic bytes as a fallback.

Chunking strategy:
  - Token-aware recursive character splitting (tiktoken cl100k_base).
  - 512 tokens per chunk, 64-token overlap — enough context for coherent
    embeddings without wasteful redundancy at scale.
  - Each chunk carries source_path, chunk_index, page_number, char_start,
    char_end, line_start, content so SearchRouter can return DeepAgents-
    compatible GrepMatch.line directly from stored metadata.
"""
from __future__ import annotations

import io
import logging
import re
from pathlib import PurePosixPath
from typing import Sequence

import tiktoken

from deepagents_mongodb_fs.dtypes import Chunk
from deepagents_mongodb_fs.errors import AdapterError, ErrorCode

logger = logging.getLogger(__name__)

_ENCODING = "cl100k_base"
_DEFAULT_TOKEN_LIMIT = 512
_DEFAULT_OVERLAP = 64

_SUPPORTED_EXTENSIONS = {".txt", ".pdf", ".docx", ".md", ".rst", ".csv"}


class Chunker:
    """Converts raw bytes into a list of Chunk objects.

    Args:
        token_limit: Maximum tokens per chunk (default 512).
        overlap: Token overlap between consecutive chunks (default 64).
    """

    def __init__(
        self,
        token_limit: int = _DEFAULT_TOKEN_LIMIT,
        overlap: int = _DEFAULT_OVERLAP,
    ) -> None:
        self._token_limit = token_limit
        self._overlap = overlap
        self._enc = tiktoken.get_encoding(_ENCODING)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chunk(self, path: str, data: bytes) -> list[Chunk]:
        """Extract text from *data* and split into overlapping chunks.

        Args:
            path: Source object key / path (used to derive filename + extension).
            data: Raw bytes of the object.

        Returns:
            Ordered list of Chunk objects.

        Raises:
            AdapterError(E9003): Unsupported file format.
            AdapterError(E9002): Extraction failure.
        """
        ext = PurePosixPath(path).suffix.lower()
        try:
            pages = self._extract(data, ext)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(ErrorCode.E9002_CHUNKER_FAILED, str(exc)) from exc

        chunks: list[Chunk] = []
        chunk_index = 0
        for page_number, page_text in pages:
            for chunk_text, char_start, char_end, line_start in self._split(page_text):
                chunks.append(
                    Chunk(
                        source_path=path,
                        chunk_index=chunk_index,
                        content=chunk_text,
                        page_number=page_number,
                        char_start=char_start,
                        char_end=char_end,
                        line_start=line_start,
                    )
                )
                chunk_index += 1

        return chunks

    # ------------------------------------------------------------------
    # Format extractors
    # ------------------------------------------------------------------

    def _extract(self, data: bytes, ext: str) -> list[tuple[int, str]]:
        """Return list of (page_number, text) pairs."""
        if ext in (".txt", ".md", ".rst", ".csv"):
            return self._extract_text(data)
        if ext == ".pdf":
            return self._extract_pdf(data)
        if ext == ".docx":
            return self._extract_docx(data)
        # magic-byte fallback: PDF starts with %PDF
        if data[:4] == b"%PDF":
            return self._extract_pdf(data)
        # DOCX is a ZIP archive
        if data[:2] == b"PK":
            return self._extract_docx(data)
        # Try plain text as last resort; warn
        logger.warning("Unrecognized extension '%s', treating as plain text.", ext)
        return self._extract_text(data)

    def _extract_text(self, data: bytes) -> list[tuple[int, str]]:
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception as exc:
            raise AdapterError(ErrorCode.E9002_CHUNKER_FAILED, f"UTF-8 decode failed: {exc}") from exc
        return [(0, text)]

    def _extract_pdf(self, data: bytes) -> list[tuple[int, str]]:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise AdapterError(ErrorCode.E9003_FORMAT_UNSUPPORTED, "pypdf is not installed") from exc
        reader = PdfReader(io.BytesIO(data))
        pages: list[tuple[int, str]] = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            pages.append((i, text))
        return pages

    def _extract_docx(self, data: bytes) -> list[tuple[int, str]]:
        try:
            import docx
        except ImportError as exc:
            raise AdapterError(ErrorCode.E9003_FORMAT_UNSUPPORTED, "python-docx is not installed") from exc
        doc = docx.Document(io.BytesIO(data))
        full_text = "\n".join(p.text for p in doc.paragraphs)
        return [(0, full_text)]

    # ------------------------------------------------------------------
    # Token-aware recursive splitter
    # ------------------------------------------------------------------

    def _split(self, text: str) -> list[tuple[str, int, int, int]]:
        """Split *text* into overlapping token-bounded windows.

        Returns:
            List of (chunk_text, char_start, char_end, line_start).
        """
        tokens = self._enc.encode(text)
        results: list[tuple[str, int, int, int]] = []
        start = 0
        while start < len(tokens):
            end = min(start + self._token_limit, len(tokens))
            chunk_tokens = tokens[start:end]
            chunk_text = self._enc.decode(chunk_tokens)
            # Locate char positions in original text for metadata
            char_start = self._token_offset(text, tokens[:start])
            char_end = char_start + len(chunk_text)
            line_start = text[:char_start].count("\n")
            results.append((chunk_text, char_start, char_end, line_start))
            if end == len(tokens):
                break
            start += self._token_limit - self._overlap
        return results

    def _token_offset(self, text: str, prefix_tokens: Sequence[int]) -> int:
        """Return the character offset in *text* after decoding *prefix_tokens*."""
        if not prefix_tokens:
            return 0
        return len(self._enc.decode(list(prefix_tokens)))
