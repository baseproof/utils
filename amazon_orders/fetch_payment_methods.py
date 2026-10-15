#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright>=1.47"]
# ///
"""Fetch order details (payment methods, ship-to, charge breakdown, items) for Amazon
orders in a date range using an already-open Chrome.

Orders (id + date) come from the amazon-orders-*.json export. For each order in the
range, the order's print page is loaded in ONE reused tab of your running Chrome
(attached over CDP), with a random 3-5 s gap between orders. Results are written
incrementally to JSON so the run can be interrupted and resumed.

Chrome must expose a DevTools endpoint, e.g. launched with:
  open -na "Google Chrome" --args --remote-debugging-port=9222 \\
      --user-data-dir="$HOME/.chrome-amazon"
(Chrome 136+ ignores --remote-debugging-port on the default profile, hence the
separate --user-data-dir; sign in to Amazon once in that window.)

Usage:
  uv run fetch_payment_methods.py --start 2026-09-01 --end 2026-09-30
  uv run fetch_payment_methods.py --start 2026-08-01 --end 2026-09-30 --debug
      # also saves each rendered page to html/<orderId>.html and the raw extraction
  uv run fetch_payment_methods.py --start 2026-08-01 --end 2026-09-30 --reparse
      # re-extracts from html/*.html offline (headless Chrome, network blocked);
      # use this to iterate on the parser without touching Amazon
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from unaligned import write_report

HERE = Path(__file__).resolve().parent
PRINT_URL = "https://www.amazon.com/gp/css/summary/print.html?orderID={order_id}"
INSTRUMENT_SEL = '[data-testid="payment-instrument"]'
# Bump when the extracted record shape changes; older records are re-fetched.
SCHEMA_VERSION = 2

# Runs in the page. Keys off Amazon's data-component / data-testid hooks, returns raw
# strings; all parsing/normalisation happens in Python.
EXTRACT_JS = r"""
() => {
  const txt = el => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
  const lines = el => (el ? el.innerText.split('\n').map(s => s.trim()).filter(Boolean) : []);
  const comp = (name, root = document) => root.querySelector(`[data-component="${name}"]`);
  // textContent minus <script>/<style>: identical whether or not CSS/JS ran (live vs --reparse).
  const clean = el => {
    if (!el) return null;
    const c = el.cloneNode(true);
    c.querySelectorAll('script,style,noscript,link,template').forEach(n => n.remove());
    return c.textContent.replace(/\s+/g, ' ').trim() || null;
  };

  const instruments = [...document.querySelectorAll('[data-testid="payment-instrument"]')].map(el => {
    const q = id => txt(el.querySelector(`[data-testid="${id}"]`));
    const img = el.querySelector('img');
    return {
      name: q('payment-instrument-name'),
      number: q('payment-instrument-number'),
      display: clean(el),
      issuerImage: img ? img.src : null,
    };
  });
  const paymentWidgets = [...document.querySelectorAll('[data-component]')]
    .filter(el => /payment/i.test(el.getAttribute('data-component')))
    .map(el => ({component: el.getAttribute('data-component'), text: clean(el)}))
    .filter(w => w.text);

  const shipTo = [...document.querySelectorAll('[data-component="shippingAddress"] li')].map(lines);
  const addressHeading = clean(document.querySelector('[data-component="shippingAddress"] h5'));
  const components = [...new Set([...document.querySelectorAll('[data-component]')]
    .filter(el => clean(el)).map(el => el.getAttribute('data-component')))].sort();

  const charges = [...document.querySelectorAll('[data-component="chargeSummary"] .od-line-item-row')].map(r => ({
    label: txt(r.querySelector('.od-line-item-row-label')),
    amount: txt(r.querySelector('.od-line-item-row-content')),
  }));

  const shipments = [...document.querySelectorAll('[data-component="shipments"] .a-box')].map(box => ({
    status: lines(comp('shipmentStatus', box)),
    items: [...box.querySelectorAll('[data-component="itemTitle"]')].map(t => {
      const item = t.closest('.a-fixed-left-grid') || t.parentElement;
      const a = t.querySelector('a');
      const price = item.querySelector('[data-component="unitPrice"] .a-offscreen');
      return {
        title: txt(t),
        url: a ? a.href : null,
        soldBy: txt(comp('orderedMerchant', item)),
        suppliedBy: txt(comp('supplierOfRecord', item)),
        returnEligibility: txt(comp('itemReturnEligibility', item)),
        unitPrice: txt(price),
        quantity: txt(comp('quantity', item)) || txt(item.querySelector('.od-item-view-qty')),
        condition: txt(comp('itemCondition', item)),
      };
    }),
  }));

  return {
    orderDate: txt(comp('orderDate')),
    orderId: txt(comp('orderId')),
    shipTo, addressHeading, components, charges, shipments, instruments, paymentWidgets,
    url: location.href,
  };
}
"""

MONEY_RE = re.compile(r"(-)?\s*[$€£]?\s*(-)?\s*([\d,]+\.\d{2})")
# Older payments widget (plain text, first seen on a Jun 2021 order):
#   "Payment method <name> ending in <last4> View related transactions" -- last4 may be blank.
LEGACY_INSTRUMENT_RE = re.compile(
    r"^Payment method (?P<display>(?P<name>.+?) ending in ?(?P<number>\d{4})?)(?: View related transactions)?$")


def money(s: str | None) -> float | None:
    if not s:
        return None
    m = MONEY_RE.search(s)
    if not m:
        return None
    v = float(m.group(3).replace(",", ""))
    return -v if (m.group(1) or m.group(2) or s.strip().startswith("-")) else v


CURRENCY_SYMBOL = {"$": "USD", "€": "EUR", "£": "GBP"}  # amazon.com shows USD as "$"


def currency_of(texts: list[str | None]) -> tuple[str | None, str | None]:
    """The currency the page shows its amounts in: a 3-letter code ("INR 9,455.10") or a
    symbol. Returns (currency, problem); a page mixing currencies or using an unknown
    prefix is a problem (the run stops), since the amounts could not be added up."""
    seen = set()
    for t in texts:
        t = (t or "").replace("\xa0", " ").strip().lstrip("-").strip()
        if not t:
            continue
        m = re.match(r"([A-Z]{3})\s", t)
        seen.add(m.group(1) if m else CURRENCY_SYMBOL.get(t[0], f"?{t[:6]}"))
    if not seen:
        return None, None
    if len(seen) > 1 or any(c.startswith("?") for c in seen):
        return None, f"amounts in {sorted(seen)}"
    return seen.pop(), None


def camel(label: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", label.replace("(s)", "s"))
    return "".join(w.lower() if i == 0 else w.capitalize() for i, w in enumerate(words))


def strip_prefix(s: str | None, prefix: str) -> str | None:
    if s is None:
        return None
    s = s.split(prefix, 1)[1] if prefix in s else s
    return s.strip() or None


# ---------------------------------------------------------------------------------
# What STOPS a run (structure): payment wording that matches no known layout, a payment
# instrument without a name, amounts/dates that cannot be read, no Grand Total / items /
# payment section. Payment is the point of this tool, so payment wording stays explicit.
# What is only REPORTED (vocabulary, like the ledger crawlers): charge labels by
# position, page sections, delivery status words, address headings, card names shown
# without a number. New values go to debug/orders-unaligned.json with the saved page, and
# are accepted with --accept-vocab (order_vocab.json).
# What is RECORDED as provided: the page's Grand Total. When the shown lines don't add up
# to it (old pages omit tax/discount lines), the gap is stored as summaryDiff, not flagged;
# the money checks that matter are Grand Total vs export total vs card charges.
# ---------------------------------------------------------------------------------
SUBTOTAL, BEFORE_TAX, GRAND = "Item(s) Subtotal", "Total before tax", "Grand Total"
VOCAB_PATH = HERE / "order_vocab.json"
# Seed for order_vocab.json: everything reviewed on the Aug-Sep 2026 corpus.
VOCAB_SEED: dict[str, list[str]] = {
    "chargeLabel": ["pre-tax: Item(s) Subtotal", "pre-tax: Shipping & Handling", "pre-tax: Free Shipping",
                    "pre-tax: Your Coupon Savings", "pre-tax: Subscription saving", "pre-tax: Promotion applied",
                    "tax: Estimated tax to be collected", "after-total: Refund Total",
                    "after-total: FSA or HSA eligible: (inc. tax and shipping)"],
    "addressHeading": ["Ship to", "PICKUP AT"],
    "shipmentStatus": ["Delivered", "Arriving", "Cancelled", "Purchased", "Refund", "Return"],
    "pageSection": ["briefOrderInfoInvoice", "briefOrderInfoInvoiceLeftGridDesktop", "briefOrderInfoInvoiceRightGrid",
                    "chargeSummary", "default", "healthBenefitAccountInformation", "itemCondition", "itemImage",
                    "itemReturnEligibility", "itemTitle", "orderCard", "orderDate", "orderDateLabel", "orderId",
                    "orderIdLabel", "orderSummary", "orderedMerchant", "printODTitle", "purchasedItems",
                    "purchasedItemsLeftGrid", "purchasedItemsRightGrid", "saveButton", "shipmentStatus", "shipments",
                    "shipmentsLeftGrid", "shippingAddress", "supplierOfRecord", "title", "titleLeftGrid", "unitPrice",
                    "viewPaymentPlanSummaryWidget"],
}
ADDRESS_KIND = {"Ship to": "ship", "PICKUP AT": "pickup"}
# Payment widget text, after removing each recognised card instrument's own text.
PAYMENT_BOILERPLATE = {
    "Payment method View related transactions": "charged",
    "Payment method No current charges": "no_current_charges",
    # Old orders (first seen on a 2005 order): Amazon shows no payment details at all.
    # Possibly transient ("at the moment"); build_orders keeps these flagged, never reconciled.
    "Payment method Unable to display payment details at the moment.": "unavailable",
}


def issue(kind: str, code: str, detail: str) -> dict[str, str]:
    """kind: unknown_format (page shape not in the registry -> fix the parser) |
    mismatch (numbers disagree -> a human should look) | parse (a value could not be read)."""
    return {"kind": kind, "code": code, "detail": detail}


def normalise(raw: dict[str, Any]) -> dict[str, Any]:
    placed_text = (raw["orderDate"] or "").strip() or None
    placed_iso = None
    if placed_text:
        try:
            placed_iso = datetime.strptime(placed_text, "%B %d, %Y").date().isoformat()
        except ValueError:
            pass

    issues: list[dict[str, str]] = []
    if placed_text and not placed_iso:
        issues.append(issue("parse", "order_date", f"could not parse {placed_text!r}"))

    # shippingAddress <li>s: [name], [street..., city/state/zip], [country]
    ship_lis = raw["shipTo"]
    ship_to = None
    heading = raw.get("addressHeading")
    if ship_lis:
        flat = [ln for li in ship_lis for ln in li]
        ship_to = {
            "name": ship_lis[0][0] if ship_lis[0] else None,
            "addressLines": [ln for li in ship_lis[1:-1] for ln in li] if len(ship_lis) > 2 else [ln for li in ship_lis[1:] for ln in li],
            "country": ship_lis[-1][0] if len(ship_lis) > 2 and ship_lis[-1] else None,
            "raw": flat,
            "kind": ADDRESS_KIND.get(heading or "", "other"),
            "heading": heading,
        }

    charge_lines = [
        {"label": (c["label"] or "").rstrip(":").strip(), "amountText": c["amount"], "amount": money(c["amount"])}
        for c in raw["charges"]
        if c["label"]
    ]
    charges = {camel(c["label"]): c["amount"] for c in charge_lines}
    currency, currency_problem = currency_of([c["amountText"] for c in charge_lines])
    if currency_problem:
        issues.append(issue("parse", "currency", currency_problem))
    issues += check_charges(charge_lines)


    shipments: list[dict[str, Any]] = []
    for s in raw["shipments"]:
        items = []
        for it in s["items"]:
            qty_digits = re.search(r"\d+", it["quantity"] or "")
            items.append({
                "title": it["title"],
                "asin": (m.group(1) if (m := re.search(r"/dp/([A-Z0-9]{10})", it["url"] or "")) else None),
                "url": it["url"],
                "quantity": int(qty_digits.group()) if qty_digits else 1,
                "unitPrice": money(it["unitPrice"]),
                "soldBy": strip_prefix((it["soldBy"] or "").split("Supplied by:")[0], "Sold by:"),
                "suppliedBy": strip_prefix((it["suppliedBy"] or "").split("Return")[0], "Supplied by:"),
                "returnEligibility": it["returnEligibility"],
                "condition": strip_prefix(it["condition"], "Condition:"),
            })
        if items:
            status = " — ".join(s["status"]) or None
            shipments.append({"status": status, "items": items})
    for it in (it for sh in shipments for it in sh["items"]):
        if it["unitPrice"] is None:
            issues.append(issue("parse", "item_price", f"no price for {it['title']!r}"))

    instruments = list(raw["instruments"])
    if not instruments:
        # Read the older plain-text layout as provided; anything else still stops the run below.
        for w in raw["paymentWidgets"]:
            m = LEGACY_INSTRUMENT_RE.match(re.sub(r"\s+", " ", w["text"] or "").strip())
            if m:
                instruments.append({"name": m["name"].strip(), "number": m["number"],
                                    "display": m["display"].strip(), "issuerImage": None, "layout": "legacy_text"})
    seen, methods = set(), []
    for inst in instruments:
        k = (inst["name"], inst["number"], inst["display"])
        if k not in seen:
            seen.add(k)
            methods.append(inst)
        # A name without a number (e.g. generic "Card" on old orders: the card is no longer on
        # file) is recorded and reported as vocabulary; only a nameless instrument stops the run.
        if not inst["name"]:
            issues.append(issue("unknown_format", "payment_instrument", f"instrument without a name: {inst['display']!r}"))

    # Everything in the payment section must be explained by a card instrument or
    # known boilerplate; leftover text (gift card, points, split tender...) is flagged.
    payment_state = None
    widgets = raw["paymentWidgets"]
    if not widgets:
        issues.append(issue("unknown_format", "payment_section", "no payment section on page"))
    for w in widgets:
        text = w["text"] or ""
        for inst in methods:
            text = text.replace(inst["display"] or "\0", "", 1)
        text = re.sub(r"\s+", " ", text).strip()
        if w["component"] == "viewPaymentPlanSummaryWidget" and text in PAYMENT_BOILERPLATE:
            payment_state = PAYMENT_BOILERPLATE[text]
        else:
            issues.append(issue("unknown_format", "payment_text", f"{w['component']}: {text!r}"))
    if payment_state == "charged" and not methods:
        issues.append(issue("unknown_format", "payment_text", "charged layout but no card instrument"))

    statuses = {sh["status"].split(" ", 1)[0] for sh in shipments if sh["status"]}
    order_state = "cancelled" if statuses == {"Cancelled"} else payment_state or "unknown"
    notes = sorted(
        ({"pickup"} if ADDRESS_KIND.get(heading or "") == "pickup" else set())
        | ({"refund"} if "refundTotal" in charges or statuses & {"Refund", "Return"} else set())
        | ({"fsa_hsa"} if any(c["label"].startswith("FSA or HSA") for c in charge_lines) else set())
        | ({"coupon"} if "yourCouponSavings" in charges else set())
        | ({"subscribe_save"} if "subscriptionSaving" in charges else set())
        | ({"split_payment"} if len(methods) > 1 else set())
    )

    return {
        "pageOrderId": raw["orderId"],
        "orderPlaced": placed_iso,
        "orderPlacedText": placed_text,
        "shipTo": ship_to,
        "paymentMethods": methods,
        "paymentWidgets": raw["paymentWidgets"] if not methods else [],
        "charges": charges,
        "chargeLines": charge_lines,
        "summaryDiff": summary_diff(charge_lines),  # Grand Total minus the shown lines (e.g. tax not shown)
        "currency": currency,  # as the page shows it (INR on a Currency Converter order); None if no amounts
        "shipments": shipments,
        "url": raw["url"],
        "orderState": order_state,
        "notes": notes,
        "formatIssues": issues,
        "status": "ok" if not issues else "needs_review",
        "vocab": {
            "chargeLabel": charge_positions(charge_lines),
            # "(none)": digital orders (gift card reloads, e-gift cards) have no address; reviewed as vocabulary
            "addressHeading": [heading] if heading else ["(none)"],
            "shipmentStatus": sorted(statuses),
            "pageSection": raw.get("components", []),
            "unnumberedInstrument": [m["name"] for m in methods if m["name"] and not m["number"]],
        },
    }


def charge_positions(lines: list[dict[str, Any]]) -> list[str]:
    """'pre-tax: Shipping & Handling' etc. -- position comes from the page layout, not a list.
    Pages without a 'Total before tax' line (e.g. 2020 Whole Foods) only have 'pre-total'."""
    labels = [c["label"] for c in lines]
    if GRAND not in labels:
        return [f"?: {lb}" for lb in labels]
    gt = labels.index(GRAND)
    if BEFORE_TAX not in labels or labels.index(BEFORE_TAX) > gt:
        return [f"{'pre-total' if i < gt else 'after-total'}: {lb}" for i, lb in enumerate(labels) if i != gt]
    bt = labels.index(BEFORE_TAX)
    return [f"{'pre-tax' if i < bt else 'tax' if bt < i < gt else 'after-total'}: {lb}"
            for i, lb in enumerate(labels) if i not in (bt, gt)]


def check_charges(lines: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Structure only: every amount must parse and there must be a Grand Total. The page's
    Grand Total is taken as provided (it is what gets checked against the export and the card
    charges); lines that don't add up to it -- old pages omit tax and discount lines, some say
    'Tax information is not available.' -- are recorded as summary_diff(), not flagged."""
    out: list[dict[str, str]] = []
    labels = [c["label"] for c in lines]
    if not lines:
        return out  # cancelled orders have an empty summary; order-state checks cover them
    for c in lines:
        if c["amount"] is None and (c["amountText"] or "").strip():  # a blank amount is a note ("Exchange rate:")
            out.append(issue("parse", "charge_amount", f"{c['label']}: {c['amountText']!r}"))
    if GRAND not in labels:
        out.append(issue("unknown_format", "charge_layout", f"no Grand Total line: {labels}"))
    return out


