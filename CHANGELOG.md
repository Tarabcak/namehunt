# Changelog

## 0.2.0 (29 October 2026)

- `--require-tlds com`: a name only counts as a win, in the next prompt and at
  the top of the export, when every required TLD is free. Before, any free TLD
  made a name a win, so a TLD that is almost always free (such as .design) hid
  that the .com was gone.
- `run` on an existing session uses the session's saved provider and model
  instead of falling back to the API-key provider. `--provider`, `--model`,
  `--style` and `--require-tlds` replace the saved values, and `resume` keeps
  them. Different `--tlds` are an error instead of being ignored.
- The feedback prompt shows its command letters again (`[a]nother`, `[l]ike`,
  and so on); rich had been reading them as markup.
- Hints print `namehunt resume ...` and `namehunt export ...` rather than
  `uv run namehunt ...`, which only works inside a checkout.
- A failing `claude` CLI reports the end of its error output, where the error
  is, rather than its start-up banner.
- First PyPI release with `--rdap-rps`, `--rdap-concurrency` and the Codex
  reliability fixes from pull request 1, which landed after 0.1.2.

## 0.1.2 (10 August 2026)

- Earlier releases: see the git history.
