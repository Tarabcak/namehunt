"""Text the user reads — spec §1.4 and §1.6: the literal feedback prompt, plain-command
hints, and the end of a failing CLI provider's error output."""

from __future__ import annotations

import io
import subprocess

import pytest
from rich.console import Console

from namehunt import cli, llm
from namehunt.availability import Availability, DomainCheck
from namehunt.llm import make_provider
from namehunt.session import RoundRecord, Session


def _console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, width=200, color_system=None), buf


def _session_with_round() -> Session:
    return Session(
        name="s",
        brief_path="brief.md",
        style=None,
        tlds=["com"],
        provider="claude",
        model="test-model",
        rounds=[RoundRecord("t0", ["nova"], {"nova.com": "available"})],
    )


# ---------------------------------------------------------------- feedback prompt


def test_feedback_prompt_shows_its_command_letters(tmp_path, monkeypatch):
    monkeypatch.setenv("NAMEHUNT_HOME", str(tmp_path))
    monkeypatch.setattr("builtins.input", lambda *args: "q")
    console, buf = _console()
    cli._gather_feedback(console, _session_with_round())
    assert "[a]nother round" in buf.getvalue()
    assert "[q]uit & export" in buf.getvalue()


def test_unknown_command_help_shows_the_letters(tmp_path, monkeypatch):
    monkeypatch.setenv("NAMEHUNT_HOME", str(tmp_path))
    answers = iter(["zzz", "q"])
    monkeypatch.setattr("builtins.input", lambda *args: next(answers))
    console, buf = _console()
    cli._gather_feedback(console, _session_with_round())
    assert "commands: [a]nother round" in buf.getvalue()


# ---------------------------------------------------------------- hints


class InterruptingProvider:
    name = "fake"

    def complete(self, *, system: str, user: str) -> str:
        raise KeyboardInterrupt


class OneNameProvider:
    name = "fake"

    def complete(self, *, system: str, user: str) -> str:
        return '["nova"]'


def _all_available(stems, tlds):
    return {
        f"{s}.{t}": DomainCheck(f"{s}.{t}", Availability.AVAILABLE) for s in stems for t in tlds
    }


@pytest.fixture
def brief(tmp_path, monkeypatch):
    monkeypatch.setenv("NAMEHUNT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "_build_checker", lambda *args, **kwargs: _all_available)
    path = tmp_path / "brief.md"
    path.write_text("A tiny startup.")
    return str(path)


def test_interrupt_hint_names_the_plain_command(brief, monkeypatch, capsys):
    monkeypatch.setattr(cli, "make_provider", lambda name, model: InterruptingProvider())
    rc = cli.main(["run", "--brief", brief, "--session", "s", "--provider", "claudecode"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "namehunt resume --session s" in out
    assert "uv run" not in out


def test_finish_hint_names_the_plain_export_command(brief, monkeypatch, capsys):
    monkeypatch.setattr(cli, "make_provider", lambda name, model: OneNameProvider())
    argv = ["run", "--brief", brief, "--session", "s", "--provider", "claudecode"]
    rc = cli.main([*argv, "--rounds", "1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "namehunt export --session s --format md" in out
    assert "uv run" not in out


# ---------------------------------------------------------------- provider error detail


def test_claude_cli_failure_reports_the_end_of_its_error_output(tmp_path, monkeypatch):
    monkeypatch.setenv("NAMEHUNT_HOME", str(tmp_path))
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/usr/local/bin/claude")
    banner = "".join(f"start-up banner line {i}\n" for i in range(40))

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr=banner + "Error: this model needs a newer CLI"
        )

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    provider = make_provider("claudecode", "sonnet")
    with pytest.raises(RuntimeError, match="needs a newer CLI"):
        provider.complete(system="s", user="u")
