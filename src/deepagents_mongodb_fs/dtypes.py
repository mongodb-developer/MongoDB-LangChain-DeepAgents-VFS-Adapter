"""Internal DTOs shared across all modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Chunk:
    """A single chunk produced by the Chunker."""

    source_path: str
    chunk_index: int
    content: str
    page_number: int = 0
    char_start: int = 0
    char_end: int = 0
    line_start: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FileRecord:
    """MongoDB document shape for a stored chunk."""

    source_path: str
    chunk_index: int
    content: str
    embedding: list[float]
    page_number: int
    char_start: int
    char_end: int
    line_start: int
    etag: str
    filename: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "chunk_index": self.chunk_index,
            "content": self.content,
            "embedding": self.embedding,
            "page_number": self.page_number,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "line_start": self.line_start,
            "etag": self.etag,
            "filename": self.filename,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class SearchHit:
    """A single result entry returned by SearchRouter operations."""

    source_path: str
    chunk_index: int
    content: str
    line_start: int
    score: float = 0.0


# ---------------------------------------------------------------------------
# DeepAgents-compatible result shapes
# These mirror the DTO contracts from deepagents.backends.filesystem
# ---------------------------------------------------------------------------

@dataclass
class LsEntry:
    name: str
    is_dir: bool
    size: int = 0


@dataclass
class LsResult:
    path: str
    entries: list[LsEntry] = field(default_factory=list)
    error: str | None = None


@dataclass
class ReadResult:
    path: str
    content: str = ""
    error: str | None = None


@dataclass
class WriteResult:
    path: str
    success: bool = True
    error: str | None = None


@dataclass
class EditResult:
    path: str
    success: bool = True
    error: str | None = None


@dataclass
class GrepMatch:
    path: str
    line: int
    content: str
    score: float = 0.0


@dataclass
class GrepResult:
    matches: list[GrepMatch] = field(default_factory=list)
    error: str | None = None


@dataclass
class GlobResult:
    paths: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class UploadResult:
    uploaded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class DownloadResult:
    downloaded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class SyncReport:
    seen: int = 0
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    error: str | None = None
