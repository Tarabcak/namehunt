"""LLM provider abstraction, prompt building, and defensive output parsing.

LLM output is untrusted: candidates are filtered to ^[a-z]{3,12}$ before they
can ever become domain queries; parsing tolerates fences/wrappers/prose."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import anthropic
from rich.console import Console

from namehunt.availability import Availability
from namehunt.session import Session, state_home

DEFAULT_MODELS = {
    "claude": "claude-sonnet-4-6",
    "openai": "gpt-4.1-mini",
    "claudecode": "sonnet",
    "codexcli": "default",  # sentinel: use the model configured in the user's codex CLI
}
KEY_ENV_VARS = {"claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}  # CLI providers: none
MAX_COMPLETION_TOKENS = 2048
INVALID_WARN_RATIO = 0.3
CURRENT_NOTES_COUNT = 2  # newest notes render as the active brief, older ones as background

_STEM_RE = re.compile(r"[a-z]{3,12}\Z")
_ARRAY_RE = re.compile(r"\[.*?\]", re.DOTALL)
_FENCE_OPEN_RE = re.compile(r"\A```[a-zA-Z]*\s*")
_FENCE_CLOSE_RE = re.compile(r"\s*```\s*\Z")

SYSTEM_PROMPT = (
    "You are a precise startup naming engine. Respond with ONLY a JSON array of "
    "candidate name stems: lowercase a-z only, 3-12 characters each, no digits, "
    "hyphens, spaces, or punctuation. No explanations, no code fences. "
    'Example: ["nova", "quill"]'
)

RETRY_SUFFIX = (
    "\n\nYour previous reply could not be parsed. Respond with ONLY a valid JSON "
    "array of lowercase name stems - no prose, no code fences, no wrapper object."
)


class MissingKeyError(Exception):
    """Raised when the selected provider's API key env var is unset."""


class Provider(Protocol):
    name: str

    def complete(self, *, system: str, user: str) -> str: ...


class ClaudeProvider:
    name = "claude"

    def __init__(self, model: str, api_key: str) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def complete(self, *, system: str, user: str) -> str:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=MAX_COMPLETION_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(block.text for block in resp.content if block.type == "text")


class OpenAIProvider:
    name = "openai"

    def __init__(self, model: str, api_key: str) -> None:
        try:
            import openai
        except ImportError as exc:
            raise RuntimeError(
                "openai package not installed - run: uv sync --extra openai"
            ) from exc
        self._client = openai.OpenAI(api_key=api_key)
        self._model = model

    def complete(self, *, system: str, user: str) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = resp.choices[0].message.content
        return content if isinstance(content, str) else ""


class ClaudeCodeProvider:
    """Headless `claude -p` — rides the local Claude Code subscription login.

    Personal-use path (Anthropic does not allow shipping products on claude.ai
    login); no API key involved, and any ANTHROPIC_API_KEY in the environment
    is deliberately withheld so calls never bill API credits.
    """

    name = "claudecode"
    timeout_seconds = 480.0  # creative batches with adaptive thinking can run minutes

    def __init__(self, model: str) -> None:
        binary = shutil.which("claude")
        if binary is None:
            raise ValueError(
                "claude CLI not found for provider 'claudecode' - install Claude Code "
                "and sign in, or use --provider claude with an API key"
            )
        self._binary = binary
        self._model = model
        self._cwd = state_home()  # neutral cwd: no project CLAUDE.md leaks into prompts
        self._cwd.mkdir(parents=True, exist_ok=True)

    def complete(self, *, system: str, user: str) -> str:
        argv = [
            self._binary,
            "-p",
            user,
            "--system-prompt",
            system,
            "--output-format",
            "json",
            "--model",
            self._model,
            "--tools",
            "",  # pure completion: no tools, so headless mode can never hang on approval
            "--effort",
            "low",  # listing names needs no deep thinking; ambient effort=max would hang
        ]
        env = _nested_cli_env()
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                cwd=self._cwd,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"claude CLI timed out after {self.timeout_seconds:.0f}s") from exc
        if proc.returncode != 0:
            detail = _error_tail(proc.stderr or proc.stdout)
            raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {detail}")
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("unexpected claude CLI output (not JSON)") from exc
        if not isinstance(data, dict) or data.get("is_error"):
            detail = str(data.get("result", ""))[:300] if isinstance(data, dict) else ""
            raise RuntimeError(f"claude CLI returned an error: {detail}")
        result = data.get("result")
        if not isinstance(result, str):
            raise RuntimeError("claude CLI returned no result text")
        return result


