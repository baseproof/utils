"""Offline tests for baserow_schema.py against a fake Baserow that enforces the rules the
script relies on (unique names, reserved names, reverse links named after the source table,
formula references, rollup targets in the linked table, option ids kept on update), plus a
flaky variant that drops the connection mid-run.

    uv run --with pytest --with httpx --with python-dotenv pytest -q test_baserow_schema.py
"""

from __future__ import annotations

import copy
import re
import time
from types import ModuleType
from typing import Any, Callable, TypeVar

import httpx
import pytest

import baserow_schema as B
import commerce_schema as S
import planning_schema as P
import schema_kit as K


class FakeBaserow:
    mode = "jwt"

    def __init__(self, existing: list[str] = ()) -> None:  # type: ignore[assignment]
        self.next_id = 1000
        self._tables: list[dict[str, Any]] = []
        self._fields: dict[int, list[dict[str, Any]]] = {}
        self._views: dict[int, list[dict[str, Any]]] = {}
        self.cells: dict[int, list[dict[int, Any]]] = {}  # table id -> each row as {field id: value}
        self.mutations = 0
        for name in existing:
            self._add_table(name, "Name")
        self.mutations = 0

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def _add_table(self, name: str, key: str) -> dict[str, Any]:
        t: dict[str, Any] = {"id": self._id(), "name": name, "database_id": 566971}
        self._tables.append(t)
        self._fields[t["id"]] = [{"id": self._id(), "name": key, "type": "text", "primary": True}]
        self._views[t["id"]] = [{"id": self._id(), "name": "Grid"}]
        return t

    def _unique(self, table_id: int, name: str) -> str:
        names = {x["name"] for x in self._fields[table_id]}
        n, i = name, 2
        while n in names:
            n, i = f"{name} {i}", i + 1
        return n

    def _field(self, field_id: int) -> tuple[int, dict[str, Any]]:
        for tid, fs in self._fields.items():
            for x in fs:
                if x["id"] == field_id:
                    return tid, x
        raise B.ApiError(404, f"field {field_id}")

    # --- Api protocol ---
    def database_name(self) -> str:
        return "Shopping"

    def tables(self, refresh: bool = False) -> list[dict[str, Any]]:
        return self._tables

    def create_table(self, name: str, key: str) -> dict[str, Any]:
        self.mutations += 1
        return self._add_table(name, key)

    def delete_table(self, table_id: int) -> None:
        self.mutations += 1
        self._tables = [t for t in self._tables if t["id"] != table_id]

    def fields(self, table_id: int, refresh: bool = False) -> list[dict[str, Any]]:
        return self._fields[table_id]

    def create_field(self, table_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.mutations += 1
        p = copy.deepcopy(payload)
        name = p["name"]
        if name in S.RESERVED_FIELD_NAMES:
            raise B.ApiError(400, f"reserved {name}")
        if any(x["name"] == name for x in self._fields[table_id]):
            raise B.ApiError(400, f"duplicate {name}")
        fd: dict[str, Any] = {"id": self._id(), "primary": False, **p}
        if p["type"] == "link_row":
            dst = p["link_row_table_id"]
            if dst not in self._fields:
                raise B.ApiError(400, "no such table")
            if p.get("has_related_field"):
                src_name = next(t["name"] for t in self._tables if t["id"] == table_id)
                rel = {"id": self._id(), "name": self._unique(dst, src_name), "type": "link_row", "primary": False,
                       "link_row_table_id": table_id, "link_row_related_field_id": fd["id"]}
                self._fields[dst].append(rel)
                fd["link_row_related_field_id"] = rel["id"]
        elif p["type"] in ("count", "rollup", "lookup"):
            _, through = self._field(p["through_field_id"])
            if through["type"] != "link_row" or through not in self._fields[table_id]:
                raise B.ApiError(400, "through must be a link on this table")
            if p["type"] in ("rollup", "lookup"):
                target_tid, _ = self._field(p["target_field_id"])
                if target_tid != through["link_row_table_id"]:
                    raise B.ApiError(400, "rollup target must be in the linked table")
        elif p["type"] == "formula":
            names = {x["name"] for x in self._fields[table_id]}
            for ref in re.findall(r"field\('([^']+)'\)", p["formula"]):
                if ref not in names:
                    raise B.ApiError(400, f"formula references unknown field {ref}")
        elif p["type"] in ("single_select", "multiple_select"):
            fd["select_options"] = [{"id": self._id(), **o} for o in p["select_options"]]
        self._fields[table_id].append(fd)
        return fd

    def update_field(self, field_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.mutations += 1
        tid, fd = self._field(field_id)
        if "name" in payload:
            if any(x["name"] == payload["name"] and x is not fd for x in self._fields[tid]):
                raise B.ApiError(400, "duplicate name")
            fd["name"] = payload["name"]
        if "select_options" in payload:
            fd["select_options"] = [o if "id" in o else {"id": self._id(), **o} for o in payload["select_options"]]
            ids = {o["id"] for o in fd["select_options"]}
            for row in self.cells.get(tid, []):          # Baserow clears a dropped option from every row
                v = row.get(field_id)
                if isinstance(v, list):
                    row[field_id] = [x for x in v if x in ids]
                elif v is not None and v not in ids:
                    row[field_id] = None
        if payload.get("type") == "formula":   # e.g. a text key field turned into a formula
            names = {x["name"] for x in self._fields[tid] if x is not fd}
            for ref in re.findall(r"field\('([^']+)'\)", payload["formula"]):
                if ref not in names:
                    raise B.ApiError(400, f"formula references unknown field {ref}")
            fd["type"], fd["formula"] = "formula", payload["formula"]
        return fd

    def views(self, table_id: int) -> list[dict[str, Any]]:
        return self._views[table_id]

    def create_view(self, table_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.mutations += 1
        v = {"id": self._id(), "table_id": table_id, "filters": [], "sorts": [], **payload}
        self._views[table_id].append(v)
        return v

    def _view(self, view_id: int) -> dict[str, Any]:
        return next(v for vs in self._views.values() for v in vs if v["id"] == view_id)

    def create_filter(self, view_id: int, payload: dict[str, Any]) -> None:
        self.mutations += 1
        v = self._view(view_id)
        tid, fd = self._field(payload["field"])
        if tid != v["table_id"]:
            raise B.ApiError(400, "filter field not in view's table")
        if payload["type"].startswith("single_select") and payload["value"] not in {str(o["id"]) for o in fd["select_options"]}:
            raise B.ApiError(400, "unknown option id")
        v["filters"].append(payload)

    def create_sort(self, view_id: int, payload: dict[str, Any]) -> None:
        self.mutations += 1
        self._view(view_id)["sorts"].append(payload)

    def view_filters(self, view_id: int) -> list[dict[str, Any]]:
        return list(self._view(view_id)["filters"])

    def view_sorts(self, view_id: int) -> list[dict[str, Any]]:
        return list(self._view(view_id)["sorts"])

    def count_rows(self, table_id: int, field: dict[str, Any], option_id: int) -> int:
        def holds(v: Any) -> bool:
            return option_id in v if isinstance(v, list) else bool(v == option_id)
        return sum(holds(row.get(field["id"])) for row in self.cells.get(table_id, []))

    def reset_cache(self) -> None:
        pass  # nothing cached: the fake's state is the server's

    # helpers for assertions
    def t(self, name: str) -> dict[str, Any]:
        return next(t for t in self._tables if t["name"] == name)

    def f(self, table: str, field: str) -> dict[str, Any] | None:
        return next((x for x in self._fields[self.t(table)["id"]] if x["name"] == field), None)


T = TypeVar("T")


def reset() -> Exception:
    return httpx.ReadError("[Errno 54] Connection reset by peer")


def gateway() -> Exception:
    return B.ApiError(520, "<!DOCTYPE html> ... Cloudflare ... Error 520")


class FlakyBaserow(FakeBaserow):
    """Fails the Nth write (with `every`, every write from then on) with a dropped connection
    or a gateway error: either before the server acts, or after it acted but before its reply
    arrives."""

    def __init__(self, existing: list[str], drop_at: int, after: bool, every: bool = False,
                 error: Callable[[], Exception] = reset) -> None:
        super().__init__(existing)
        self.drop_at, self.after, self.every, self.error = drop_at, after, every, error
        self.writes = self.drops = 0

    def _write(self, fn: Callable[..., T], *args: Any) -> T:
        self.writes += 1
        if self.writes == self.drop_at or (self.every and self.writes > self.drop_at):
            self.drops += 1
            if self.after:
                fn(*args)
            raise self.error()
        return fn(*args)

    def create_table(self, name: str, key: str) -> dict[str, Any]:
        return self._write(super().create_table, name, key)

    def create_field(self, table_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._write(super().create_field, table_id, payload)

    def update_field(self, field_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._write(super().update_field, field_id, payload)

    def create_view(self, table_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._write(super().create_view, table_id, payload)

    def create_filter(self, view_id: int, payload: dict[str, Any]) -> None:
        self._write(super().create_filter, view_id, payload)

    def create_sort(self, view_id: int, payload: dict[str, Any]) -> None:
        self._write(super().create_sort, view_id, payload)


def shape(api: FakeBaserow) -> dict[str, Any]:
    """Everything a run builds, by name (ids differ between runs; filter option ids are
    turned back into option values)."""
    tables = {t["id"]: t["name"] for t in api._tables}
    out: dict[str, Any] = {}
    for tid, name in tables.items():
        by_id = {x["id"]: x for x in api._fields[tid]}

        def filt(f: dict[str, Any]) -> tuple[str, str, str]:
            fd = by_id[f["field"]]
            value = str(f["value"])
            if f["type"].startswith("single_select"):
                value = next(o["value"] for o in fd["select_options"] if str(o["id"]) == value)
            return fd["name"], f["type"], value

        out[name] = {
            "fields": sorted((x["name"], x["type"], tables.get(x.get("link_row_table_id") or 0, ""),
                              tuple(sorted(o["value"] for o in x.get("select_options") or [])))
                             for x in by_id.values()),
            "views": sorted((v["name"], tuple(sorted(filt(f) for f in v.get("filters", []))),
                             tuple((by_id[s["field"]]["name"], s["order"]) for s in v.get("sorts", [])))
                            for v in api._views[tid]),
        }
    return out


EXISTING = ["Table", "Amazon Orders"]  # what the Shopping database holds today


def apply(api: FakeBaserow, suffix: str = "") -> B.Reconciler:
    r = B.Reconciler(api, suffix, apply=True)
    r.run()
    return r


def test_schema_is_valid() -> None:
    assert S.validate() == []


def test_validator_catches_mistakes(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = copy.deepcopy(S.TABLES)
    orders = next(t for t in broken if t["name"] == "Orders")
    orders["fields"].append(K.text("Tip"))                      # duplicate field
    orders["fields"].append(K.text("order"))                    # reserved
    monkeypatch.setattr(S, "TABLES", broken)
    monkeypatch.setitem(S.DERIVED, "Addresses", [K.rollup("Order Spend", "Orders", "No Such Field", "sum")])
    errs = "\n".join(S.validate())
    assert "duplicate field names ['Tip']" in errs
    assert "Orders.order: reserved" in errs
    assert "has no field 'No Such Field'" in errs


def test_plan_changes_nothing() -> None:
    api = FakeBaserow(EXISTING)
    r = B.Reconciler(api, "", apply=False)
    r.run()
    assert api.mutations == 0
    assert sum(c.startswith("create table") for c in r.changes) == len(S.TABLES)


def test_apply_builds_everything_and_leaves_existing_tables_alone() -> None:
    api = FakeBaserow(EXISTING)
    before = copy.deepcopy(api._fields)
    r = apply(api)
    assert r.warnings == []
    for name in EXISTING:  # the existing tables are untouched
        assert api._fields[api.t(name)["id"]] == before[api.t(name)["id"]]
    rev = S.reverse_fields()
    for t in S.TABLES:
        fields = api._fields[api.t(t["name"])["id"]]
        primary = [x for x in fields if x["primary"]]
        assert [x["name"] for x in primary] == [t["key"]]
        for fd in S.all_fields(t):
            got = api.f(t["name"], fd["name"])
            assert got is not None, f"{t['name']}.{fd['name']} missing"
            assert got["type"] == B.API_TYPE.get(fd["type"], fd["type"])
        for rname in rev[t["name"]]:
            assert api.f(t["name"], rname) is not None, f"reverse {t['name']}.{rname} missing"
        # nothing extra beyond key + own + reverse
        assert len(fields) == 1 + len(S.all_fields(t)) + len(rev[t["name"]])
    # links point to the right tables and reverse links point back
    link = api.f("Order Items", "Order")
    assert link and link["link_row_table_id"] == api.t("Orders")["id"]
    back = api.f("Orders", "Items")
    assert back and back["link_row_related_field_id"] == link["id"]
    # views exist with their filters and sorts resolved
    views = {v["name"]: v for v in api._views[api.t("Orders")["id"]]}
    assert len(views["Needs review"]["filters"]) == len(S.RECON_OK)
    assert views["Needs review"]["sorts"] and views["Recent"]["sorts"]
    assert {v["name"] for v in api._views[api.t("Products")["id"]]} >= {"Most ordered"}
    assert len(next(v for v in api._views[api.t("Issues")["id"]] if v["name"] == "Open")["filters"]) == 4


def test_second_apply_is_a_no_op() -> None:
    api = FakeBaserow(EXISTING)
    apply(api)
    api.mutations = 0
    r = apply(api)
    assert r.changes == [] and r.warnings == [] and api.mutations == 0


def test_drift_is_repaired_additively() -> None:
    api = FakeBaserow(EXISTING)
    apply(api)
    res = api.f("Issues", "Resolution")
    assert res is not None
    kept_ids = {o["value"]: o["id"] for o in res["select_options"]}
    res["select_options"] = [o for o in res["select_options"] if o["value"] != "wont_fix"]   # option removed in UI
    rev = api.f("Platforms", "Orders")
    assert rev is not None
    rev["name"] = "Orders 2"                                                                  # reverse link renamed
    api._fields[api.t("Orders")["id"]].append({"id": 1, "name": "My Stuff", "type": "text", "primary": False})
    tip = api.f("Orders", "Tip")
    assert tip is not None
    tip["type"] = "text"                                                                      # type changed by hand
    api.mutations = 0
    r = apply(api)
    assert sorted(r.changes) == sorted([
        "add options ['wont_fix'] to Issues.Resolution",
        "name reverse link Platforms.'Orders 2' -> 'Orders'",
        # the re-added option has a new id, so the Open view's filter on it is added again
        "add filter Resolution single_select_not_equal 'wont_fix' to view Issues / 'Open'",
    ])
    assert api.mutations == 3
    assert any("Orders.Tip: is text, schema says number" in w for w in r.warnings)
    res = api.f("Issues", "Resolution")
    assert res is not None
    assert {o["value"]: o["id"] for o in res["select_options"] if o["value"] != "wont_fix"} == \
           {k: v for k, v in kept_ids.items() if k != "wont_fix"}                             # existing option ids kept
    assert api.f("Orders", "My Stuff") is not None and api.f("Platforms", "Orders") is not None


def test_items_join_store_codes_and_receipts_keep_their_files() -> None:
    api = FakeBaserow(EXISTING)
    assert apply(api).warnings == []
    item = api.f("Products", "Item")                                  # every store code points at one Item
    back = api.f("Items", "Products")
    assert item and item["link_row_table_id"] == api.t("Items")["id"]
    assert back and back["link_row_related_field_id"] == item["id"]
    for name, of in [("Times Bought", "Times Ordered"), ("Units", "Units"), ("Spend", "Item Spend"),
                     ("First Bought", "First Ordered"), ("Last Bought", "Last Ordered")]:
        total, source = api.f("Items", name), api.f("Products", of)  # an Item totals its Products' totals
        assert total and source and total["type"] == "rollup" and total["target_field_id"] == source["id"], name
    shown = api.f("Order Items", "Item")                              # a purchase line shows its item
    assert shown and shown["type"] == "lookup" and shown["target_field_id"] == item["id"]
    for table in ("Products", "Items", "Order Items"):                # no price statistics anywhere
        assert not any(api.f(table, n) for n in ("Lowest Price", "Highest Price", "Average Price", "Paid Per Unit"))
    kind = api.f("Orders", "Purchase Type")                           # the LLM's call, per order
    assert kind and [o["value"] for o in kind["select_options"]] == S.PURCHASE_TYPE
    assert S.ITEMIZED == ["Retail Purchase", "Food Order", "Demand Delivery"] and set(S.ITEMIZED) <= set(S.PURCHASE_TYPE)
    assert api.f("Order Charges", "Original Amount") and api.f("Orders", "Delivered At")   # DoorDash's full picture
    files = api.f("Purchase Receipts", "Files")                       # 1-10 original files per receipt
    assert files and files["type"] == "file"
    proves, receipts = api.f("Purchase Receipts", "Order"), api.f("Orders", "Receipts")
    assert proves and proves["link_row_table_id"] == api.t("Orders")["id"]   # a receipt proves an order,
    assert receipts and receipts["link_row_related_field_id"] == proves["id"]  # an order has its receipts
    assert not any(api.f("Orders", n) for n in ("Receipt Files", "Receipt Text", "Time As Printed"))
    unmatched = next(v for v in api._views[api.t("Purchase Receipts")["id"]] if v["name"] == "Unmatched")
    assert unmatched["filters"] == [{"field": proves["id"], "type": "empty", "value": ""}]


def test_test_copy_is_isolated() -> None:
    api = FakeBaserow(EXISTING)
    apply(api, suffix=" (test)")
    assert {t["name"] for t in api._tables} == set(EXISTING) | {t["name"] + " (test)" for t in S.TABLES}
    link = api.f("Order Items (test)", "Order")
    assert link and link["link_row_table_id"] == api.t("Orders (test)")["id"]


def test_teardown_only_removes_test_copies() -> None:
    api = FakeBaserow(EXISTING)
    apply(api)
    with pytest.raises(SystemExit):
        B.teardown(api, "", yes=True)
    apply(api, suffix=" (test)")
    B.teardown(api, " (test)", yes=True)
    names = {t["name"] for t in api._tables}
    assert names == set(EXISTING) | {t["name"] for t in S.TABLES}


@pytest.mark.parametrize("error", [reset, gateway], ids=["connection-reset", "cloudflare-520"])
@pytest.mark.parametrize("after", [False, True], ids=["before-server-acts", "reply-lost"])
def test_dropped_connection_carries_on(after: bool, error: Callable[[], Exception]) -> None:
    clean = FakeBaserow(EXISTING)
    apply(clean)
    want, writes = shape(clean), clean.mutations
    for drop_at in range(1, writes + 1):  # a failure at every single write of a full run
        api = FlakyBaserow(EXISTING, drop_at, after, error=error)
        r, passes = B.run_passes(api, "", apply=True, pause=0)
        assert (api.drops, passes) == (1, 2), drop_at
        assert shape(api) == want, drop_at            # same result as a run that never dropped
        assert r.warnings == [], drop_at
        again = B.Reconciler(api, "", apply=True)     # and nothing left over or made twice
        again.run()
        assert again.changes == [], drop_at


def client(monkeypatch: pytest.MonkeyPatch, replies: list[httpx.Response]) -> tuple[B.Baserow, list[float]]:
    """The real HTTP client, talking to canned replies instead of Baserow."""
    monkeypatch.setenv("BASEROW_TOKEN", "test-token")
    api = B.Baserow("https://baserow.test", 1, needs_jwt=False)
    it = iter(replies)
    api.c = httpx.Client(base_url="https://baserow.test", transport=httpx.MockTransport(lambda request: next(it)))
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    return api, slept


def test_rate_limit_waits_as_told_then_backs_off(monkeypatch: pytest.MonkeyPatch) -> None:
    api, slept = client(monkeypatch, [
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(429, headers={"Retry-After": "Wed, 23 Sep 2026 17:00:00 GMT"}),   # a date: back off
        httpx.Response(429),
        httpx.Response(200, json={"ok": True})])
    assert api.req("GET", "/api/x/") == {"ok": True}
    assert slept == [3.0, 2.0, 4.0]


def test_rate_limit_gives_up_after_five(monkeypatch: pytest.MonkeyPatch) -> None:
    api, slept = client(monkeypatch, [httpx.Response(429)] * 5)
    with pytest.raises(B.ApiError, match="rate limited five times"):
        api.req("GET", "/api/x/")
    assert slept == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_gives_up_after_three_drops() -> None:
    api = FlakyBaserow(EXISTING, drop_at=1, after=False, every=True)
    with pytest.raises(SystemExit, match="run the same command again"):
        B.run_passes(api, "", apply=True, pause=0)
    assert api.drops == 3


def test_half_made_view_is_completed() -> None:
    api = FakeBaserow(EXISTING)
    apply(api)
    view = next(v for v in api._views[api.t("Orders")["id"]] if v["name"] == "Needs review")
    view["filters"], view["sorts"] = view["filters"][:3], []
    r = apply(api)
    assert len(view["filters"]) == len(S.RECON_OK) and len(view["sorts"]) == 1
    assert sum(c.startswith("add filter") for c in r.changes) == len(S.RECON_OK) - 3
    assert sum(c.startswith("add sort") for c in r.changes) == 1


# ---- Financial Planning (planning_schema.py) -------------------------------------------------
def build_planning(api: FakeBaserow) -> B.Reconciler:
    r, _ = B.run_passes(api, "", apply=True, pause=0, schema=P)
    return r


def test_planning_schema_is_valid() -> None:
    assert P.validate() == []


def test_planning_builds_into_an_empty_database_and_reapplies_as_a_no_op() -> None:
    api = FakeBaserow([])
    r = build_planning(api)
    assert r.warnings == []
    key = api.f("Categories", "Category")                       # "Group › Name", a formula key
    assert key and key["primary"] and key["type"] == "formula" and key["formula"] == P.TABLES[0]["key_formula"]
    part_of = api.f("Plans", "Part Of")                         # links to its own table, no reverse field
    assert part_of and part_of["link_row_table_id"] == api.t("Plans")["id"]
    assert not part_of.get("link_row_related_field_id")
    back = api.f("Categories", "Plans")
    assert back and back["type"] == "link_row"
    group = api.f("Plans", "Group")
    assert group and group["type"] == "lookup"
    monthly = api.f("Plans", "Monthly")
    assert monthly and monthly["number_decimal_places"] == 2
    tags = api.f("Plans", "Tags")
    assert tags and tags["type"] == "multiple_select" and [o["value"] for o in tags["select_options"]] == P.TAGS
    assert all(api.f("Plans", n) for n in ("Created", "Modified", "Ends", "Month", "Price", "Tax", "Fees"))
    amount = api.f("Plans", "Amount")                           # computed: Price + Tax + Fees
    assert amount and amount["type"] == "formula" and amount["number_decimal_places"] == 2
    item, who = api.f("Plans", "Item"), api.f("Plans", "Who's it for")
    assert item and item["primary"] and api.f("Plans", "Description")
    assert who and [o["value"] for o in who["select_options"]] == P.PEOPLE
    views = {v["name"]: v for v in api._views[api.t("Plans")["id"]]}
    assert len(views["Upcoming"]["filters"]) == 2 and views["Bought"]["sorts"]
    api.mutations = 0
    again = build_planning(api)
    assert again.changes == [] and again.warnings == [] and api.mutations == 0


@pytest.mark.parametrize("after", [False, True], ids=["before-server-acts", "reply-lost"])
def test_planning_survives_a_failure_at_every_write(after: bool) -> None:
    clean = FakeBaserow([])
    build_planning(clean)
    want, writes = shape(clean), clean.mutations
    for drop_at in range(1, writes + 1):
        api = FlakyBaserow([], drop_at, after, error=gateway)
        r, passes = B.run_passes(api, "", apply=True, pause=0, schema=P)
        assert (api.drops, passes) == (1, 2) and shape(api) == want and r.warnings == [], drop_at


# The Financial Planning database as first applied (2026-09-23), before P.MIGRATIONS: key Name,
# For with the first list of people, no Description.
OLD_PEOPLE = ["Anil", "Nandana", "Vedanth", "Buddy", "Clyde", "Venkat", "Ammaji", "Rishi", "Krishna", "Das",
              "Vimala", "Household", "Baseproof"]


def legacy_planning() -> ModuleType:
    tables = copy.deepcopy(P.TABLES)
    plans = next(t for t in tables if t["name"] == "Plans")
    plans["key"] = "Name"
    plans["fields"] = [fd for fd in plans["fields"] if fd["name"] != "Description"]
    next(fd for fd in plans["fields"] if fd["name"] == "Who's it for").update(name="For", options=list(OLD_PEOPLE))
    m = ModuleType("legacy_planning")
    for attr, value in {"TABLES": tables, "DERIVED": P.DERIVED, "EXTRA_VIEWS": P.EXTRA_VIEWS,
                        "link_target": lambda t, ln: K.link_target(tables, t, ln)}.items():
        setattr(m, attr, value)
    return m


def legacy_db(api: FakeBaserow) -> dict[str, int]:
    """Build the legacy database in `api` with two plans (one for Household, one for Anil and
    Nandana). Returns the ids the migration must keep: the key field, For, and each option."""
    B.run_passes(api, "", apply=True, pause=0, schema=legacy_planning())
    key, who = api.f("Plans", "Name"), api.f("Plans", "For")
    assert key and key["primary"] and who
    opt = {o["value"]: int(o["id"]) for o in who["select_options"]}
    api.cells[api.t("Plans")["id"]] = [{who["id"]: [opt["Household"]]}, {who["id"]: [opt["Anil"], opt["Nandana"]]}]
    api.mutations = 0
    return {"key": int(key["id"]), "who": int(who["id"]), **opt}


def test_planning_migration_plan_shows_the_renames_and_changes_nothing() -> None:
    api = FakeBaserow([])
    legacy_db(api)
    r = B.Reconciler(api, "", apply=False, schema=P)
    r.run()
    assert api.mutations == 0 and r.warnings == []            # no "primary is Name" warning, no second field
    assert r.changes == [
        "rename Plans.Name -> Item",
        "rename Plans.For -> Who's it for",
        "set Plans.Who's it for options to the schema's list: rename Household -> Home; "
        "add Pragna, Krishna Das, Other; drop Venkat, Ammaji, Krishna, Das, Baseproof (no rows use them)",
        "create Plans.Description (text)",
    ]


def test_planning_migration_renames_in_place_and_keeps_every_value() -> None:
    api = FakeBaserow([])
    ids = legacy_db(api)
    r = build_planning(api)
    assert r.warnings == [] and len(r.changes) == 4
    item, who = api.f("Plans", "Item"), api.f("Plans", "Who's it for")
    assert item and item["primary"] and item["id"] == ids["key"]              # renamed, not recreated
    assert who and who["id"] == ids["who"] and api.f("Plans", "For") is None
    assert [o["value"] for o in who["select_options"]] == P.PEOPLE             # the schema's list, in its order
    assert next(o["id"] for o in who["select_options"] if o["value"] == "Home") == ids["Household"]
    assert [row[who["id"]] for row in api.cells[api.t("Plans")["id"]]] == \
           [[ids["Household"]], [ids["Anil"], ids["Nandana"]]]                 # every row keeps its people
    fresh = FakeBaserow([])
    build_planning(fresh)
    assert shape(api) == shape(fresh)                                          # as if built new today
    api.mutations = 0
    again = build_planning(api)
    assert again.changes == [] and again.warnings == [] and api.mutations == 0


def test_planning_migration_stops_when_a_dropped_option_is_still_used() -> None:
    api = FakeBaserow([])
    ids = legacy_db(api)
    api.cells[api.t("Plans")["id"]].append({ids["who"]: [ids["Venkat"]]})
    with pytest.raises(SystemExit, match="Venkat"):
        build_planning(api)
    who = api.f("Plans", "Who's it for")                                       # the renames before it were made,
    assert who and [o["value"] for o in who["select_options"]] == OLD_PEOPLE  # its options were left alone


@pytest.mark.parametrize("after", [False, True], ids=["before-server-acts", "reply-lost"])
def test_planning_migration_survives_a_failure_at_every_write(after: bool) -> None:
    clean = FakeBaserow([])
    legacy_db(clean)
    build_planning(clean)
    want, writes = shape(clean), clean.mutations
    assert writes == 4
    for drop_at in range(1, writes + 1):
        api = FlakyBaserow([], 0, after, error=gateway)                        # no failures while building legacy
        legacy_db(api)
        api.writes, api.drop_at = 0, drop_at
        r, passes = B.run_passes(api, "", apply=True, pause=0, schema=P)
        assert (api.drops, passes) == (1, 2) and shape(api) == want and r.warnings == [], drop_at


def test_planning_validator_checks_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(P, "MIGRATIONS", [
        K.rename_field("Plans", "Name", "Title"),                                 # Title is not in the schema
        K.rename_field("Plans", "Vendor", "Item"),                                # Vendor is still in the schema
        K.set_options("Plans", "Who's it for", renames={"Household": "House"}),   # House is not an option
        K.set_options("Plans", "Notes"),                                          # not a select field
        K.rename_field("Nope", "a", "b"),
    ])
    errs = "\n".join(P.validate())
    assert "Plans.Name -> 'Title', which is not in the schema" in errs
    assert "Plans.'Vendor' is renamed but still in the schema" in errs
    assert "renames 'Household' -> 'House'" in errs
    assert "Plans.'Notes' is not a select field" in errs
    assert "unknown table 'Nope'" in errs


def test_count_rows_asks_baserow_for_rows_holding_the_option(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={"count": 2, "next": None, "previous": None, "results": [{}]})

    monkeypatch.setenv("BASEROW_TOKEN", "test-token")
    api = B.Baserow("https://baserow.test", 1, needs_jwt=False)
    api.c = httpx.Client(base_url="https://baserow.test", transport=httpx.MockTransport(handler))
    assert api.count_rows(5, {"id": 7, "type": "multiple_select"}, 9) == 2
    assert api.count_rows(5, {"id": 8, "type": "single_select"}, 3) == 2
    assert [(u.path, dict(u.params)) for u in seen] == [
        ("/api/database/rows/table/5/", {"size": "1", "filter__field_7__multiple_select_has": "9"}),
        ("/api/database/rows/table/5/", {"size": "1", "filter__field_8__single_select_equal": "3"})]


def test_database_is_found_by_name_or_created(monkeypatch: pytest.MonkeyPatch) -> None:
    home = {"id": 7, "name": "Home"}
    listing = [{"id": 566971, "name": "Shopping", "type": "database", "workspace": home},
               {"id": 566810, "name": "Finances", "type": "database", "workspace": home},
               {"id": 9, "name": "Some builder app", "type": "builder", "workspace": home}]
    sent: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json=listing)
        return httpx.Response(200, json={"id": 600001, "name": "Financial Planning", "type": "database"})

    monkeypatch.setenv("BASEROW_TOKEN", "test-token")
    api = B.Baserow("https://baserow.test", None, needs_jwt=False)
    api.c = httpx.Client(base_url="https://baserow.test", transport=httpx.MockTransport(handler))
    assert api.tables() == []                                                       # no database yet: nothing to list
    assert B.resolve_database(api, "Shopping", create=False, workspace_id=None) == 566971
    assert B.resolve_database(api, "Financial Planning", create=False, workspace_id=None) is None   # plan
    assert all(method == "GET" for method, _ in sent)                               # a plan creates nothing
    assert B.resolve_database(api, "Financial Planning", create=True, workspace_id=None) == 600001
    assert sent[-1] == ("POST", "/api/applications/workspace/7/")                   # in your only workspace
    listing.append({"id": 11, "name": "Elsewhere", "type": "database", "workspace": {"id": 8, "name": "Work"}})
    with pytest.raises(SystemExit, match="pass --workspace-id"):
        B.resolve_database(api, "Financial Planning", create=True, workspace_id=None)
