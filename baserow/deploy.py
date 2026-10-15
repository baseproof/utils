#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "python-dotenv>=1.0"]
# ///
"""
deploy.py — build and backfill the nanny pay ledger in Baserow.

    uv run deploy.py check             show which auth is in use and which tables it can reach
    uv run deploy.py schema            create the 7 tables, fields and views   (needs email + password)
    uv run deploy.py backfill          load Sep 7–21 pay slips, workdays, accounts, Venmo transactions
    uv run deploy.py verify            print Gross / Paid / Balance per pay slip
    uv run deploy.py teardown --yes    trash the 7 tables (never Timesheet)

Every command is safe to re-run: tables, fields, views and rows that already exist are left alone.
Timesheet is never written to. Its rows are read once during backfill, and Workdays link to them
from the Workdays side with no field added to Timesheet.

Auth: BASEROW_TOKEN (your database token) is used whenever it is set. It covers check, backfill
      and verify. Baserow does not expose table/field creation to database tokens (see the
      generated API docs: only row, field-list, upload and all-tables endpoints), so `schema`
      and `teardown` log in with BASEROW_EMAIL + BASEROW_PASSWORD to get a JWT for that one run.
"""

from __future__ import annotations

import datetime as dt
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = os.environ.get("BASEROW_URL", "https://api.baserow.io").rstrip("/")
DB_ID = int(os.environ.get("BASEROW_DATABASE_ID", "566910"))
TIMESHEET_ID = int(os.environ.get("BASEROW_TIMESHEET_TABLE_ID", "1215333"))
TZ = "America/Los_Angeles"

TABLES = ["Pay Terms", "Pay Slip", "Workdays", "Adjustments", "Accounts", "Transactions", "Allocations"]

OVERNIGHT = {"2026-09-11", "2026-09-12"}

# ----------------------------------------------------------------------------------------------
# API client
# ----------------------------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body}")
        self.status, self.body = status, body


class Baserow:
    """Database token when BASEROW_TOKEN is set; JWT login only for commands that need it."""

    def __init__(self, needs_jwt: bool) -> None:
        self.c = httpx.Client(base_url=BASE, timeout=60)
        email, pw, tok = (os.environ.get(k) for k in ("BASEROW_EMAIL", "BASEROW_PASSWORD", "BASEROW_TOKEN"))
        if needs_jwt:
            if not (email and pw):
                sys.exit("This command creates or deletes tables, which Baserow only allows with a login (JWT);\n"
                         "database tokens cover rows only. Add BASEROW_EMAIL and BASEROW_PASSWORD to .env for\n"
                         "this run — you can remove them again afterwards and keep using BASEROW_TOKEN.")
            j = self.c.post("/api/user/token-auth/", json={"email": email, "password": pw})
            if j.status_code >= 400:
                sys.exit(f"Login failed: {j.text}")
            j = j.json()
            self.c.headers["Authorization"] = f"JWT {j.get('access_token') or j.get('token')}"
            self.mode = "jwt"
        elif tok:
            self.c.headers["Authorization"] = f"Token {tok}"
            self.mode = "token"
        elif email and pw:
            j = self.c.post("/api/user/token-auth/", json={"email": email, "password": pw})
            if j.status_code >= 400:
                sys.exit(f"Login failed: {j.text}")
            j = j.json()
            self.c.headers["Authorization"] = f"JWT {j.get('access_token') or j.get('token')}"
            self.mode = "jwt"
        else:
            sys.exit("Set BASEROW_TOKEN in .env (or BASEROW_EMAIL + BASEROW_PASSWORD).")
        self._fields: dict[int, list[dict]] = {}
        self._tables: list[dict] | None = None

    def req(self, method: str, path: str, **kw):
        r = self.c.request(method, path, **kw)
        if r.status_code >= 400:
            raise ApiError(r.status_code, r.text)
        return r.json() if r.content else None

    # tables -----------------------------------------------------------------------------------
    def tables(self, refresh=False) -> list[dict]:
        if self._tables is None or refresh:
            if self.mode == "token":
                # The only table-listing endpoint a database token may call; filtered to our database.
                self._tables = [t for t in self.req("GET", "/api/database/tables/all-tables/")
                                if t["database_id"] == DB_ID]
            else:
                self._tables = self.req("GET", f"/api/database/tables/database/{DB_ID}/")
        return self._tables

    def table(self, name: str) -> dict:
        for t in self.tables():
            if t["name"] == name:
                return t
        raise KeyError(f"table {name!r} not found; run `schema` first")

    def ensure_table(self, name: str) -> dict:
        for t in self.tables():
            if t["name"] == name:
                print(f"  table {name}: exists")
                return t
        t = self.req("POST", f"/api/database/tables/database/{DB_ID}/",
                     json={"name": name, "data": [["Name"]], "first_row_header": True})
        self.tables(refresh=True)
        print(f"  table {name}: created (id {t['id']})")
        return t

    # fields -----------------------------------------------------------------------------------
    def fields(self, table_id: int, refresh=False) -> list[dict]:
        if table_id not in self._fields or refresh:
            self._fields[table_id] = self.req("GET", f"/api/database/fields/table/{table_id}/")
        return self._fields[table_id]

    def field(self, table_id: int, name: str) -> dict | None:
        return next((f for f in self.fields(table_id) if f["name"] == name), None)

    def create_field(self, table_id: int, payload: dict) -> dict:
        f = self.req("POST", f"/api/database/fields/table/{table_id}/", json=payload)
        self.fields(table_id, refresh=True)
        return f

    def update_field(self, field_id: int, payload: dict) -> dict:
        f = self.req("PATCH", f"/api/database/fields/{field_id}/", json=payload)
        self._fields.clear()
        return f

    # rows -------------------------------------------------------------------------------------
    def rows(self, table_id: int) -> list[dict]:
        out, page = [], 1
        while True:
            j = self.req("GET", f"/api/database/rows/table/{table_id}/",
                         params={"user_field_names": "true", "size": 200, "page": page})
            out += j["results"]
            if not j.get("next"):
                return out
            page += 1

    def add_rows(self, table_id: int, items: list[dict]) -> list[dict]:
        if not items:
            return []
        return self.req("POST", f"/api/database/rows/table/{table_id}/batch/",
                        params={"user_field_names": "true"}, json={"items": items})["items"]

    def update_rows(self, table_id: int, items: list[dict]) -> list[dict]:
        if not items:
            return []
        return self.req("PATCH", f"/api/database/rows/table/{table_id}/batch/",
                        params={"user_field_names": "true"}, json={"items": items})["items"]

    # views ------------------------------------------------------------------------------------
    def views(self, table_id: int) -> list[dict]:
        return self.req("GET", f"/api/database/views/table/{table_id}/")


