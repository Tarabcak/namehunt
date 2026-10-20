"""Tests for namehunt.session — spec §1.8 item 6.

Save → resume must reproduce the exact loop context. Session names are
untrusted input (they become file names), so hostile names must be rejected.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from namehunt.llm import context_from_session
from namehunt.session import RoundRecord, Session, load_session, save_session, session_path


def sample_session() -> Session:
    return Session(
        name="alpha",
        brief_path="examples/brief.md",
        style="short, mythic",
        tlds=["com", "ai"],
        provider="claude",
        model="claude-sonnet-4-6",
        rounds=[
            RoundRecord(
                timestamp="2026-08-08T12:00:00+00:00",
                candidates=["nova", "quill"],
                results={
                    "nova.com": "available",
                    "nova.ai": "unknown",
                    "quill.com": "taken",
                    "quill.ai": "taken",
                },
            )
        ],
        likes=["nova"],
        dislikes=["quill"],
        notes=["more sky"],
        all_generated={"nova", "quill"},
    )


def test_save_writes_into_sessions_dir(tmp_path):
    save_session(sample_session(), base=tmp_path)
    assert (tmp_path / "sessions" / "alpha.json").is_file()


def test_roundtrip_preserves_everything(tmp_path):
    sess = sample_session()
    save_session(sess, base=tmp_path)
    assert load_session("alpha", base=tmp_path) == sess


def test_resume_reproduces_identical_loop_context(tmp_path):
    sess = sample_session()
    save_session(sess, base=tmp_path)
    loaded = load_session("alpha", base=tmp_path)
    assert context_from_session(loaded, "brief text", 20) == context_from_session(
        sess, "brief text", 20
    )


def test_session_path_layout(tmp_path):
    assert session_path("ab-c_9", base=tmp_path) == tmp_path / "sessions" / "ab-c_9.json"


@pytest.mark.parametrize("bad", ["../evil", "a/b", "UPPER", "sp ace", "", "dot.dot"])
def test_hostile_session_names_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        session_path(bad, base=tmp_path)


def test_save_rejects_hostile_name(tmp_path):
    sess = replace(sample_session(), name="../evil")
    with pytest.raises(ValueError):
        save_session(sess, base=tmp_path)


def test_load_missing_session_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_session("ghost", base=tmp_path)
