"""`run` on an existing session keeps the session's own settings — spec §1.4.

Explicit --provider/--model/--style replace the stored values; NAMEHUNT_PROVIDER only
picks the provider of a new session; differing --tlds are an error. The provider and the
checker are faked at the cli boundary: no network, no subprocesses.
"""

from __future__ import annotations

import pytest

from namehunt import cli
from namehunt.availability import Availability, DomainCheck
from namehunt.llm import DEFAULT_MODELS
from namehunt.session import Session, load_session, save_session


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, *, system: str, user: str) -> str:
        self.prompts.append(user)
        return '["nova", "quill"]'


def _all_available(stems, tlds):
    return {
        f"{s}.{t}": DomainCheck(f"{s}.{t}", Availability.AVAILABLE) for s in stems for t in tlds
    }


def _no_input(*args, **kwargs):
    raise AssertionError("interactive input during test")


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Isolated home, a saved claudecode session 's', and a recorder for make_provider."""
    monkeypatch.setenv("NAMEHUNT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("NAMEHUNT_PROVIDER", raising=False)
    monkeypatch.setattr("builtins.input", _no_input)
    brief = tmp_path / "brief.md"
    brief.write_text("A tiny startup.")
    save_session(
        Session(
            name="s",
            brief_path=str(brief),
            style=None,
            tlds=["com", "dev"],
            provider="claudecode",
            model="sonnet",
        )
    )
    made: list[tuple[str, str]] = []
    provider = FakeProvider()

    def fake_make_provider(name: str, model: str) -> FakeProvider:
        made.append((name, model))
        return provider

    monkeypatch.setattr(cli, "make_provider", fake_make_provider)
    monkeypatch.setattr(cli, "_build_checker", lambda *args, **kwargs: _all_available)
    return made, provider


def test_run_on_existing_session_uses_its_saved_provider_and_model(harness):
    made, _ = harness
    assert cli.main(["run", "--session", "s", "--rounds", "1"]) == 0
    assert made == [("claudecode", "sonnet")]


def test_env_provider_does_not_override_an_existing_session(harness, monkeypatch):
    made, _ = harness
    monkeypatch.setenv("NAMEHUNT_PROVIDER", "openai")
    assert cli.main(["run", "--session", "s", "--rounds", "1"]) == 0
    assert made == [("claudecode", "sonnet")]


def test_explicit_provider_switches_the_session_for_later_resumes(harness):
    made, _ = harness
    assert cli.main(["run", "--session", "s", "--rounds", "1", "--provider", "codexcli"]) == 0
    assert made == [("codexcli", DEFAULT_MODELS["codexcli"])]
    saved = load_session("s")
    assert (saved.provider, saved.model) == ("codexcli", DEFAULT_MODELS["codexcli"])


def test_explicit_model_replaces_the_saved_model(harness):
    made, _ = harness
    assert cli.main(["run", "--session", "s", "--rounds", "1", "--model", "opus"]) == 0
    assert made == [("claudecode", "opus")]
    assert load_session("s").model == "opus"


def test_explicit_style_replaces_the_saved_style(harness):
    _, provider = harness
    assert cli.main(["run", "--session", "s", "--rounds", "1", "--style", "short, mythic"]) == 0
    assert load_session("s").style == "short, mythic"
    assert "STYLE DIRECTION: short, mythic" in provider.prompts[0]


def test_differing_tlds_on_an_existing_session_exit_2(harness, capsys):
    made, _ = harness
    assert cli.main(["run", "--session", "s", "--rounds", "1", "--tlds", "com,ai"]) == 2
    captured = capsys.readouterr()
    assert "com,dev" in captured.out + captured.err
    assert made == []


def test_same_tlds_on_an_existing_session_are_accepted(harness):
    made, _ = harness
    assert cli.main(["run", "--session", "s", "--rounds", "1", "--tlds", "com,dev"]) == 0
    assert made == [("claudecode", "sonnet")]