# ----------------------------------------------------------------------------------------------
# Schema helpers
# ----------------------------------------------------------------------------------------------

api: Baserow


def tid(name: str) -> int:
    return TIMESHEET_ID if name == "Timesheet" else api.table(name)["id"]


def fid(table: str, field: str) -> int:
    f = api.field(tid(table), field)
    if f is None:
        raise KeyError(f"{table}.{field} not found")
    return f["id"]


def linked_table_name(table: str, link_field: str) -> str:
    target = api.field(tid(table), link_field)["link_row_table_id"]
    if target == TIMESHEET_ID:
        return "Timesheet"
    return next(t["name"] for t in api.tables() if t["id"] == target)


def ensure(table: str, name: str, payload: dict, alternatives: list[dict] | None = None,
           optional: bool = False) -> dict | None:
    """Create a field if it doesn't exist. `alternatives` are tried in order until one is accepted."""
    existing = api.field(tid(table), name)
    if existing:
        print(f"  {table}.{name}: exists")
        return existing
    errors = []
    for candidate in [payload] + (alternatives or []):
        try:
            f = api.create_field(tid(table), {"name": name, **candidate})
            print(f"  {table}.{name}: created ({candidate['type']})")
            return f
        except ApiError as e:
            errors.append(e.body)
    if optional:
        print(f"  {table}.{name}: SKIPPED — no variant accepted. Last error: {errors[-1][:200]}")
        return None
    raise SystemExit(f"{table}.{name}: every variant rejected.\n" + "\n".join(errors))


def text(): return {"type": "text"}
def long_text(): return {"type": "long_text"}
def boolean(): return {"type": "boolean"}
def file(): return {"type": "file"}
def number(dp=2): return {"type": "number", "number_decimal_places": dp, "number_negative": True}
def date(): return {"type": "date", "date_include_time": False, "date_format": "ISO"}
def datetime(): return {"type": "date", "date_include_time": True, "date_format": "ISO",
                        "date_time_format": "24", "date_force_timezone": TZ}
def select(*opts): return {"type": "single_select", "select_options": [{"value": o, "color": "blue"} for o in opts]}
def formula(expr): return {"type": "formula", "formula": expr}


