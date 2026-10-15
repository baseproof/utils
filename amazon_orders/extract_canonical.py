#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Turn the Amazon build into the retailer-neutral canonical file that
../commerce/load_baserow.py loads into Baserow.

    uv run extract_canonical.py                        -> canonical.json (every order)
    uv run extract_canonical.py --since 2026-08-01     -> canonical-since-2026-08-01.json, a small
                                                          sample (for the "(test)" tables)

Reads orders_db.json (run build_orders.py first), the orders export it names,
order-details.json and transactions.json. Table and field names are commerce_schema.py's and
values use its vocabularies. The Amazon specifics are mapped here: order-summary labels ->
charge categories (CHARGE_CATEGORY, reviewed against every label seen so far) and card
networks -> payment kinds. A label without a mapping becomes an Issue, never a guess.

Output: {"platform", "extractor", "extractorVersion", "generatedAt", "complete", "since",
"sourceFiles", "tables": {table: [row, ...]}}. A row maps field names to values; a link is a
list of keys of the linked table's rows. "complete" is false for a --since sample, so the
loader never marks the orders a sample leaves out as missing.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import build_orders as B

HERE = Path(__file__).resolve().parent
PLATFORM = "amazon"
EXTRACTOR_VERSION = "3"   # 2: Purchase Type and Items for the inventory window; 3: pending charges wait
# The purchase-inventory window starts here (your scope, 2026-09-23): orders from this date on get
# a Purchase Type, and every product they bought gets an Item. Older orders are left as they are.
INVENTORY_SINCE = "2025-12-01"
PURCHASE_TYPE = "Retail Purchase"   # everything bought on Amazon

# Every order-summary label seen on the fetched order pages (28, reviewed 2026-09-23).
CHARGE_CATEGORY: dict[str, str] = {
    "Item(s) Subtotal": "subtotal",
    "Shipping & Handling": "shipping",
    "Free Shipping": "discount",
    "Subscribe & Save": "discount",
    "Your Coupon Savings": "discount",
    "Promotion Applied": "discount",
    "Promotion applied": "discount",
    "Extra Savings": "discount",
    "Super Saver Discount": "discount",
    "Amazon Discount": "discount",
    "Subscription saving": "discount",
    "No-Rush Discount": "discount",
    "Whole Foods – Prime": "discount",
    "Estimated tax to be collected": "tax",
    "Driver tip": "tip",
    "CA Electronic Waste Recycling Fee": "other_fee",
    "Gift Wrap": "other_fee",
    "Import Fees Deposit": "other_fee",
    "Bottle Deposit Fee": "other_fee",
    "Exchange rate guarantee fee": "other_fee",
    "Gift Card Amount": "gift_card",
    "Rewards Points": "points",
    "Refund Total": "refund",
    "Grand Total": "total",
    "Total before tax": "info",     # running subtotal
    "Total": "info",                # running subtotal on some pages that also show Grand Total
    "Exchange rate": "info",
    "FSA or HSA eligible: (inc. tax and shipping)": "info",
}
# Orders' breakdown fields: the sum of the order's summary lines in these categories.
BREAKDOWN = {"Items Subtotal": ("subtotal",), "Shipping": ("shipping",), "Discounts": ("discount",),
             "Tax": ("tax",), "Tip": ("tip",), "Fees": ("delivery_fee", "service_fee", "other_fee")}
FULFILLMENT = {"ship": "ship", "store": "pickup", "none": "unknown", "other": "unknown"}
RECON_STATUS = {"needs_order_page": "needs_source"}   # builder status -> Recon Status, where the names differ
PAYMENT_SOURCE = {"ledger": "ledger", "order_page": "order_page", "review": "manual"}
AMOUNT_SOURCE = {"ledger": "ledger", "export_total": "export_total", "review": "manual"}


def r2(x: float) -> float:
    return round(x + 0.0, 2)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def norm_desc(s: str) -> str:
    """A description as the code of an item that has none: 'Christmas Tree' -> 'CHRISTMAS TREE'."""
    return " ".join(re.sub(r"[^A-Z0-9 ]+", " ", s.upper()).split())


def method_key(card_key: str) -> str:
    """Builder card key -> Payment Methods key. A card is keyed by network and last 4
    ('Visa|6293' -> 'visa-6293') so the same card links up across platforms."""
    if card_key == "unknown":
        return "unknown"
    prefix, _, rest = card_key.partition("|")
    if prefix == "manual":
        return slug(rest)                                 # manual|cash -> cash
    if prefix == "unidentified":
        return f"{PLATFORM}-unidentified-{slug(rest)}"    # a card the page shows without a number
    return f"{slug(prefix)}-{rest}"