def summary_diff(lines: list[dict[str, Any]]) -> float | None:
    """Grand Total minus what the shown lines add up to (None when they agree or there is
    no Grand Total). Running subtotals -- a line equal to everything above it, like 2018's
    'Total' -- restate rather than add."""
    labels = [c["label"] for c in lines]
    if GRAND not in labels:
        return None
    gt = labels.index(GRAND)
    stop = labels.index(BEFORE_TAX) if BEFORE_TAX in labels and labels.index(BEFORE_TAX) < gt else None
    total = 0.0
    for i, c in enumerate(lines[:gt]):
        a = c["amount"] or 0
        if i == stop:
            total = a  # the page's own 'Total before tax' replaces the pre-tax running sum
            continue
        if i > 1 and a and abs(a - total) <= 0.005:
            continue
        total = round(total + a, 2)
    diff = round((lines[gt]["amount"] or 0) - total, 2)
    return diff if abs(diff) > 0.005 else None


def validate(rec: dict[str, Any], o: dict[str, Any], page_order_id: str | None) -> list[dict[str, str]]:
    """Format issues from normalise() plus identity checks against the export (order id,
    date). Amounts are recorded as provided, not flagged: rec['exportDiff'] = page Grand
    Total - export total (Whole Foods pages show shelf prices, the export the amount paid;
    reconciliation against card charges happens in build_orders.py)."""
    if rec.get("status") == "error":
        return [issue("parse", "fetch_error", rec.get("error", ""))]
    issues = list(rec.get("formatIssues", []))
    if page_order_id != o["orderId"]:
        issues.append(issue("mismatch", "order_id", f"page shows {page_order_id!r}, requested {o['orderId']}"))
    if rec.get("orderPlaced") != o["orderDate"]:
        issues.append(issue("mismatch", "order_date", f"page {rec.get('orderPlaced')!r} != export {o['orderDate']}"))
    items = [it for sh in rec.get("shipments", []) for it in sh["items"]]
    if not items:
        issues.append(issue("unknown_format", "items", "no items found on page"))
    state = rec.get("orderState")
    charges = rec.get("charges") or {}
    gt = charges.get("grandTotal")
    if state == "cancelled":
        if charges:
            issues.append(issue("unknown_format", "cancelled_with_charges", f"cancelled order shows charges {charges}"))
        return issues
    rec["exportDiff"] = None
    if gt is None:
        issues.append(issue("unknown_format", "grand_total", "no Grand Total line"))
    elif o.get("totalAmount") is not None and abs(gt - o["totalAmount"]) > 0.005:
        rec["exportDiff"] = round(gt - o["totalAmount"], 2)
    if state == "charged" and not rec.get("paymentMethods"):
        issues.append(issue("unknown_format", "payment", "charged but no card found"))
    sub = charges.get("itemsSubtotal")
    # Item prices are shown in $ even when the summary is in another currency (Currency
    # Converter orders), so the two only compare when the summary is in USD.
    if items and sub is not None and rec.get("currency") == "USD" and all(it["unitPrice"] is not None for it in items):
        total = round(sum(it["unitPrice"] * it["quantity"] for it in items), 2)
        if abs(total - sub) > 0.005:
            issues.append(issue("mismatch", "items_sum", f"items sum to {total}, page subtotal {sub}"))
    return issues