def link(table: str, name: str, to: str, related_name: str | None = None) -> None:
    """Link field. related_name=None → no field is added to the target table (used for Timesheet)."""
    if api.field(tid(table), name):
        print(f"  {table}.{name}: exists")
        return
    f = api.create_field(tid(table), {"name": name, "type": "link_row", "link_row_table_id": tid(to),
                                      "has_related_field": related_name is not None})
    print(f"  {table}.{name}: created (link → {to})")
    if related_name and f.get("link_row_related_field_id"):
        api.update_field(f["link_row_related_field_id"], {"name": related_name})
        print(f"  {to}.{related_name}: reverse link named")


def lookup(table: str, name: str, through: str, target: str) -> None:
    ensure(table, name, {"type": "lookup", "through_field_id": fid(table, through),
                         "target_field_id": fid(linked_table_name(table, through), target)})


def rollup(table: str, name: str, through: str, target: str, fn: str, optional=False) -> None:
    ensure(table, name,
           {"type": "rollup", "through_field_id": fid(table, through),
            "target_field_id": fid(linked_table_name(table, through), target), "rollup_function": fn},
           alternatives=[formula(f"{fn}(lookup('{through}', '{target}'))")], optional=optional)


def count(table: str, name: str, through: str) -> None:
    ensure(table, name, {"type": "count", "through_field_id": fid(table, through)})


def primary(table: str, expr: str, alternatives: list[str] = ()) -> None:
    p = next(f for f in api.fields(tid(table)) if f["primary"])
    if p["type"] == "formula" and p.get("formula") == expr:
        print(f"  {table}.Name: primary formula set")
        return
    for e in [expr, *alternatives]:
        try:
            api.update_field(p["id"], {"name": "Name", "type": "formula", "formula": e})
            print(f"  {table}.Name: primary → formula")
            return
        except ApiError as err:
            last = err.body
    raise SystemExit(f"{table}.Name primary formula rejected: {last}")


def view(table: str, name: str, filters: list[tuple] = (), filter_type="AND",
         sort: tuple | None = None, footer_sum: list[str] = ()) -> None:
    t = tid(table)
    if any(v["name"] == name for v in api.views(t)):
        print(f"  {table} view {name!r}: exists")
        return
    v = api.req("POST", f"/api/database/views/table/{t}/", json={"name": name, "type": "grid", "filter_type": filter_type})
    for field, ftype, value in filters:
        api.req("POST", f"/api/database/views/{v['id']}/filters/",
                json={"field": fid(table, field), "type": ftype, "value": value})
    if sort:
        api.req("POST", f"/api/database/views/{v['id']}/sortings/", json={"field": fid(table, sort[0]), "order": sort[1]})
    if footer_sum:
        opts = {str(fid(table, f)): {"aggregation_type": "sum", "aggregation_raw_type": "sum"} for f in footer_sum}
        api.req("PATCH", f"/api/database/views/{v['id']}/field-options/", json={"field_options": opts})
    print(f"  {table} view {name!r}: created")


def option_id(table: str, field: str, value: str) -> int:
    return next(o["id"] for o in api.field(tid(table), field)["select_options"] if o["value"] == value)


# ----------------------------------------------------------------------------------------------
# schema
# ----------------------------------------------------------------------------------------------


