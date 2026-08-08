# namehunt

Small Python 3.11+ CLI: LLM proposes startup names → RDAP availability check (whoisit)
→ results feed the next LLM round. ~600 lines. Full spec: @namehunt-SPEC.md (Part 1).

## Commands (ALWAYS use uv — never bare pip/python/pytest)
- Install/sync: `uv sync` · Add dep: `uv add <pkg>` (dev: `uv add --dev <pkg>`)
- Run CLI: `uv run namehunt ...` · Tests: `uv run pytest -q`
- Lint/format: `uv run ruff check --fix .` then `uv run ruff format .` · Types: `uv run mypy src`

## Workflow — STRICT TDD
1. RED: write failing tests from the spec FIRST; run pytest; confirm they fail for the
   right reason (missing implementation, not import typos). NO implementation yet.
2. Commit the failing tests before implementing ("test: … (red)").
3. GREEN: minimum code to pass. Do NOT modify committed tests to make them pass —
   if a test seems wrong, STOP and ask.
4. Iterate until pytest, ruff, and mypy are ALL clean. Show command OUTPUT as evidence,
   never just assert success.

## Testing rules
- ALL network mocked in the pytest suite (whoisit + anthropic client at call boundaries).
- The one live smoke test is manual only — never part of pytest/CI.

## Domain-availability contract (CORRECTNESS-CRITICAL)
- Three-state enum: AVAILABLE / TAKEN / UNKNOWN.
- Timeout, connection error, RDAP/HTTP error, bootstrap failure, parse failure
  → UNKNOWN, NEVER AVAILABLE. This is the worst possible bug class in this tool.
- Only AVAILABLE names feed the next round's "these worked" signal; UNKNOWN excluded.

## Style & repo etiquette
- Type hints on all public functions; small pure functions; rich for terminal output,
  no bare print in library code.
- Small commits. Never edit uv.lock by hand. Don't edit [tool.ruff]/[tool.mypy] configs.
