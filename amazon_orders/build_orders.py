#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Build one reconciled record per order in the orders export, from every source we have.

Sources (all local, no Amazon requests):
  amazon-orders-*.json   the export: order id, date, total            (every order)
  transactions.json      Your Payments ledger: card/points charges     (Sep 2021 ->, see its "coverage")
  giftcards.json         Gift Card activity: applications, releases    (Dec 2015 ->)
  order-details.json     order print pages: payment methods as shown   (whatever has been fetched)
  reviews.json           your decisions per order (REVIEW_KEYS; see apply_review)

Money rule (verified on the data: 940 of 944 in-window orders): the export's totalAmount
is what was charged to EXTERNAL methods (cards, Venmo, ...) after gift card and points
were applied. So an order reconciles when
    sum(ledger charges to external methods) == export total        (to the cent)
Gift card (net of hold releases) and points are recorded on top: orderValue = export
total + gift card + points. Gift card amounts come from giftcards.json only; the ledger's
own "Amazon Gift Card" rows are only used to cross-check it (adding them would double
count). Charges that name several orders at once are reconciled for the whole group.

Every order gets a `status`, its payment methods with amounts and sources, and `flags`.
Any order that does not reconcile is written, with all its evidence, to
debug/orders-unreconciled.json. Orders whose payment method we cannot establish from
the ledger or gift cards are listed in debug/orders-need-order-page.txt, ready for
fetch_payment_methods.py --ids-file.

A flagged order stays flagged until you settle it in reviews.json: `acknowledge` lists the
flag codes you checked and accept (the computed card stands), `noCard` says no credit card
was charged (points / gift card paid), `cardAmount` replaces the card's amount (e.g. the
ledger lists one charge twice), `manualPaymentMethod` names a method no source shows (e.g.
cash). Settled that way, the status becomes `reviewed` (or `manual`). `items` lists what an
order bought when the export lists nothing (a Whole Foods in-store purchase), as its order page
shows it.

Outputs: orders_db.json, amazon.db (SQLite), debug/orders-unreconciled.json,
debug/orders-need-order-page.txt.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEBUG = HERE / "debug"
CENT = 0.005
# Charges land up to a few weeks after the order (shipments, pre-orders). Orders this
# close to the ledger's oldest date may have charges that fell off the ledger.
WINDOW_EDGE_DAYS = 30
WF_MATCH_DAYS = 3


def r2(x: float) -> float:
    return round(x + 0.0, 2)


def latest_export() -> Path:
    files = sorted(HERE.glob("amazon-orders-*.json"))
    if not files:
        raise SystemExit(f"no amazon-orders-*.json in {HERE}")
    return files[-1]


def load_json(path: Path, default: Any) -> Any:
    return json.loads(path.read_text()) if path.exists() else default


def method_key_ledger(r: dict[str, Any]) -> dict[str, Any]:
    if r["instrumentKind"] == "card":
        return {"method": f"{r['network']} ****{r['last4']}", "kind": "card", "network": r["network"], "last4": r["last4"]}
    return {"method": r["instrumentLabel"], "kind": r["instrumentKind"], "network": None, "last4": None}


class Union:
    def __init__(self) -> None:
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def join(self, a: str, b: str) -> None:
        self.p[self.find(a)] = self.find(b)