def parse_date(s: str) -> date:
    return date.fromisoformat(s)


def latest_orders_json() -> Path:
    files = sorted(HERE.glob("amazon-orders-*.json"))
    if not files:
        sys.exit(f"no amazon-orders-*.json found in {HERE}; pass --orders-json")
    return files[-1]


def load_orders(path: Path, start: date, end: date) -> list[dict]:
    orders = json.loads(path.read_text())
    picked = [
        {"orderId": o["orderId"], "orderDate": o["orderDate"], "totalAmount": o.get("totalAmount")}
        for o in orders
        if start <= parse_date(o["orderDate"]) <= end
    ]
    picked.sort(key=lambda o: o["orderDate"])
    return picked


def load_state(out: Path) -> dict:
    if out.exists():
        return json.loads(out.read_text())
    return {"orders": {}}


def load_reviews(path: Path) -> dict[str, dict]:
    """Human decisions, kept apart from scraped data. Shape per order id:
      {"manualPaymentMethod": {"name": ..., "source": ..., "confidence": ...},
       "acknowledge": [{"code": ..., "detail": ...}], "note": ...}
    An acknowledgement matches code AND detail exactly, so if the underlying numbers
    change the issue is raised again."""
    return json.loads(path.read_text()) if path.exists() else {}


def apply_review(rec: dict[str, Any], review: dict[str, Any] | None) -> None:
    rec.pop("review", None)
    rec.pop("acknowledgedIssues", None)
    if review:
        acks = {(a["code"], a["detail"]) for a in review.get("acknowledge", [])}
        rec["acknowledgedIssues"] = [i for i in rec["issues"] if (i["code"], i["detail"]) in acks]
        rec["issues"] = [i for i in rec["issues"] if (i["code"], i["detail"]) not in acks]
        rec["review"] = {k: v for k, v in review.items() if k != "acknowledge"}
    if rec.get("status") != "error":
        rec["status"] = "ok" if not rec["issues"] else "needs_review"


