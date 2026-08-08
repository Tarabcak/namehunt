"""Loop-level tests — spec §1.8 items 4 and 5, plus cli exit codes (§1.6).

run_round is exercised with a fake Provider and a fake checker: no network,
no interactivity.
"""

from __future__ import annotations

import copy

from namehunt.availability import Availability, DomainCheck
from namehunt.cli import main, run_round
from namehunt.llm import context_from_session, summarize_results
from namehunt.session import RoundRecord, Session


class FakeProvider:
    name = "fake"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


class SpyChecker:
    def __init__(self, statuses=None, default=Availability.AVAILABLE) -> None:
        self.calls: list[list[str]] = []
        self.statuses = dict(statuses or {})
        self.default = default

    def __call__(self, stems, tlds):
        self.calls.append(list(stems))
        results = {}
        for stem in stems:
            for tld in tlds:
                domain = f"{stem}.{tld}"
                results[domain] = DomainCheck(domain, self.statuses.get(domain, self.default))
        return results


def make_session(**overrides) -> Session:
    base = dict(
        name="testsession",
        brief_path="examples/brief.md",
        style=None,
        tlds=["com", "ai"],
        provider="claude",
        model="test-model",
    )
    base.update(overrides)
    return Session(**base)


# -------------------------------------------------- summarization (§1.8 #5)


def test_summarize_excludes_unknown_from_both_lists():
    results = {
        "nova.com": "available",
        "nova.ai": "taken",
        "quill.com": "taken",
        "quill.ai": "taken",
        "echo.com": "unknown",
        "echo.ai": "unknown",
        "brio.com": "taken",
        "brio.ai": "unknown",
    }
    available, taken = summarize_results(results)
    assert available == ["nova.com"]
    assert taken == ["brio", "quill"]
    assert "echo" not in taken
    assert all("echo" not in d for d in available)


def test_summarize_stem_with_any_available_is_a_win_not_a_taken():
    available, taken = summarize_results({"nova.com": "available", "nova.ai": "taken"})
    assert available == ["nova.com"]
    assert taken == []


def test_context_from_session_merges_rounds_and_feedback():
    sess = make_session(
        style="mythic",
        rounds=[
            RoundRecord(
                timestamp="t1",
                candidates=["arc"],
                results={"arc.com": "available", "arc.ai": "unknown"},
            ),
            RoundRecord(
                timestamp="t2",
                candidates=["bay"],
                results={"bay.com": "taken", "bay.ai": "taken"},
            ),
        ],
        likes=["arc"],
        dislikes=["bay"],
        notes=["shorter"],
        all_generated={"arc", "bay"},
    )
    ctx = context_from_session(sess, "the brief text", 9)
    assert ctx.brief == "the brief text"
    assert ctx.style == "mythic"
    assert ctx.batch == 9
    assert ctx.available_domains == ["arc.com"]
    assert ctx.taken_stems == ["bay"]
    assert ctx.likes == ["arc"]
    assert ctx.dislikes == ["bay"]
    assert ctx.notes == ["shorter"]
    assert ctx.all_generated == frozenset({"arc", "bay"})


# ------------------------------------------------------- run_round (§1.8 #4)


def test_run_round_dedupes_and_records():
    sess = make_session(all_generated={"nova"})
    before = copy.deepcopy(sess)
    provider = FakeProvider(['["nova", "zephyr"]'])
    checker = SpyChecker()
    new = run_round(sess, "Brief.", provider, checker, batch=5)
    assert checker.calls == [["zephyr"]]  # "nova" was already generated: never re-checked
    assert new.rounds[-1].candidates == ["zephyr"]
    assert new.rounds[-1].results == {"zephyr.com": "available", "zephyr.ai": "available"}
    assert new.rounds[-1].timestamp
    assert "zephyr" in new.all_generated
    assert "nova" in new.all_generated
    assert len(new.rounds) == len(sess.rounds) + 1
    assert sess == before  # input session is not mutated


def test_run_round_never_repeats_across_rounds():
    provider = FakeProvider(['["alpha", "beta"]', '["beta", "gamma"]'])
    checker = SpyChecker()
    s0 = make_session()
    s1 = run_round(s0, "B", provider, checker)
    s2 = run_round(s1, "B", provider, checker)
    assert checker.calls == [["alpha", "beta"], ["gamma"]]
    assert s2.rounds[0].candidates == ["alpha", "beta"]
    assert s2.rounds[1].candidates == ["gamma"]
    assert s2.all_generated == {"alpha", "beta", "gamma"}


def test_run_round_feeds_results_and_feedback_into_prompt():
    sess = make_session(
        tlds=["com"],
        rounds=[
            RoundRecord(
                timestamp="t1",
                candidates=["nova", "quill", "echo"],
                results={"nova.com": "available", "quill.com": "taken", "echo.com": "unknown"},
            )
        ],
        likes=["nova"],
        dislikes=["quill"],
        notes=["shorter please"],
        all_generated={"nova", "quill", "echo"},
    )
    provider = FakeProvider(['["fresh"]'])
    checker = SpyChecker()
    run_round(sess, "Brief.", provider, checker)
    _system, user = provider.calls[0]
    i_avail = user.index("AVAILABLE DOMAINS")
    i_taken = user.index("TAKEN STEMS")
    i_never = user.index("DO NOT REPEAT")
    avail_section = user[i_avail:i_taken]
    taken_section = user[i_taken:i_never]
    assert "nova.com" in avail_section
    assert "quill" in taken_section
    assert "echo" not in avail_section  # UNKNOWN feeds neither signal
    assert "echo" not in taken_section
    assert "echo" in user[i_never:]  # ... but is still never repeated
    assert "shorter please" in user


def test_run_round_with_no_new_candidates_checks_nothing():
    sess = make_session(all_generated={"nova"})
    provider = FakeProvider(['["nova"]'])
    checker = SpyChecker()
    new = run_round(sess, "B", provider, checker)
    assert not any(checker.calls)
    assert new.rounds[-1].candidates == []
    assert new.rounds[-1].results == {}


# ------------------------------------------------------ cli exit codes (§1.6)


def _forbid_input(monkeypatch) -> None:
    def _boom(*args, **kwargs):
        raise AssertionError("interactive input during test")

    monkeypatch.setattr("builtins.input", _boom)


def test_missing_key_exits_2_naming_env_var(tmp_path, monkeypatch, capsys):
    _forbid_input(monkeypatch)
    brief = tmp_path / "brief.md"
    brief.write_text("A tiny startup.")
    monkeypatch.setenv("NAMEHUNT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("NAMEHUNT_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rc = main(["run", "--brief", str(brief), "--session", "t1"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "ANTHROPIC_API_KEY" in captured.out + captured.err


def test_provider_env_var_selects_provider(tmp_path, monkeypatch, capsys):
    _forbid_input(monkeypatch)
    brief = tmp_path / "brief.md"
    brief.write_text("A tiny startup.")
    monkeypatch.setenv("NAMEHUNT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NAMEHUNT_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rc = main(["run", "--brief", str(brief), "--session", "t2"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "OPENAI_API_KEY" in captured.out + captured.err