def build(export_path: Path) -> dict[str, Any]:
    orders = json.loads(export_path.read_text())
    tx = load_json(HERE / "transactions.json", {"pages": [], "coverage": {}, "end": {}})
    gc = load_json(HERE / "giftcards.json", {"pages": []})
    details = load_json(HERE / "order-details.json", {"orders": {}})["orders"]
    reviews = load_json(HERE / "reviews.json", {})
    check_reviews(reviews, {o["orderId"] for o in orders})

    # retainedRows: charges from earlier complete crawls that aged out of Amazon's window
    txr = [r for p in tx["pages"] for r in p["rows"]] + tx.get("retainedRows", [])
    gcr = [r for p in gc["pages"] for r in p["rows"]]
    ledger_oldest: str | None = min((r["chargeDate"] for r in txr if r["chargeDate"]), default=None)
    ledger_complete = bool(tx.get("end", {}).get("reached"))
    gc_oldest = min((r["date"] for r in gcr if r["date"]), default=None)
    by_id = {o["orderId"]: o for o in orders}

    # Card names shown on order pages ("Apple Card") keyed by last4, to label ledger cards.
    aliases: dict[str, set[str]] = defaultdict(set)
    for d in details.values():
        for m in d.get("paymentMethods", []):
            if m.get("number") and m.get("name"):
                aliases[m["number"]].add(m["name"])

    # ---- ledger rows -> orders -------------------------------------------------------
    ledger_by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ledger_gc_by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)
    uf = Union()
    unlinked_ledger: list[dict[str, Any]] = []
    wf_rows = []
    for r in txr:
        refs = [x for x in r["orderRefs"] if x in by_id]
        if r["orderRefPrefixes"] == ["uuid"]:
            wf_rows.append(r)
            continue
        if not refs:
            unlinked_ledger.append(r)
            continue
        for a, b in zip(refs, refs[1:]):
            uf.join(a, b)
        target = ledger_gc_by_order if r["instrumentKind"] == "gift_card" else ledger_by_order
        target[refs[0]].append({**r, "_sharedWith": refs[1:]})

    # Whole Foods / store rows carry a store UUID: match by amount + date to one export order.
    wf_links: dict[str, dict[str, Any]] = {}
    wf_ambiguous: list[dict[str, Any]] = []
    for r in wf_rows:
        cd = date.fromisoformat(r["chargeDate"])
        cands = [o for o in orders if o["orderId"] not in wf_links
                 and abs((o["totalAmount"] or 0) - (r["amount"] or 0)) <= CENT
                 and timedelta(0) <= cd - date.fromisoformat(o["orderDate"]) <= timedelta(days=WF_MATCH_DAYS)]
        if len(cands) == 1:
            wf_links[cands[0]["orderId"]] = r
            ledger_by_order[cands[0]["orderId"]].append({**r, "_matchedBy": "store charge: same amount within 3 days"})
        else:
            (wf_ambiguous if cands else unlinked_ledger).append({**r, "_candidates": [o["orderId"] for o in cands]})

    # ---- gift card rows -> orders ----------------------------------------------------
    gc_by_order: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in gcr:
        for oid in r["orderIds"]:
            gc_by_order[oid].append(r)

    # ---- per order -------------------------------------------------------------------
    records: dict[str, dict[str, Any]] = {}
    for o in orders:
        oid, odate, total = o["orderId"], o["orderDate"], r2(o.get("totalAmount") or 0)
        flags: list[dict[str, str]] = []
        methods: dict[str, dict[str, Any]] = {}

        def add(m: dict[str, Any], amount: float, source: str) -> None:
            e = methods.setdefault(m["method"], {**m, "amount": 0.0, "sources": set()})
            e["amount"] = r2(e["amount"] + amount)
            e["sources"].add(source)

        charges = [r for r in ledger_by_order.get(oid, []) if r["direction"] == "charge"]
        refunds = [r for r in ledger_by_order.get(oid, []) if r["direction"] == "refund"]
        for r in charges:
            add(method_key_ledger(r), r["amount"], "ledger")

        gc_rows = gc_by_order.get(oid, [])
        gc_applied = r2(sum(-r["amount"] for r in gc_rows if r["amount"] < 0))
        gc_released = r2(sum(r["amount"] for r in gc_rows if r["amount"] > 0 and "hold" in (r["label"] or "").lower()))
        gc_refunded = r2(sum(r["amount"] for r in gc_rows if r["amount"] > 0 and "hold" not in (r["label"] or "").lower()))
        gc_net = r2(gc_applied - gc_released)
        if gc_net:
            add({"method": "Amazon Gift Card", "kind": "gift_card", "network": None, "last4": None}, gc_net, "giftcards")

        # Cross-check: the ledger's gift card rows must equal the gift card page's net.
        led_gc = r2(sum(r["amount"] if r["direction"] == "charge" else -r["amount"] for r in ledger_gc_by_order.get(oid, [])))
        if ledger_gc_by_order.get(oid) and abs(led_gc - gc_net) > CENT:
            flags.append({"code": "giftcard_crosscheck", "detail": f"ledger gift card rows {led_gc} vs gift card page net {gc_net}"})

        page = details.get(oid)
        page_methods = page.get("paymentMethods", []) if page else []
        for m in page_methods:
            last4 = m.get("number")
            match = next((k for k, v in methods.items() if last4 and v.get("last4") == last4), None)
            if match:
                methods[match]["sources"].add("order_page")
            elif not charges:  # no ledger evidence: the order page is the source (amount unknown)
                label = m.get("display") if last4 else f"{m.get('name') or '?'} (no number shown)"
                e = methods.setdefault(label, {"method": label, "kind": "card" if last4 else "card_unidentified",
                                               "network": None, "last4": last4, "amount": None, "sources": set()})
                e["sources"].add("order_page")

        review = reviews.get(oid)
        if review and review.get("manualPaymentMethod"):
            mm = review["manualPaymentMethod"]
            add({"method": f"{mm['name']} (manual)", "kind": "manual", "network": None, "last4": None}, 0.0, "review")

        external = r2(sum(r["amount"] for r in charges if r["instrumentKind"] not in ("gift_card", "points")))
        points = r2(sum(r["amount"] for r in charges if r["instrumentKind"] == "points"))
        in_window = ledger_oldest is not None and odate >= ledger_oldest
        edge = (ledger_oldest is not None and in_window
                and date.fromisoformat(odate) - date.fromisoformat(ledger_oldest) < timedelta(days=WINDOW_EDGE_DAYS))

        records[oid] = {
            "orderId": oid, "orderDate": odate, "exportTotal": total,
            # Currency as the order page shows it (the export's label is unreliable: it calls
            # $0 orders "EUR"); without a page, the export's label is all we have.
            "currency": (page.get("currency") if page else None) or o.get("currency"),
            "currencySource": "order_page" if page and page.get("currency") else "export",
            "exportCurrency": o.get("currency"),
            "paidExternal": external, "diff": r2(external - total),
            "giftCardNet": gc_net, "points": points, "orderValue": r2(total + gc_net + points),
            "ledgerWindow": "inside" if in_window and not edge else "edge" if edge else "before",
            "paymentMethods": methods,
            "ledgerCharges": [{"date": r["chargeDate"], "method": method_key_ledger(r)["method"], "amount": r["amount"],
                               "merchant": r["merchant"], "page": r["page"], "position": r["position"],
                               **({"matchedBy": r["_matchedBy"]} if "_matchedBy" in r else {}),
                               **({"sharedWith": r["_sharedWith"]} if r.get("_sharedWith") else {})} for r in charges],
            "ledgerRefunds": [{"date": r["chargeDate"], "method": method_key_ledger(r)["method"], "amount": r["amount"],
                               "page": r["page"], "position": r["position"]} for r in refunds],
            "giftCard": {"applied": gc_applied, "holdReleased": gc_released, "refundedToGiftCard": gc_refunded,
                         "rows": [{"date": r["date"], "label": r["label"], "amount": r["amount"], "page": r["page"],
                                   "position": r["position"]} for r in gc_rows]},
            "orderPage": {"fetched": bool(page), "paymentMethods": [m.get("display") for m in page_methods],
                          "grandTotal": (page.get("charges") or {}).get("grandTotal") if page else None,
                          "orderState": page.get("orderState") if page else None,
                          # page Grand Total - export total; Grand Total - shown lines (tax not shown etc.)
                          "exportDiff": page.get("exportDiff") if page else None,
                          "summaryDiff": page.get("summaryDiff") if page else None,
                          "html": f"html/{oid}.html" if (HERE / "html" / f"{oid}.html").exists() else None},
            "review": review,
            "flags": flags,
        }

    # ---- status, with shared charges reconciled per group ----------------------------
    groups: dict[str, list[str]] = defaultdict(list)
    for oid in records:
        groups[uf.find(oid) if oid in uf.p else oid].append(oid)
    for members in groups.values():
        group_paid = r2(sum(records[m]["paidExternal"] for m in members))
        group_total = r2(sum(records[m]["exportTotal"] for m in members))
        for oid in members:
            rec = records[oid]
            rec["group"] = members if len(members) > 1 else None
            rec["status"] = status_for(rec, group_paid, group_total, len(members) > 1, ledger_complete, gc_oldest)
    for rec in records.values():
        for m in rec["paymentMethods"].values():
            m["sources"] = sorted(m["sources"])
            if m.get("last4") and aliases.get(m["last4"]):
                m["aliases"] = sorted(aliases[m["last4"]])
        rec["paymentMethods"] = sorted(rec["paymentMethods"].values(), key=lambda m: -(m["amount"] or 0))

    # Card network by last4 as the ledger names it ("Amazon Store Card"), to unify with
    # order-page names ("AmazonPLCC") for the same card.
    ledger_network: dict[str, set[str]] = defaultdict(set)
    for r in txr:
        if r["instrumentKind"] == "card":
            ledger_network[r["last4"]].add(r["network"])
    for oid, rec in records.items():
        rec["card"] = card_for(rec, details.get(oid), ledger_network, aliases)
        apply_review(rec)
        rec["shipTo"] = ship_for(by_id[oid], details.get(oid))
        rec["items"] = items_for(by_id[oid], rec["review"])

    return {"records": records, "unlinkedLedger": unlinked_ledger, "wfAmbiguous": wf_ambiguous,
            "meta": {"export": export_path.name, "ledgerOldest": ledger_oldest, "ledgerComplete": ledger_complete,
                     "giftcardOldest": gc_oldest, "orderPages": len(details)}}


