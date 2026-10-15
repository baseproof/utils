"""Offline checks of fetch_transactions.py: the row parser, and how a run is saved on top of the
last crawl. Temporary folders only -- no Amazon, no real files.

    uv run --with pytest --with playwright pytest -q test_fetch_transactions.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import fetch_transactions as F


def test_pending_is_a_status_not_the_merchant() -> None:
    r = F.normalise_row({"date": "September 23, 2026", "instrument": "Mastercard ****6689", "amount": "-$69.76",
                         "parts": ["Mastercard ****6689-$69.76", "Pending", "Order #114-6081006-8185829", "AMZN Mktp US"],
                         "section": "In Progress"})
    assert r["issues"] == [] and r["pending"] and r["merchant"] == "AMZN Mktp US"
    assert (r["network"], r["last4"], r["amount"], r["direction"], r["orderRefs"]) == \
        ("Mastercard", "6689", 69.76, "charge", ["114-6081006-8185829"])


def row(date: str, amount: float, ref: str, pending: bool = False) -> dict[str, Any]:
    return {"chargeDate": date, "instrumentLabel": "Visa ****1234", "amount": amount, "direction": "charge",
            "orderRefs": [ref], "pending": pending, "issues": []}


def ledger(rows: list[dict[str, Any]], end_reached: bool, updated: str) -> dict[str, Any]:
    dates = [r["chargeDate"] for r in rows]
    return {"updatedAt": updated, "end": {"reached": end_reached}, "coverage": {"newest": max(dates), "oldest": min(dates)},
            "pageCount": 1, "rowCount": len(rows), "rowsWithIssues": [], "pages": [{"page": 1, "rows": rows}],
            "retainedRows": []}


A, B = row("2026-09-22", 10.0, "A"), row("2026-09-10", 20.0, "B")
C = row("2026-08-28", 30.0, "C")                 # twice in the old crawl: a real double charge
D, E = row("2026-08-01", 40.0, "D"), row("2021-09-07", 50.0, "E")
P = row("2026-09-21", 60.0, "P", pending=True)   # pending in the old crawl
N = row("2026-09-23", 70.0, "N", pending=True)   # new, pending


def setup(tmp: Path, old: dict[str, Any] | None, new: dict[str, Any]) -> argparse.Namespace:
    args = argparse.Namespace(out=tmp / "transactions.json", html_dir=tmp / "html")
    if old:
        args.out.write_text(json.dumps(old))
    F.partial_path(args.out).write_text(json.dumps(new))
    args.html_dir.mkdir()
    (args.html_dir / "page-001.html").write_text("old")
    return args


def run_dir(args: argparse.Namespace) -> Path:
    d = args.html_dir / "runs" / "now"
    d.mkdir(parents=True)
    (d / "page-001.html").write_text("new")
    return d


def test_an_update_keeps_every_older_row_and_only_reads_new_pages(tmp_path: Path) -> None:
    args = setup(tmp_path, ledger([A, B, C, C, D, E, P], True, "then"), ledger([N, A, B, C], False, "now"))
    F.finish(args, run_dir(args), at_start=True)
    doc = json.loads(args.out.read_text())
    assert [r["orderRefs"][0] for r in doc["pages"][0]["rows"]] == ["N", "A", "B", "C"]
    # the second C (a double charge), D and E are carried; P (pending, may have posted since) is not
    assert [(r["orderRefs"][0], r["retainedFrom"]) for r in doc["retainedRows"]] == [("C", "then"), ("D", "then"), ("E", "then")]
    assert doc["end"]["reached"] and "carried" in doc["end"]["via"]            # still a complete history
    assert (args.html_dir / "page-001.html").read_text() == "new" and not F.partial_path(args.out).exists()


def test_an_update_needs_a_complete_crawl_to_add_to(tmp_path: Path) -> None:
    old = ledger([A, B, C, D], False, "then")
    args = setup(tmp_path, old, ledger([N, A, B, C], False, "now"))
    F.finish(args, run_dir(args), at_start=True)
    assert json.loads(args.out.read_text()) == old and F.partial_path(args.out).exists()


def test_an_update_that_would_lose_rows_is_refused(tmp_path: Path) -> None:
    old = ledger([A, B, C, C, D, E], True, "then")
    args = setup(tmp_path, old, ledger([N, C], False, "now"))                   # A and B vanished: suspicious
    F.finish(args, run_dir(args), at_start=True)
    assert json.loads(args.out.read_text()) == old and F.partial_path(args.out).exists()


def test_a_run_that_stops_early_without_at_start_is_still_refused(tmp_path: Path) -> None:
    old = ledger([A, B, C, D, E], True, "then")
    args = setup(tmp_path, old, ledger([N, A, B, C], False, "now"))
    F.finish(args, run_dir(args))
    assert json.loads(args.out.read_text()) == old