def ledger_method_key(r: dict[str, Any]) -> str:
    return f"{slug(r['network'])}-{r['last4']}" if r["instrumentKind"] == "card" else slug(r["instrumentLabel"])


def payment_kind(kind: str, network: str | None, label: str) -> str:
    """Builder/ledger instrument kind -> PAYMENT_KIND. A card counts as a credit card unless
    its network says otherwise (Kind is yours to correct in Baserow; the loader proposes it once)."""
    if kind in ("gift_card", "points", "unknown"):
        return kind
    if kind == "other":
        return "wallet" if label.lower().startswith(("venmo", "paypal")) else "other"
    if kind == "manual":
        return "cash" if label.lower() == "cash" else "other"
    if network == "Amazon Store Card":
        return "store_card"
    if network == "FSA or HSA card":
        return "debit_card"
    return "credit_card"


def load(name: str) -> Any:
    return json.loads((HERE / name).read_text())


def extract(since: str | None = None) -> dict[str, Any]:
    db = load("orders_db.json")
    export = {o["orderId"]: o for o in load(db["export"])}
    details = load("order-details.json")["orders"]
    tx = load("transactions.json")
    ledger = [r for p in tx["pages"] for r in p["rows"]] + tx.get("retainedRows", [])
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()

    recs = sorted((r for r in db["orders"] if since is None or r["orderDate"] >= since), key=lambda r: r["orderId"])
    chosen = {r["orderId"] for r in recs}
    okey = {oid: f"{PLATFORM}|{oid}" for oid in export}

    methods: dict[str, dict[str, Any]] = {}
    aliases: dict[str, dict[str, Any]] = {}
    addresses: dict[str, dict[str, Any]] = {}
    products: dict[str, dict[str, Any]] = {}
    orders: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    charges: list[dict[str, Any]] = []
    payments: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    def method(key: str, label: str, kind: str, network: str | None, last4: str | None) -> str:
        methods.setdefault(key, {"Method Key": key, "Label": label, "Kind": kind,
                                 "Network": network or "", "Last4": last4 or ""})
        return key

    def alias(text: str, last4: str | None, mkey: str | None, when: str) -> None:
        a = aliases.setdefault(text, {"Alias Key": f"{PLATFORM}|{text}", "Display Text": text, "Platform": [PLATFORM],
                                      "Last4 Seen": last4 or "", "_method": None, "Times Seen": 0,
                                      "First Seen": when, "Last Seen": when})
        a["_method"] = a["_method"] or mkey
        a["Times Seen"] += 1
        a["First Seen"], a["Last Seen"] = min(a["First Seen"], when), max(a["Last Seen"], when)

    def issue(order_key: str, key_suffix: str, code: str, severity: str, detail: str, evidence: str, active: bool) -> None:
        issues.append({"Issue Key": f"{order_key}|{key_suffix}", "Order": [order_key], "Code": code,
                       "Severity": severity, "Detail": detail, "Evidence": evidence,
                       "First Seen": today, "Last Seen": today, "Active": active})

    # ---- orders, their items, summary lines and issues -----------------------------------------
    for rec in recs:
        oid = rec["orderId"]
        k = okey[oid]
        page = details.get(oid) or {}
        in_window = rec["orderDate"] >= INVENTORY_SINCE
        card = rec["card"]
        mkey = None
        if card:
            label = card["label"].removesuffix(" (manual)")
            mkey = method(method_key(card["key"]), label, payment_kind(card["kind"], card["network"], label),
                          card["network"], card["last4"])
        ship = rec["shipTo"]
        skey = ship["key"] if ship["street"] else None
        if skey:
            a = addresses.setdefault(skey, {"Address Key": skey, "Street": ship["street"], "City Postal": ship["cityPostal"] or "",
                                            "Country": ship["country"] or "", "Kind": "unknown", "_latest": ""})
            if rec["orderDate"] >= a["_latest"]:
                a["Recipient"], a["_latest"] = ship["name"] or "", rec["orderDate"]
            if ship["kind"] == "store":
                a["Kind"] = "store"

        sums: dict[str, float] = defaultdict(float)
        for pos, line in enumerate(page.get("chargeLines") or [], 1):
            category = CHARGE_CATEGORY.get(line["label"])
            if category is None:
                category = "unknown"
                issue(k, f"unmapped_charge_label|{slug(line['label'])}", "unmapped_charge_label", "review",
                      f"order-summary label {line['label']!r} has no category in extract_canonical.CHARGE_CATEGORY",
                      rec["orderPage"]["html"] or "", True)
            if line["amount"] is not None:
                sums[category] += line["amount"]
            charges.append({"Charge Key": f"{k}|{pos}", "Order": [k], "Position": pos, "Label": line["label"],
                            "Category": category, "Amount": line["amount"]})

        # The export labels $0 orders "EUR" (see build_orders): no currency rather than a wrong one.
        currency = None if rec["currencySource"] == "export" and rec["exportTotal"] == 0 else rec["currency"]
        evidence = [f"ledger p{c['page']}#{c['position']}" for c in rec["ledgerCharges"]]
        if rec["orderPage"]["html"]:
            evidence.append(f"page {rec['orderPage']['html']}")
        note = (rec["review"] or {}).get("note")
        if note:
            evidence.append(f"review: {note}")
        status = RECON_STATUS.get(rec["status"], rec["status"])
        orders.append({
            "Order Key": k, "Platform": [PLATFORM], "External Order ID": oid, "Order Date": rec["orderDate"],
            "Order Status": export[oid].get("orderStatus") or "", "Fulfillment": FULFILLMENT[ship["kind"]],
            "Ship To": [skey] if skey else [], "Currency": currency,
            **{fname: (r2(sum(sums[c] for c in cats)) if any(c in sums for c in cats) else None)
               for fname, cats in BREAKDOWN.items()},
            "Credits Applied": r2(rec["giftCardNet"] + rec["points"]),
            "Grand Total": rec["orderPage"]["grandTotal"] if rec["orderPage"]["grandTotal"] is not None else rec["exportTotal"],
            "Order Value": rec["orderValue"],
            "Payment Method": [mkey] if mkey else [],
            "Card Amount": card["amount"] if card else None,
            "Payment Source": PAYMENT_SOURCE[card["cardSource"]] if card else "none",
            "Amount Source": AMOUNT_SOURCE[card["amountSource"]] if card else "none",
            "Recon Status": status, "Recon Diff": rec["diff"],
            "Platform Key": PLATFORM, "Method Key": mkey or "", "Ship To Key": skey or "",
            "Order URL": B.ORDER_URL.format(oid), "Evidence": "; ".join(evidence),
            "Missing From Source": False,
            **({"Purchase Type": PURCHASE_TYPE} if in_window else {}),
        })

        for it in rec["items"]:
            pkey: str | None = None
            if it["asin"]:
                pkey, native, id_type = f"{PLATFORM}|{it['asin']}", it["asin"], "asin"
            elif in_window and it["title"]:   # an in-store item without a code: its description names it
                native = norm_desc(it["title"])
                pkey, id_type = f"{PLATFORM}|desc:{native}", "merchant_desc"
            if pkey:
                p = products.setdefault(pkey, {"Product Key": pkey, "Platform": [PLATFORM], "Native ID": native,
                                               **({"Product URL": B.PRODUCT_URL.format(it["asin"])} if it["asin"] else {}),
                                               "_latest": ""})
                if rec["orderDate"] >= p["_latest"]:
                    p["Title"], p["_latest"] = it["title"] or "", rec["orderDate"]
                if in_window:   # the code names the product; its Item is what it is, across stores
                    p["ID Type"], p["Item"] = id_type, [f"item|{pkey}"]
            items.append({
                "Item Key": f"{k}|{it['line']}", "Order": [k], "Product": [pkey] if pkey else [],
                "Line": it["line"], "Title As Ordered": it["title"] or "", "Quantity": it["quantity"],
                "Unit Price": it["unitPrice"], "Line Total": it["lineTotal"], "Discount": it["discount"],
                "Order Date": rec["orderDate"], "Platform Key": PLATFORM, "Method Key": mkey or "", "Ship To Key": skey or "",
            })

        for fl in rec["flags"]:
            issue(k, fl["code"], fl["code"], "review", fl["detail"], rec["orderPage"]["html"] or "", True)
        for fl in rec.get("acknowledgedFlags", []):
            issue(k, fl["code"], fl["code"], "info", fl["detail"] + (f" | settled in reviews.json: {note}" if note else ""),
                  rec["orderPage"]["html"] or "", False)

    # ---- ledger rows -> Payments ------------------------------------------------------------------
    # A ledger row has no id of its own: its key is its content, plus a count for identical rows
    # (a real double charge shows twice). Counted over the whole ledger, so a sample uses the same keys.
    store_match = {(c["page"], c["position"]): rec["orderId"]
                   for rec in db["orders"] for c in rec["ledgerCharges"] if c.get("matchedBy")}
    occurrence: Counter[str] = Counter()
    for r in ledger:
        if r.get("pending"):
            # Not posted yet ("In Progress"): its order already counts it (build_orders), but a
            # Payment waits until it posts -- a posted charge can carry a new date, so a Payment
            # made now would be a second row later.
            continue
        mkey = ledger_method_key(r)
        base = "|".join([PLATFORM, "ledger", r["chargeDate"], mkey, r["direction"], f"{r['amount']:.2f}",
                         "+".join(r["orderRefs"]) or "-"])
        occurrence[base] += 1
        linked = [x for x in r["orderRefs"] if x in export]
        if not linked and (r["page"], r["position"]) in store_match and r["orderRefPrefixes"] == ["uuid"]:
            linked = [store_match[(r["page"], r["position"])]]   # a store charge matched by build_orders
        if since is not None:
            linked = [x for x in linked if x in chosen]
            if not linked:
                continue
        label = f"{r['network']} ****{r['last4']}" if r["instrumentKind"] == "card" else r["instrumentLabel"]
        method(mkey, label, payment_kind(r["instrumentKind"], r["network"], label), r["network"], r["last4"])
        alias(r["instrumentLabel"], r["last4"], mkey, r["chargeDate"])
        payments.append({
            "Payment Key": f"{base}|{occurrence[base]}", "Date": r["chargeDate"], "Payment Method": [mkey],
            "Direction": r["direction"], "Amount": r["amount"],
            "Signed Amount": r["amount"] if r["direction"] == "charge" else -r["amount"],
            "Orders": [okey[x] for x in linked], "Platform": [PLATFORM],
            "Descriptor": r["merchant"] or "", "Refs As Shown": ", ".join(r["orderRefs"]), "Source": "ledger",
            "Evidence": f"transactions.json page {r['page']} #{r['position']}",
        })

    # ---- how the order pages name each payment method -> Payment Aliases --------------------------
    by_last4: dict[str, list[str]] = defaultdict(list)
    for key, m in methods.items():
        if m["Last4"]:
            by_last4[m["Last4"]].append(key)
    for rec in recs:
        for m in (details.get(rec["orderId"]) or {}).get("paymentMethods") or []:
            number, name = m.get("number"), m.get("name") or ""
            if number:
                found = by_last4.get(number, [])
                mkey = found[0] if len(found) == 1 else None
            else:
                mkey = next((c for c in (slug(name), f"{PLATFORM}-unidentified-{slug(name)}") if c in methods), None)
            alias(m.get("display") or name, number, mkey, rec["orderDate"])

    for a in aliases.values():
        a["Payment Method"] = [a["_method"]] if a["_method"] else []
        a["Status"] = "mapped" if a["_method"] else "needs_review"

    platform = {"Platform Key": PLATFORM, "Name": "Amazon", "Kind": "marketplace", "Website": "https://www.amazon.com",
                "Order URL Template": B.ORDER_URL.replace("{}", "{id}"),
                "Product URL Template": B.PRODUCT_URL.replace("{}", "{id}")}

    def clean(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        return sorted(({f: v for f, v in r.items() if not f.startswith("_")} for r in rows), key=lambda r: str(r[key]))

    # One Item per product bought in the window, named from its latest title (the name is yours after that).
    inventory = [{"Item Key": p["Item"][0], "Name": p["Title"]} for p in products.values() if p.get("Item")]

    return {
        "platform": PLATFORM, "extractor": "amazon_orders/extract_canonical.py", "extractorVersion": EXTRACTOR_VERSION,
        "generatedAt": now.isoformat(timespec="seconds"), "complete": since is None, "since": since,
        "sourceFiles": [db["export"], "orders_db.json", "order-details.json", "transactions.json", "reviews.json"],
        "tables": {
            "Platforms": [platform],
            "Addresses": clean(list(addresses.values()), "Address Key"),
            "Payment Methods": clean(list(methods.values()), "Method Key"),
            "Payment Aliases": clean(list(aliases.values()), "Alias Key"),
            "Items": clean(inventory, "Item Key"),
            "Products": clean(list(products.values()), "Product Key"),
            "Orders": clean(orders, "Order Key"),
            "Order Items": clean(items, "Item Key"),
            "Order Charges": clean(charges, "Charge Key"),
            "Payments": clean(payments, "Payment Key"),
            "Issues": clean(issues, "Issue Key"),
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="only orders placed on or after this date (YYYY-MM-DD): a sample for testing")
    ap.add_argument("--out", type=Path, help="default: canonical.json, or canonical-since-<date>.json with --since")
    args = ap.parse_args()
    if args.since:
        datetime.strptime(args.since, "%Y-%m-%d")   # fail on a malformed date, not silently on string order
    doc = extract(args.since)
    out = args.out or HERE / (f"canonical-since-{args.since}.json" if args.since else "canonical.json")
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    print(f"{out.name}: {'every order' if doc['complete'] else 'orders since ' + str(args.since)}")
    for name, rows in doc["tables"].items():
        print(f"  {name:<16} {len(rows):>6}")
    print(f"  {'total rows':<16} {sum(len(r) for r in doc['tables'].values()):>6}")
    active = sum(1 for i in doc["tables"]["Issues"] if i["Active"])
    if active:
        print(f"  {active} open issue(s) -- see Issues in the file")


if __name__ == "__main__":
    main()