def cmd_schema() -> None:
    print("Tables")
    for name in TABLES:
        api.ensure_table(name)

    print("Pay Terms")
    ensure("Pay Terms", "Caretaker", text())
    ensure("Pay Terms", "Effective From", date())
    ensure("Pay Terms", "Effective To", date())
    ensure("Pay Terms", "Regular Rate", number())
    ensure("Pay Terms", "OT Multiplier", number())
    ensure("Pay Terms", "Per-diem Rate", number())
    ensure("Pay Terms", "Daily OT After (h)", number(0))     # 9  — the formulas assume this
    ensure("Pay Terms", "Weekly OT After (h)", number(0))    # 45 — the formulas assume this
    ensure("Pay Terms", "Agreed Terms", long_text())          # the agreement in her words; printed on every stub
    ensure("Pay Terms", "Notes", long_text())
    primary("Pay Terms", "concat(field('Caretaker'), ' · $', totext(field('Regular Rate')), '/hr from ', "
                         "datetime_format(field('Effective From'), 'YYYY-MM-DD'))")

    print("Pay Slip (part 1)")
    ensure("Pay Slip", "Period Start", date())
    ensure("Pay Slip", "Period End", formula("field('Period Start') + date_interval('6 days')"))
    ensure("Pay Slip", "Monday Check", formula("if(datetime_format(field('Period Start'), 'D') = '2', '', 'NOT A MONDAY')"))
    link("Pay Slip", "Pay Terms", to="Pay Terms", related_name="Pay Slips")
    ensure("Pay Slip", "Rate", formula("first(lookup('Pay Terms', 'Regular Rate'))"))
    ensure("Pay Slip", "OT Rate", formula("round(field('Rate') * first(lookup('Pay Terms', 'OT Multiplier')), 2)"))
    ensure("Pay Slip", "Notes", long_text())
    ensure("Pay Slip", "Statements Sent", file())

    print("Workdays")
    ensure("Workdays", "Date", date())
    link("Workdays", "Shifts", to="Timesheet", related_name=None)          # nothing is added to Timesheet
    link("Workdays", "Pay Slip", to="Pay Slip", related_name="Workdays")
    ensure("Workdays", "Location", select("Home", "San Diego", "Other"))
    # Where the day was worked, for printing her local times on the statement. Blank = Pacific.
    # Pay is unaffected: the workday and workweek are always America/Los_Angeles (fixed, per CA law).
    ensure("Workdays", "Time Zone", select("America/Los_Angeles", "America/Denver", "America/Chicago",
                                           "America/New_York", "Pacific/Honolulu", "America/Mexico_City",
                                           "Europe/London", "Asia/Kolkata"))
    ensure("Workdays", "Overnight", boolean())
    ensure("Workdays", "Notes", long_text())                  # breaks announced, why a day ran long, etc.
    rollup("Workdays", "Total", through="Shifts", target="Total Duration", fn="sum")
    ensure("Workdays", "Minutes", formula("round(toseconds(field('Total')) / 60, 0)"),
           alternatives=[formula("round(tonumber(field('Total')) / 60, 0)")])
    ensure("Workdays", "Reg Min", formula("if(field('Minutes') > 540, 540, field('Minutes'))"))
    ensure("Workdays", "Daily OT Min", formula("if(field('Minutes') > 540, field('Minutes') - 540, 0)"))
    ensure("Workdays", "Night", formula("if(field('Overnight'), 1, 0)"))
    count("Workdays", "Shift Count", through="Shifts")
    ensure("Workdays", "OT Note",
           formula("if(field('Daily OT Min') > 0, concat(datetime_format(field('Date'), 'Mon DD'), ': ', "
                   "totext(floor(field('Minutes') / 60)), 'h ', totext(mod(field('Minutes'), 60)), 'm', "
                   "if(field('Shift Count') > 1, concat(' in ', totext(field('Shift Count')), ' blocks'), ''), "
                   "' = 9h regular + ', totext(floor(field('Daily OT Min') / 60)), 'h ', "
                   "totext(mod(field('Daily OT Min'), 60)), 'm overtime'), '')"),
           alternatives=[formula("if(field('Daily OT Min') > 0, concat(datetime_format(field('Date'), 'Mon DD'), ': ', "
                                 "totext(field('Minutes')), ' min worked, ', totext(field('Daily OT Min')), ' min overtime'), '')")])
    lookup("Workdays", "Shift Dates", through="Shifts", target="Date")
    ensure("Workdays", "Date Check",
           formula("if(and(min(date_diff('day', lookup('Shifts', 'Date'), field('Date'))) = 0, "
                   "max(date_diff('day', lookup('Shifts', 'Date'), field('Date'))) = 0), '', 'DATE MISMATCH')"),
           alternatives=[formula("if(count(filter(lookup('Shifts', 'Date'), lookup('Shifts', 'Date') != field('Date'))) > 0, "
                                 "'DATE MISMATCH', '')")],
           optional=True)
    primary("Workdays", "datetime_format(field('Date'), 'Dy YYYY-MM-DD')")

    print("Adjustments")
    ensure("Adjustments", "Date", date())
    ensure("Adjustments", "Type", select("Reimbursement", "Paid absence", "Correction", "Other"))
    ensure("Adjustments", "What for", text())
    ensure("Adjustments", "Amount", number())
    ensure("Adjustments", "Receipt", file())
    link("Adjustments", "Pay Slip", to="Pay Slip", related_name="Adjustments")
    ensure("Adjustments", "Reimbursement Amt", formula("if(totext(field('Type')) = 'Reimbursement', field('Amount'), 0)"))
    ensure("Adjustments", "Other Amt", formula("field('Amount') - field('Reimbursement Amt')"))
    primary("Adjustments", "concat(totext(field('Type')), ' · $', totext(field('Amount')), ' · ', field('What for'))")

    print("Accounts")
    ensure("Accounts", "Provider", select("Zelle", "Venmo", "Apple Cash", "Check", "Cash"))
    ensure("Accounts", "Owner", select("Anil", "Raghu", "Vanessa Lopez"))
    ensure("Accounts", "Handle", text())
    ensure("Accounts", "Active", boolean())
    ensure("Accounts", "Notes", long_text())
    ensure("Accounts", "Provider Text", formula("totext(field('Provider'))"))
    primary("Accounts", "concat(totext(field('Provider')), ' · ', totext(field('Owner')), ' · ', field('Handle'))")

    print("Transactions (part 1)")
    ensure("Transactions", "Date", datetime())
    ensure("Transactions", "Amount", number())
    ensure("Transactions", "Status", select("Completed", "Pending", "Failed", "Returned"))
    ensure("Transactions", "Type", select("Regular pay", "Partial", "Correction", "Reimbursement", "Refund", "Other"))
    link("Transactions", "From", to="Accounts", related_name="Sent")
    link("Transactions", "To", to="Accounts", related_name="Received")
    ensure("Transactions", "Transaction ID", text())
    ensure("Transactions", "Memo", text())
    ensure("Transactions", "Receipt", file())
    ensure("Transactions", "Notes", long_text())
    ensure("Transactions", "Counted", formula("if(totext(field('Status')) = 'Completed', field('Amount'), 0)"))
    ensure("Transactions", "Provider Check",
           formula("if(first(lookup('From', 'Provider Text')) = first(lookup('To', 'Provider Text')), '', 'PROVIDER MISMATCH')"))
    primary("Transactions",
            "concat(datetime_format_tz(field('Date'), 'YYYY-MM-DD', 'America/Los_Angeles'), ' · ', "
            "first(lookup('From', 'Name')), ' · $', totext(field('Amount')), ' · #', field('Transaction ID'))",
            alternatives=["concat(datetime_format(field('Date'), 'YYYY-MM-DD'), ' · ', first(lookup('From', 'Name')), "
                          "' · $', totext(field('Amount')), ' · #', field('Transaction ID'))"])

    print("Allocations")
    link("Allocations", "Transaction", to="Transactions", related_name="Allocations")
    link("Allocations", "Pay Slip", to="Pay Slip", related_name="Allocations")
    ensure("Allocations", "Amount", number())
    ensure("Allocations", "Counted", formula("if(sum(lookup('Transaction', 'Counted')) != 0, field('Amount'), 0)"))
    ensure("Allocations", "Txn Date", formula("first(lookup('Transaction', 'Date'))"))
    primary("Allocations", "concat(first(lookup('Pay Slip', 'Name')), ' ← $', totext(field('Amount')))")

    print("Transactions (part 2)")
    rollup("Transactions", "Allocated", through="Allocations", target="Amount", fn="sum")
    ensure("Transactions", "Unallocated", formula("field('Amount') - when_empty(field('Allocated'), 0)"))

    print("Pay Slip (part 2)")
    rollup("Pay Slip", "Reg Min", through="Workdays", target="Reg Min", fn="sum")
    rollup("Pay Slip", "Daily OT Min", through="Workdays", target="Daily OT Min", fn="sum")
    rollup("Pay Slip", "Nights", through="Workdays", target="Night", fn="sum")
    ensure("Pay Slip", "Weekly OT Min", formula("if(field('Reg Min') > 2700, field('Reg Min') - 2700, 0)"))
    ensure("Pay Slip", "Total Time", formula("toduration((field('Reg Min') + field('Daily OT Min')) * 60)"), optional=True)
    ensure("Pay Slip", "Regular Pay", formula("round((field('Reg Min') - field('Weekly OT Min')) / 60 * field('Rate'), 2)"))
    ensure("Pay Slip", "OT Pay", formula("round((field('Daily OT Min') + field('Weekly OT Min')) / 60 * field('OT Rate'), 2)"))
    ensure("Pay Slip", "Per-diem", formula("when_empty(field('Nights'), 0) * first(lookup('Pay Terms', 'Per-diem Rate'))"))
    rollup("Pay Slip", "Reimbursements", through="Adjustments", target="Reimbursement Amt", fn="sum")
    rollup("Pay Slip", "Other Adjustments", through="Adjustments", target="Other Amt", fn="sum")
    ensure("Pay Slip", "Gross", formula("field('Regular Pay') + field('OT Pay') + field('Per-diem') + "
                                        "when_empty(field('Reimbursements'), 0) + when_empty(field('Other Adjustments'), 0)"))
    rollup("Pay Slip", "Paid", through="Allocations", target="Counted", fn="sum")
    ensure("Pay Slip", "Balance", formula("field('Gross') - when_empty(field('Paid'), 0)"))
    ensure("Pay Slip", "Status", formula("if(field('Balance') <= 0, 'Paid', if(field('Period End') < today(), 'Due', 'Open'))"))
    rollup("Pay Slip", "Last Paid", through="Allocations", target="Txn Date", fn="max", optional=True)
    ensure("Pay Slip", "OT Notes",
           formula("concat(join(filter(lookup('Workdays', 'OT Note'), lookup('Workdays', 'OT Note') != ''), '; '), "
                   "if(field('Weekly OT Min') > 0, concat(' | Week: ', totext(field('Weekly OT Min')), "
                   "' min of regular time beyond 45h paid as overtime'), ''))"),
           alternatives=[formula("join(lookup('Workdays', 'OT Note'), '; ')")], optional=True)
    ensure("Pay Slip", "Agreed Terms", formula("first(lookup('Pay Terms', 'Agreed Terms'))"))
    ensure("Pay Slip", "Terms Check",
           formula("if(and(first(lookup('Pay Terms', 'Daily OT After (h)')) = 9, "
                   "first(lookup('Pay Terms', 'Weekly OT After (h)')) = 45), '', 'FORMULAS ASSUME 9h / 45h — REVIEW')"))
    primary("Pay Slip", "concat(first(lookup('Pay Terms', 'Caretaker')), ' · ', datetime_format(field('Period Start'), 'MM/DD'), "
                        "'–', datetime_format(field('Period End'), 'MM/DD/YYYY'))")

    print("Views")
    view("Pay Slip", "Statement", sort=("Period Start", "DESC"), footer_sum=["Gross", "Paid", "Balance"])
    view("Pay Slip", "Needs attention", filters=[("Status", "equal", "Due")])
    view("Workdays", "Needs attention", filter_type="OR",
         filters=[("Shift Count", "lower_than", "1")] + ([("Date Check", "not_empty", "")] if api.field(tid("Workdays"), "Date Check") else []))
    view("Transactions", "Needs attention", filter_type="OR", filters=[
        ("Unallocated", "not_equal", "0"),
        ("Status", "single_select_not_equal", str(option_id("Transactions", "Status", "Completed"))),
        ("Transaction ID", "empty", ""),
        ("Receipt", "empty", ""),
    ])
    print("\nSchema done. Next: uv run deploy.py backfill")


