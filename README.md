# namehunt

A local CLI that runs a **closed feedback loop** for naming a company:

1. An LLM (Claude by default, OpenAI optional) generates candidate name stems from your brief.
2. Real domain availability is checked for each stem across your TLDs via **RDAP** (free, keyless, authoritative).
3. Results — plus your likes/dislikes/notes — feed straight back into the next generation round.
4. Repeat until you have a shortlist of names whose domains are actually available.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

Optional OpenAI support:

```bash
uv sync --extra openai
```

## Environment variables

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | required for the default `claude` provider |
| `OPENAI_API_KEY` | required only with `--provider openai` |
| `NAMEHUNT_PROVIDER` | default provider (`claude` or `openai`) when `--provider` is omitted |
| `NAMEHUNT_HOME` | state directory override (default `~/.namehunt` — sessions + RDAP bootstrap cache) |

Keys are read from the environment only and are never written to disk.

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
