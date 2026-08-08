"""Command-line interface: argument parsing and the interactive naming loop."""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import httpx
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from namehunt.availability import DomainCheck, check_domains, doh_prefilter
from namehunt.export import CAVEAT, render_export
from namehunt.llm import (
    DEFAULT_MODELS,
    MissingKeyError,
    Provider,
    context_from_session,
    generate_candidates,
    make_provider,
)
from namehunt.session import RoundRecord, Session, load_session, save_session, session_path

STATUS_ICONS = {"available": "✅", "taken": "❌", "unknown": "❓"}
DEFAULT_TLDS = "com,ai,io"
PROMPT = "[a]nother round  [l]ike <numbers>  [d]islike <numbers>  [n]ote <text>  [q]uit & export"
_TLD_RE = re.compile(r"[a-z0-9-]{2,63}\Z")

Checker = Callable[[Sequence[str], Sequence[str]], dict[str, DomainCheck]]


def run_round(
    sess: Session,
    brief_text: str,
    provider: Provider,
    checker: Checker,
    *,
    batch: int = 20,
    debug: bool = False,
) -> Session:
    """One generate → dedupe → check cycle. Returns a NEW session; never mutates."""
    ctx = context_from_session(sess, brief_text, batch)
    names = generate_candidates(ctx, provider, debug=debug)
    checks = checker(names, sess.tlds) if names else {}
    record = RoundRecord(
        timestamp=datetime.now(UTC).isoformat(),
        candidates=list(names),
        results={domain: check.status.value for domain, check in checks.items()},
    )
    return replace(
        sess,
        rounds=[*sess.rounds, record],
        all_generated=sess.all_generated | set(names),
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    console = Console()
    errors = Console(stderr=True)
    try:
        if args.command == "run":
            return _cmd_run(args, console, errors)
        if args.command == "resume":
            return _cmd_resume(args, console, errors)
        return _cmd_export(args, console)
    except MissingKeyError as exc:
        errors.print(str(exc))
        return 2
    except ValueError as exc:
        errors.print(f"error: {exc}")
        return 2
    except FileNotFoundError as exc:
        errors.print(f"error: file not found: {exc.filename or exc}")
        return 1


def _cmd_run(args: argparse.Namespace, console: Console, errors: Console) -> int:
    tlds = _parse_tlds(args.tlds)
    _validate_counts(args)
    provider_name = args.provider or os.environ.get("NAMEHUNT_PROVIDER") or "claude"
    if provider_name not in DEFAULT_MODELS:
        raise ValueError(
            f"unknown provider {provider_name!r}: expected 'claude', 'claudecode' or 'openai'"
        )
    model = args.model or DEFAULT_MODELS[provider_name]
    if session_path(args.session).exists():
        sess = load_session(args.session)
        if args.brief:
            sess = replace(sess, brief_path=args.brief)
        console.print(f"continuing session [bold]{sess.name}[/] ({len(sess.rounds)} rounds so far)")
    else:
        if not args.brief:
            errors.print("error: --brief is required on the first run of a session")
            return 2
        sess = Session(
            name=args.session,
            brief_path=args.brief,
            style=args.style,
            tlds=tlds,
            provider=provider_name,
            model=model,
        )
    brief_text = Path(sess.brief_path).read_text().strip()
    provider = make_provider(provider_name, model)
    return _loop(sess, brief_text, provider, _build_checker(args.fast_prefilter), args, console)


def _cmd_resume(args: argparse.Namespace, console: Console, errors: Console) -> int:
    if not session_path(args.session).exists():
        errors.print(
            f"error: no session named {args.session!r} - start one with: "
            f"uv run namehunt run --brief <file> --session {args.session}"
        )
        return 1
    _validate_counts(args)
    sess = load_session(args.session)
    brief_text = Path(sess.brief_path).read_text().strip()
    provider = make_provider(sess.provider, sess.model)
    console.print(f"resuming session [bold]{sess.name}[/] ({len(sess.rounds)} rounds so far)")
    return _loop(sess, brief_text, provider, _build_checker(args.fast_prefilter), args, console)


def _cmd_export(args: argparse.Namespace, console: Console) -> int:
    sess = load_session(args.session)
    out = Path(f"namehunt-{sess.name}.{args.format}")
    out.write_text(render_export(sess, args.format))
    console.print(f"wrote {out}")
    console.print(CAVEAT)
    return 0


def _loop(
    sess: Session,
    brief_text: str,
    provider: Provider,
    checker: Checker,
    args: argparse.Namespace,
    console: Console,
) -> int:
    try:
        while True:
            console.print()
            with console.status(f"round {len(sess.rounds) + 1}: generating and checking..."):
                sess = run_round(
                    sess, brief_text, provider, checker, batch=args.batch, debug=args.debug
                )
            save_session(sess)
            _render_round(console, sess)
            if args.rounds > 0:
                if len(sess.rounds) >= args.rounds:
                    break
                continue
            sess, want_quit = _gather_feedback(console, sess)
            if want_quit:
                break
    except KeyboardInterrupt:
        save_session(sess)
        console.print()
        console.print(f"session saved - resume with: uv run namehunt resume --session {sess.name}")
        return 0
    except Exception as exc:  # provider/CLI failure mid-round: keep state, fail cleanly
        save_session(sess)
        console.print(f"[red]error:[/] {escape(str(exc))}")
        console.print(f"session saved - resume with: uv run namehunt resume --session {sess.name}")
        return 1
    save_session(sess)
    console.print(CAVEAT)
    console.print(f"export with: uv run namehunt export --session {sess.name} --format md")
    return 0


def _gather_feedback(console: Console, sess: Session) -> tuple[Session, bool]:
    """Prompt until [a]nother round or [q]uit; likes/dislikes/notes update the session."""
    last = sess.rounds[-1]
    while True:
        try:
            line = console.input(f"{PROMPT} > ").strip()
        except EOFError:
            return sess, True
        if not line:
            continue
        cmd, _, rest = line.partition(" ")
        cmd = cmd.lower()
        if cmd in ("a", "another"):
            return sess, False
        if cmd in ("q", "quit"):
            return sess, True
        if cmd in ("l", "d"):
            stems = _stems_from_numbers(rest, last.candidates, console)
            if not stems:
                continue
            if cmd == "l":
                sess = replace(sess, likes=_extend_unique(sess.likes, stems))
            else:
                sess = replace(sess, dislikes=_extend_unique(sess.dislikes, stems))
            save_session(sess)
            console.print(f"{'liked' if cmd == 'l' else 'disliked'}: {', '.join(stems)}")
        elif cmd in ("n", "note") and rest.strip():
            sess = replace(sess, notes=[*sess.notes, rest.strip()])
            save_session(sess)
            console.print("note saved")
        else:
            console.print(f"commands: {PROMPT}")


def _stems_from_numbers(rest: str, candidates: list[str], console: Console) -> list[str]:
    stems = []
    for token in rest.split():
        if token.isdigit() and 1 <= int(token) <= len(candidates):
            stems.append(candidates[int(token) - 1])
        else:
            console.print(f"[yellow]ignoring[/] {token!r}: expected a table row number")
    return stems


def _extend_unique(existing: list[str], new: list[str]) -> list[str]:
    return [*existing, *[s for s in new if s not in existing]]


def _render_round(console: Console, sess: Session) -> None:
    record = sess.rounds[-1]
    if not record.candidates:
        console.print("[yellow]no new candidates this round[/] - try again or steer with a note")
        return
    table = Table(title=f"round {len(sess.rounds)} — {len(record.candidates)} new candidates")
    table.add_column("#", justify="right", style="dim")
    table.add_column("name")
    for tld in sess.tlds:
        table.add_column(f".{tld}", justify="center")
    for i, stem in enumerate(record.candidates, start=1):
        cells = [
            STATUS_ICONS.get(record.results.get(f"{stem}.{tld}", "unknown"), "❓")
            for tld in sess.tlds
        ]
        table.add_row(str(i), stem, *cells)
    console.print(table)
    console.print("[dim]✅ available   ❌ taken   ❓ unknown (unresolved, never 'available')[/]")


def _build_checker(fast_prefilter: bool) -> Checker:
    if not fast_prefilter:
        return check_domains
    client = httpx.Client(timeout=5.0)
    return partial(check_domains, prefilter=partial(doh_prefilter, client=client))


def _parse_tlds(raw: str) -> list[str]:
    tlds = [t.strip().lstrip(".").lower() for t in raw.split(",") if t.strip()]
    if not tlds:
        raise ValueError("at least one TLD is required")
    for tld in tlds:
        if not _TLD_RE.fullmatch(tld):
            raise ValueError(f"invalid TLD {tld!r}")
    return tlds


def _validate_counts(args: argparse.Namespace) -> None:
    if args.batch < 1:
        raise ValueError("--batch must be at least 1")
    if args.rounds < 0:
        raise ValueError("--rounds must be 0 (interactive) or a positive count")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="namehunt", description="closed-loop startup naming with real domain checks"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    loop_flags = argparse.ArgumentParser(add_help=False)
    loop_flags.add_argument("--batch", type=int, default=20, help="names per round (default 20)")
    loop_flags.add_argument("--rounds", type=int, default=0, help="0 = interactive; N autonomous")
    loop_flags.add_argument("--fast-prefilter", action="store_true", help="DoH pre-filter first")
    loop_flags.add_argument("--debug", action="store_true", help="print the outgoing LLM prompt")

    run = sub.add_parser("run", parents=[loop_flags], help="start (or continue) a session")
    run.add_argument("--brief", help="path to a markdown/text brief (required on first run)")
    run.add_argument("--tlds", default=DEFAULT_TLDS, help=f"comma-separated ({DEFAULT_TLDS})")
    run.add_argument(
        "--provider", choices=["claude", "claudecode", "openai"], help="or $NAMEHUNT_PROVIDER"
    )
    run.add_argument("--model", help="model override")
    run.add_argument("--style", help='style steering, e.g. "short, mythic, 2 syllables"')
    run.add_argument("--session", default="default", help="session name")

    res = sub.add_parser("resume", parents=[loop_flags], help="resume with full history")
    res.add_argument("--session", required=True)

    exp = sub.add_parser("export", help="export a session shortlist to a file")
    exp.add_argument("--session", required=True)
    exp.add_argument("--format", required=True, choices=["csv", "json", "md"])
    return parser
