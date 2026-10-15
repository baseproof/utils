#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright>=1.47"]
# ///
"""Crawl Amazon's payment ledger (Your Payments > Transactions) back to a start date.

This is Amazon's record of every charge/refund: card, amount, order reference and
merchant. Per-order "View related transactions" misses Whole Foods (those rows carry
a store transaction UUID, not the order id), so the only complete route is paging
through the whole list via "Next Page" -- a form POST, so it has to be clicked.

Uses its OWN tab in your already-running Chrome (CDP), a random 3-5 s gap between pages,
and writes transactions.json + html/transactions/page-NNN.html after every page.
Each run re-crawls from the newest page (the list has no stable ids to resume from). A run
that reaches the end of the ledger replaces the file; a run that passes its --start date is
saved on top of the last complete crawl (its older rows are carried forward), so an update
only reads the new pages.

Usage:
  uv run fetch_transactions.py --start 2026-08-01
  uv run fetch_transactions.py --start 2026-08-01 --reparse   # re-extract saved pages offline
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import random
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from snapshots import new_run_dir, partial_path, promote
from unaligned import write_report

HERE = Path(__file__).resolve().parent
LEDGER_URL = "https://www.amazon.com/cpe/yourpayments/transactions"
NEXT_SEL = 'input[name^="ppw-widgetEvent:DefaultNextPageNavigationEvent"]'
# Identity of the page currently shown: the Next button's page key plus the first row.
PAGE_KEY_JS = """() => {
  const n = document.querySelector('input[name^="ppw-widgetEvent:DefaultNextPageNavigationEvent"]');
  const r = document.querySelector('.apx-transactions-line-item-component-container');
  return (n ? n.name : '') + '|' + (r ? r.textContent.replace(/\\s+/g, ' ') : '');
}"""

# Walk the ledger in document order so each line item picks up the nearest preceding
# date header and section heading ("Completed", ...). Raw strings only.
EXTRACT_JS = r"""
() => {
  const t = e => e.textContent.replace(/\s+/g, ' ').trim();
  const rows = [];
  let date = null, section = null;
  const sel = '.apx-transaction-date-container, .apx-transactions-line-item-component-container, .apx-transactions-sleeve-header-container, h2, h3';
  for (const n of document.querySelectorAll(sel)) {
    if (n.matches('.apx-transaction-date-container')) { date = t(n); continue; }
    if (!n.matches('.apx-transactions-line-item-component-container')) { section = t(n); continue; }
    const head = n.querySelector('.a-row');
    const cols = head ? [...head.querySelectorAll('.a-column')].map(t) : [];
    rows.push({
      section, date,
      parts: [...n.children].map(t),
      instrument: cols[0] ?? null,
      amount: cols[1] ?? null,
      links: [...n.querySelectorAll('a')].map(a => a.href),
      html: n.outerHTML,
    });
  }
  const next = document.querySelector('input[name^="ppw-widgetEvent:DefaultNextPageNavigationEvent"]');
  return {rows, hasNext: !!(next && !next.disabled), nextKey: next ? next.name : null, url: location.href};
}
"""

# ---------------------------------------------------------------------------------
# Generic row parsing -- no allowlists of labels. A ledger row is structurally:
#   [instrument  amount] [("Refund: ")"Order #"ref]... [merchant]?
# * STRUCTURAL problems (no date, unreadable amount, no order reference, sign that
#   contradicts "Refund:", unexplained extra parts) are `issues` -> the crawl stops.
# * New VALUES (a payment label never seen, a new order-id prefix, a new section or
#   merchant) are not errors: they are collected into a vocabulary and diffed against
#   ledger_vocab.json (the reviewed set), reported for review, and accepted with
#   --accept-vocab. Nothing is hardcoded; nothing new goes unseen.
# ---------------------------------------------------------------------------------
CARD_RE = re.compile(r"^(?P<network>.+?) \*{4}(?P<last4>\d{4})$")
AMOUNT_RE = re.compile(r"^(?P<sign>[-+])\$(?P<value>[\d,]+\.\d{2})$")
REF_RE = re.compile(r"^(?P<refund>Refund: )?Order #(?P<ref>\S+)$")
ID_SHAPE_RE = re.compile(r"^(?P<prefix>[A-Z0-9]{3})-\d{7}-\d{7}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
VOCAB_FIELDS = ("section", "instrumentLabel", "network", "orderRefPrefix", "merchant")


def issue(kind: str, code: str, detail: str) -> dict[str, str]:
    return {"kind": kind, "code": code, "detail": detail}


def ref_prefix(ref: str) -> str:
    """Shape of an order reference, derived not listed: '112', 'D01', 'TRN', 'uuid', or 'other'."""
    if m := ID_SHAPE_RE.match(ref):
        return m["prefix"]
    return "uuid" if UUID_RE.match(ref) else "other"


def instrument_kind(label: str) -> str:
    """Loose description for querying only -- never used to reject a row."""
    low = label.lower()
    return "points" if "points" in low else "gift_card" if "gift card" in low else "other"


def normalise_row(raw: dict[str, Any]) -> dict[str, Any]:
    issues = []
    parts = raw["parts"]

    charge_date = None
    try:
        charge_date = datetime.strptime(raw["date"] or "", "%B %d, %Y").date().isoformat()
    except ValueError:
        issues.append(issue("structure", "date", repr(raw["date"])))

    label = (raw["instrument"] or "").strip()
    network = last4 = None
    if m := CARD_RE.match(label):
        network, last4, kind = m["network"], m["last4"], "card"
    elif label:
        kind = instrument_kind(label)
    else:
        issues.append(issue("structure", "instrument", "empty instrument column"))
        kind = None

    amount = direction = None
    if m := AMOUNT_RE.match(raw["amount"] or ""):
        amount = float(m["value"].replace(",", ""))
        direction = "charge" if m["sign"] == "-" else "refund"
    else:
        issues.append(issue("structure", "amount", repr(raw["amount"])))

    # Everything after the header is an order reference, the merchant, or "Pending" (a charge
    # Amazon has not posted yet; it is a status, not the merchant).
    refs, refund_flags, rest, pending = [], set(), [], False
    for part in parts[1:]:
        if m := REF_RE.match(part):
            refs.append(m["ref"])
            refund_flags.add(bool(m["refund"]))
        elif part == "Pending":
            pending = True
        else:
            rest.append(part)
    if not refs:
        issues.append(issue("structure", "order_ref", f"no 'Order #' reference in {parts[1:]}"))
    if len(rest) > 1:
        issues.append(issue("structure", "extra_parts", f"unexplained parts {rest}"))
    if len(refund_flags) > 1:
        issues.append(issue("structure", "refund_mixed", f"mixed refund/order references {parts[1:]}"))
    is_refund_ref = refund_flags == {True}
    if direction and refs and (direction == "refund") != is_refund_ref:
        issues.append(issue("structure", "refund_sign", f"amount {raw['amount']} vs reference {parts[1:]}"))

    return {
        "chargeDate": charge_date,
        "instrumentLabel": label or None,
        "instrumentKind": kind,
        "network": network,
        "last4": last4,
        "amount": amount,
        "direction": direction,
        "orderRefs": refs,
        "orderRefPrefixes": [ref_prefix(r) for r in refs],
        # Some digital rows repeat the order id where the merchant goes; that is not a merchant.
        "merchant": (rest[0] or None) if rest and rest[0] not in refs else None,
        "pending": pending,   # not posted yet: evidence for the order, but no Payment until it posts
        "section": raw["section"],
        "issues": issues,
        "raw": raw,
    }


def row_values(r: dict[str, Any]) -> dict[str, list[Any]]:
    """The vocabulary values one row contributes, per field."""
    return {
        "section": [r["section"]],
        "instrumentLabel": [r["instrumentLabel"]] if r["instrumentKind"] != "card" else [],
        "network": [r["network"]],
        "orderRefPrefix": r["orderRefPrefixes"],
        "merchant": [r["merchant"]],
    }


def vocabulary(rows: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """Every distinct value per field, with a count and first-seen (newest) charge date."""
    vocab: dict[str, dict[str, dict[str, Any]]] = {f: {} for f in VOCAB_FIELDS}
    for r in rows:
        for field, vals in row_values(r).items():
            for v in vals:
                if v is None:
                    continue
                e = vocab[field].setdefault(v, {"count": 0, "example": None})
                e["count"] += 1
                e["example"] = e["example"] or f"{r['chargeDate']} | " + " | ".join(r["raw"]["parts"])
    return vocab


def new_vocab(vocab: dict[str, dict[str, Any]], reviewed_path: Path) -> dict[str, dict[str, Any]]:
    reviewed = json.loads(reviewed_path.read_text()) if reviewed_path.exists() else {}
    return {f: {v: e for v, e in vals.items() if v not in reviewed.get(f, [])} for f, vals in vocab.items()}


def extract(page: Page, page_no: int) -> dict[str, Any]:
    data = page.evaluate(EXTRACT_JS)
    rows = [normalise_row(r) for r in data["rows"]]
    for i, r in enumerate(rows):
        r["page"], r["position"] = page_no, i
    return {"page": page_no, "url": data["url"], "hasNext": data["hasNext"], "nextKey": data["nextKey"], "rows": rows}


def print_flags(pages: list[dict[str, Any]]) -> int:
    n = 0
    for p in pages:
        for r in p["rows"]:
            for i in r["issues"]:
                n += 1
                print(f"  FLAG page {p['page']} #{r['position']} [{i['kind']}] {i['code']}: {i['detail']}")
                print(f"       row: {r['raw']['date']} | " + " | ".join(r["raw"]["parts"]))
    return n


VOCAB_PATH = HERE / "ledger_vocab.json"


def save(out: Path, pages: list[dict[str, Any]], start: date, end: dict[str, Any] | None = None,
         retained: list[dict[str, Any]] | None = None) -> None:
    """end: how the crawl finished. Only {"reached": True} means the list is complete."""
    rows = [r for p in pages for r in p["rows"]]
    vocab = vocabulary(rows)
    reviewed = json.loads(VOCAB_PATH.read_text()) if VOCAB_PATH.exists() else {}
    counts = write_report(
        "transactions", rows,
        values_of=row_values,
        reviewed={f: set(v) for f, v in reviewed.items()},
        page_file=lambda r: f"html/transactions/page-{r['page']:03d}.html",
        row_text=lambda r: f"{r['raw']['date']} | " + " | ".join(r["raw"]["parts"]),
    )
    print(f"  debug/transactions-unaligned.json: {counts['structural']} structural, {counts['unreviewedValues']} unreviewed values", flush=True)
    doc = {
        "vocabulary": vocab,
        "newVocabulary": new_vocab(vocab, VOCAB_PATH),
        "start": start.isoformat(),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "end": end or {"reached": False, "reason": "crawl in progress or stopped early"},
        "coverage": {"newest": max((r["chargeDate"] for r in rows if r["chargeDate"]), default=None),
                     "oldest": min((r["chargeDate"] for r in rows if r["chargeDate"]), default=None)},
        "pageCount": len(pages),
        "rowCount": len(rows),
        "rowsWithIssues": [{"page": r["page"], "position": r["position"], "issues": r["issues"]} for r in rows if r["issues"]],
        "pages": pages,
        # Rows from earlier complete crawls that have since aged out of Amazon's ~5-year
        # ledger window. Kept so the history only ever grows.
        "retainedRows": retained or [],
    }
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    tmp.replace(out)


def needs_human(page: Page) -> bool:
    u = page.url
    return "/ap/signin" in u or "/errors/validateCaptcha" in u or page.locator("form[action*='validateCaptcha']").count() > 0


def wait_for_human(page: Page) -> None:
    msg = "Amazon wants sign-in/CAPTCHA. Resolve it in the Chrome tab"
    if sys.stdin.isatty():
        input(f"\n{msg}, then press Enter... ")
        return
    print(f"\n{msg}; polling for up to 10 minutes...", flush=True)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        time.sleep(5)
        if not needs_human(page):
            return
    sys.exit("still blocked on sign-in/CAPTCHA after 10 minutes; progress saved")


def oldest(p: dict[str, Any]) -> str | None:
    dates = [r["chargeDate"] for r in p["rows"] if r["chargeDate"]]
    return min(dates) if dates else None


STOP_HELP = """
Stopped: a row's STRUCTURE could not be parsed (saved; nothing lost). To build along:
  1. adjust the generic row parser (normalise_row) in fetch_transactions.py
  2. uv run fetch_transactions.py --start {start} --reparse     # confirm 0 flagged, offline
  3. uv run fetch_transactions.py --start {start}               # re-crawl; Next Page only walks from the top