def reviewed_vocab() -> dict[str, set[str]]:
    if not VOCAB_PATH.exists():
        VOCAB_PATH.write_text(json.dumps(VOCAB_SEED, indent=2, ensure_ascii=False))
    return {f: set(v) for f, v in json.loads(VOCAB_PATH.read_text()).items()}


def write_vocab_report(state: dict) -> dict[str, int]:
    """debug/orders-unaligned.json: unreviewed vocabulary + structural issues, per order page."""
    rows = [{"page": oid, "position": 0, "raw": {"html": None}, "vocab": r.get("vocab", {}),
             "issues": [i for i in r.get("issues", []) if i["kind"] in ("unknown_format", "parse")],
             "orderDate": r.get("exportOrderDate")}
            for oid, r in sorted(state["orders"].items())]
    return write_report("orders", rows, values_of=lambda r: r["vocab"], reviewed=reviewed_vocab(),
                        page_file=lambda r: f"html/{r['page']}.html",
                        row_text=lambda r: f"{r['orderDate']} order {r['page']}")


def save_state(out: Path, state: dict) -> None:
    uniq: dict[str, dict] = {}
    for oid, rec in state["orders"].items():
        for inst in rec.get("paymentMethods", []):
            key = f'{inst.get("name") or "?"} ••••{inst.get("number")}' if inst.get("number") else (inst.get("name") or inst.get("display") or "?")
            u = uniq.setdefault(key, {"method": key, "name": inst.get("name"), "number": inst.get("number"), "orderCount": 0, "grandTotal": 0.0, "orderIds": []})
            u["orderCount"] += 1
            u["grandTotal"] = round(u["grandTotal"] + (rec.get("charges", {}).get("grandTotal") or 0), 2)
            u["orderIds"].append(oid)
        manual = (rec.get("review") or {}).get("manualPaymentMethod")
        if manual:
            key = f'{manual["name"]} (manual: {manual.get("confidence", "?")})'
            u = uniq.setdefault(key, {"method": key, "name": manual["name"], "number": None, "orderCount": 0, "grandTotal": 0.0, "orderIds": [], "manual": True})
            u["orderCount"] += 1
            u["grandTotal"] = round(u["grandTotal"] + (rec.get("charges", {}).get("grandTotal") or 0), 2)
            u["orderIds"].append(oid)
    state["uniquePaymentMethods"] = sorted(uniq.values(), key=lambda u: -u["orderCount"])
    state["ordersWithIssues"] = {oid: r["issues"] for oid, r in sorted(state["orders"].items()) if r.get("issues")}
    state["updatedAt"] = datetime.now(timezone.utc).isoformat()
    write_vocab_report(state)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(out)


