"""Tests for the codexcli provider — subscription-powered via `codex exec`.

Mirrors the claudecode provider: the codex binary is mocked at the subprocess
boundary, no real invocations, no network. Committed tests stay untouched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from namehunt import llm
from namehunt.llm import DEFAULT_MODELS, make_provider
from namehunt.session import state_home


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("NAMEHUNT_HOME", str(tmp_path))


@pytest.fixture
def codex_present(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: f"/usr/local/bin/{name}")


def _fake_run_writing(result: str):
    """Fake subprocess.run that writes `result` to the --output-last-message file."""
    calls: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = list(argv)
        calls["kwargs"] = kwargs
        out_path = argv[argv.index("--output-last-message") + 1]
        Path(out_path).write_text(result)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    return fake_run, calls


def test_codexcli_has_a_default_model():
    assert "codexcli" in DEFAULT_MODELS


def test_codexcli_needs_no_api_key(monkeypatch, codex_present):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = make_provider("codexcli", DEFAULT_MODELS["codexcli"])
    assert provider.name == "codexcli"


def test_codexcli_missing_binary_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="codex"):
        make_provider("codexcli", DEFAULT_MODELS["codexcli"])


def test_complete_invokes_exec_sandboxed(monkeypatch, codex_present):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-subprocess")
    fake_run, calls = _fake_run_writing('["nova", "quill"]')
    monkeypatch.setattr(llm.subprocess, "run", fake_run)

    provider = make_provider("codexcli", DEFAULT_MODELS["codexcli"])
    reply = provider.complete(system="SYSTEM RULES", user="USER PROMPT")
    assert reply == '["nova", "quill"]'

    argv = calls["argv"]
    assert argv[0].endswith("codex")
    assert "exec" in argv
    prompt_arg = next(a for a in argv if "USER PROMPT" in a)
    assert "SYSTEM RULES" in prompt_arg  # system + user travel in one prompt
    assert "--sandbox" in argv
    assert "read-only" in argv  # generation must never execute anything
    assert "--skip-git-repo-check" in argv
    assert "-m" not in argv  # default model sentinel: use the user's codex config

    kwargs = calls["kwargs"]
    assert kwargs["cwd"] == state_home()
    assert "OPENAI_API_KEY" not in kwargs["env"]  # subscription login, never API billing


def test_complete_model_override_passes_flag(monkeypatch, codex_present):
    fake_run, calls = _fake_run_writing('["oak"]')
    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    provider = make_provider("codexcli", "gpt-5")
    provider.complete(system="s", user="u")
    argv = calls["argv"]
    assert "-m" in argv
    assert "gpt-5" in argv


def test_complete_nonzero_exit_raises(monkeypatch, codex_present):
    monkeypatch.setattr(
        llm.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr="not logged in"),
    )
    provider = make_provider("codexcli", DEFAULT_MODELS["codexcli"])
    with pytest.raises(RuntimeError, match="not logged in"):
        provider.complete(system="s", user="u")


def test_complete_missing_output_raises(monkeypatch, codex_present):
    monkeypatch.setattr(
        llm.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="", stderr=""),
    )
    provider = make_provider("codexcli", DEFAULT_MODELS["codexcli"])
    with pytest.raises(RuntimeError, match="no output"):
        provider.complete(system="s", user="u")


def test_complete_timeout_raises_clean_error(monkeypatch, codex_present):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 480)

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    provider = make_provider("codexcli", DEFAULT_MODELS["codexcli"])
    with pytest.raises(RuntimeError, match="timed out"):
        provider.complete(system="s", user="u")


def test_explicit_codex_binary_override(monkeypatch, codex_present):
    monkeypatch.setenv("NAMEHUNT_CODEX_BIN", "/native/bin/codex")
    monkeypatch.setattr(llm.shutil, "which", lambda name: name)
    fake_run, calls = _fake_run_writing('["oak"]')
    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    make_provider("codexcli", "default").complete(system="s", user="u")
    assert calls["argv"][0] == "/native/bin/codex"


def test_codex_uses_unique_output_files_and_structured_schema(monkeypatch, codex_present):
    paths = []

    def fake_run(argv, **kwargs):
        import json

        output = Path(argv[argv.index("--output-last-message") + 1])
        paths.append(output)
        schema = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text())
        assert schema["required"] == ["names"]
        assert schema["properties"]["names"]["type"] == "array"
        assert 'model_reasoning_effort="low"' in argv
        assert "CODEX_API_KEY" not in kwargs["env"]
        output.write_text('{"names": ["oak"]}')
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setenv("CODEX_API_KEY", "must-not-bill")
    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    provider = make_provider("codexcli", "default")
    provider.complete(system="s", user="u")
    provider.complete(system="s", user="u")
    assert paths[0] != paths[1]
    assert all(not p.exists() for p in paths)


def test_empty_output_is_an_actionable_failure(monkeypatch, codex_present):
    fake_run, _ = _fake_run_writing("   \n")
    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="empty output"):
        make_provider("codexcli", "default").complete(system="s", user="u")