# ----------------------------------------------------------------------------------------------
# backfill
# ----------------------------------------------------------------------------------------------

AGREED_TERMS = """\
Regular rate: $27.22 per hour for every hour you are with Vedanth, including nap time and quiet time. \
(That is your $25 base with your 10 vacation days and the federal holidays folded in, so days off are already paid.)

Overtime: 1.5x, $40.83 per hour, for every hour over 9 hours in a workday or over 45 hours in a workweek \
(Monday through Sunday). A workday is a calendar day, Pacific time, wherever we are. \
If a day has two blocks with an announced break between them, the blocks add together for that day. \
This follows California Labor Code section 1454.

Per diem: $75 for every night you are away from home with us (out of town), on top of your hourly pay. \
Travel days are paid like any other workday, including time in the car, at the airport or on a flight.

Reimbursements: anything you pay out of pocket for Vedanth is paid back on the next statement.

Hours come from the front-door camera and the travel log, so you never keep a timesheet. \
If anything on a statement looks off, tell us and we will go through it together."""

PAY_TERMS = {"Caretaker": "Vanessa Lopez", "Effective From": "2026-09-07", "Regular Rate": "27.22",
             "OT Multiplier": "1.5", "Per-diem Rate": "75",
             "Daily OT After (h)": "9", "Weekly OT After (h)": "45", "Agreed Terms": AGREED_TERMS,
             "Notes": "$25 base; 10 vacation days + federal holidays folded into the hourly rate."}