def _error_tail(output: str, limit: int = 800) -> str:
    """The end of a CLI's error output: the actual error comes after its start-up banner."""
    return output.strip()[-limit:]


def _nested_cli_env() -> dict[str, str]:
    """Subprocess env for a nested claude/codex CLI, as if launched from a fresh shell.

    Drops ANTHROPIC_API_KEY and OPENAI_API_KEY (subscription logins must never
    fall back to API billing) and the CLAUDE* session vars a parent Claude Code
    session injects (an inherited effort=max makes generations think until they
    time out). CLAUDE_CODE_OAUTH_TOKEN survives: users set it deliberately for
    headless auth.
    """
    prefixes = ("CLAUDE_CODE_", "CLAUDE_EFFORT", "CLAUDE_PID", "CLAUDE_AGENT_", "CLAUDE_PREVIEW_")
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY"):
            continue
        if key != "CLAUDE_CODE_OAUTH_TOKEN" and (key == "CLAUDECODE" or key.startswith(prefixes)):
            continue
        env[key] = value
    return env


class CodexCliProvider:
    """Non-interactive `codex exec` — rides the local Codex CLI (ChatGPT) login.

    Same personal-use posture as ClaudeCodeProvider: no API key involved, and
    OPENAI_API_KEY is withheld from the subprocess so calls never bill the API.
    Runs with a read-only filesystem sandbox; this does not disable all tools.
    """

    name = "codexcli"
    timeout_seconds = 480.0

    def __init__(self, model: str) -> None:
        binary = shutil.which(os.environ.get("NAMEHUNT_CODEX_BIN") or "codex")
        if binary is None:
            raise ValueError(
                "codex CLI not found for provider 'codexcli' - install Codex and sign in, "
                "or use --provider openai with an API key"
            )
        self._binary = binary
        self._model = model
        self._cwd = state_home()  # neutral cwd: no project AGENTS.md leaks into prompts
        self._cwd.mkdir(parents=True, exist_ok=True)

    def complete(self, *, system: str, user: str) -> str:
        with tempfile.TemporaryDirectory(prefix=".codex-", dir=self._cwd) as directory:
            return self._complete_in(Path(directory), system=system, user=user)

    def _complete_in(self, directory: Path, *, system: str, user: str) -> str:
        out_path = directory / "result.txt"
        schema_path = directory / "schema.json"
        schema_path.write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {"names": {"type": "array", "items": {"type": "string"}}},
                    "required": ["names"],
                    "additionalProperties": False,
                }
            )
        )
        argv = [
            self._binary,
            "exec",
            f'{system}\n\n{user}\n\nReturn the names in the schema object: {{"names": [...]}}.',
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "-c",
            'model_reasoning_effort="low"',
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(out_path),
        ]
        if self._model != "default":
            argv += ["-m", self._model]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                cwd=self._cwd,
                env=_nested_cli_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"codex CLI timed out after {self.timeout_seconds:.0f}s") from exc
        except OSError as exc:
            raise RuntimeError(
                "Could not start codex; check NAMEHUNT_CODEX_BIN and your CLI installation"
            ) from exc
        if proc.returncode != 0:
            detail = _error_tail(proc.stderr or proc.stdout)
            raise RuntimeError(
                f"codex CLI failed (exit {proc.returncode}): {detail}. "
                "Check `codex --version` and `codex login status`; "
                "NAMEHUNT_CODEX_BIN can select a working executable."
            )
        if not out_path.exists():
            raise RuntimeError("codex CLI produced no output message")
        result = out_path.read_text()
        if not result.strip():
            raise RuntimeError("codex CLI returned empty output; check CLI login and model access")
        return result