def wait_for_human(page: Page, order_id: str) -> None:
    """Sign-in/CAPTCHA must be resolved by the user in the Chrome tab; never automated."""
    msg = f"[{order_id}] Amazon wants sign-in/CAPTCHA. Resolve it in the Chrome tab"
    if sys.stdin.isatty():
        input(f"\n{msg}, then press Enter... ")
        return
    print(f"\n{msg}; polling for up to 10 minutes...", flush=True)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        time.sleep(5)
        if not needs_human(page):
            return
    sys.exit(f"[{order_id}] still blocked on sign-in/CAPTCHA after 10 minutes; progress saved")


def needs_human(page: Page) -> bool:
    u = page.url
    return "/ap/signin" in u or "/errors/validateCaptcha" in u or page.locator("form[action*='validateCaptcha']").count() > 0


def fetch_order(page: Page, order_id: str, html_dir: Path | None) -> dict:
    page.goto(PRINT_URL.format(order_id=order_id), wait_until="domcontentloaded", timeout=45_000)
    while needs_human(page):
        wait_for_human(page, order_id)
        page.goto(PRINT_URL.format(order_id=order_id), wait_until="domcontentloaded", timeout=45_000)
    try:
        # The payment widget is client-rendered; give it time to appear.
        page.wait_for_selector(INSTRUMENT_SEL, timeout=12_000)
    except PWTimeout:
        pass
    raw = page.evaluate(EXTRACT_JS)
    rec = normalise(raw)
    if html_dir is not None:
        # Every fetch is kept as its own version; html/<id>.html is only replaced if the
        # fetch is accepted (see quality()).
        versions = html_dir / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        v = versions / f"{order_id}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.html"
        v.write_text(page.content())
        rec["raw"] = raw
        rec["_version"] = str(v)
    return rec