"""


def goto_next(page: Page) -> None:
    """Click Next Page and wait for the list to re-render in place -- not a navigation.
    (Re-targeting the button at a saved page key does not work: Amazon returns page 1.)"""
    before = page.evaluate(PAGE_KEY_JS)
    page.locator(NEXT_SEL).first.click()
    if needs_human(page):
        sys.exit("Amazon asked for sign-in/CAPTCHA mid-crawl; resolve it and re-run (pages so far are saved)")
    page.wait_for_function(f"() => ({PAGE_KEY_JS})() !== {json.dumps(before)}", timeout=45_000)


# Amazon's empty-list message. Seen after the last page even though "Next Page" was
# still enabled there (the ledger only reaches back ~5 years).
EMPTY_LEDGER_TEXT = "You don't have any transactions yet."


def end_file(html_dir: Path, page_no: int) -> Path:
    return html_dir / f"page-{page_no:03d}-END.html"


def wait_for_rows(page: Page, html_dir: Path, page_no: int) -> bool:
    """True when rows are showing. After page 1, Amazon's empty-list message means the
    end of the ledger: keep the page as evidence and return False. Any other row-less
    page is a state we have not seen: save it and stop, rather than crash or guess."""
    try:
        page.wait_for_selector(".apx-transactions-line-item-component-container", timeout=30_000)
        return True
    except PWTimeout:
        text = page.evaluate("() => document.body.innerText")
        if page_no > 1 and EMPTY_LEDGER_TEXT in text:
            end_file(html_dir, page_no).write_text(page.content())
            return False
        dump = html_dir / f"page-{page_no:03d}-NO-ROWS.html"
        dump.write_text(page.content())
        # the ledger section starts at its "Transactions" heading; show what is there instead of rows
        tail = text[text.find("Transactions"):] if "Transactions" in text else text
        visible = " | ".join(ln.strip() for ln in tail.splitlines() if ln.strip())[:600]
        sys.exit(f"page {page_no}: no transaction rows after 30s (earlier pages saved). Tab URL {page.url}\n"
                 f"saved {dump}\nvisible text: {visible}")


def crawl(args: argparse.Namespace) -> None:
    args.html_dir.mkdir(parents=True, exist_ok=True)
    run_dir = new_run_dir(args.html_dir)  # this run's pages; promoted only if the run completes cleanly
    partial = partial_path(args.out)
    print(f"writing to {partial.name} and {run_dir.relative_to(HERE)}/ ; {args.out.name} is only replaced by a complete, clean run")
    pages: list[dict[str, Any]] = []
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(args.cdp)
        page = browser.contexts[0].new_page()  # own tab in the same window; never hijack the user's tab
        page.goto(LEDGER_URL, wait_until="domcontentloaded", timeout=45_000)
        while needs_human(page):
            wait_for_human(page)
            page.goto(LEDGER_URL, wait_until="domcontentloaded", timeout=45_000)
        page_no = 1
        while True:
            if not wait_for_rows(page, run_dir, page_no):
                end = {"reached": True, "afterPage": page_no - 1, "evidence": str((args.html_dir / end_file(run_dir, page_no).name).relative_to(HERE)),
                       "reason": f"page {page_no} shows {EMPTY_LEDGER_TEXT!r}"}
                save(partial, pages, args.start, end)
                finish(args, run_dir)
                print(f"page {page_no}: {EMPTY_LEDGER_TEXT!r} -> end of the ledger (evidence {end['evidence']})")
                break
            (run_dir / f"page-{page_no:03d}.html").write_text(page.content())
            p = extract(page, page_no)
            pages.append(p)
            save(partial, pages, args.start)
            flagged = sum(1 for r in p["rows"] if r["issues"])
            print(f"page {page_no}: {len(p['rows'])} rows, {p['rows'][0]['chargeDate'] if p['rows'] else '-'} .. {oldest(p)}, {flagged} flagged", flush=True)
            if flagged and not args.no_stop:
                print_flags([p])
                print(STOP_HELP.format(start=args.start))
                sys.exit(2)
            if not p["hasNext"]:
                save(partial, pages, args.start, {"reached": True, "afterPage": page_no, "evidence": None,
                                                  "reason": f"page {page_no} has no enabled Next Page"})
                finish(args, run_dir)
                print("no Next Page: reached the end of the ledger")
                break
            if (o := oldest(p)) and o < args.start.isoformat():
                print(f"passed start date {args.start}")
                finish(args, run_dir, at_start=True)
                break
            if page_no >= args.max_pages:
                print(f"stopped at --max-pages {args.max_pages}")
                break
            time.sleep(random.uniform(args.min_gap, args.max_gap))
            goto_next(page)
            page_no += 1


def row_identity(r: dict[str, Any]) -> tuple[Any, ...]:
    return (r["chargeDate"], r["instrumentLabel"], r["amount"], r["direction"], tuple(r["orderRefs"]))


def finish(args: argparse.Namespace, run_dir: Path, at_start: bool = False) -> None:
    """Promote this run if it has no structural flags and either reached the end of the ledger
    or (at_start) passed its start date on top of a complete previous crawl. Rows of the
    previous crawl older than this run's oldest row are carried forward, so the history only
    ever grows; a pending row is never carried (it may have posted since, under a new date)."""
    partial = partial_path(args.out)
    doc = json.loads(partial.read_text())
    prev = json.loads(args.out.read_text()) if args.out.exists() else None
    if doc["rowsWithIssues"]:
        print(f"NOT promoted (flags): {args.out.name} kept as is; this run is in {partial.name}")
        return
    if at_start and not (prev and prev["end"].get("reached")):
        print(f"NOT promoted: a run that stops at its start date adds to a complete crawl, and {args.out.name} "
              f"is not one; crawl to the end once (--start 2000-01-01). This run is in {partial.name}")
        return
    if not at_start and not doc["end"].get("reached"):
        print(f"NOT promoted (end not reached): {args.out.name} kept as is; this run is in {partial.name}")
        return
    retained: list[dict[str, Any]] = []
    if prev:
        prev_rows = [r for p in prev.get("pages", []) for r in p["rows"]] + prev.get("retainedRows", [])
        oldest = doc["coverage"]["oldest"] or ""
        # Multiset, not set: two identical rows (same day, card, amount, order) are two real
        # charges (e.g. a double charge) and must both survive.
        have = Counter(row_identity(r) for p in doc["pages"] for r in p["rows"])
        for r in prev_rows:
            # <= : the boundary day can be split across the old and new window
            if not r["chargeDate"] or r["chargeDate"] > oldest or r.get("pending"):
                continue
            k = row_identity(r)
            if have[k] > 0:
                have[k] -= 1
            else:
                retained.append({**r, "retainedFrom": prev.get("updatedAt")})
        posted = sum(1 for r in prev_rows if not r.get("pending"))
        if doc["rowCount"] + len(retained) < posted:
            print(f"NOT promoted: new run has {doc['rowCount']} rows (+{len(retained)} retained) but {args.out.name} has {posted}; "
                  f"kept as is, this run is in {partial.name}")
            return
        if at_start:   # the older rows come from a crawl that reached the end, so the history still does
            doc["end"] = {**prev["end"], "via": f"rows before {oldest} carried from the crawl of {prev.get('updatedAt')}"}
    doc["retainedRows"] = retained
    archive = promote(run_dir, args.html_dir, args.out, doc)
    print(f"promoted: {args.out.name} = {doc['rowCount']} rows ({doc['coverage']['oldest']} .. {doc['coverage']['newest']}) "
          f"+ {len(retained)} retained older rows" + (f"; previous pages archived to {archive.relative_to(HERE)}" if archive else ""))


def reparse(args: argparse.Namespace) -> None:
    files = sorted(args.html_dir.glob("page-[0-9][0-9][0-9].html"))
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
    # The crawl's end evidence is a saved page right after the last one; re-verify it.
    end = None
    if pages:
        ef = end_file(args.html_dir, pages[-1]["page"] + 1)
        if ef.exists() and EMPTY_LEDGER_TEXT in ef.read_text():
            end = {"reached": True, "afterPage": pages[-1]["page"], "evidence": str(ef.relative_to(HERE)),
                   "reason": f"{ef.name} shows {EMPTY_LEDGER_TEXT!r}"}
        elif not pages[-1]["hasNext"]:
            end = {"reached": True, "afterPage": pages[-1]["page"], "evidence": None, "reason": "last page has no enabled Next Page"}
    prev = json.loads(args.out.read_text()) if args.out.exists() else {}
    retained = prev.get("retainedRows", [])
    if end is None and retained and prev.get("end", {}).get("reached"):
        end = prev["end"]   # saved pages stop at a start date; the older rows complete the history
    save(args.out, pages, args.start, end, retained)
    print(f"re-parsed {len(files)} saved pages; end reached: {bool(end)}; kept {len(retained)} retained rows")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True, type=date.fromisoformat, help="crawl back until charges older than this (YYYY-MM-DD)")
    ap.add_argument("--out", type=Path, default=HERE / "transactions.json")
    ap.add_argument("--html-dir", type=Path, default=HERE / "html" / "transactions")
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument("--min-gap", type=float, default=3.0)
    ap.add_argument("--max-gap", type=float, default=5.0)
    ap.add_argument("--max-pages", type=int, default=500)
    ap.add_argument("--reparse", action="store_true", help="re-extract saved pages offline with the current rules")
    ap.add_argument("--no-stop", action="store_true", help="keep going past flagged rows instead of stopping")
    ap.add_argument("--accept-vocab", action="store_true", help="mark every value currently in transactions.json as reviewed (writes ledger_vocab.json)")
    args = ap.parse_args()
    if args.accept_vocab:
        vocab = json.loads(args.out.read_text())["vocabulary"]
        VOCAB_PATH.write_text(json.dumps({f: sorted(v) for f, v in vocab.items()}, indent=2, ensure_ascii=False))
        print(f"accepted {sum(len(v) for v in vocab.values())} values -> {VOCAB_PATH}")
        return
    reparse(args) if args.reparse else crawl(args)
    doc = json.loads(args.out.read_text())
    print(f"{doc['rowCount']} rows on {doc['pageCount']} pages, {len(doc['rowsWithIssues'])} structural flags -> {args.out}")
    for r in doc["rowsWithIssues"]:
        print(f"  page {r['page']} #{r['position']}: " + "; ".join(f"{i['code']}: {i['detail']}" for i in r["issues"]))
    new = {f: v for f, v in doc["newVocabulary"].items() if v}
    if new:
        print("\nNew values to review (not errors; accept with --accept-vocab once they look right):")
        for field, vals in new.items():
            print(f"  {field}:")
            for v, e in sorted(vals.items(), key=lambda kv: -kv[1]["count"]):
                print(f"    {v!r:<48} x{e['count']:<5} e.g. {e['example'][:110]}")


if __name__ == "__main__":
    main()
