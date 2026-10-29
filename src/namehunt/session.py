"""Session persistence: one JSON file per session under <home>/sessions/.

Session names become file names, so they are strictly validated; saves are
atomic (temp file + os.replace) so Ctrl-C can't corrupt state."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
SESSIONS_DIR = "sessions"


def state_home() -> Path:
    """State directory: $NAMEHUNT_HOME override (used by tests) or ~/.namehunt."""
    override = os.environ.get("NAMEHUNT_HOME")
    return Path(override) if override else Path.home() / ".namehunt"


@dataclass
class RoundRecord:
    timestamp: str
    candidates: list[str]  # NEW stems this round, post-dedupe
    results: dict[str, str]  # "stem.tld" -> "available" | "taken" | "unknown"


@dataclass
class Session:
    name: str
    brief_path: str
    style: str | None
    tlds: list[str]
    provider: str
    model: str
    rounds: list[RoundRecord] = field(default_factory=list)
    likes: list[str] = field(default_factory=list)
    dislikes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    all_generated: set[str] = field(default_factory=set)
    required_tlds: list[str] = field(default_factory=list)  # a name only wins with all of these


def session_path(name: str, base: Path | None = None) -> Path:
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"invalid session name {name!r}: use lowercase letters, digits, '-' and '_' only"
        )
    root = base if base is not None else state_home()
    return root / SESSIONS_DIR / f"{name}.json"


def save_session(sess: Session, base: Path | None = None) -> None:
    path = session_path(sess.name, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(sess)
    payload["all_generated"] = sorted(sess.all_generated)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.replace(tmp, path)  # atomic swap: a crash mid-write never corrupts the session


def load_session(name: str, base: Path | None = None) -> Session:
    path = session_path(name, base)
    data = json.loads(path.read_text())  # FileNotFoundError propagates to the caller
    try:
        return Session(
            name=str(data["name"]),
            brief_path=str(data["brief_path"]),
            style=None if data["style"] is None else str(data["style"]),
            tlds=[str(t) for t in data["tlds"]],
            provider=str(data["provider"]),
            model=str(data["model"]),
            rounds=[
                RoundRecord(
                    timestamp=str(r["timestamp"]),
                    candidates=[str(c) for c in r["candidates"]],
                    results={str(k): str(v) for k, v in r["results"].items()},
                )
                for r in data["rounds"]
            ],
            likes=[str(x) for x in data["likes"]],
            dislikes=[str(x) for x in data["dislikes"]],
            notes=[str(x) for x in data["notes"]],
            all_generated={str(x) for x in data["all_generated"]},
            required_tlds=[str(t) for t in data.get("required_tlds", [])],  # absent before 0.2
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"corrupt session file: {path}") from exc