ACCOUNTS = [  # provider, owner, handle — all pay so far has gone through Venmo
    ("Venmo", "Anil", "@AnilCG"),
    ("Venmo", "Vanessa Lopez", "@vanessa_lopez"),
]

# date, amount, type, memo, allocations [(slip Monday, amount)]
TRANSACTIONS = [
    ("2026-09-13T12:00:00-07:00", "1088.00", "Partial", "Vanessa pay 9/7–9/13 (partial)", [("2026-09-07", "1088.00")]),
    ("2026-09-20T12:00:00-07:00", "222.25", "Regular pay", "Vanessa pay 9/7–9/13 (balance)", [("2026-09-07", "222.25")]),
    ("2026-09-20T12:00:00-07:00", "1272.53", "Regular pay", "Vanessa pay 9/14–9/20", [("2026-09-14", "1272.53")]),
]


def monday_of(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def cmd_backfill() -> None:
    def existing(table: str) -> list[dict]:
        return api.rows(tid(table))

    def sel(table: str, field: str, value: str) -> int:
        return option_id(table, field, value)

    print("Pay Terms")
    terms = existing("Pay Terms")
    term = next((r for r in terms if r["Caretaker"] == PAY_TERMS["Caretaker"]), None)
    if term is None:
        term = api.add_rows(tid("Pay Terms"), [PAY_TERMS])[0]
        print("  created")
    else:
        missing = {k: v for k, v in PAY_TERMS.items() if k in term and not term.get(k)}
        if missing:
            term = api.update_rows(tid("Pay Terms"), [{"id": term["id"], **missing}])[0]
            print(f"  exists — filled in {', '.join(missing)}")
        else:
            print("  exists")

    print("Timesheet (read only)")
    shifts = api.rows(TIMESHEET_ID)
    by_day: dict[str, list[dict]] = {}
    for s in shifts:
        by_day.setdefault(str(s["Date"])[:10], []).append(s)
    print(f"  {len(shifts)} shifts over {len(by_day)} days")

    print("Pay Slip")
    slips = {r["Period Start"]: r for r in existing("Pay Slip")}
    mondays = sorted({monday_of(dt.date.fromisoformat(d)).isoformat() for d in by_day})
    new = [{"Period Start": m, "Pay Terms": [term["id"]]} for m in mondays if m not in slips]
    for r in api.add_rows(tid("Pay Slip"), new):
        slips[r["Period Start"]] = r
    print(f"  {len(new)} created, {len(mondays) - len(new)} existed")

    print("Workdays")
    days = {r["Date"]: r for r in existing("Workdays")}
    new = []
    for d, rows in sorted(by_day.items()):
        if d in days:
            continue
        travel = any((r.get("Travel") or {}).get("value") == "Yes" for r in rows)
        new.append({
            "Date": d,
            "Shifts": [r["id"] for r in rows],
            "Pay Slip": [slips[monday_of(dt.date.fromisoformat(d)).isoformat()]["id"]],
            "Location": sel("Workdays", "Location", "San Diego" if travel else "Home"),
            "Overnight": d in OVERNIGHT,
        })
    api.add_rows(tid("Workdays"), new)
    print(f"  {len(new)} created, {len(by_day) - len(new)} existed")

    print("Accounts")
    accts = existing("Accounts")

    def find_acct(provider: str, owner: str) -> dict | None:
        return next((a for a in accts if (a["Provider"] or {}).get("value") == provider
                     and (a["Owner"] or {}).get("value") == owner), None)

    new = [{"Provider": sel("Accounts", "Provider", p), "Owner": sel("Accounts", "Owner", o), "Handle": h, "Active": True}
           for p, o, h in ACCOUNTS if find_acct(p, o) is None]
    accts += api.add_rows(tid("Accounts"), new)
    print(f"  {len(new)} created, {len(ACCOUNTS) - len(new)} existed")
    frm, to = find_acct("Venmo", "Anil"), find_acct("Venmo", "Vanessa Lopez")

    print("Transactions + Allocations")
    txns = existing("Transactions")
    allocs = existing("Allocations")
    created_t = created_a = 0
    for when, amount, typ, memo, allocations in TRANSACTIONS:
        t = next((x for x in txns if x["Memo"] == memo and float(x["Amount"] or 0) == float(amount)), None)
        if t is None:
            t = api.add_rows(tid("Transactions"), [{
                "Date": when, "Amount": amount, "Memo": memo,
                "Status": sel("Transactions", "Status", "Completed"),
                "Type": sel("Transactions", "Type", typ),
                "From": [frm["id"]], "To": [to["id"]],
                "Notes": "Backfilled from the Sep 21 statement. Add the Venmo transaction ID and screenshot.",
            }])[0]
            txns.append(t)
            created_t += 1
        for monday, amt in allocations:
            slip = slips[monday]
            if any(a["Transaction"] and a["Transaction"][0]["id"] == t["id"]
                   and a["Pay Slip"] and a["Pay Slip"][0]["id"] == slip["id"] for a in allocs):
                continue
            allocs += api.add_rows(tid("Allocations"), [{"Transaction": [t["id"]], "Pay Slip": [slip["id"]], "Amount": amt}])
            created_a += 1
    print(f"  {created_t} transactions, {created_a} allocations created")
    print("\nBackfill done. Next: uv run deploy.py verify")


# ----------------------------------------------------------------------------------------------
# verify / teardown
# ----------------------------------------------------------------------------------------------

EXPECTED = {"2026-09-07": ("1324.54", "14.29"), "2026-09-14": ("1274.57", "2.04")}


def cmd_verify() -> None:
    print(f"{'Pay slip':<28}{'Reg min':>9}{'OT min':>8}{'Gross':>11}{'Paid':>11}{'Balance':>10}  Status")
    ok = True
    for r in sorted(api.rows(tid("Pay Slip")), key=lambda r: r["Period Start"]):
        gross, bal = r.get("Gross"), r.get("Balance")
        flag = ""
        if r["Period Start"] in EXPECTED:
            eg, eb = EXPECTED[r["Period Start"]]
            if float(gross or 0) != float(eg) or float(bal or 0) != float(eb):
                flag, ok = f"  ← expected gross {eg}, balance {eb}", False
        print(f"{r['Name']:<28}{str(r.get('Reg Min')):>9}{str(r.get('Daily OT Min')):>8}{str(gross):>11}"
              f"{str(r.get('Paid')):>11}{str(bal):>10}  {r.get('Status')}{flag}")
    print("\nMatches the Sep 21 statement." if ok else "\nMISMATCH — see flags above.")

    # Natural-key check on Timesheet: a row is a shift keyed by (Start, End). Two rows with the same
    # key, or shifts that overlap, would double-count hours on the Workday.
    shifts = sorted(api.rows(TIMESHEET_ID), key=lambda r: (r["Start"] or "", r["End"] or ""))
    keys = [(r["Start"], r["End"]) for r in shifts]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    overlaps = [(a["id"], b["id"]) for a, b in zip(shifts, shifts[1:])
                if a["End"] and b["Start"] and a["End"] > b["Start"]]
    if dupes or overlaps:
        print("\nTimesheet integrity:")
        for k in dupes:
            print(f"  DUPLICATE shift {k[0]} – {k[1]} entered more than once")
        for a, b in overlaps:
            print(f"  OVERLAP rows {a} and {b}: the second starts before the first ends")
    else:
        print(f"Timesheet integrity: {len(shifts)} shifts, no duplicate (Start, End) keys, no overlaps.")


def cmd_check() -> None:
    print(f"Auth in use : {'database token (BASEROW_TOKEN)' if api.mode == 'token' else 'JWT login (BASEROW_EMAIL)'}")
    print(f"Server      : {BASE}")
    print(f"Database    : {DB_ID}\n")
    names = {t["name"]: t["id"] for t in api.tables()}
    print("Tables this auth can reach:")
    for n in ["Timesheet", *TABLES]:
        if n in names:
            print(f"  {n:<14} id {names[n]}")
        else:
            why = "not created yet — run `schema`" if api.mode == "jwt" else \
                  "not visible — not created yet, or the token lacks permission on it"
            print(f"  {n:<14} {why}")
    if "Timesheet" in names:
        n = len(api.rows(TIMESHEET_ID))
        print(f"\nTimesheet read OK: {n} rows (read only; the script never writes to it).")
    missing = [n for n in TABLES if n not in names]
    if not missing:
        print("All 7 ledger tables reachable. Ready for `backfill` / `verify`.")
    elif api.mode == "token":
        print(f"\n{len(missing)} ledger table(s) not reachable with this token. If `schema` has already run,\n"
              "open the token in Baserow → Settings → Database tokens and make sure it covers these tables\n"
              "(tokens created for specific tables don't pick up new ones automatically).")


def cmd_teardown(yes: bool) -> None:
    if not yes:
        sys.exit("Add --yes to trash the 7 ledger tables. Timesheet is never touched.")
    for t in api.tables():
        if t["name"] in TABLES and t["id"] != TIMESHEET_ID:
            api.req("DELETE", f"/api/database/tables/{t['id']}/")
            print(f"  trashed {t['name']}")


# ----------------------------------------------------------------------------------------------

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in {"check", "schema", "backfill", "verify", "teardown"}:
        sys.exit(__doc__)
    api = Baserow(needs_jwt=cmd in {"schema", "teardown"})
    {"check": cmd_check, "schema": cmd_schema, "backfill": cmd_backfill, "verify": cmd_verify,
     "teardown": lambda: cmd_teardown("--yes" in sys.argv)}[cmd]()