def quality(rec: dict | None) -> int:
    """How much a record tells us; a fetch never replaces a better one.
    4 card(s) shown > 3 a definite no-charge state > 2 payment unavailable/unknown >
    1 page we cannot parse > 0 fetch error / nothing."""
    if not rec or rec.get("status") == "error":
        return 0
    if any(i["kind"] in ("unknown_format", "parse") for i in rec.get("issues", [])):
        return 1
    if rec.get("paymentMethods"):
        return 4
    if rec.get("orderState") in ("no_current_charges", "cancelled"):
        return 3
    return 2


def reparse(orders: list[dict], state: dict, out: Path, html_dir: Path, reviews: dict[str, dict]) -> None:
    """Re-run the extractor over saved HTML in a throwaway headless Chrome with all network blocked."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context(java_script_enabled=False)
        ctx.route("**/*", lambda route: route.abort())
        page = ctx.new_page()
        n = 0
        for o in orders:
            f = html_dir / f"{o['orderId']}.html"
            if not f.exists():
                continue
            page.set_content(f.read_text(), wait_until="domcontentloaded")
            raw = page.evaluate(EXTRACT_JS)
            prev = state["orders"].get(o["orderId"], {})
            rec = normalise(raw)
            rec.update(
                url=prev.get("url") or PRINT_URL.format(order_id=o["orderId"]),
                raw=raw,
                schemaVersion=SCHEMA_VERSION,
                exportOrderDate=o["orderDate"],
                exportTotalAmount=o["totalAmount"],
                fetchedAt=prev.get("fetchedAt"),
                reparsedAt=datetime.now(timezone.utc).isoformat(),
            )
            rec["issues"] = validate(rec, o, raw.get("orderId"))
            apply_review(rec, reviews.get(o["orderId"]))
            state["orders"][o["orderId"]] = rec
            n += 1
        browser.close()
    save_state(out, state)
    print(f"re-parsed {n} saved pages -> {out}")
    report(state)


def is_done(rec: dict | None) -> bool:
    return rec is not None and rec.get("status") == "ok" and rec.get("schemaVersion", 1) >= SCHEMA_VERSION


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=parse_date, default=date(1990, 1, 1), help="inclusive, YYYY-MM-DD")
    ap.add_argument("--end", type=parse_date, default=date(2100, 1, 1), help="inclusive, YYYY-MM-DD")
    ap.add_argument("--ids-file", type=Path, default=None,
                    help="only these order ids (one per line), e.g. debug/orders-need-order-page.txt from build_orders.py")
    ap.add_argument("--no-stop", action="store_true", help="keep going past pages we cannot parse (default: stop at the first)")
    ap.add_argument("--accept-vocab", action="store_true", help="mark every charge label/section/status/heading seen so far as reviewed")
    ap.add_argument("--orders-json", type=Path, default=None, help="default: newest amazon-orders-*.json next to this script")
    ap.add_argument("--out", type=Path, default=HERE / "order-details.json")
    ap.add_argument("--cdp", default="http://127.0.0.1:9222", help="Chrome DevTools endpoint")
    ap.add_argument("--min-gap", type=float, default=3.0)
    ap.add_argument("--max-gap", type=float, default=5.0)
    ap.add_argument("--refetch", action="store_true", help="re-fetch orders already in --out")
    ap.add_argument("--debug", action="store_true", help="save rendered HTML + raw extraction per order")
    ap.add_argument("--reparse", action="store_true", help="re-extract from saved HTML only; no Amazon requests")
    ap.add_argument("--html-dir", type=Path, default=HERE / "html")
    ap.add_argument("--reviews", type=Path, default=HERE / "reviews.json", help="human review decisions (see load_reviews)")
    args = ap.parse_args()
    if args.accept_vocab:
        state = load_state(args.out)
        vocab = {f: set(v) for f, v in reviewed_vocab().items()}
        for r in state["orders"].values():
            for f, vals in r.get("vocab", {}).items():
                vocab.setdefault(f, set()).update(v for v in vals if v)
        VOCAB_PATH.write_text(json.dumps({f: sorted(v) for f, v in vocab.items()}, indent=2, ensure_ascii=False))
        print(f"accepted {sum(len(v) for v in vocab.values())} values -> {VOCAB_PATH}")
        return
    if args.start > args.end:
        sys.exit("--start must be <= --end")

    orders_path = args.orders_json or latest_orders_json()
    orders = load_orders(orders_path, args.start, args.end)
    if args.ids_file:
        wanted = {ln.strip() for ln in args.ids_file.read_text().splitlines() if ln.strip()}
        missing = wanted - {o["orderId"] for o in orders}
        if missing:
            print(f"warning: {len(missing)} ids from {args.ids_file} are not in the export/date range, e.g. {sorted(missing)[:3]}")
        orders = [o for o in orders if o["orderId"] in wanted]
    state = load_state(args.out)
    state.update(source=str(orders_path), range={"start": args.start.isoformat(), "end": args.end.isoformat()})
    reviews = load_reviews(args.reviews)
    if args.reparse:
        reparse(orders, state, args.out, args.html_dir, reviews)
        return
    html_dir = args.html_dir if args.debug else None
    if html_dir is not None and not args.refetch:
        # debug mode: a saved page is the evidence; re-evaluate it with --reparse instead of refetching
        todo = [o for o in orders if not (html_dir / f"{o['orderId']}.html").exists()]
    else:
        todo = [o for o in orders if args.refetch or not is_done(state["orders"].get(o["orderId"]))]
    print(f"{len(orders)} orders in range, {len(todo)} to fetch -> {args.out}")
    if not todo:
        save_state(args.out, state)
        return

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(args.cdp)
        page = browser.contexts[0].new_page()  # own tab in the same window; never hijack the user's tab
        try:
            for i, o in enumerate(todo, 1):
                oid = o["orderId"]
                try:
                    rec = fetch_order(page, oid, html_dir)
                except Exception as e:  # keep going; record the failure for a later retry
                    rec = {"paymentMethods": [], "status": "error", "error": f"{type(e).__name__}: {e}"}
                rec.update(
                    schemaVersion=SCHEMA_VERSION,
                    exportOrderDate=o["orderDate"],
                    exportTotalAmount=o["totalAmount"],
                    fetchedAt=datetime.now(timezone.utc).isoformat(),
                )
                rec["issues"] = validate(rec, o, rec.get("pageOrderId"))
                apply_review(rec, reviews.get(oid))
                version = rec.pop("_version", None)
                existing = state["orders"].get(oid)
                if existing and quality(rec) < quality(existing):
                    # Worse than what we have (e.g. a transient "unable to display"): keep the old record.
                    existing.setdefault("rejectedFetches", []).append(
                        {"fetchedAt": rec["fetchedAt"], "status": rec.get("status"), "orderState": rec.get("orderState"),
                         "quality": quality(rec), "keptQuality": quality(existing), "html": version})
                    save_state(args.out, state)
                    print(f"[{i}/{len(todo)}] {o['orderDate']} {oid}: new fetch is worse ({quality(rec)} < {quality(existing)}); kept existing", flush=True)
                    if i < len(todo):
                        time.sleep(random.uniform(args.min_gap, args.max_gap))
                    continue
                if existing and existing.get("rejectedFetches"):
                    rec["rejectedFetches"] = existing["rejectedFetches"]
                if version and html_dir is not None:
                    shutil.copyfile(version, html_dir / f"{oid}.html")
                state["orders"][oid] = rec
                save_state(args.out, state)
                pay = ", ".join(m["display"] for m in rec["paymentMethods"]) or rec["status"]
                total = rec.get("charges", {}).get("grandTotal")
                print(f"[{i}/{len(todo)}] {o['orderDate']} {oid}: {pay} | total {total}", flush=True)
                unparsed = [x for x in rec["issues"] if x["kind"] in ("unknown_format", "parse")]
                if unparsed and not args.no_stop:
                    for x in unparsed:
                        print(f"  FLAG {x['kind']} {x['code']}: {x['detail']}")
                    print(f"\nStopped on an order page we cannot fully parse (saved: html/{oid}.html + order-details.json).\n"
                          "Fix the parser, confirm with --reparse, then re-run the same command to continue.")
                    sys.exit(2)
                if i < len(todo):
                    time.sleep(random.uniform(args.min_gap, args.max_gap))
        except KeyboardInterrupt:
            print("\ninterrupted; progress saved")
        finally:
            page.close()
        # Leaving sync_playwright() just drops the CDP connection; the user's Chrome stays open.

    report(state)


def report(state: dict) -> None:
    counts = write_vocab_report(state)
    print(f"debug/orders-unaligned.json: {counts['structural']} unparseable pages, {counts['unreviewedValues']} unreviewed values "
          "(labels/sections/statuses; review, then --accept-vocab)")
    bad = state.get("ordersWithIssues") or {}
    acked = sum(1 for r in state["orders"].values() if r.get("acknowledgedIssues"))
    print(f"\n{len(state['orders'])} orders stored, {len(bad)} need review, {acked} with reviewed (acknowledged) issues")
    for kind in ("unknown_format", "parse", "mismatch"):
        rows = [(oid, i) for oid, iss in bad.items() for i in iss if i["kind"] == kind]
        if rows:
            print(f"\n  {kind} ({len(rows)}):")
            for oid, i in rows:
                print(f"    {oid}  {i['code']}: {i['detail']}")
    print("\nUnique payment methods:")
    for u in state["uniquePaymentMethods"]:
        print(f"  {u['method']:<45} {u['orderCount']:>4} orders  ${u['grandTotal']:,.2f}")


if __name__ == "__main__":
    main()
