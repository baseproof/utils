#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright>=1.47"]
# ///
"""Crawl Amazon Gift Card Balance activity (amazon.com/gc/balance) back to a start date.

The payments ledger (fetch_transactions.py) shows gift card *spending* but not where the
balance came from. This page has both: cards added, reloads, refunds into the balance,
hold releases, and every application to an order -- with a closing balance per row.

Integrity check, no label lists: rows are newest-first, so for consecutive rows
    closing[newer] == closing[older] + amount[newer]
across page boundaries too. A missed, duplicated or misread row breaks the chain.

Pagination is a plain "Next" link carrying the position (next=...), so --resume really
continues from the last saved page. Uses its OWN tab in your running Chrome (CDP), a
random 3-5 s gap between pages, and writes giftcards.json + html/giftcards/page-NNN.html
after every page. Stops at the first structural problem so the parser can be fixed.

Usage:
  uv run fetch_giftcards.py --start 2000-01-01
  uv run fetch_giftcards.py --start 2000-01-01 --resume      # continue after the last saved page
  uv run fetch_giftcards.py --start 2000-01-01 --reparse     # re-extract saved pages offline
  uv run fetch_giftcards.py --start 2000-01-01 --accept-vocab
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from snapshots import new_run_dir, partial_path, promote
from unaligned import write_report

HERE = Path(__file__).resolve().parent
BALANCE_URL = "https://www.amazon.com/gc/balance"
TABLE_SEL = "table#gc-balance-ledger-table"
VOCAB_PATH = HERE / "giftcard_vocab.json"

EXTRACT_JS = r"""
() => {
  const t = e => (e ? e.textContent.replace(/\s+/g, ' ').trim() : null);
  const table = document.querySelector('table#gc-balance-ledger-table');
  const head = table ? [...table.querySelectorAll('th')].map(t) : [];
  const rows = table ? [...table.querySelectorAll('tr')].filter(r => r.querySelector('td')).map(r => {
    const c = [...r.querySelectorAll('td')];
    const desc = c[1];
    return {
      cells: c.map(t),
      html: r.outerHTML,
      label: desc ? t(desc.querySelector('span')) : null,
      links: desc ? [...desc.querySelectorAll('a')].map(a => ({text: t(a), href: a.getAttribute('href')})) : [],
      detailLines: desc ? desc.innerText.split('\n').map(s => s.trim()).filter(Boolean) : [],
    };
  }) : [];
  const next = document.querySelector('li.a-last:not(.a-disabled) a[href]');
  const bm = document.body.innerText.match(/Your Gift Card Balance:\s*(-?\$[\d,]+\.\d{2})/);
  const balance = bm ? bm[1] : null;
  return {head, rows, nextHref: next ? next.getAttribute('href') : null, balanceText: balance, url: location.href};
}
"""

DATE_FMT = "%B %d, %Y"
MONEY_RE = re.compile(r"^(?P<sign>-)?\$(?P<value>[\d,]+\.\d{2})$")
ORDER_ID_RE = re.compile(r"orderID=([A-Za-z0-9-]+)")
EXPECTED_HEAD = ["Date", "Description", "Amount", "Closing balance"]


def issue(kind: str, code: str, detail: str) -> dict[str, str]:
    return {"kind": kind, "code": code, "detail": detail}


def money(s: str | None) -> float | None:
    m = MONEY_RE.match((s or "").strip())
    if not m:
        return None
    v = float(m["value"].replace(",", ""))
    return -v if m["sign"] else v


def normalise_row(raw: dict[str, Any]) -> dict[str, Any]:
    issues = []
    cells = raw["cells"]
    if len(cells) != 4:
        issues.append(issue("structure", "row_shape", f"{len(cells)} cells: {cells}"))
    d = amount = closing = None
    try:
        d = datetime.strptime(cells[0], DATE_FMT).date().isoformat()
    except (ValueError, IndexError):
        issues.append(issue("structure", "date", repr(cells[:1])))
    if len(cells) > 2 and (amount := money(cells[2])) is None:
        issues.append(issue("structure", "amount", repr(cells[2])))
    if len(cells) > 3 and (closing := money(cells[3])) is None:
        issues.append(issue("structure", "closing_balance", repr(cells[3])))
    if not raw["label"]:
        issues.append(issue("structure", "label", f"no description label in {cells[1:2]}"))
    order_ids = [m.group(1) for l in raw["links"] if (m := ORDER_ID_RE.search(l["href"] or ""))]
    # Anything in the description besides the label and order links (e.g. masked claim code).
    extra = [ln for ln in raw["detailLines"] if ln != raw["label"] and ln not in order_ids]
    return {
        "date": d,
        "label": raw["label"],
        "amount": amount,
        "direction": None if amount is None else ("credit" if amount > 0 else "debit"),
        "closingBalance": closing,
        "orderIds": order_ids,
        "detail": " ".join(extra) or None,
        "issues": issues,
        "raw": raw,
    }


def check_chain(rows: list[dict[str, Any]]) -> None:
    """Newest-first: closing[i] must equal closing[i+1] + amount[i]. Flags the newer row."""
    for newer, older in zip(rows, rows[1:]):
        if None in (newer["closingBalance"], older["closingBalance"], newer["amount"]):
            continue
        expect = round(older["closingBalance"] + newer["amount"], 2)
        if abs(expect - newer["closingBalance"]) > 0.005:
            newer["issues"].append(issue("structure", "balance_chain",
                f"{older['closingBalance']} + {newer['amount']} = {expect}, page shows {newer['closingBalance']} "
                f"(row missing/misread between {older['date']} and {newer['date']}?)"))


def extract(page: Page, page_no: int) -> dict[str, Any]:
    data = page.evaluate(EXTRACT_JS)
    rows = [normalise_row(r) for r in data["rows"]]
    for i, r in enumerate(rows):
        r["page"], r["position"] = page_no, i
    head_issue = [] if data["head"] == EXPECTED_HEAD else [issue("structure", "table_header", repr(data["head"]))]
    return {"page": page_no, "url": data["url"], "nextHref": data["nextHref"], "balanceText": data["balanceText"],
            "headIssues": head_issue, "rows": rows}


def check_page_balance(pages: list[dict[str, Any]]) -> None:
    """The newest row's closing balance must equal the balance Amazon shows on page 1."""
    if not pages or not pages[0]["rows"]:
        return
    first = pages[0]["rows"][0]
    first["issues"] = [i for i in first["issues"] if i["code"] != "page_balance"]
    shown = money(pages[0].get("balanceText"))
    if shown is None:
        first["issues"].append(issue("structure", "page_balance", f"could not read 'Your Gift Card Balance' ({pages[0].get('balanceText')!r})"))
    elif first["closingBalance"] is not None and abs(shown - first["closingBalance"]) > 0.005:
        first["issues"].append(issue("structure", "page_balance", f"page shows balance {shown}, newest row closes at {first['closingBalance']}"))


