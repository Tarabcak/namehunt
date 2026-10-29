# namehunt

Naming a company usually goes: generate names, check domains, find everything
taken, start over. namehunt closes that loop in one CLI — an LLM proposes
names from your brief, RDAP checks live availability on your TLDs, and the
results plus your feedback steer the next round.

```
$ namehunt run --brief brief.md --tlds com,ai

 round 2 — 20 new candidates
┏━━━┳━━━━━━━━━┳━━━━━━┳━━━━━┓
┃ # ┃ name    ┃ .com ┃ .ai ┃
┡━━━╇━━━━━━━━━╇━━━━━━╇━━━━━┩
│ 1 │ vesper  │  ❌  │ ❌  │
│ 2 │ corvid  │  ❌  │ ❌  │
│ 3 │ prescry │  ✅  │ ✅  │
└───┴─────────┴──────┴─────┘
[a]nother round  [l]ike <numbers>  [d]islike <numbers>  [n]ote <text>  [q]uit & export
```

Works with an Anthropic or OpenAI API key — or with no key at all, using your
existing Claude Code or Codex CLI login.

## Quickstart

Needs [uv](https://docs.astral.sh/uv/). Write a few lines about your project,
then run:

```bash
printf 'CLI that turns messy CSVs into typed parquet.\nFor data engineers. Vibe: short, unix-y.\n' > brief.md
uvx namehunt run --brief brief.md --provider claudecode --tlds com,ai
```

(No Claude Code? Use `--provider claude` with `ANTHROPIC_API_KEY` exported.)

Each round prints the table, then asks for feedback:

- `l 2 5` / `d 3` — like / dislike rows, steers the next round
- `n shorter, more mythic` — free-text steering note
- `a` — another round, `q` — quit

Ctrl-C is always safe; state is saved after every round.

```bash
uvx namehunt resume --session default               # pick up where you left off
uvx namehunt export --session default --format md   # or csv, json
```

Useful flags: `--batch 20` names per round, `--rounds 3` to run unattended,
`--style "short, mythic"`, `--session <name>` for parallel hunts, `--debug` to
print the outgoing LLM prompt. Permanent install: `uv tool install namehunt`.

`--require-tlds com` makes a name count only when its .com is free. Without it,
any free TLD counts as a win, so a TLD that is almost always free (such as
.design) hides that the .com is gone. Required TLDs steer the next rounds and
go first in the export; `--require-tlds none` clears them.

Running `run` again with the same `--session` continues that session with its
saved provider, model, style and TLDs. Pass `--provider`, `--model`, `--style`
or `--require-tlds` to change them for the rest of the session, `resume`
included. Different `--tlds` need a new session.

## Providers

| `--provider` | Needs | Billing |
|---|---|---|
| `claudecode` | Claude Code installed + signed in | your Claude subscription |
| `codexcli` | Codex CLI installed + signed in | your ChatGPT subscription |
| `claude` (default) | `ANTHROPIC_API_KEY` | Anthropic API, per token |
| `openai` | `OPENAI_API_KEY`, `uv sync --extra openai` | OpenAI API, per token |

The CLI providers run `claude -p` / `codex exec` (sandboxed read-only) as
subprocesses and strip API keys from their environment, so they can never
silently bill an API. Subscription login is for personal use — don't ship a
product on it ([Anthropic's policy](https://code.claude.com/docs/en/agent-sdk/overview)).

`NAMEHUNT_PROVIDER` sets the default provider; `NAMEHUNT_HOME` moves the state
dir (default `~/.namehunt`). Keys are read from the environment and never
written to disk.

### Codex CLI

```bash
uvx namehunt run --brief brief.md --provider codexcli --tlds cz
```

Uses the CLI's configured model, or the explicit `--model` override, with low
reasoning effort and a JSON output schema. Temporary schema/output files are
isolated per invocation and removed on success or failure. The read-only
filesystem sandbox does not disable every Codex tool.

If the CLI fails before generating names, check `codex --version` and
`codex login status`. For a broken PATH entry (for example an npm launcher with
a broken Node installation), select a working installed executable explicitly:

```bash
NAMEHUNT_CODEX_BIN=/absolute/path/to/codex uvx namehunt run \
  --brief brief.md --provider codexcli --tlds cz
```

`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and `CODEX_API_KEY` are omitted from nested
CLI environments. The selected CLI still uses its own saved login/configuration.

### Registry pacing

Some registries reject the default four checks per second. Lower the rate and
concurrency on either `run` or `resume` (0.2.0 and later):

```bash
uvx namehunt run --brief brief.md --provider codexcli --tlds cz \
  --rdap-rps 0.4 --rdap-concurrency 1
```

These are invocation options; supply them again when resuming. HTTP failures
remain unknown, never available. Slower pacing does not guarantee registry uptime.

## How availability works

- ✅ available — the registry's RDAP says the domain doesn't exist
- ❌ taken — RDAP returned a registration
- ❓ unknown — the check failed (timeout, rate limit, or a TLD without RDAP,
  like `.io`). Never treated as available.

RDAP "available" is a strong signal, not a guarantee: premium and reserved
names can still be unregisterable. Re-check at your registrar and run a
trademark search before you fall in love.

## Development

```bash
git clone https://github.com/Tarabcak/namehunt && cd namehunt && uv sync
uv run pytest -q                     # all network mocked; suite blocks real sockets
uv run ruff check . && uv run mypy src
```

Built spec-first with strict TDD by Claude Code; the spec, workflow rules and
hooks are in the repo ([namehunt-SPEC.md](namehunt-SPEC.md),
[CLAUDE.md](CLAUDE.md)). Every RDAP failure path is tested to map to
`unknown`, never `available`.
