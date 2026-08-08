"""Shortlist export: CSV / JSON / Markdown, sorted by promise."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass

from namehunt.availability import Availability
from namehunt.session import Session

CAVEAT = (
    "⚠️ RDAP 'available' is a strong signal, not a guarantee — premium/reserved names "
    "can still be unregisterable. Re-check at your registrar before buying, and run a "
    "trademark search."
)


@dataclass(frozen=True)
class ExportRow:
    stem: str
    statuses: dict[str, str]  # tld -> status value
    available_count: int
    liked: bool


def _rows(sess: Session) -> list[ExportRow]:
    merged: dict[str, str] = {}
    for record in sess.rounds:
        merged.update(record.results)
    stems = {domain.split(".", 1)[0] for domain in merged}
    rows = []
    for stem in stems:
        statuses = {
            tld: merged.get(f"{stem}.{tld}", Availability.UNKNOWN.value) for tld in sess.tlds
        }
        rows.append(
            ExportRow(
                stem=stem,
                statuses=statuses,
                available_count=sum(
                    1 for s in statuses.values() if s == Availability.AVAILABLE.value
                ),
                liked=stem in sess.likes,
            )
        )
    # Spec §1.6: available-TLD count descending, then liked first, then name.
    rows.sort(key=lambda r: (-r.available_count, not r.liked, r.stem))
    return rows


def render_export(sess: Session, fmt: str) -> str:
    rows = _rows(sess)
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["name", *sess.tlds, "available_tlds", "liked"])
        for row in rows:
            writer.writerow(
                [
                    row.stem,
                    *(row.statuses[tld] for tld in sess.tlds),
                    row.available_count,
                    "yes" if row.liked else "no",
                ]
            )
        return buf.getvalue()
    if fmt == "json":
        return json.dumps(
            [
                {
                    "name": row.stem,
                    "domains": row.statuses,
                    "available_tlds": row.available_count,
                    "liked": row.liked,
                }
                for row in rows
            ],
            indent=2,
        )
    if fmt == "md":
        header = f"| name | {' | '.join('.' + t for t in sess.tlds)} | available | liked |"
        divider = "|" + "---|" * (len(sess.tlds) + 3)
        lines = [f"# namehunt shortlist — {sess.name}", "", header, divider]
        for row in rows:
            cells = " | ".join(row.statuses[tld] for tld in sess.tlds)
            liked = "★" if row.liked else ""
            lines.append(f"| {row.stem} | {cells} | {row.available_count} | {liked} |")
        lines += ["", f"> {CAVEAT}", ""]
        return "\n".join(lines)
    raise ValueError(f"unknown export format {fmt!r}: expected csv, json or md")
