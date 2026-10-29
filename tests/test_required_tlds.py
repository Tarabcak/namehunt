"""Required TLDs — spec §1.4: a stem only wins when every required TLD is AVAILABLE.

Without required TLDs the original rule stands (any AVAILABLE domain is a win), so a
mostly-free TLD such as .design can no longer hide that the .com the user needs is gone.
"""

from __future__ import annotations

import json

import pytest

from namehunt import cli
from namehunt.availability import Availability, DomainCheck
from namehunt.export import render_export
from namehunt.llm import build_messages, context_from_session, summarize_results
from namehunt.session import RoundRecord, Session, load_session, save_session


def _session(**overrides) -> Session:
    base = dict(
        name="t",
        brief_path="brief.md",
        style=None,
        tlds=["com", "design"],
        provider="claude",
        model="test-model",
    )
    base.update(overrides)
    return Session(**base)


MIXED = {
    "nova.com": "taken",
    "nova.design": "available",
    "quill.com": "available",
    "quill.design": "available",
}

# ---------------------------------------------------------------- summarization


def test_without_required_tlds_any_available_domain_is_still_a_win():
    available, taken = summarize_results({"nova.com": "taken", "nova.design": "available"})
    assert available == ["nova.design"]
    assert taken == []


def test_required_tld_taken_makes_the_stem_taken_despite_other_free_tlds():
    available, taken = summarize_results(MIXED, required_tlds=["com"])
    assert available == ["quill.com", "quill.design"]
    assert taken == ["nova"]


def test_unknown_required_tld_feeds_neither_list():
    results = {"echo.com": "unknown", "echo.design": "available"}
    available, taken = summarize_results(results, required_tlds=["com"])
    assert available == []
    assert taken == []


def test_every_required_tld_must_be_available():
    results = {"nova.com": "available", "nova.dev": "taken", "nova.design": "available"}
    available, taken = summarize_results(results, required_tlds=["com", "dev"])
    assert available == []
    assert taken == ["nova"]


# ---------------------------------------------------------------- context and prompt


def test_context_and_prompt_carry_required_tlds():
    sess = _session(required_tlds=["com"], rounds=[RoundRecord("t0", ["nova", "quill"], MIXED)])
    ctx = context_from_session(sess, "A tiny startup.", 10)
    assert ctx.required_tlds == ("com",)
    assert ctx.available_domains == ["quill.com", "quill.design"]
    assert ctx.taken_stems == ["nova"]
    _, user = build_messages(ctx)
    assert "REQUIRED" in user
    assert ".com" in user


def test_prompt_has_no_required_line_by_default():
    _, user = build_messages(context_from_session(_session(), "A tiny startup.", 10))
    assert "REQUIRED" not in user


# ---------------------------------------------------------------- session file


def test_required_tlds_survive_save_and_load(tmp_path):
    save_session(_session(required_tlds=["com"]), base=tmp_path)
    assert load_session("t", base=tmp_path).required_tlds == ["com"]


def test_old_session_files_load_without_required_tlds(tmp_path):
    save_session(_session(), base=tmp_path)
    path = tmp_path / "sessions" / "t.json"
    data = json.loads(path.read_text())
    data.pop("required_tlds", None)
    path.write_text(json.dumps(data))
    assert load_session("t", base=tmp_path).required_tlds == []


# ---------------------------------------------------------------- export


def test_export_lists_names_with_every_required_tld_first():
    results = {
        "nova.com": "taken",
        "nova.design": "available",
        "quill.com": "available",
        "quill.design": "taken",
    }
    sess = _session(required_tlds=["com"], rounds=[RoundRecord("t0", ["nova", "quill"], results)])
    md = render_export(sess, "md")
    assert md.index("| quill |") < md.index("| nova |")


# ---------------------------------------------------------------- cli


def _all_available(stems, tlds):
    return {
        f"{s}.{t}": DomainCheck(f"{s}.{t}", Availability.AVAILABLE) for s in stems for t in tlds
    }


class FakeProvider:
    name = "fake"

    def complete(self, *, system: str, user: str) -> str:
        return '["nova"]'


@pytest.fixture
def brief(tmp_path, monkeypatch):
    monkeypatch.setenv("NAMEHUNT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "make_provider", lambda name, model: FakeProvider())
    monkeypatch.setattr(cli, "_build_checker", lambda *args, **kwargs: _all_available)
    path = tmp_path / "brief.md"
    path.write_text("A tiny startup.")
    return str(path)


def test_run_stores_required_tlds_on_a_new_session(brief):
    argv = ["run", "--brief", brief, "--session", "n", "--tlds", "com,design"]
    rc = cli.main([*argv, "--require-tlds", "com", "--rounds", "1", "--provider", "claudecode"])
    assert rc == 0
    assert load_session("n").required_tlds == ["com"]


def test_required_tlds_must_be_among_the_session_tlds(brief, capsys):
    argv = ["run", "--brief", brief, "--session", "n", "--tlds", "com,design"]
    rc = cli.main([*argv, "--require-tlds", "ai", "--rounds", "1", "--provider", "claudecode"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "ai" in captured.out + captured.err


def test_require_tlds_on_an_existing_session_replaces_them(brief):
    save_session(_session(name="s", brief_path=brief, provider="claudecode", model="sonnet"))
    assert cli.main(["run", "--session", "s", "--require-tlds", "com", "--rounds", "1"]) == 0
    assert load_session("s").required_tlds == ["com"]