ORDER_URL = "https://www.amazon.com/gp/your-account/order-details?orderID={}"
PRODUCT_URL = "https://www.amazon.com/dp/{}"
NO_CARD_METHODS = ("gift card", "points")


def card_for(rec: dict[str, Any], page: dict[str, Any] | None, ledger_network: dict[str, set[str]],
             aliases: dict[str, set[str]]) -> dict[str, Any] | None:
    """The one credit card an order was charged to (gift card / points ignored).
    Ledger evidence first (exact amount); else the card on the order page, with the
    export total as its amount (the export total is the post-gift-card charge)."""
    def label(network: str | None, last4: str | None, name: str | None) -> str:
        return f"{network or name or 'Card'} ****{last4}" if last4 else f"{name or 'Card'} (no number shown)"

    ledger_cards = [m for m in rec["paymentMethods"] if m["kind"] == "card" and "ledger" in m["sources"] and m["amount"]]
    if ledger_cards:
        primary = max(ledger_cards, key=lambda m: m["amount"])
        card = {"key": f"{primary['network']}|{primary['last4']}", "label": primary["method"], "kind": "card",
                "network": primary["network"],
                "last4": primary["last4"], "aliases": sorted(aliases.get(primary["last4"], set())),
                "amount": r2(sum(m["amount"] for m in ledger_cards)), "cardSource": "ledger", "amountSource": "ledger"}
        if len(ledger_cards) > 1:
            rec["flags"].append({"code": "multi_card", "detail": "charged to several cards: " + ", ".join(
                f"{m['method']} {m['amount']}" for m in ledger_cards) + f"; primary = {primary['method']}"})
            # Each card's own share, so spend per card stays exact; the order's card is the primary.
            card["split"] = [{"key": f"{m['network']}|{m['last4']}", "label": m["method"],
                              "aliases": sorted(aliases.get(m["last4"], set())), "amount": m["amount"]} for m in ledger_cards]
        return card
    if rec["status"] == "payment_unknown":
        return {"key": "unknown", "label": "Unknown", "kind": "unknown", "network": None, "last4": None, "aliases": [],
                "amount": rec["exportTotal"], "cardSource": "order_page", "amountSource": "export_total"}
    if rec["status"] == "manual":
        mm = rec["review"]["manualPaymentMethod"]
        return {"key": f"manual|{mm['name'].lower()}", "label": f"{mm['name']} (manual)", "kind": "manual", "network": None,
                "last4": None, "aliases": [], "amount": rec["exportTotal"], "cardSource": "review", "amountSource": "export_total"}
    if not page or rec["status"] in ("zero_total", "paid_by_credit"):
        return None
    shown = [m for m in page.get("paymentMethods", [])
             if m.get("name") and not any(w in m["name"].lower() for w in NO_CARD_METHODS)]
    if not shown:
        return None
    m = shown[0]
    last4 = m.get("number")
    nets = ledger_network.get(last4 or "", set())
    network = next(iter(nets)) if len(nets) == 1 else None
    if len(shown) > 1:
        rec["flags"].append({"code": "multi_card", "detail": "order page shows several cards: " + ", ".join(x.get("display") or "?" for x in shown)})
    return {"key": f"{network or m['name']}|{last4}" if last4 else f"unidentified|{m['name']}",
            "label": label(network, last4, m["name"]), "kind": "card" if last4 else "card_unidentified",
            "network": network, "last4": last4,
            "aliases": sorted(aliases.get(last4, set()) | {m["name"]}) if last4 else [m["name"]],
            "amount": rec["exportTotal"], "cardSource": "order_page", "amountSource": "export_total"}


