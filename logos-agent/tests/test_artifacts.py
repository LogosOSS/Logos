"""Untrusted artifacts cannot redirect or exhaust runner reads."""

import os
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from app import analysis_ingest, artifacts, sessions


@pytest.mark.parametrize("relative", ["reply.md", "replies/123.md"])
def test_reads_regular_files_at_the_configured_limit(tmp_path, monkeypatch, relative):
    monkeypatch.setattr(artifacts, "settings", replace(artifacts.settings, artifact_max_bytes=4))
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"test")
    assert artifacts.read_artifact_text(tmp_path, relative) == "test"
    target.write_bytes(b"tests")
    with pytest.raises(OSError, match="exceeds"):
        artifacts.read_artifact(tmp_path, relative)


@pytest.mark.parametrize("kind", ["symlink", "parent_symlink", "root_symlink", "fifo", "directory"])
def test_unsafe_artifacts_are_unreadable(tmp_path, caplog, kind):
    root = tmp_path / "session"
    root.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "reply.md").write_text("private session data")
    relative = "reply.md"
    if kind == "symlink":
        (root / relative).symlink_to(victim / relative)
    elif kind == "parent_symlink":
        (root / "replies").symlink_to(victim, target_is_directory=True)
        relative = "replies/reply.md"
    elif kind == "root_symlink":
        root.rmdir()
        root.symlink_to(victim, target_is_directory=True)
    elif kind == "fifo":
        os.mkfifo(root / relative)
    else:
        (root / relative).mkdir()
    with pytest.raises(OSError):
        artifacts.read_artifact(root, relative)
    assert "unreadable artifact" in caplog.text
    assert "private session data" not in caplog.text


@pytest.mark.parametrize("relative", ["../victim", "/proc/self/environ"])
def test_paths_must_be_relative_to_the_session(tmp_path, relative):
    with pytest.raises(OSError, match="within its session"):
        artifacts.read_artifact(tmp_path, relative)


def test_missing_files_keep_their_exception(tmp_path):
    with pytest.raises(FileNotFoundError):
        artifacts.read_artifact(tmp_path, "absent")


def test_invalid_utf8_is_unreadable(tmp_path, caplog):
    (tmp_path / "reply.md").write_bytes(b"\xff")
    with pytest.raises(OSError, match="UTF-8"):
        artifacts.read_artifact_text(tmp_path, "reply.md")
    assert "UTF-8" in caplog.text


def test_a_file_growing_during_read_never_reads_beyond_the_limit(tmp_path, monkeypatch):
    target = tmp_path / "result.json"
    target.write_bytes(b"1234")
    original_read = os.read
    sizes = []

    def growing_read(fd, size):
        sizes.append(size)
        with target.open("ab") as handle:
            handle.write(b"56789")
        return original_read(fd, size)

    monkeypatch.setattr(artifacts.os, "read", growing_read)
    with pytest.raises(OSError, match="exceeds"):
        artifacts.read_artifact(tmp_path, "result.json", limit=4)
    assert sizes == [4]


def test_parent_swap_cannot_redirect_an_open_directory(tmp_path, monkeypatch):
    root = tmp_path / "session"
    replies = root / "replies"
    replies.mkdir(parents=True)
    (replies / "123.md").write_text("answer")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "123.md").write_text("private data")
    original_open = os.open

    def swapping_open(path, flags, **kwargs):
        fd = original_open(path, flags, **kwargs)
        if path == "replies":
            replies.rename(root / "moved")
            replies.symlink_to(victim)
        return fd

    monkeypatch.setattr(artifacts.os, "open", swapping_open)
    assert artifacts.read_artifact_text(root, "replies/123.md") == "answer"


@pytest.mark.parametrize("kind", ["symlink", "oversize", "fifo", "invalid_utf8"])
async def test_runner_text_readers_reject_untrusted_files(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(
        sessions, "settings", replace(sessions.settings, artifact_root=str(tmp_path), artifact_max_bytes=32)
    )
    attempt = AsyncMock()
    monkeypatch.setattr(sessions.db, "record_reply_attempt", attempt)
    directory = tmp_path / "1"
    (directory / "replies").mkdir(parents=True)
    secret = tmp_path / "analysis.json"
    secret.write_text('{"private": true}')
    for name in ["reply.md", "review-comments.json", "result.json", "replies/123.md"]:
        target = directory / name
        if kind == "symlink":
            target.symlink_to(secret)
        elif kind == "oversize":
            target.write_text("x" * 33)
        elif kind == "fifo":
            os.mkfifo(target)
        else:
            target.write_bytes(b"\xff")
    manager = sessions.SessionManager()
    assert await manager._read_answer(1) is None
    assert manager._read_review_comments(1) is None
    assert manager._read_result(1) == {}
    assert await manager._combined_review_answers(1) is None
    assert attempt.await_count == 2


async def test_runner_missing_files_keep_existing_results(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "settings", replace(sessions.settings, artifact_root=str(tmp_path)))
    manager = sessions.SessionManager()
    assert await manager._read_answer(1) == ""
    assert manager._read_review_comments(1) == ([], "")
    assert manager._read_result(1) == {}
    assert await manager._combined_review_answers(1) == ""


def test_analysis_uses_the_configured_artifact_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(analysis_ingest, "settings", replace(analysis_ingest.settings, artifact_max_bytes=2))
    target = tmp_path / "analysis.json"
    target.write_bytes(b"{}")
    assert analysis_ingest.read_analysis_artifact(target) == b"{}"
    target.write_bytes(b"{} ")
    with pytest.raises(OSError, match="exceeds"):
        analysis_ingest.read_analysis_artifact(target)
