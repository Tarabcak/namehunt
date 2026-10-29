# namehunt — Complete Build Spec for Claude Code

This single file contains everything: the product spec (Part 1), the workflow rules you must follow (Part 2), the hooks config to install (Part 3), and the independent verifier prompt (Part 4).

---

# PART 1 — PRODUCT SPEC

## 1.1 Overview

`namehunt` is a local CLI tool that runs a **closed feedback loop** for naming a company:

1. An LLM (Claude by default, OpenAI optional) generates candidate brand names from a project brief.
2. The tool checks real domain availability for each candidate across configured TLDs using **RDAP** (free, keyless, authoritative).
3. Results (available / taken / unknown) plus optional user feedback ("more like X, less like Y") are fed **back into the LLM** for the next generation round.
4. Repeat until the user has a shortlist of names with available domains.

**Target user:** a founder running it on their own machine with their own API key.
**Vibe:** small, sharp, done in an afternoon. Not a platform. Target ≤ ~600 lines of source (excluding tests) — if it's ballooning past that, cut scope, don't add abstraction.

## 1.2 Non-goals

- No web UI, no hosted service, no accounts, no telemetry.
- No domain purchasing/registration, no trademark checking (print a reminder instead).
- No premium/reserved-name price detection (RDAP can't do it; print a caveat that RDAP 404 ≠ guaranteed registerable).
- No support for exotic ccTLDs without RDAP servers — report them as `UNKNOWN`, don't crash.

## 1.3 Tech stack

- **Python 3.11+**, managed with `uv` (single project, `pyproject.toml`).
- **`whoisit`** for RDAP (handles IANA bootstrap; cache bootstrap data in `~/.namehunt/bootstrap.json`, refresh if older than 7 days).
- **`anthropic`** SDK (default provider); **`openai`** SDK optional. Provider via `--provider claude|openai` or `NAMEHUNT_PROVIDER` env var. Keys from `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`. Never write keys to disk.
- **`rich`** for terminal tables/spinners; **`httpx`** for optional DNS-over-HTTPS pre-filter.
- Default models: `claude-sonnet-4-6` (Anthropic), `gpt-4.1-mini` (OpenAI) — overridable via `--model`.

## 1.4 CLI interface

```
namehunt run --brief brief.md [--tlds com,ai,io] [--require-tlds com] [--batch 20] [--rounds 0] \
             [--provider claude] [--model MODEL] [--style "short, mythic, 2 syllables"] \
             [--session mysession] [--fast-prefilter] [--debug]
namehunt resume --session mysession
namehunt export --session mysession --format csv|json|md
```

- `--brief`: path to a markdown/text file describing the business (required on first run).
- `--batch`: names generated per round (default 20).
- `--rounds 0`: interactive mode (default); `--rounds N`: run N rounds autonomously, steering only on availability results.
- `--fast-prefilter`: DoH DNS check (Cloudflare `https://cloudflare-dns.com/dns-query`) first; only NXDOMAIN candidates go to RDAP. **DNS is a heuristic only — never report "available" from DNS alone.**
- `--session`: session name; state persisted to `~/.namehunt/sessions/<name>.json`.
- `--require-tlds`: comma-separated subset of `--tlds`, stored in the session. A stem only counts as a
  winner in the next prompt when every required TLD is AVAILABLE; a required TLD that is TAKEN makes the
  stem a taken stem even if other TLDs are free. UNKNOWN still feeds neither list. The prompt names the
  required TLDs, and export lists stems with every required TLD available first.
- `run` on an existing session continues it with the session's own provider, model, style and TLDs.
  `--provider`, `--model`, `--style` and `--require-tlds` given explicitly replace the stored values, so a
  later `resume` uses them too. `NAMEHUNT_PROVIDER` only picks the provider of a new session. `--tlds`
  that differ from the session's TLDs are an error (exit 2): start a new session to change TLDs.
- `--debug`: print the full outgoing LLM prompt each round.

### Interactive round flow

After each round, render a `rich` table: name, per-TLD status (✅ available / ❌ taken / ❓ unknown), then prompt:

```
[a]nother round  [l]ike <numbers>  [d]islike <numbers>  [n]ote <free text>  [q]uit & export
```

The prompt is printed literally (never parsed as `rich` markup), so the bracketed letters stay visible.

Likes/dislikes/notes are stored in session state and included in the next LLM prompt.

## 1.5 Core loop design

```
brief + style + history summary
        │
        ▼
  LLM: generate BATCH candidate name stems (JSON array of lowercase a-z strings)
        │  dedupe against ALL previously generated stems (session history)
        ▼
  Availability check: for each stem × TLD → RDAP lookup
        │  concurrency: max 4 in flight; global throttle ≤ 4 req/s; 10s timeout/request
        │  HTTP 404 → AVAILABLE; 200 → TAKEN
        │  429 → retry once with exponential backoff (2s, then 8s); still failing → UNKNOWN
        │  timeout / network error / no RDAP server for TLD → UNKNOWN (NEVER "available")
        ▼
  Summarize round compactly for the LLM:
        - available names (full list) + taken names (stems only)
        - user likes/dislikes/notes
        - instruction: "generate BATCH new names; favor patterns of the available/liked ones;
          avoid patterns of the taken/disliked ones; never repeat any name in <all_generated>"
        ▼
  loop (until user quits or --rounds exhausted)
```

### Domain-availability contract (CORRECTNESS-CRITICAL)

- Availability is a **three-state enum**: `AVAILABLE` / `TAKEN` / `UNKNOWN`.
- On timeout, connection error, RDAP/HTTP error, bootstrap failure, or any parse failure → return `UNKNOWN`, **never** `AVAILABLE`. Reporting a taken domain as available is the worst possible bug in this tool.
- Only `AVAILABLE` names feed the "these worked" signal in the next round; `UNKNOWN` is excluded from both the available and taken lists.

### LLM prompt contract

- System prompt instructs: return **only** a JSON array of candidate stems, lowercase, a–z only (no digits/hyphens unless `--style` allows), 3–12 chars, no explanations.
- Parse defensively: strip code fences, tolerate a top-level `{"names": [...]}` wrapper, drop invalid entries, warn if >30% of the batch was invalid. One retry on unparseable output with an error-corrective message.

### Session state (JSON on disk)

`brief_path`, `style`, `tlds`, `required_tlds` (older files without it load as none), `provider`, `model`, `rounds` (each: timestamp, candidates, per-TLD results), `likes`, `dislikes`, `notes`, cumulative `all_generated` set. Resume must reproduce the exact loop context.

## 1.6 Output & guardrails

- Live `rich` table per round; `export` sorts by (available-TLD count desc, then liked first) to CSV/JSON/Markdown.
- On every export and on quit, print: *"⚠️ RDAP 'available' is a strong signal, not a guarantee — premium/reserved names can still be unregisterable. Re-check at your registrar before buying, and run a trademark search."*
- Missing API key → clear one-line error naming the env var, exit code 2.
- Ctrl-C mid-round → save session state, print resume hint, exit 0. Hints name the plain `namehunt` command
  (it may be installed with uv tool or run through uvx), never `uv run namehunt`.
- A failing CLI provider reports the end of the CLI's error output, where the actual error is, not its
  start-up banner.
- All network calls have timeouts; no unbounded retries. No calls anywhere except the chosen LLM API, RDAP servers, and (optionally) Cloudflare DoH.

## 1.7 Suggested layout

```
namehunt/
  pyproject.toml
  README.md
  CLAUDE.md                  # created in Phase 0 from Part 2 of this file
  .claude/settings.json      # created in Phase 0 from Part 3 of this file
  examples/brief.md          # sample brief: data/knowledge/agentic-AI startup
  src/namehunt/
    __init__.py
    cli.py                   # arg parsing, interactive loop
    llm.py                   # provider abstraction, prompt building, parsing
    availability.py          # RDAP + optional DoH pre-filter, throttling
    session.py               # state persistence
    export.py
  tests/
    test_availability.py
    test_llm_parsing.py
    test_session.py
    test_loop.py
```

## 1.8 Required tests (pytest, ALL network mocked — no live calls in the default run)

1. Availability mapping: 404→AVAILABLE, 200→TAKEN, 429-after-retries→UNKNOWN, timeout→UNKNOWN, connection error→UNKNOWN, parse error→UNKNOWN.
2. DNS pre-filter never produces AVAILABLE on its own; NXDOMAIN candidates are RDAP-confirmed.
3. LLM output parsing: clean JSON array, fenced JSON, `{"names": [...]}` wrapper, garbage → retry path, invalid entries dropped.
4. Dedupe: stems from earlier rounds never re-checked or re-shown.
5. Feedback summarization: likes/dislikes/notes appear in the next prompt payload; UNKNOWN names appear in neither the available nor taken lists.
6. Session save → resume → identical loop context.
7. Throttle: with 50 queued checks, request rate never exceeds the cap (use a fake clock).

## 1.9 Acceptance criteria

- [ ] `uv run namehunt run --brief examples/brief.md` completes a full interactive round end-to-end with a real Anthropic key.
- [ ] A second round demonstrably conditions on round-one results (verify via `--debug`).
- [ ] No candidate ever repeats across rounds in a session.
- [ ] Killing the process and running `resume` continues with full history.
- [ ] `pytest` passes with zero network access.
- [ ] README covers: install (`uv sync`), env vars, a 60-second quickstart, the RDAP caveat.
- [ ] Source ≤ ~600 lines excluding tests.

---

# PART 2 — WORKFLOW RULES (copy this section into `CLAUDE.md` in Phase 0)

```markdown
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
```

---

# PART 3 — HOOKS (create as `.claude/settings.json` in Phase 0)

```json
{
  "permissions": { "defaultMode": "acceptEdits" },
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "f=\"$CLAUDE_FILE_PATH\"; case \"$f\" in *.py) uv run ruff format \"$f\"; uv run ruff check --fix \"$f\"; uv run mypy \"$f\" 2>&1 | head -20 ;; esac; exit 0",
            "timeout": 60
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "uv run pytest -q >&2 || exit 2",
            "timeout": 120
          }
        ]
      }
    ]
  }
}
```

Notes: the PostToolUse hook lints/type-checks only the edited file and always exits 0 (never blocks mid-plan). The Stop hook blocks ending the turn until pytest is green. **During Phase 1 (RED), tests are supposed to fail — add the Stop hook only at the start of Phase 2**, or temporarily disable it via `/hooks` during RED.

---

# PART 4 — INDEPENDENT VERIFIER (superseded by Part 5)

**Note: Part 5 replaces this with a specialist review panel that loops until no
CRITICAL/HIGH findings remain. Use Part 5. This single-reviewer prompt is kept only
as a lightweight fallback if you want one quick pass instead of the full loop.**

Paste this into a **new** session in the project folder:

```
You are a skeptical senior Python reviewer. You did NOT write this code. Review the
namehunt implementation against Parts 1 and 2 of @namehunt-SPEC.md. Use ONLY read-only
tools (Read, Grep, Glob) plus running the test suite; do NOT edit files.

Report ONLY findings that affect correctness or violate a stated requirement — not
style preferences. For each finding: file:line, the problem, the minimal fix.

Check specifically:
1. SAFETY INVARIANT: trace every RDAP failure path (timeout, connection error, HTTP/RDAP
   error, bootstrap failure, parse error). Prove each returns UNKNOWN, never AVAILABLE.
   Hunt for any path where an exception or empty result could read as "available".
2. FEEDBACK LOOP: confirm only AVAILABLE names feed the next LLM round (UNKNOWN excluded),
   and already-seen candidates are never re-proposed.
3. TEST INTEGRITY: is all network actually mocked? Any tautological tests that would pass
   even if the implementation were wrong? Is timeout→UNKNOWN actually asserted? Was any
   committed test weakened after the red commit (check git log/diff)?
4. Acceptance criteria (spec §1.9) one by one: PASS/FAIL with evidence.
5. Run `uv run pytest -q`, `uv run ruff check .`, `uv run mypy src`; report actual output.

End with a verdict — READY / NEEDS ATTENTION / NEEDS WORK — and a short prioritized fix
list. Flag only what a careful maintainer would insist on before shipping.
```

---

# PART 5 — SPECIALIST REVIEW LOOP (iterate until zero CRITICAL/HIGH)

Run this AFTER the build (Phases 0–3) is complete. The Part 4 prompt is retired —
it's replaced by the specialist panel below, which uses a shared severity scale
and loops until clean.

## 5.1 Severity scale (all reviewers MUST use this)

- **CRITICAL** — violates the availability safety invariant (any path where an error/timeout
  could surface as AVAILABLE), leaks API keys, corrupts/loses session state, or crashes the loop.
- **HIGH** — spec requirement not met (§1.4–§1.9), wrong feedback-loop behavior (UNKNOWN
  leaking into available/taken lists, repeated candidates), test that would pass with a broken
  implementation, unmocked network in the test suite, rate-limit cap violated.
- **MEDIUM** — correctness edge cases unlikely in normal use, misleading output, missing
  error message clarity.
- **LOW** — style, naming, minor docs. Reviewers may list these but they NEVER block.

**Exit condition: zero CRITICAL and zero HIGH across all reviewers. Max 3 loop iterations —
if not clean after 3, stop and escalate to the human.**

## 5.2 The specialists (each in a FRESH context — new session or headless run)

Every specialist prompt starts with this common header:

> You are a skeptical specialist reviewer. You did NOT write this code. Review the namehunt
> repo against Parts 1–2 of @namehunt-SPEC.md. Read-only: do NOT edit files. Report findings
> as: `[SEVERITY] file:line — problem — minimal fix`, using the severity scale in §5.1
> exactly. Do not report style preferences above LOW. Do not propose refactors or new
> features. If an area is clean, say so explicitly. End with a count: `CRITICAL: n, HIGH: n,
> MEDIUM: n, LOW: n`.

**Specialist A — Safety invariant & loop logic (Claude):**
> Focus ONLY on: (1) every RDAP failure path (timeout, connection error, HTTP/RDAP error,
> bootstrap failure, parse error) — prove each returns UNKNOWN, never AVAILABLE; hunt for
> exception handlers, default values, or truthiness checks that could read as "available".
> (2) The feedback loop: only AVAILABLE names in the "available" signal, UNKNOWN excluded
> from both lists, no candidate ever repeated across rounds, dedupe survives resume.
> (3) Throttle and retry logic actually bounds request rate and retry count.

**Specialist B — Security & robustness (Claude):**
> Focus ONLY on: API keys never written to disk/logs/session files/debug output; LLM output
> treated as untrusted (a malicious/curious model response must not achieve path traversal
> via session names, injection into shell/filesystem, or oversized-input DoS); session file
> paths sanitized; no network calls beyond LLM API, RDAP, and optional Cloudflare DoH;
> Ctrl-C handling can't corrupt the session file (atomic write?).

**Specialist C — Test integrity & reward-hacking audit (Claude):**
> Focus ONLY on the test suite: run `git log --oneline` and `git diff <red-commit> HEAD -- tests/`
> to verify committed tests were never weakened after the red commit. Identify tautological
> tests (would pass with a broken implementation), missing assertions for timeout→UNKNOWN and
> 429→retry→UNKNOWN, any real network reachable from pytest, and whether §1.8 items 1–7 are
> each genuinely covered. Run `uv run pytest -q` and report actual output.

**Specialist D — Cross-model review (Codex / OpenAI):**
Run from the repo root with the Codex CLI (requires OpenAI auth; check `codex --help` for
current flags — sandbox/approval flags change between versions):
```
codex exec --sandbox read-only "You are reviewing a Python CLI you did not write. Read \
namehunt-SPEC.md Parts 1-2 and the src/ and tests/ directories. Report only correctness \
and spec-compliance findings as [SEVERITY] file:line - problem - minimal fix, where \
severity CRITICAL/HIGH/MEDIUM/LOW follows section 5.1 of the spec. Pay special attention \
to: error paths in availability checking that could misreport a domain as available, \
concurrency/throttling bugs, and JSON parsing of LLM output. Do not edit anything. End \
with counts: CRITICAL: n, HIGH: n, MEDIUM: n, LOW: n."
```
If Codex is unavailable, substitute any second model family (e.g. Gemini CLI) — the point
is a reviewer that doesn't share the builder's blind spots.

## 5.3 The loop protocol

```
iteration = 1
repeat:
  1. Run Specialists A–D, each in a fresh context. Collect all findings.
  2. If CRITICAL == 0 and HIGH == 0 across all four → DONE. Optionally batch-fix
     MEDIUMs once, re-run pytest/ruff/mypy, and stop.
  3. Else: paste ONLY the CRITICAL/HIGH findings into the BUILDER session with:
     "Fix exactly these findings, nothing else. Do not modify committed tests
      unless the finding says the test itself is the bug — and then explain why
      before changing it. Show passing pytest/ruff/mypy output when done. Commit
      as 'fix: review iteration <n>'."
  4. Re-run ONLY the specialists whose domain was touched (plus Specialist C
     always, since fixes can introduce test tampering).
  5. iteration += 1; if iteration > 3 → STOP, hand remaining findings to the human.
```

Rules to keep the loop honest and cheap:
- Specialists never fix; the builder never grades. Keep the roles separate.
- Fresh context per review iteration — do not reuse a reviewer session that has
  already seen (and anchored on) earlier verdicts.
- Reviewers finding zero issues on iteration 1 is suspicious for a first build;
  reviewers still finding new HIGHs on iteration 3 means the spec or architecture
  has a real problem — stop looping, think.
- Chasing MEDIUMs/LOWs across iterations is how a 600-line tool becomes a 2,000-line
  tool. The gate is CRITICAL/HIGH only.

## 5.4 Optional: scripted loop (headless)

For a hands-off version, drive it from bash — Claude Code headless (`claude -p`) +
`codex exec` — capping iterations:

```bash
for i in 1 2 3; do
  claude -p "$(cat review-prompt-a.md)" > findings-a.txt
  claude -p "$(cat review-prompt-b.md)" > findings-b.txt
  claude -p "$(cat review-prompt-c.md)" > findings-c.txt
  codex exec --sandbox read-only "$(cat review-prompt-d.md)" > findings-d.txt
  if ! grep -qE '^\[(CRITICAL|HIGH)\]' findings-*.txt; then echo "CLEAN"; break; fi
  claude -p "Fix exactly these findings, nothing else. Do not touch committed tests \
unless a finding says the test is the bug: $(grep -hE '^\[(CRITICAL|HIGH)\]' findings-*.txt)"
done
```

Manual (paste-per-session) is fine for this project's size; the script matters more
if you reuse this workflow on bigger builds. Verify current `claude -p` and `codex exec`
flags before relying on the script — both CLIs change frequently.
