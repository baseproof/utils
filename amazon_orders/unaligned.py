"""Shared "save for debug" report for the Amazon crawlers.

A row is *unaligned* when it has a structural issue, or when any of its vocabulary
values (payment label, merchant, description type, ...) is not in the reviewed file.
Every unaligned value is written to debug/<source>-unaligned.json, grouped by value,
with each occurrence's saved page file, row text and the row's exact HTML -- enough to
investigate offline without going back to Amazon. Regenerated on every save/reparse,
so accepting a value (or fixing the parser) removes it from the report.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

DEBUG_DIR = Path(__file__).resolve().parent / "debug"
HTML_PER_VALUE = 20


def write_report(
    source: str,
    rows: list[dict[str, Any]],
    values_of: Callable[[dict[str, Any]], dict[str, list[str]]],
    reviewed: dict[str, set[str]],
    page_file: Callable[[dict[str, Any]], str],
    row_text: Callable[[dict[str, Any]], str],
) -> dict[str, int]:
    """values_of(row) -> {field: [values]}; reviewed -> {field: {accepted values}}."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    structural = []
    for r in rows:
        occ = {"page": r["page"], "position": r["position"], "pageFile": page_file(r),
               "text": row_text(r), "html": r["raw"].get("html")}
        for field, vals in values_of(r).items():
            for v in vals:
                if v is not None and v not in reviewed.get(field, set()):
                    g = groups.setdefault((field, v), {"field": field, "value": v, "reason": "not in reviewed vocabulary",
                                                       "count": 0, "occurrences": []})
                    g["count"] += 1
                    # every occurrence is listed; row HTML only on the first few (the rest is in pageFile)
                    g["occurrences"].append(occ if g["count"] <= HTML_PER_VALUE else {k: v for k, v in occ.items() if k != "html"})
        if r["issues"]:
            structural.append({**occ, "issues": r["issues"]})
    DEBUG_DIR.mkdir(exist_ok=True)
    out = DEBUG_DIR / f"{source}-unaligned.json"
    doc = {
        "source": source,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "structural": structural,
        "unreviewedValues": sorted(groups.values(), key=lambda g: (g["field"], -g["count"])),
    }
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    tmp.replace(out)
    return {"structural": len(structural), "unreviewedValues": len(groups)}
