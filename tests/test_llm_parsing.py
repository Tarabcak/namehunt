"""Tests for namehunt.llm — spec §1.8 item 3 (parsing) and the prompt contract.

No network: the LLM boundary is the Provider protocol, faked here.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from namehunt.llm import (
    MissingKeyError,
    RoundContext,
    build_messages,
    generate_candidates,
    make_provider,
    parse_names,
)


class FakeProvider:
    name = "fake"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def make_ctx(**overrides) -> RoundContext:
    base = dict(
        brief="A calm agentic knowledge base for engineering teams.",
        style=None,
        batch=20,
        available_domains=[],
        taken_stems=[],
        likes=[],
        dislikes=[],
        notes=[],
        all_generated=frozenset(),
    )
    base.update(overrides)
    return RoundContext(**base)


def recording_console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, width=200), buf


# -------------------------------------------------------------------- parsing


def test_parse_clean_json_array():
    parsed = parse_names('["zephyr", "quill", "arc"]')
    assert parsed.valid == ["zephyr", "quill", "arc"]
    assert parsed.dropped == 0
    assert parsed.total == 3


@pytest.mark.parametrize(
    "raw",
    [
        '```json\n["alpha", "brio"]\n```',
        '```\n["alpha", "brio"]\n```',
        'Here are my suggestions:\n["alpha", "brio"]\nHope these help!',
    ],
)
def test_parse_tolerates_fences_and_prose(raw):
    assert parse_names(raw).valid == ["alpha", "brio"]


def test_parse_names_wrapper_object():
    assert parse_names('{"names": ["kestrel", "fable"]}').valid == ["kestrel", "fable"]


def test_parse_fenced_wrapper_object():
    assert parse_names('```json\n{"names": ["moss"]}\n```').valid == ["moss"]


def test_parse_drops_invalid_entries():
    raw = '["good", "Bad", "has space", "xy", "n4me", "waytoolongstemname", "fine"]'
    parsed = parse_names(raw)
    assert parsed.valid == ["good", "fine"]
    assert parsed.dropped == 5
    assert parsed.total == 7


def test_parse_drops_non_string_entries():
    parsed = parse_names('["okay", 42, null, ["nested"]]')
    assert parsed.valid == ["okay"]
    assert parsed.dropped == 3
    assert parsed.total == 4


def test_parse_dedupes_within_batch():
    parsed = parse_names('["echo", "echo", "arc"]')
    assert parsed.valid == ["echo", "arc"]
    assert parsed.dropped == 0
    assert parsed.total == 3


def test_parse_garbage_finds_nothing():
    parsed = parse_names("Names: Zephyr, Quill - great options!")
    assert parsed.valid == []
    assert parsed.total == 0


# ----------------------------------------------------------------- generation


def test_generate_happy_path():
    provider = FakeProvider(['["nova", "lumen"]'])
    names = generate_candidates(make_ctx(), provider)
    assert names == ["nova", "lumen"]
    assert len(provider.calls) == 1


def test_generate_dedupes_against_history():
    provider = FakeProvider(['["nova", "lumen"]'])
    names = generate_candidates(make_ctx(all_generated=frozenset({"nova"})), provider)
    assert names == ["lumen"]


def test_generate_truncates_to_batch():
    provider = FakeProvider(['["aaa", "bbb", "ccc", "ddd"]'])
    names = generate_candidates(make_ctx(batch=2), provider)
    assert names == ["aaa", "bbb"]


def test_generate_retries_once_on_unparseable_output():
    provider = FakeProvider(["I refuse to answer in JSON.", '["rescue"]'])
    names = generate_candidates(make_ctx(), provider)
    assert names == ["rescue"]
    assert len(provider.calls) == 2
    first_user = provider.calls[0][1]
    second_user = provider.calls[1][1]
    assert second_user != first_user  # retry carries an error-corrective message
    assert "json" in second_user.lower()


def test_generate_gives_up_after_single_retry():
    provider = FakeProvider(["nope", "still nope"])
    names = generate_candidates(make_ctx(), provider)
    assert names == []
    assert len(provider.calls) == 2  # bounded: exactly one retry


def test_generate_all_duplicates_does_not_retry():
    provider = FakeProvider(['["nova"]'])
    names = generate_candidates(make_ctx(all_generated=frozenset({"nova"})), provider)
    assert names == []
    assert len(provider.calls) == 1  # parse succeeded, so no retry


def test_generate_warns_when_many_invalid():
    console, buf = recording_console()
    provider = FakeProvider(['["good", "name", "BAD", "AL5O", "NO PE"]'])
    names = generate_candidates(make_ctx(), provider, console=console)
    assert names == ["good", "name"]
    assert "invalid" in buf.getvalue().lower()  # 3/5 invalid crosses the 30% bar


def test_generate_clean_batch_prints_no_warning():
    console, buf = recording_console()
    provider = FakeProvider(['["good", "name"]'])
    generate_candidates(make_ctx(), provider, console=console)
    assert "invalid" not in buf.getvalue().lower()


def test_generate_debug_prints_outgoing_prompt():
    console, buf = recording_console()
    provider = FakeProvider(['["nova"]'])
    generate_candidates(
        make_ctx(brief="quantum biscuits marketplace"), provider, debug=True, console=console
    )
    assert "quantum biscuits marketplace" in buf.getvalue()


# ------------------------------------------------------------ prompt building


def test_build_messages_full_context():
    ctx = RoundContext(
        brief="We build maps for caves.",
        style="short, mythic",
        batch=7,
        available_domains=["nova.com", "nova.ai"],
        taken_stems=["quill"],
        likes=["nova"],
        dislikes=["vertex"],
        notes=["more celestial please"],
        all_generated=frozenset({"nova", "quill", "vertex", "echo"}),
    )
    system, user = build_messages(ctx)
    assert "JSON" in system
    assert "lowercase" in system
    assert "We build maps for caves." in user
    assert "short, mythic" in user
    assert "7" in user

    i_avail = user.index("AVAILABLE DOMAINS")
    i_taken = user.index("TAKEN STEMS")
    i_never = user.index("DO NOT REPEAT")
    assert i_avail < i_taken < i_never
    avail_section = user[i_avail:i_taken]
    taken_section = user[i_taken:i_never]
    never_section = user[i_never:]

    assert "nova.com" in avail_section
    assert "nova.ai" in avail_section
    assert "quill" in taken_section
    # UNKNOWN-ish stems ("echo" was generated but has no verdict) appear in
    # neither the available nor the taken signal — only in the dedupe list.
    assert "echo" not in avail_section
    assert "echo" not in taken_section
    for stem in ("nova", "quill", "vertex", "echo"):
        assert stem in never_section
    assert "more celestial please" in user
    assert "vertex" in user


def test_build_messages_sections_present_when_empty():
    _system, user = build_messages(make_ctx())
    assert "AVAILABLE DOMAINS" in user
    assert "TAKEN STEMS" in user
    assert "DO NOT REPEAT" in user


# ------------------------------------------------------------------ providers


def test_missing_anthropic_key_raises_named_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(MissingKeyError, match="ANTHROPIC_API_KEY"):
        make_provider("claude", "claude-sonnet-4-6")


def test_missing_openai_key_raises_named_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(MissingKeyError, match="OPENAI_API_KEY"):
        make_provider("openai", "gpt-4.1-mini")


def test_unknown_provider_rejected(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with pytest.raises(ValueError, match="provider"):
        make_provider("grok", "some-model")


def test_claude_provider_constructs_offline(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    provider = make_provider("claude", "claude-sonnet-4-6")
    assert provider.name == "claude"


# ------------------------------------------------- note recency (added later)
# Added after the original red commit: in long sessions, old steering notes
# drowned out the newest round's directive. Recent notes must render as the
# active brief; older ones as demoted background.


def test_build_messages_recent_notes_render_as_current_direction():
    ctx = make_ctx(notes=["old brief one", "old brief two", "newest direction", "newest rules"])
    _system, user = build_messages(ctx)
    i_taken = user.index("TAKEN STEMS")
    i_earlier = user.index("EARLIER NOTES")
    i_never = user.index("DO NOT REPEAT")
    i_current = user.index("CURRENT DIRECTION")
    i_task = user.index("TASK:")
    # earlier notes sit demoted mid-prompt; current direction sits last, by TASK
    assert i_taken < i_earlier < i_never < i_current < i_task
    current_block = user[i_current:i_task]
    earlier_block = user[i_earlier:i_never]
    assert "newest direction" in current_block
    assert "newest rules" in current_block
    assert "old brief one" not in current_block
    assert "old brief two" not in current_block
    assert "old brief one" in earlier_block
    assert "old brief two" in earlier_block
    assert "newest direction" not in earlier_block


def test_build_messages_two_or_fewer_notes_are_all_current():
    _system, user = build_messages(make_ctx(notes=["only note"]))
    assert "CURRENT DIRECTION" in user
    assert "EARLIER NOTES" not in user
    assert "only note" in user[user.index("CURRENT DIRECTION") :]


def test_build_messages_no_notes_means_no_note_headers():
    _system, user = build_messages(make_ctx())
    assert "CURRENT DIRECTION" not in user
    assert "EARLIER NOTES" not in user
