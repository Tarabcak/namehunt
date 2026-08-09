"""Tests for the claudecode provider — subscription-powered via headless `claude -p`.

Added after the original red commit as part of the approved claudecode
deviation; the originally committed tests are untouched. The `claude` CLI is
mocked at the subprocess boundary — no real invocations, no network.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from namehunt import llm
from namehunt.llm import DEFAULT_MODELS, make_provider
from namehunt.session import state_home


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("NAMEHUNT_HOME", str(tmp_path))


@pytest.fixture
def cli_present(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/usr/local/bin/claude")


def _ok_payload(result: str) -> str:
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": result})


def test_claudecode_has_a_default_model():
    assert "claudecode" in DEFAULT_MODELS


def test_claudecode_needs_no_api_key(monkeypatch, cli_present):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = make_provider("claudecode", "sonnet")
    assert provider.name == "claudecode"


def test_claudecode_missing_cli_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="claude"):
        make_provider("claudecode", "sonnet")


def test_complete_runs_headless_print_json(monkeypatch, cli_present):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-subprocess")
    calls: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = list(argv)
        calls["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            argv, 0, stdout=_ok_payload('["nova", "quill"]'), stderr=""
        )

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    provider = make_provider("claudecode", "sonnet")
    reply = provider.complete(system="SYSTEM RULES", user="USER PROMPT")
    assert reply == '["nova", "quill"]'

    argv = calls["argv"]
    assert argv[0] == "/usr/local/bin/claude"
    assert "-p" in argv
    assert "USER PROMPT" in argv
    assert "SYSTEM RULES" in argv  # delivered via the system-prompt flag
    assert "--output-format" in argv
    assert "json" in argv
    assert "sonnet" in argv

    kwargs = calls["kwargs"]
    assert kwargs["cwd"] == state_home()  # neutral cwd: no project CLAUDE.md pollution
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]  # subscription login, never API billing


def test_complete_nonzero_exit_raises(monkeypatch, cli_present):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not logged in")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    provider = make_provider("claudecode", "sonnet")
    with pytest.raises(RuntimeError, match="not logged in"):
        provider.complete(system="s", user="u")


def test_complete_error_payload_raises(monkeypatch, cli_present):
    payload = json.dumps({"type": "result", "is_error": True, "result": "usage limit reached"})
    monkeypatch.setattr(
        llm.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=payload, stderr=""),
    )
    provider = make_provider("claudecode", "sonnet")
    with pytest.raises(RuntimeError, match="usage limit"):
        provider.complete(system="s", user="u")


def test_complete_garbage_stdout_raises(monkeypatch, cli_present):
    monkeypatch.setattr(
        llm.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="not json", stderr=""),
    )
    provider = make_provider("claudecode", "sonnet")
    with pytest.raises(RuntimeError):
        provider.complete(system="s", user="u")


def test_complete_timeout_raises_clean_error(monkeypatch, cli_present):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 180)

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    provider = make_provider("claudecode", "sonnet")
    with pytest.raises(RuntimeError, match="timed out"):
        provider.complete(system="s", user="u")


def test_complete_scrubs_nested_session_env_and_pins_effort(monkeypatch, cli_present):
    """When namehunt itself runs inside a Claude Code session, the harness's
    CLAUDE* session vars must not leak into the nested CLI (inherited
    effort=max makes generations think unboundedly and time out)."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-abc")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    monkeypatch.setenv("CLAUDE_EFFORT", "max")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "keep-me")  # legit headless auth
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, stdout=_ok_payload('["oak"]'), stderr="")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    provider = make_provider("claudecode", "sonnet")
    assert provider.complete(system="s", user="u") == '["oak"]'

    env = seen["env"]
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_SESSION_ID" not in env
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in env
    assert "CLAUDE_EFFORT" not in env
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "keep-me"
    argv = seen["argv"]
    assert "--effort" in argv  # explicit, so no ambient setting can inflate it
