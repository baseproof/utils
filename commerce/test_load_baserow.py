"""Offline tests for load_baserow.py against a fake Baserow built by the real schema script
and holding rows the way Baserow does (select options by id, links by row id, numbers with
fixed decimals, read-only derived fields), plus a flaky variant that drops the connection.

    uv run --with pytest --with httpx --with python-dotenv pytest -q test_load_baserow.py
"""

from __future__ import annotations

import contextlib
import copy
import io
from decimal import Decimal
from typing import Any, Callable, TypeVar

import httpx
import pytest

import baserow_schema as B
import load_baserow as L
import test_baserow_schema as T

NOW = "2026-09-23T12:00:00Z"
R = TypeVar("R")


class FakeRows(T.FakeBaserow):
    """The fake schema server, with the schema applied, plus rows."""

    def __init__(self) -> None:
        super().__init__(T.EXISTING)
        with contextlib.redirect_stdout(io.StringIO()):
            B.Reconciler(self, "", apply=True).run()
        self.rows: dict[int, list[dict[str, Any]]] = {t["id"]: [] for t in self._tables}
        self.next_row = 0
        self.writes = 0

    def _cell(self, tid: int, name: str, value: Any) -> Any:
        fd = next((x for x in self._fields[tid] if x["name"] == name), None)
        if fd is None:
            raise B.ApiError(400, f"no field {name!r}")
        t = fd["type"]
        if t in ("count", "rollup", "formula"):
            raise B.ApiError(400, f"{name} is read only")
        if value is None:
            return [] if t == "link_row" else None
        if t == "number":
            d = Decimal(str(value))
            if -d.as_tuple().exponent > fd["number_decimal_places"]:  # type: ignore[operator]
                raise B.ApiError(400, f"{name}: more than {fd['number_decimal_places']} decimal places")
            return str(d.quantize(Decimal(1).scaleb(-fd["number_decimal_places"])))
        if t == "single_select":
            opt = next((o for o in fd["select_options"] if o["id"] == value), None)
            if opt is None:
                raise B.ApiError(400, f"{name}: no option id {value!r}")
            return dict(opt)
        if t == "link_row":
            target = self.rows[fd["link_row_table_id"]]
            key = next(x["name"] for x in self._fields[fd["link_row_table_id"]] if x["primary"])
            out = []
            for rid in value:
                row = next((r for r in target if r["id"] == rid), None)
                if row is None:
                    raise B.ApiError(400, f"{name}: no row {rid}")
                out.append({"id": rid, "value": row[key]})
            return out
        if t == "boolean" and not isinstance(value, bool):
            raise B.ApiError(400, f"{name}: not a boolean")
        return value

    def list_rows(self, table_id: int) -> list[dict[str, Any]]:
        return copy.deepcopy(self.rows[table_id])

    def create_rows(self, table_id: int, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.writes += 1
        made = []
        for item in items:  # validate the whole batch first: Baserow's batches are all or nothing
            made.append({n: self._cell(table_id, n, v) for n, v in item.items()})
        for row in made:
            self.next_row += 1
            row["id"] = self.next_row
        self.rows[table_id] += made
        return copy.deepcopy(made)

    def update_rows(self, table_id: int, items: list[dict[str, Any]]) -> None:
        self.writes += 1
        patches = [(next(r for r in self.rows[table_id] if r["id"] == item["id"]),
                    {n: self._cell(table_id, n, v) for n, v in item.items() if n != "id"}) for item in items]
        for row, patch in patches:
            row.update(patch)

    # helpers for assertions
    def row(self, table: str, key: str) -> dict[str, Any]:
        tid = self.t(table)["id"]
        key_field = next(x["name"] for x in self._fields[tid] if x["primary"])
        return next(r for r in self.rows[tid] if r[key_field] == key)

    def snapshot(self) -> dict[str, list[tuple[Any, ...]]]:
        """Every row's content, ids replaced by what they point at."""
        out: dict[str, list[tuple[Any, ...]]] = {}
        for t in self._tables:
            rows = []
            for r in self.rows[t["id"]]:
                cells = []
                for n, v in sorted(r.items()):
                    if n in ("id", "Last Synced", "Sync Run"):
                        continue
                    if isinstance(v, dict):
                        v = v["value"]
                    elif isinstance(v, list):
                        v = tuple(sorted(x["value"] for x in v))
                    cells.append((n, v))
                rows.append(tuple(cells))
            out[t["name"]] = sorted(rows)
        return out


reset, gateway = T.reset, T.gateway


class FlakyRows(FakeRows):
    """Fails the Nth row write (a dropped connection, a gateway error, ...): before Baserow
    acts, or after it acted but before its reply arrives."""

    def __init__(self, drop_at: int, after: bool, error: Callable[[], Exception] = reset) -> None:
        super().__init__()
        self.drop_at, self.after, self.error, self.count, self.drops = drop_at, after, error, 0, 0

    def _flaky(self, fn: Callable[..., R], *args: Any) -> R:
        self.count += 1
        if self.count == self.drop_at:
            self.drops += 1
            if self.after:
                fn(*args)
            raise self.error()
        return fn(*args)

    def create_rows(self, table_id: int, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._flaky(super().create_rows, table_id, items)

    def update_rows(self, table_id: int, items: list[dict[str, Any]]) -> None:
        self._flaky(super().update_rows, table_id, items)


def small_doc() -> dict[str, Any]:
    return {"platform": "shop", "complete": True, "tables": {
        "Platforms": [{"Platform Key": "shop", "Name": "Shop", "Kind": "aggregator"}],
        "Addresses": [{"Address Key": "1 MAIN ST | TOWN", "Street": "1 MAIN ST", "City Postal": "TOWN", "Kind": "unknown"}],
        "Payment Methods": [{"Method Key": "visa-1111", "Label": "Visa ****1111", "Kind": "credit_card",
                             "Network": "Visa", "Last4": "1111"}],
        "Payment Aliases": [{"Alias Key": "shop|Visa ••••1111", "Display Text": "Visa ••••1111", "Platform": ["shop"],
                             "Payment Method": ["visa-1111"], "Status": "mapped", "Times Seen": 2,
                             "First Seen": "2026-01-01", "Last Seen": "2026-02-01"}],
        "Products": [{"Product Key": "shop|p1", "Platform": ["shop"], "Native ID": "p1", "Title": "Widget"}],
        "Orders": [
            {"Order Key": "shop|o1", "Platform": ["shop"], "External Order ID": "o1", "Order Date": "2026-01-01",
             "Ship To": ["1 MAIN ST | TOWN"], "Payment Method": ["visa-1111"], "Card Amount": 10.5,
             "Grand Total": 10.5, "Recon Status": "reconciled", "Missing From Source": False},
            {"Order Key": "shop|o2", "Platform": ["shop"], "External Order ID": "o2", "Order Date": "2026-02-01",
             "Ship To": [], "Payment Method": [], "Card Amount": None, "Grand Total": 0,
             "Recon Status": "zero_total", "Missing From Source": False}],
        "Order Items": [{"Item Key": "shop|o1|1", "Order": ["shop|o1"], "Product": ["shop|p1"], "Line": 1,
                         "Quantity": 2, "Unit Price": 5.25, "Line Total": 10.5}],
        "Order Charges": [{"Charge Key": "shop|o1|1", "Order": ["shop|o1"], "Position": 1, "Label": "Subtotal",
                           "Category": "subtotal", "Amount": 10.5}],
        "Payments": [{"Payment Key": "shop|pay1", "Date": "2026-01-02", "Payment Method": ["visa-1111"],
                      "Direction": "charge", "Amount": 10.5, "Signed Amount": 10.5, "Orders": ["shop|o1"],
                      "Platform": ["shop"], "Source": "ledger"}],
        "Issues": [{"Issue Key": "shop|o2|x", "Order": ["shop|o2"], "Code": "x", "Severity": "review",
                    "Detail": "d", "First Seen": "2026-02-01", "Last Seen": "2026-02-01", "Active": True}],
    }}


def load(api: L.RowApi, doc: dict[str, Any], apply: bool = True, run: str = "run1") -> dict[str, dict[str, int]]:
    with contextlib.redirect_stdout(io.StringIO()):
        status = L.load(api, doc, "", apply, run, NOW, pause=0)
    return L.summary(doc, status)


def totals(counts: dict[str, dict[str, int]]) -> dict[str, int]:
    return {k: sum(c[k] for c in counts.values()) for k in L.RANK}


def test_file_is_checked_before_anything_is_sent() -> None:
    assert L.validate_doc(small_doc()) == []
    bad = small_doc()
    orders = bad["tables"]["Orders"]
    orders[0]["Recon Status"] = "fine"            # not an option
    orders[0]["Notes"] = "x"                      # yours
    orders[0]["Item Count"] = 3                   # derived
    orders[0]["Last Synced"] = NOW                # the loader's own
    orders[1]["Order Date"] = "2026-13-01"        # not a date
    orders.append(dict(orders[0]))                # same key twice
    errs = "\n".join(L.validate_doc(bad))
    for expected in ("'fine' is not a valid single_select", "Orders.Notes: yours", "Orders.Item Count: not a field",
                     "Orders.Last Synced: written by the loader", "not a YYYY-MM-DD date", "key 'shop|o1' appears twice"):
        assert expected in errs, expected


def test_first_load_creates_everything_and_a_second_changes_nothing() -> None:
    api, doc = FakeRows(), small_doc()
    first = totals(load(api, doc))
    assert first["created"] == sum(len(r) for r in doc["tables"].values()) and first["updated"] == 0
    o1 = api.row("Orders", "shop|o1")
    assert o1["Recon Status"]["value"] == "reconciled" and o1["Card Amount"] == "10.50"
    assert [x["value"] for x in o1["Payment Method"]] == ["visa-1111"] and [x["value"] for x in o1["Ship To"]] == ["1 MAIN ST | TOWN"]
    assert (o1["Last Synced"], o1["Sync Run"]) == (NOW, "run1")
    assert api.row("Order Items", "shop|o1|1")["Quantity"] == "2.000"
    writes = api.writes
    again = totals(load(api, doc, run="run2"))
    assert again["created"] == again["updated"] == again["marked"] == 0 and api.writes == writes
    assert api.row("Orders", "shop|o1")["Sync Run"] == "run1"   # untouched rows keep their stamp


def test_your_edits_are_kept() -> None:
    api, doc = FakeRows(), small_doc()
    load(api, doc)
    api.row("Orders", "shop|o1")["Notes"] = "mine"              # user field
    visa = api.row("Payment Methods", "visa-1111")
    visa["Label"], visa["Network"] = "My Visa", ""              # loader_if_empty: edited / emptied
    doc["tables"]["Payment Methods"][0]["Label"] = "Visa ****1111 (renamed at source)"
    doc["tables"]["Orders"][0]["Order Status"] = "Delivered"
    counts = load(api, doc, run="run2")
    o1, visa = api.row("Orders", "shop|o1"), api.row("Payment Methods", "visa-1111")
    assert o1["Notes"] == "mine" and o1["Order Status"] == "Delivered" and o1["Sync Run"] == "run2"
    assert visa["Label"] == "My Visa" and visa["Network"] == "Visa"
    assert counts["Orders"]["updated"] == 1 and counts["Payment Methods"]["updated"] == 1


def test_rows_gone_from_a_complete_extract_are_marked_never_deleted() -> None:
    api, doc = FakeRows(), small_doc()
    load(api, doc)
    api.create_rows(api.t("Orders")["id"], [{"Order Key": "other|o9"}])     # another platform's row
    sample = copy.deepcopy(doc)
    sample["complete"] = False
    del sample["tables"]["Orders"][1], sample["tables"]["Issues"][0]
    assert totals(load(api, sample))["marked"] == 0                         # a sample marks nothing
    gone = copy.deepcopy(sample)
    gone["complete"] = True
    counts = load(api, gone)
    assert counts["Orders"]["marked"] == 1 and counts["Issues"]["marked"] == 1
    assert api.row("Orders", "shop|o2")["Missing From Source"] is True
    assert api.row("Issues", "shop|o2|x")["Active"] is False
    assert api.row("Orders", "other|o9").get("Missing From Source") in (None, False)
    assert len(api.rows[api.t("Orders")["id"]]) == 3                        # nothing deleted


def test_duplicate_keys_in_baserow_stop_the_load() -> None:
    api = FakeRows()
    api.create_rows(api.t("Platforms")["id"], [{"Platform Key": "shop"}, {"Platform Key": "shop"}])
    with pytest.raises(SystemExit, match="used by more than one row"):
        load(api, small_doc())


def test_a_link_to_a_missing_key_stops_the_load() -> None:
    doc = small_doc()
    doc["tables"]["Orders"][0]["Ship To"] = ["NO SUCH ADDRESS"]
    with pytest.raises(SystemExit, match="no Addresses row with key"):
        load(FakeRows(), doc)


def test_plan_writes_nothing() -> None:
    api = FakeRows()
    counts = totals(load(api, small_doc(), apply=False))
    assert api.writes == 0 and counts["created"] == sum(len(r) for r in small_doc()["tables"].values())


@pytest.mark.parametrize("error", [reset, gateway], ids=["connection-reset", "cloudflare-520"])
@pytest.mark.parametrize("after", [False, True], ids=["before-baserow-acts", "reply-lost"])
def test_dropped_connection_carries_on(after: bool, error: Callable[[], Exception]) -> None:
    clean = FakeRows()
    load(clean, small_doc())
    want, writes = clean.snapshot(), clean.writes
    for drop_at in range(1, writes + 1):
        api = FlakyRows(drop_at, after, error)
        load(api, small_doc())
        assert api.drops == 1, drop_at
        assert api.snapshot() == want, drop_at            # same rows, none twice


def test_a_real_refusal_stops_at_once() -> None:
    api = FlakyRows(1, after=False, error=lambda: B.ApiError(400, '{"error": "ERROR_REQUEST_BODY_VALIDATION"}'))
    with pytest.raises(B.ApiError):
        load(api, small_doc())
    assert api.drops == 1   # no second pass: a 400 is a verdict, not a hiccup