def ship_for(o: dict[str, Any], page: dict[str, Any] | None) -> dict[str, Any]:
    """Ship-to from the export; kind from the order page heading when we have it
    ('Ship to' -> ship, 'PICKUP AT' -> store), so store purchases are not mistaken for deliveries."""
    street, city = (o.get("recipientStreet") or "").strip(), (o.get("recipientCityPostal") or "").strip()
    page_kind = ((page or {}).get("shipTo") or {}).get("kind")
    if not street:
        kind, source = "none", "export"
    elif page_kind:
        kind, source = {"pickup": "store"}.get(page_kind, page_kind), "order_page"
    else:
        kind, source = "ship", "export (unconfirmed)"
    return {"key": f"{street.upper()} | {city.upper()}" if street else "(none)", "name": o.get("recipientName"),
            "street": street or None, "cityPostal": city or None, "country": o.get("recipientCountry"),
            "kind": kind, "kindSource": source}


def items_for(o: dict[str, Any], review: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The export's items; for an order the export lists none, the items you recorded in
    reviews.json from its order page. Both at once is a contradiction and stops the build."""
    given = (review or {}).get("items") or []
    if given and o.get("items"):
        raise SystemExit(f"reviews.json: {o['orderId']}: items given, but the export lists {len(o['items'])}")
    out = []
    for n, it in enumerate(o.get("items", []), 1):
        qty = it.get("quantity") or 1
        out.append({"itemId": f"{o['orderId']}#{n}", "line": n, "asin": it.get("asin"), "title": it.get("title"),
                    "quantity": qty, "unitPrice": it.get("price"), "discount": it.get("discount") or 0,
                    "lineTotal": r2((it.get("price") or 0) * qty), "productUrl": PRODUCT_URL.format(it["asin"]) if it.get("asin") else None,
                    "itemUrl": it.get("itemUrl")})
    for n, it in enumerate(given, 1):
        out.append({"itemId": f"{o['orderId']}#{n}", "line": n, "asin": None, "title": it["title"],
                    "quantity": it["quantity"], "unitPrice": it["unitPrice"], "discount": it.get("discount", 0),
                    "lineTotal": it["lineTotal"], "productUrl": None, "itemUrl": None})
    return out


def status_for(rec: dict[str, Any], group_paid: float, group_total: float, grouped: bool,
               ledger_complete: bool, gc_oldest: str | None) -> str:
    """One word per order. Settled outcomes carry no flag: reconciled* (ledger to the cent),
    paid_by_credit / zero_total, order_page_only (card as the page shows it, export total as
    amount), payment_unknown (Amazon shows no payment details). Everything else is flagged
    until reviews.json settles it (apply_review)."""
    total, ext, flags = rec["exportTotal"], rec["paidExternal"], rec["flags"]
    credit = r2(rec["giftCardNet"] + rec["points"])
    page = rec["orderPage"]
    manual = rec["review"] and rec["review"].get("manualPaymentMethod")
    # A group member can carry none of the shared charge itself; the group total is what reconciles.
    if abs(group_paid - group_total) <= CENT and (ext or (grouped and group_paid)):
        return "reconciled_group" if grouped else "reconciled"
    if total == 0 and group_paid == 0:
        return "paid_by_credit" if credit else "zero_total"
    if manual:
        return "manual"  # decided by you in reviews.json (e.g. cash); settled, not a review item
    if rec["currencySource"] == "order_page" and rec["currency"] != "USD" and ext:
        # e.g. a Currency Converter order: totals in INR, the card charged in USD. Settled by
        # the ledger: the card amount is the USD charge; the order's totals stay in rec['currency'].
        return "foreign_currency"
    if page["fetched"] and page["orderState"] in ("no_current_charges", "unavailable") and ext == 0:
        # Amazon shows no payment details ("No current charges" / "Unable to display...") and no card
        # charge exists. Decided 2026-09-22: the method is Unknown; the amount is the export total
        # (what was paid after gift card / points). A settled outcome, not a review item.
        return "payment_unknown"
    if rec["ledgerWindow"] == "before":
        if page["fetched"]:
            # Settled as provided: the card the order page names (or "Card (no number shown)"),
            # with the export total as its amount. Not a review item.
            return "order_page_only"
        flags.append({"code": "needs_order_page", "detail": "placed before the ledger window and no order page fetched yet"})
        return "needs_order_page"
    if rec["ledgerWindow"] == "edge" and ext < total:
        flags.append({"code": "ledger_edge", "detail": f"placed within {WINDOW_EDGE_DAYS} days of the ledger's oldest charge; earlier charges may have fallen off"})
    if not ledger_complete:
        flags.append({"code": "ledger_incomplete", "detail": "transactions.json does not record reaching the end of the ledger"})
    if grouped:
        flags.append({"code": "group_mismatch", "detail": f"orders {rec['group']} share charges: external {group_paid} vs totals {group_total}"})
        return "group_mismatch"
    if ext == 0:
        flags.append({"code": "no_ledger_charge", "detail": f"export total {total} but no external charge in the ledger"})
        return "unpaid"
    flags.append({"code": "underpaid" if ext < total else "overpaid", "detail": f"external charges {ext} vs export total {total} (diff {rec['diff']})"})
    return "underpaid" if ext < total else "overpaid"


# Outcomes that need nothing from you; any other status carries a flag.
SETTLED = {"reconciled", "reconciled_group", "paid_by_credit", "zero_total", "order_page_only",
           "payment_unknown", "foreign_currency", "manual", "reviewed"}
REVIEW_KEYS = {"acknowledge", "noCard", "cardAmount", "manualPaymentMethod", "items", "note", "source"}
ITEM_KEYS = {"title", "quantity", "unitPrice", "lineTotal", "discount"}     # discount is optional


def check_reviews(reviews: dict[str, Any], order_ids: set[str]) -> None:
    """Fail closed on a mistyped decision: an unknown key or order id would otherwise be ignored silently."""
    problems = [f"{oid}: not an order in the export" for oid in reviews if oid not in order_ids]
    problems += [f"{oid}: unknown key {k!r} (known: {sorted(REVIEW_KEYS)})"
                 for oid, review in reviews.items() for k in review if k not in REVIEW_KEYS]
    problems += [f"{oid}: cardAmount must be a number" for oid, review in reviews.items()
                 if "cardAmount" in review and (isinstance(review["cardAmount"], bool)
                                                or not isinstance(review["cardAmount"], (int, float)))]
    for oid, review in reviews.items():
        items = review.get("items", [])
        if not isinstance(items, list):
            problems.append(f"{oid}: items must be a list")
            continue
        for i, it in enumerate(items):
            ok = isinstance(it, dict) and ITEM_KEYS - {"discount"} <= set(it) <= ITEM_KEYS
            if not ok or not (isinstance(it["title"], str) and it["title"].strip()) or not all(
                    isinstance(it[k], (int, float)) and not isinstance(it[k], bool)
                    for k in ("quantity", "unitPrice", "lineTotal", *(("discount",) if "discount" in it else ()))):
                problems.append(f"{oid}: items[{i}] needs a title and numeric quantity, unitPrice, lineTotal "
                                "(discount optional)")
    if problems:
        raise SystemExit("reviews.json:\n  " + "\n  ".join(problems))


def apply_review(rec: dict[str, Any]) -> None:
    """Your decisions settle an order: flags whose code you acknowledged move to
    acknowledgedFlags (the computed card stands); noCard drops the card (no credit card was
    charged); cardAmount replaces the card's amount. A flagged order with nothing left open,
    or one you gave noCard, becomes 'reviewed'."""
    review = rec["review"] or {}
    acked = {a if isinstance(a, str) else a["code"] for a in review.get("acknowledge", [])}
    rec["acknowledgedFlags"] = [f for f in rec["flags"] if f["code"] in acked]
    rec["flags"] = [f for f in rec["flags"] if f["code"] not in acked]
    if review.get("noCard"):
        rec["card"] = None
    if "cardAmount" in review:
        if not rec["card"] or rec["card"].get("split"):
            raise SystemExit(f"reviews.json: {rec['orderId']}: cardAmount needs exactly one card to apply to")
        rec["card"]["amount"], rec["card"]["amountSource"] = r2(review["cardAmount"]), "review"
    if review and not rec["flags"] and (review.get("noCard") or rec["status"] not in SETTLED):
        rec["statusBeforeReview"], rec["status"] = rec["status"], "reviewed"


def write_outputs(result: dict[str, Any]) -> None:
    records = result["records"]
    DEBUG.mkdir(exist_ok=True)
    counts: dict[str, int] = defaultdict(int)
    for r in records.values():
        counts[r["status"]] += 1

    doc = {"generatedAt": datetime.now(timezone.utc).isoformat(), **result["meta"],
           "statusCounts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
           "orders": sorted(records.values(), key=lambda r: r["orderDate"], reverse=True),
           "ledgerRowsNotInExport": [{"date": r["chargeDate"], "method": method_key_ledger(r)["method"], "amount": r["amount"],
                                      "direction": r["direction"], "orderRefs": r["orderRefs"], "merchant": r["merchant"],
                                      "candidates": r.get("_candidates")} for r in result["unlinkedLedger"]],
           "storeChargesAmbiguous": [{"date": r["chargeDate"], "amount": r["amount"], "merchant": r["merchant"],
                                      "candidates": r["_candidates"]} for r in result["wfAmbiguous"]]}
    (HERE / "orders_db.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False))

    bad = [r for r in doc["orders"] if r["flags"]]
    (DEBUG / "orders-unreconciled.json").write_text(json.dumps(
        {"generatedAt": doc["generatedAt"], "count": len(bad),
         "byStatus": {s: sum(1 for r in bad if r["status"] == s) for s in sorted({r["status"] for r in bad})},
         "orders": bad}, indent=2, ensure_ascii=False))
    # "Suspect" orders: anything not reconciled whose order page we do not have yet.
    need = [r["orderId"] for r in doc["orders"] if r["flags"] and not r["orderPage"]["fetched"]]
    (DEBUG / "orders-need-order-page.txt").write_text("\n".join(need) + ("\n" if need else ""))

    write_sqlite(doc)
    print(f"{len(records)} orders -> orders_db.json, amazon.db")
    for s, n in doc["statusCounts"].items():
        print(f"  {s:<22} {n}")
    print(f"flagged: {len(bad)} -> debug/orders-unreconciled.json")
    print(f"need an order page fetched: {len(need)} -> debug/orders-need-order-page.txt")
    print(f"ledger rows not in the export (digital, in-store, ...): {len(doc['ledgerRowsNotInExport'])}; ambiguous store charges: {len(doc['storeChargesAmbiguous'])}")


def write_sqlite(doc: dict[str, Any]) -> None:
    path = HERE / "amazon.db"
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript("""
      CREATE TABLE orders(order_id TEXT PRIMARY KEY, order_date TEXT, export_total REAL, paid_external REAL, diff REAL,
                          gift_card REAL, points REAL, order_value REAL,
                          status TEXT, ledger_window TEXT, flags TEXT, order_page_methods TEXT,
                          card_key TEXT, card_label TEXT, card_network TEXT, card_last4 TEXT, card_aliases TEXT,
                          card_amount REAL, card_source TEXT, card_amount_source TEXT,
                          ship_key TEXT, ship_name TEXT, ship_street TEXT, ship_city_postal TEXT, ship_country TEXT,
                          ship_kind TEXT, ship_kind_source TEXT, order_url TEXT, order_page_html TEXT,
                          card_kind TEXT, page_export_diff REAL, currency TEXT, currency_source TEXT,
                          review_note TEXT, acknowledged_flags TEXT);
      CREATE TABLE items(item_id TEXT PRIMARY KEY, order_id TEXT REFERENCES orders(order_id), line INTEGER,
                         asin TEXT, title TEXT, quantity INTEGER, unit_price REAL, discount REAL, line_total REAL,
                         product_url TEXT, item_url TEXT);
      CREATE INDEX items_asin ON items(asin);
      CREATE INDEX orders_card ON orders(card_key);
      -- products: one row per ASIN, latest title
      CREATE VIEW top_items AS
        SELECT i.asin, (SELECT title FROM items j JOIN orders p USING(order_id) WHERE j.asin = i.asin ORDER BY p.order_date DESC LIMIT 1) AS title,
               COUNT(DISTINCT i.order_id) AS orders, SUM(i.quantity) AS units, ROUND(SUM(i.line_total), 2) AS item_spend,
               MIN(o.order_date) AS first_ordered, MAX(o.order_date) AS last_ordered, 'https://www.amazon.com/dp/' || i.asin AS product_url
        FROM items i JOIN orders o USING(order_id) GROUP BY i.asin ORDER BY orders DESC, units DESC;
      -- one row per card an order was charged to (almost always one); an order split across
      -- cards gives each card its own share, while orders.card_* names the primary card
      CREATE TABLE order_cards(order_id TEXT REFERENCES orders(order_id), card_key TEXT, card_label TEXT,
                               card_aliases TEXT, card_kind TEXT, amount REAL, amount_source TEXT);
      CREATE INDEX order_cards_card ON order_cards(card_key);
      CREATE VIEW spend_by_card AS
        SELECT c.card_key, c.card_label, c.card_aliases, COUNT(*) AS orders, ROUND(SUM(c.amount), 2) AS card_spend,
               SUM(c.amount_source = 'ledger') AS exact_orders, SUM(c.amount_source = 'export_total') AS derived_orders,
               MIN(o.order_date) AS first, MAX(o.order_date) AS last
        FROM order_cards c JOIN orders o USING(order_id) GROUP BY c.card_key ORDER BY card_spend DESC;
      CREATE VIEW spend_by_card_year AS
        SELECT substr(o.order_date, 1, 4) AS year, c.card_label, COUNT(*) AS orders, ROUND(SUM(c.amount), 2) AS card_spend
        FROM order_cards c JOIN orders o USING(order_id) GROUP BY year, c.card_key ORDER BY year DESC, card_spend DESC;
      CREATE VIEW items_by_card AS
        SELECT o.card_label, i.asin, MAX(i.title) AS title, COUNT(DISTINCT i.order_id) AS orders, SUM(i.quantity) AS units,
               ROUND(SUM(i.line_total), 2) AS item_spend
        FROM items i JOIN orders o USING(order_id) WHERE o.card_key IS NOT NULL GROUP BY o.card_key, i.asin ORDER BY orders DESC;
      CREATE VIEW spend_by_ship_to AS
        SELECT ship_key, ship_kind, COUNT(*) AS orders, ROUND(SUM(export_total), 2) AS order_total,
               ROUND(SUM(card_amount), 2) AS card_spend, MIN(order_date) AS first, MAX(order_date) AS last
        FROM orders GROUP BY ship_key, ship_kind ORDER BY orders DESC;
      CREATE VIEW items_by_ship_to AS
        SELECT o.ship_key, o.ship_kind, i.asin, MAX(i.title) AS title, COUNT(DISTINCT i.order_id) AS orders, SUM(i.quantity) AS units
        FROM items i JOIN orders o USING(order_id) GROUP BY o.ship_key, i.asin ORDER BY orders DESC;
      CREATE TABLE order_payments(order_id TEXT, method TEXT, kind TEXT, network TEXT, last4 TEXT,
                                  aliases TEXT, amount REAL, sources TEXT);
      CREATE TABLE ledger(charge_date TEXT, method TEXT, amount REAL, direction TEXT, order_refs TEXT,
                          merchant TEXT, in_export INTEGER);
      CREATE VIEW spend_by_method AS
        SELECT method, kind, last4, aliases, COUNT(DISTINCT order_id) AS orders, ROUND(SUM(amount), 2) AS total
        FROM order_payments WHERE amount IS NOT NULL GROUP BY method ORDER BY total DESC;
      CREATE VIEW spend_by_method_year AS
        SELECT substr(o.order_date, 1, 4) AS year, p.method, ROUND(SUM(p.amount), 2) AS total, COUNT(*) AS orders
        FROM order_payments p JOIN orders o USING(order_id) WHERE p.amount IS NOT NULL
        GROUP BY year, p.method ORDER BY year DESC, total DESC;
    """)
    for r in doc["orders"]:
        c, sh = r["card"] or {}, r["shipTo"]
        db.execute("INSERT INTO orders VALUES (" + ",".join("?" * 35) + ")",
                   (r["orderId"], r["orderDate"], r["exportTotal"], r["paidExternal"], r["diff"], r["giftCardNet"], r["points"],
                    r["orderValue"], r["status"], r["ledgerWindow"],
                    json.dumps(r["flags"]), json.dumps(r["orderPage"]["paymentMethods"]),
                    c.get("key"), c.get("label"), c.get("network"), c.get("last4"), ", ".join(c.get("aliases", [])),
                    c.get("amount"), c.get("cardSource"), c.get("amountSource"),
                    sh["key"], sh["name"], sh["street"], sh["cityPostal"], sh["country"], sh["kind"], sh["kindSource"],
                    ORDER_URL.format(r["orderId"]), r["orderPage"]["html"], c.get("kind"), r["orderPage"]["exportDiff"],
                    r["currency"], r["currencySource"], (r["review"] or {}).get("note"),
                    json.dumps(r["acknowledgedFlags"])))
        for s in (c.get("split") or [c]) if c else []:
            db.execute("INSERT INTO order_cards VALUES (?,?,?,?,?,?,?)",
                       (r["orderId"], s["key"], s["label"], ", ".join(s.get("aliases", [])), c["kind"], s["amount"],
                        c["amountSource"]))
        for it in r["items"]:
            db.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (it["itemId"], r["orderId"], it["line"], it["asin"], it["title"], it["quantity"], it["unitPrice"],
                        it["discount"], it["lineTotal"], it["productUrl"], it["itemUrl"]))
        for m in r["paymentMethods"]:
            db.execute("INSERT INTO order_payments VALUES (?,?,?,?,?,?,?,?)",
                       (r["orderId"], m["method"], m["kind"], m.get("network"), m.get("last4"),
                        ", ".join(m.get("aliases", [])), m["amount"], ",".join(m["sources"])))
    tx = load_json(HERE / "transactions.json", {"pages": []})
    ids = {r["orderId"] for r in doc["orders"]}
    for r in [r for p in tx["pages"] for r in p["rows"]] + tx.get("retainedRows", []):
        db.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?)",
                   (r["chargeDate"], method_key_ledger(r)["method"], r["amount"], r["direction"], ",".join(r["orderRefs"]),
                    r["merchant"], int(bool(set(r["orderRefs"]) & ids))))
    db.commit()
    db.close()
    tmp.replace(path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--orders-json", type=Path, default=None, help="default: newest amazon-orders-*.json")
    args = ap.parse_args()
    write_outputs(build(args.orders_json or latest_export()))


if __name__ == "__main__":
    main()