def make_provider(provider: str, model: str) -> Provider:
    """Build a provider; for key-based providers the env var is checked first."""
    if provider not in DEFAULT_MODELS:
        raise ValueError(
            f"unknown provider {provider!r}: expected 'claude', 'claudecode', "
            "'codexcli' or 'openai'"
        )
    if provider == "claudecode":
        return ClaudeCodeProvider(model)
    if provider == "codexcli":
        return CodexCliProvider(model)
    env_var = KEY_ENV_VARS[provider]
    api_key = os.environ.get(env_var)
    if not api_key:
        raise MissingKeyError(f"missing API key: set the {env_var} environment variable")
    if provider == "claude":
        return ClaudeProvider(model, api_key)
    return OpenAIProvider(model, api_key)


@dataclass(frozen=True)
class RoundContext:
    brief: str
    style: str | None
    batch: int
    available_domains: list[str]  # full domains, AVAILABLE results only
    taken_stems: list[str]  # stems with a TAKEN result and no AVAILABLE one
    likes: list[str]
    dislikes: list[str]
    notes: list[str]
    all_generated: frozenset[str]  # cumulative; dedupe survives resume
    required_tlds: tuple[str, ...] = ()  # a stem only wins when all of these are AVAILABLE


@dataclass(frozen=True)
class ParsedNames:
    valid: list[str]
    dropped: int  # entries that failed validation (duplicates are not "dropped")
    total: int


def _extract_entries(raw: str) -> list[object] | None:
    text = _FENCE_CLOSE_RE.sub("", _FENCE_OPEN_RE.sub("", raw.strip())).strip()
    candidates = [text]
    array_match = _ARRAY_RE.search(text)
    if array_match:
        candidates.append(array_match.group())
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            data = data.get("names")
        if isinstance(data, list):
            return data
    return None


def parse_names(raw: str) -> ParsedNames:
    """Parse an LLM reply into validated stems: tolerant in shape, strict in content."""
    entries = _extract_entries(raw)
    if entries is None:
        return ParsedNames([], 0, 0)
    valid: list[str] = []
    seen: set[str] = set()
    dropped = 0
    for entry in entries:
        if not isinstance(entry, str) or not _STEM_RE.fullmatch(entry.strip()):
            dropped += 1
            continue
        stem = entry.strip()
        if stem not in seen:
            seen.add(stem)
            valid.append(stem)
    return ParsedNames(valid, dropped, len(entries))


def summarize_results(
    results: Mapping[str, str], required_tlds: Sequence[str] = ()
) -> tuple[list[str], list[str]]:
    """Return (available full domains, fully-failed taken stems). UNKNOWN feeds
    neither list; a stem with any AVAILABLE domain is a win, not a taken stem.
    With required TLDs a stem only wins when every one of them is AVAILABLE, and a
    TAKEN required TLD makes it a taken stem even if other TLDs are free."""
    if required_tlds:
        return _summarize_required(results, required_tlds)
    available = sorted(d for d, s in results.items() if s == Availability.AVAILABLE.value)
    winner_stems = {d.split(".", 1)[0] for d in available}
    taken = sorted(
        {
            d.split(".", 1)[0]
            for d, s in results.items()
            if s == Availability.TAKEN.value and d.split(".", 1)[0] not in winner_stems
        }
    )
    return available, taken


def _summarize_required(
    results: Mapping[str, str], required_tlds: Sequence[str]
) -> tuple[list[str], list[str]]:
    stems = {d.split(".", 1)[0] for d in results}
    winners = {
        stem
        for stem in stems
        if all(results.get(f"{stem}.{t}") == Availability.AVAILABLE.value for t in required_tlds)
    }
    available = sorted(
        d
        for d, s in results.items()
        if s == Availability.AVAILABLE.value and d.split(".", 1)[0] in winners
    )
    taken = sorted(
        stem
        for stem in stems
        if any(results.get(f"{stem}.{t}") == Availability.TAKEN.value for t in required_tlds)
    )
    return available, taken