def all_rows(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for p in pages for r in p["rows"]]


def recheck(pages: list[dict[str, Any]]) -> None:
    """Re-run the chain over every saved row (drops stale chain flags first)."""
    rows = all_rows(pages)
    for r in rows:
        r["issues"] = [i for i in r["issues"] if i["code"] != "balance_chain"]
    check_chain(rows)


def vocab_key(r: dict[str, Any]) -> str:
    """Description type = label + shape (sign, has order link, has extra detail)."""
    return f"{r['label']} [{r['direction']}{', order' if r['orderIds'] else ''}{', detail' if r['detail'] else ''}]"


def vocabulary(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    vocab: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = vocab_key(r)
        e = vocab.setdefault(key, {"count": 0, "example": None})
        e["count"] += 1
        e["example"] = e["example"] or " | ".join(r["raw"]["cells"])
    return vocab


def save(out: Path, pages: list[dict[str, Any]], start: date, html_dir: Path, run_dir: Path | None = None) -> None:
    """html_dir: where this data's page files live (a run folder while a crawl is in progress)."""
    rows = all_rows(pages)
    check_page_balance(pages)
    vocab = vocabulary(rows)
    reviewed = set(json.loads(VOCAB_PATH.read_text())) if VOCAB_PATH.exists() else set()
    counts = write_report(
        "giftcards", rows,
        values_of=lambda r: {"descriptionType": [vocab_key(r)]},
        reviewed={"descriptionType": reviewed},
        page_file=lambda r: str((html_dir / f"page-{r['page']:03d}.html").relative_to(HERE)),
        row_text=lambda r: " | ".join(r["raw"]["cells"]),
    )
    print(f"  debug/giftcards-unaligned.json: {counts['structural']} structural, {counts['unreviewedValues']} unreviewed types", flush=True)
    doc = {
        "start": start.isoformat(),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "runDir": str(run_dir.relative_to(HERE)) if run_dir else None,
        "complete": bool(pages) and not pages[-1]["nextHref"],
        "pageCount": len(pages),
        "rowCount": len(rows),
        "rowsWithIssues": [{"page": r["page"], "position": r["position"], "issues": r["issues"]} for r in rows if r["issues"]]
        + [{"page": p["page"], "position": None, "issues": p["headIssues"]} for p in pages if p["headIssues"]],
        "vocabulary": vocab,
        "newVocabulary": {k: v for k, v in vocab.items() if k not in reviewed},
        "pages": pages,
    }
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    tmp.replace(out)


def needs_human(page: Page) -> bool:
    u = page.url
    return "/ap/signin" in u or "/errors/validateCaptcha" in u or page.locator("form[action*='validateCaptcha']").count() > 0


def load(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    if needs_human(page):
        sys.exit("Amazon wants sign-in/CAPTCHA: resolve it in the crawler's tab, then re-run with --resume (pages so far are saved)")
    try:
        page.wait_for_selector(TABLE_SEL, timeout=30_000)
    except PWTimeout:
        sys.exit(f"no gift card activity table at {page.url} after 30s (pages so far are saved)")


def page_flags(p: dict[str, Any]) -> list[str]:
    out = [f"[header] {i['code']}: {i['detail']}" for i in p["headIssues"]]
    for r in p["rows"]:
        out += [f"#{r['position']} {i['code']}: {i['detail']}\n       row: " + " | ".join(r["raw"]["cells"]) for i in r["issues"]]
    return out


def crawl(args: argparse.Namespace) -> None:
    """Writes to giftcards.partial.json + html/giftcards/runs/<stamp>/; giftcards.json is only
    replaced when a run reaches the last page with an intact balance chain."""
    args.html_dir.mkdir(parents=True, exist_ok=True)
    partial = partial_path(args.out)
    pages: list[dict[str, Any]] = []
    url = BALANCE_URL
    if args.resume:
        if not partial.exists():
            sys.exit(f"nothing to resume: no {partial.name} (the last run was promoted or never started)")
        state = json.loads(partial.read_text())
        pages, run_dir = state["pages"], HERE / state["runDir"]
        if not pages[-1]["nextHref"]:
            sys.exit(f"page {pages[-1]['page']} has no Next link: already at the end")
        url = urljoin(BALANCE_URL, pages[-1]["nextHref"])
        print(f"resuming {run_dir.relative_to(HERE)} after page {pages[-1]['page']}")
    else:
        run_dir = new_run_dir(args.html_dir)
    print(f"writing to {partial.name} and {run_dir.relative_to(HERE)}/ ; {args.out.name} is only replaced by a complete, clean run")
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(args.cdp)
        page = browser.contexts[0].new_page()  # own tab in the same window; never hijack the user's tab
        try:
            while True:
                load(page, url)
                page_no = len(pages) + 1
                (run_dir / f"page-{page_no:03d}.html").write_text(page.content())
                p = extract(page, page_no)
                pages.append(p)
                recheck(pages)
                save(partial, pages, args.start, run_dir, run_dir)
                flags = page_flags(p)
                dates = [r["date"] for r in p["rows"] if r["date"]]
                print(f"page {page_no}: {len(p['rows'])} rows, {max(dates, default='-')} .. {min(dates, default='-')}, {len(flags)} flagged", flush=True)
                if flags and not args.no_stop:
                    print("\n".join("  FLAG " + f for f in flags))
                    print(f"\nStopped ({args.out.name} untouched; this run is in {partial.name}). "
                          "Fix the parser, check with --reparse, then --resume.")
                    sys.exit(2)
                if not p["nextHref"]:
                    print("no Next link: reached the end of gift card activity")
                    finish(args, run_dir)
                    break
                if dates and min(dates) < args.start.isoformat():
                    print(f"passed start date {args.start} (partial history: not promoted)")
                    break
                time.sleep(random.uniform(args.min_gap, args.max_gap))
                url = urljoin(BALANCE_URL, p["nextHref"])
        finally:
            page.close()


def finish(args: argparse.Namespace, run_dir: Path) -> None:
    partial = partial_path(args.out)
    doc = json.loads(partial.read_text())
    prev_rows = json.loads(args.out.read_text())["rowCount"] if args.out.exists() else 0
    if doc["rowsWithIssues"]:
        print(f"NOT promoted (structural flags): {args.out.name} kept as is; this run is in {partial.name}")
        return
    if doc["rowCount"] < prev_rows:
        print(f"NOT promoted: {doc['rowCount']} rows < {prev_rows} in {args.out.name} (gift card history never shrinks); kept as is")
        return
    doc["runDir"] = None
    archive = promote(run_dir, args.html_dir, args.out, doc)
    print(f"promoted: {args.out.name} = {doc['rowCount']} rows" + (f"; previous pages archived to {archive.relative_to(HERE)}" if archive else ""))


def reparse(args: argparse.Namespace) -> None:
    """Re-extract offline. If a crawl is in progress, its run folder -> the partial file;
    otherwise the promoted pages -> giftcards.json."""
    partial = partial_path(args.out)
    run_dir = HERE / json.loads(partial.read_text())["runDir"] if partial.exists() else None
    src, dest = (run_dir, partial) if run_dir else (args.html_dir, args.out)
    files = sorted(src.glob("page-[0-9][0-9][0-9].html"))
    pages = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True)
        c = b.new_context(java_script_enabled=False)
        c.route("**/*", lambda r: r.abort())
        page = c.new_page()
        for f in files:
            page.set_content(f.read_text(), wait_until="domcontentloaded")
            pages.append(extract(page, int(f.stem.split("-")[1])))
        b.close()
    recheck(pages)
    save(dest, pages, args.start, src, run_dir)
    print(f"re-parsed {len(files)} saved pages from {src.relative_to(HERE)} -> {dest.name}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True, type=date.fromisoformat, help="crawl back until activity older than this (YYYY-MM-DD)")
    ap.add_argument("--out", type=Path, default=HERE / "giftcards.json")
    ap.add_argument("--html-dir", type=Path, default=HERE / "html" / "giftcards")
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument("--min-gap", type=float, default=3.0)
    ap.add_argument("--max-gap", type=float, default=5.0)
    ap.add_argument("--resume", action="store_true", help="continue after the last saved page (follows its saved Next link)")
    ap.add_argument("--reparse", action="store_true", help="re-extract saved pages offline with the current rules")
    ap.add_argument("--no-stop", action="store_true", help="keep going past flagged rows")
    ap.add_argument("--accept-vocab", action="store_true", help="mark every description type seen so far as reviewed")
    args = ap.parse_args()
    if args.accept_vocab:
        vocab = json.loads(args.out.read_text())["vocabulary"]
        VOCAB_PATH.write_text(json.dumps(sorted(vocab), indent=2, ensure_ascii=False))
        print(f"accepted {len(vocab)} description types -> {VOCAB_PATH}")
        return
    reparse(args) if args.reparse else crawl(args)
    doc = json.loads(args.out.read_text())
    print(f"{doc['rowCount']} rows on {doc['pageCount']} pages, {len(doc['rowsWithIssues'])} structural flags -> {args.out}")
    if doc["newVocabulary"]:
        print("\nDescription types to review (accept with --accept-vocab):")
        for k, v in sorted(doc["newVocabulary"].items(), key=lambda kv: -kv[1]["count"]):
            print(f"  {k:<75} x{v['count']:<4} e.g. {v['example'][:90]}")


if __name__ == "__main__":
    main()
