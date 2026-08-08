"""LLM provider abstraction, prompt building, and defensive output parsing.

LLM output is untrusted: candidates are filtered to ^[a-z]{3,12}$ before they
can ever become domain queries; parsing tolerates fences/wrappers/prose."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import anthropic
from rich.console import Console

from namehunt.availability import Availability
from namehunt.session import Session, state_home

DEFAULT_MODELS = {"claude": "claude-sonnet-4-6", "openai": "gpt-4.1-mini", "claudecode": "sonnet"}
KEY_ENV_VARS = {"claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}  # claudecode: none
MAX_COMPLETION_TOKENS = 2048
INVALID_WARN_RATIO = 0.3

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
    timeout_seconds = 180.0

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
        ]
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
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
            detail = (proc.stderr or proc.stdout).strip()[:300]
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


def make_provider(provider: str, model: str) -> Provider:
    """Build a provider; for key-based providers the env var is checked first."""
    if provider not in DEFAULT_MODELS:
        raise ValueError(
            f"unknown provider {provider!r}: expected 'claude', 'claudecode' or 'openai'"
        )
    if provider == "claudecode":
        return ClaudeCodeProvider(model)
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


def summarize_results(results: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """Return (available full domains, fully-failed taken stems). UNKNOWN feeds
    neither list; a stem with any AVAILABLE domain is a win, not a taken stem."""
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


def context_from_session(sess: Session, brief_text: str, batch: int) -> RoundContext:
    """Rebuild the exact loop context from persisted state (used by resume too)."""
    merged: dict[str, str] = {}
    for record in sess.rounds:
        merged.update(record.results)
    available, taken = summarize_results(merged)
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
    )


def build_messages(ctx: RoundContext) -> tuple[str, str]:
    """Build the (system, user) prompt pair. Pure and deterministic."""
    lines = ["PROJECT BRIEF:", ctx.brief, ""]
    if ctx.style:
        lines += [f"STYLE DIRECTION: {ctx.style}", ""]
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
    if ctx.notes:
        lines.append("USER NOTES:")
        lines += [f"- {n}" for n in ctx.notes]
    if ctx.likes or ctx.dislikes or ctx.notes:
        lines.append("")
    lines.append("DO NOT REPEAT ANY OF THESE STEMS:")
    lines.append(", ".join(sorted(ctx.all_generated)) if ctx.all_generated else "(nothing yet)")
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
