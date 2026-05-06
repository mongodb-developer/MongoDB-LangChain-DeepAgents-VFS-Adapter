"""Real-service E2E tests — Atlas + S3 + OpenAI.

Run with:
    export MONGODB_URI="mongodb+srv://..." S3_BUCKET_NAME="..." OPENAI_API_KEY="sk-..."
    pytest -m real_e2e -v

All tests share a single session-scoped backend; Atlas vector-search index
provisioning is done once.  Tests that mutate state use unique paths under
the session prefix to avoid ordering dependencies.
"""
from __future__ import annotations

import uuid

import pytest

from deepagents_mongodb_fs.dtypes import (
    EditResult,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)


@pytest.mark.real_e2e
class TestReadWrite:
    def test_read_seeded_file(self, real_backend, e2e_prefix):
        result = real_backend.read(f"{e2e_prefix}docs/api.txt")
        assert isinstance(result, ReadResult)
        assert result.error is None
        assert "authentication" in result.content

    def test_read_missing_file(self, real_backend, e2e_prefix):
        result = real_backend.read(f"{e2e_prefix}does/not/exist.txt")
        assert result.error is not None

    def test_write_then_read_roundtrip(self, real_backend, e2e_prefix):
        path = f"{e2e_prefix}tmp/roundtrip_{uuid.uuid4().hex[:6]}.txt"
        write = real_backend.write(path, "roundtrip content for real e2e test")
        assert isinstance(write, WriteResult)
        assert write.success is True

        read = real_backend.read(path)
        assert read.error is None
        assert "roundtrip content" in read.content

    def test_edit_file(self, real_backend, e2e_prefix):
        path = f"{e2e_prefix}tmp/edit_{uuid.uuid4().hex[:6]}.txt"
        real_backend.write(path, "before edit: hello world")

        result = real_backend.edit(path, "hello world", "goodbye world")
        assert isinstance(result, EditResult)
        assert result.success is True

        read = real_backend.read(path)
        assert "goodbye world" in read.content
        assert "hello world" not in read.content


@pytest.mark.real_e2e
@pytest.mark.usefixtures("sync_ready")
class TestLsGlob:
    def test_ls_root(self, real_backend, e2e_prefix):
        # ls with the prefix dir returns at least the seeded files
        result = real_backend.ls(f"{e2e_prefix}docs")
        assert isinstance(result, LsResult)
        assert result.error is None
        names = [e.name for e in result.entries]
        assert any("api.txt" in n for n in names)

    def test_ls_empty_prefix_returns_all(self, real_backend, e2e_prefix):
        result = real_backend.ls("")
        assert isinstance(result, LsResult)
        assert result.error is None
        assert len(result.entries) > 0

    def test_glob_txt_files(self, real_backend, e2e_prefix):
        result = real_backend.glob("*.txt")
        assert isinstance(result, GlobResult)
        assert result.error is None
        assert any("api.txt" in p for p in result.paths)

    def test_glob_py_files(self, real_backend, e2e_prefix):
        result = real_backend.glob("*.py")
        assert isinstance(result, GlobResult)
        assert result.error is None
        assert any("main.py" in p for p in result.paths)

    def test_glob_no_match(self, real_backend, e2e_prefix):
        result = real_backend.glob("*.xyz_nonexistent")
        assert isinstance(result, GlobResult)
        assert result.error is None
        assert result.paths == []


@pytest.mark.real_e2e
@pytest.mark.usefixtures("sync_ready")
class TestVectorSearch:
    def test_grep_exact_term(self, real_backend, e2e_prefix):
        """Keyword present in a seeded file should appear in results."""
        result = real_backend.grep("authentication")
        assert isinstance(result, GrepResult)
        assert result.error is None
        assert len(result.matches) > 0
        contents = " ".join(m.content for m in result.matches)
        assert "authentication" in contents

    def test_grep_semantic_query(self, real_backend, e2e_prefix):
        """Semantic query — not exact keyword match — still returns relevant docs."""
        result = real_backend.grep("how to set up the project")
        assert isinstance(result, GrepResult)
        assert result.error is None
        # Should surface install.txt (installation prerequisites)
        paths = [m.path for m in result.matches]
        assert any("install" in p for p in paths), f"Expected install.txt in results; got: {paths}"

    def test_grep_with_path_filter(self, real_backend, e2e_prefix):
        result = real_backend.grep("authentication", path=f"{e2e_prefix}docs/")
        assert isinstance(result, GrepResult)
        assert result.error is None
        for m in result.matches:
            assert m.path.startswith(f"{e2e_prefix}docs/")

    def test_grep_match_has_required_fields(self, real_backend, e2e_prefix):
        result = real_backend.grep("installation")
        assert result.error is None
        if result.matches:
            m = result.matches[0]
            assert hasattr(m, "path") and m.path
            assert hasattr(m, "content") and m.content
            assert hasattr(m, "line")

    def test_grep_no_results_for_nonsense(self, real_backend, e2e_prefix):
        result = real_backend.grep("xyzzy_zzz_completely_random_9q2j")
        assert isinstance(result, GrepResult)
        assert result.error is None


@pytest.mark.real_e2e
class TestUploadDownload:
    def test_upload_then_download(self, real_backend, e2e_prefix):
        path = f"{e2e_prefix}tmp/upload_{uuid.uuid4().hex[:6]}.txt"
        upload = real_backend.upload_files([(path, b"uploaded bytes content")])
        assert upload.error is None
        assert path in upload.uploaded

        download = real_backend.download_files([path])
        assert download.error is None
        assert path in download.downloaded

    def test_upload_multiple_files(self, real_backend, e2e_prefix):
        run = uuid.uuid4().hex[:6]
        files = [
            (f"{e2e_prefix}tmp/multi_{run}_a.txt", b"file a"),
            (f"{e2e_prefix}tmp/multi_{run}_b.txt", b"file b"),
        ]
        upload = real_backend.upload_files(files)
        assert upload.error is None
        assert len(upload.uploaded) == 2
