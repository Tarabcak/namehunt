# namehunt

**Name your company with a closed feedback loop — and run it on your Claude
subscription, no API key needed.** We built namehunt to rename our own startup:
8 rounds, 210 live-checked names, one winner. Every "AI name generator" hands
you names that turn out to be taken; namehunt checks real availability via RDAP
*inside the loop*, so every round learns from what's actually free.

A local CLI that runs a **closed feedback loop** for naming a company:

1. An LLM (Claude by default, OpenAI optional) generates candidate name stems from your brief.
2. Real domain availability is checked for each stem across your TLDs via **RDAP** (free, keyless, authoritative).
3. Results — plus your likes/dislikes/notes — feed straight back into the next generation round.
4. Repeat until you have a shortlist of names whose domains are actually available.

## Install

The fifteen-second way (requires [uv](https://docs.astral.sh/uv/)):

```bash
uvx namehunt run --brief brief.md --provider claudecode
```

Or install it properly: `uv tool install namehunt` (also: `pipx install namehunt`).

From source (Python 3.11+):

```bash
git clone https://github.com/Tarabcak/namehunt && cd namehunt && uv sync
```

Optional OpenAI support:

```bash
uv sync --extra openai
```

## Providers & environment variables

| Provider | Auth | Notes |
|---|---|---|
| `claude` (default) | `ANTHROPIC_API_KEY` | Anthropic API, billed per token |
| `claudecode` | your existing Claude Code login | runs `claude -p` headless on your Pro/Max **subscription** — no API credits. Requires Claude Code installed & signed in. Personal use only: Anthropic [does not allow](https://code.claude.com/docs/en/agent-sdk/overview) shipping products on claude.ai login, and any `ANTHROPIC_API_KEY` in the env is withheld from the subprocess so it can never bill the API |
| `openai` | `OPENAI_API_KEY` | needs `uv sync --extra openai` |

Other variables: `NAMEHUNT_PROVIDER` picks the default provider when `--provider` is
omitted; `NAMEHUNT_HOME` overrides the state directory (default `~/.namehunt` —
sessions + RDAP bootstrap cache). Keys are read from the environment only and are
never written to disk.

```bash
# subscription-powered, no API key needed:
uv run namehunt run --brief examples/brief.md --provider claudecode
```

## 60-second quickstart

```bash
export ANTHROPIC_API_KEY=sk-ant-...
uv run namehunt run --brief examples/brief.md --tlds com,ai --session myco
```

Each round prints a table — ✅ available, ❌ taken, ❓ unknown — then prompts:

```
[a]nother round  [l]ike <numbers>  [d]islike <numbers>  [n]ote <text>  [q]uit & export
```

- `l 2 5` / `d 3` — like/dislike rows from the table (steers the next round)
- `n shorter, more mythic` — free-text steering note
- `a` — next round; `q` — quit
- `--rounds 3` runs three rounds autonomously (no prompts); `--debug` prints the full LLM prompt

Ctrl-C is safe at any point — the session is saved, then:

```bash
uv run namehunt resume --session myco
uv run namehunt export --session myco --format md   # also: csv, json
```

## The RDAP caveat

> ⚠️ RDAP 'available' is a strong signal, not a guarantee — premium/reserved names can
> still be unregisterable. Re-check at your registrar before buying, and run a
> trademark search.

`unknown` (❓) means the check failed (timeout, rate limit, TLD without RDAP…) — namehunt
**never** reports a domain as available unless RDAP itself said so.

## Development

```bash
uv run pytest -q            # all network mocked; suite blocks real sockets
uv run ruff check .
uv run mypy src
```

## How it was built

Spec-first, strict TDD, by Claude Code: the full product spec, the workflow
rules, and the hooks that enforced red→green live in this repo as artifacts —
see [namehunt-SPEC.md](namehunt-SPEC.md) and [CLAUDE.md](CLAUDE.md). The test
suite hard-blocks real network access, and every RDAP failure path is proven
to map to `unknown`, never `available`.

---

*namehunt was built in one weekend to rename our own startup — it's called
**prescry** now (prescry.com), the cross-repo impact layer for AI coding
agents: your agents write code in one repo, prescry makes sure it doesn't
break the other forty. Story coming soon. — Róbert & Norbert*
