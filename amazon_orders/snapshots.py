"""Never overwrite good data with worse data.

A crawl writes its pages into its own run folder (html/<source>/runs/<stamp>/) and its
JSON to <name>.partial.json. Only a run that finished cleanly is promoted: the current
page files are archived to html/<source>/previous/<stamp>/ (never deleted), the run's
pages take their place, and the partial JSON becomes the real one. Anything else leaves
the existing data untouched and says so.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PAGE_GLOB = "page-*.html"


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def new_run_dir(html_dir: Path) -> Path:
    run = html_dir / "runs" / stamp()
    run.mkdir(parents=True, exist_ok=True)
    return run


def partial_path(out: Path) -> Path:
    return out.with_name(out.stem + ".partial.json")


def promote(run_dir: Path, html_dir: Path, out: Path, doc: dict[str, Any]) -> Path | None:
    """Archive the current pages, move the run's pages in, write doc to out, drop the partial.
    Returns the archive folder (None when there was nothing to archive)."""
    current = sorted(html_dir.glob(PAGE_GLOB))
    archive = None
    if current:
        archive = html_dir / "previous" / stamp()
        archive.mkdir(parents=True, exist_ok=True)
        for f in current:
            shutil.move(str(f), archive / f.name)
    for f in sorted(run_dir.glob(PAGE_GLOB)):
        shutil.move(str(f), html_dir / f.name)
    run_dir.rmdir()
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    tmp.replace(out)
    partial_path(out).unlink(missing_ok=True)
    return archive
