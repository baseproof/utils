"""Checks extract_canonical.py on the real local data (orders_db.json and its sources), and
loads the --since sample into the commerce tests' fake Baserow.

    uv run --with pytest --with httpx --with python-dotenv pytest -q test_extract_canonical.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

import build_orders as B
import extract_canonical as E

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "commerce"))
import commerce_schema as S  # noqa: E402
import load_baserow as L  # noqa: E402
import test_load_baserow as TL  # noqa: E402

SAMPLE_SINCE = "2026-08-01"


@pytest.fixture(scope="module")
def full() -> dict[str, Any]:
    return E.extract()


@pytest.fixture(scope="module")
def sample() -> dict[str, Any]:
    return E.extract(SAMPLE_SINCE)


def dangling(doc: dict[str, Any]) -> list[str]:
    """Links whose key is not a row of the linked table in the same file."""
    keys = {t["name"]: {r[t["key"]] for r in doc["tables"].get(t["name"], [])} for t in S.TABLES}
    out = []
    for t in S.TABLES:
        links = [fd for fd in t["fields"] if fd["type"] == "link_row"]
        for row in doc["tables"].get(t["name"], []):
            for fd in links:
                out += [f"{t['name']} {row[t['key']]} {fd['name']} -> {k}" for k in row.get(fd["name"], []) if k not in keys[fd["to"]]]
    return out


def by_key(doc: dict[str, Any], table: str) -> dict[str, dict[str, Any]]:
    spec = next(t for t in S.TABLES if t["name"] == table)
    return {r[spec["key"]]: r for r in doc["tables"][table]}


def test_every_order_item_product_and_ledger_row_is_there(full: dict[str, Any]) -> None:
    db = json.loads((E.HERE / "orders_db.json").read_text())
    tx = json.loads((E.HERE / "transactions.json").read_text())
    t = full["tables"]
    assert full["complete"] and L.validate_doc(full) == []
    assert len(t["Orders"]) == len(db["orders"])
    assert len(t["Order Items"]) == sum(len(o["items"]) for o in db["orders"])
    described = {E.norm_desc(i["title"]) for o in db["orders"] if o["orderDate"] >= E.INVENTORY_SINCE
                 for i in o["items"] if not i["asin"] and i["title"]}      # code-less, in the window
    assert len(t["Products"]) == len({i["asin"] for o in db["orders"] for i in o["items"] if i["asin"]}) + len(described)
    ledger = [r for p in tx["pages"] for r in p["rows"]] + tx.get("retainedRows", [])
    assert len(t["Payments"]) == sum(1 for r in ledger if not r.get("pending"))   # pending: a Payment once posted
    assert dangling(full) == []


def test_sample_is_self_contained(sample: dict[str, Any]) -> None:
    assert not sample["complete"] and L.validate_doc(sample) == []
    assert all(r["Order Date"] >= SAMPLE_SINCE for r in sample["tables"]["Orders"])
    assert dangling(sample) == []   # loads into empty tables without a missing link


def test_every_summary_label_has_a_category(full: dict[str, Any]) -> None:
    assert [i for i in full["tables"]["Issues"] if i["Code"] == "unmapped_charge_label"] == []
    assert {c["Category"] for c in full["tables"]["Order Charges"]} <= set(S.CHARGE_CATEGORY) - {"unknown"}


def test_decisions_come_through(full: dict[str, Any]) -> None:
    orders, pays = by_key(full, "Orders"), full["tables"]["Payments"]
    split = orders["amazon|114-1881572-4059413"]            # one order, two cards (agreed: primary carries it)
    assert split["Payment Method"] == ["mastercard-6689"] and split["Card Amount"] == 240.07
    assert sorted((p["Payment Method"][0], p["Amount"]) for p in pays if p["Orders"] == ["amazon|114-1881572-4059413"]) == \
        [("amazon-gift-card", 50.0), ("american-express-1004", 82.0), ("mastercard-6689", 158.07)]
    squaretrade = orders["amazon|111-5386452-9908247"]       # noCard: points, Grand Total $0
    assert squaretrade["Payment Method"] == [] and squaretrade["Recon Status"] == "reviewed"
    double = orders["amazon|113-3896753-4929843"]            # cardAmount: the ledger listed it twice
    assert (double["Card Amount"], double["Amount Source"]) == (23.23, "manual")
    assert sum(1 for p in pays if p["Orders"] == ["amazon|113-3896753-4929843"] and p["Amount"] == 23.23) == 2
    assert Counter(o["Currency"] for o in full["tables"]["Orders"])[None] == 224   # the export's "EUR" on $0 orders
    issues = full["tables"]["Issues"]
    assert len(issues) == 8 and not any(i["Active"] for i in issues)               # all settled in reviews.json


def test_the_inventory_window_gets_purchase_types_and_items(full: dict[str, Any]) -> None:
    t = full["tables"]
    window = {o["Order Key"] for o in t["Orders"] if o["Order Date"] >= E.INVENTORY_SINCE}
    assert all(("Purchase Type" in o) == (o["Order Key"] in window) for o in t["Orders"])   # older orders untouched
    assert {o["Purchase Type"] for o in t["Orders"] if o["Order Key"] in window} == {"Retail Purchase"}
    lines = [i for i in t["Order Items"] if i["Order"][0] in window]
    assert all(i["Product"] for i in lines)                                                # every line has a product
    bought = {i["Product"][0] for i in lines}
    products = by_key(full, "Products")
    assert {k for k, p in products.items() if p.get("Item")} == bought                    # exactly the window's
    assert all(products[k]["Item"] == [f"item|{k}"] and products[k]["ID Type"] ==
               ("merchant_desc" if k.startswith("amazon|desc:") else "asin") for k in bought)
    items = by_key(full, "Items")
    assert set(items) == {f"item|{k}" for k in bought}
    assert all(items[f"item|{k}"]["Name"] == products[k]["Title"] for k in bought)         # named from its title
    tree = [i for i in lines if i["Order"] == ["amazon|113-7892851-4138659"]]              # Whole Foods, in store
    assert [(i["Title As Ordered"], i["Line Total"], i["Product"]) for i in tree] == \
        [("Christmas Tree", 69.99, ["amazon|desc:CHRISTMAS TREE"])]
    assert (len(window), len(lines), len(bought)) == (432, 1080, 828)


def test_review_items_fail_closed() -> None:
    with pytest.raises(SystemExit, match="needs a title"):
        B.check_reviews({"X": {"items": [{"title": "Tree", "quantity": 1, "unitPrice": 69.99}]}}, {"X"})
    with pytest.raises(SystemExit, match="export lists 1"):
        B.items_for({"orderId": "X", "items": [{"asin": "B0", "title": "t", "price": 1.0}]},
                    {"items": [{"title": "Tree", "quantity": 1, "unitPrice": 1.0, "lineTotal": 1.0}]})


def test_every_page_alias_is_mapped(full: dict[str, Any]) -> None:
    assert [a["Display Text"] for a in full["tables"]["Payment Aliases"] if a["Status"] != "mapped"] == []


def test_sample_loads_and_reloads_cleanly(sample: dict[str, Any]) -> None:
    api = TL.FakeRows()
    first = TL.totals(TL.load(api, sample))
    assert first["created"] == sum(len(r) for r in sample["tables"].values())
    again = TL.totals(TL.load(api, sample, run="run2"))
    assert again["created"] == again["updated"] == again["marked"] == 0