def context_from_session(sess: Session, brief_text: str, batch: int) -> RoundContext:
    """Rebuild the exact loop context from persisted state (used by resume too)."""
    merged: dict[str, str] = {}
    for record in sess.rounds:
        merged.update(record.results)
    available, taken = summarize_results(merged, sess.required_tlds)
    return RoundContext(
        brief=brief_text,
        style=sess.style,
        batch=batch,
        available_domains=available,
        taken_stems=taken,
        likes=list(sess.likes),
        dislikes=list(sess.dislikes),
        notes=list(sess.notes),
        all_generated=frozenset(sess.all_generated),
        required_tlds=tuple(sess.required_tlds),
    )


def build_messages(ctx: RoundContext) -> tuple[str, str]:
    """Build the (system, user) prompt pair. Pure and deterministic."""
    lines = ["PROJECT BRIEF:", ctx.brief, ""]
    if ctx.style:
        lines += [f"STYLE DIRECTION: {ctx.style}", ""]
    if ctx.required_tlds:
        required = ", ".join(f".{t}" for t in ctx.required_tlds)
        lines += [
            f"REQUIRED DOMAINS: a name only counts when {required} "
            f"{'is' if len(ctx.required_tlds) == 1 else 'are'} available. Names with a "
            "required domain taken are listed as taken below.",
            "",
        ]
    lines.append("AVAILABLE DOMAINS (verified available - favor the patterns of these winners):")
    lines += [f"- {d}" for d in ctx.available_domains] or ["(none yet)"]
    lines.append("")
    lines.append("TAKEN STEMS (avoid similar patterns):")
    lines += [f"- {s}" for s in ctx.taken_stems] or ["(none yet)"]
    lines.append("")
    if ctx.likes:
        lines.append("USER LIKES: " + ", ".join(ctx.likes))
    if ctx.dislikes:
        lines.append("USER DISLIKES: " + ", ".join(ctx.dislikes))
    earlier_notes = ctx.notes[:-CURRENT_NOTES_COUNT]
    current_notes = ctx.notes[-CURRENT_NOTES_COUNT:]
    if earlier_notes:
        lines.append("EARLIER NOTES (background):")
        lines += [f"- {n}" for n in earlier_notes]
    if ctx.likes or ctx.dislikes or earlier_notes:
        lines.append("")
    lines.append("DO NOT REPEAT ANY OF THESE STEMS:")
    lines.append(", ".join(sorted(ctx.all_generated)) if ctx.all_generated else "(nothing yet)")
    lines.append("")
    if current_notes:
        lines.append("CURRENT DIRECTION (overrides earlier notes):")
        lines += [f"- {n}" for n in current_notes]
        lines.append("")
    lines.append(
        f"TASK: Generate exactly {ctx.batch} NEW candidate name stems for this brief. "
        "Favor the patterns of the available and liked names; avoid the patterns of "
        "the taken and disliked names. Never repeat any stem listed above. "
        "Return ONLY a JSON array of lowercase strings."
    )
    return SYSTEM_PROMPT, "\n".join(lines)


def generate_candidates(
    ctx: RoundContext,
    provider: Provider,
    *,
    debug: bool = False,
    console: Console | None = None,
) -> list[str]:
    """One generation call (plus at most one corrective retry), deduped and capped."""
    out = console if console is not None else Console(stderr=True)
    system, user = build_messages(ctx)
    parsed = _complete_and_parse(provider, system, user, debug=debug, console=out)
    if not parsed.valid:
        parsed = _complete_and_parse(
            provider, system, user + RETRY_SUFFIX, debug=debug, console=out
        )
    if parsed.total and parsed.dropped / parsed.total > INVALID_WARN_RATIO:
        out.print(
            f"[yellow]warning:[/] dropped {parsed.dropped}/{parsed.total} invalid "
            "candidates from the model's reply"
        )
    fresh = [stem for stem in parsed.valid if stem not in ctx.all_generated]
    return fresh[: ctx.batch]


def _complete_and_parse(
    provider: Provider, system: str, user: str, *, debug: bool, console: Console
) -> ParsedNames:
    if debug:
        dump = f"--- outgoing LLM prompt ---\n{system}\n\n{user}\n--- end prompt ---"
        console.print(dump, markup=False, highlight=False)
    return parse_names(provider.complete(system=system, user=user))
