#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "python-dotenv>=1.0"]
# ///
"""
baserow_schema.py -- create a schema in Baserow: the retailer-neutral commerce schema
(commerce_schema.py, database Shopping) or, with --schema planning, Financial Planning
(planning_schema.py).

    uv run baserow_schema.py plan                       what would be created; changes nothing (token is enough)
    uv run baserow_schema.py apply --suffix " (test)"   create test copies first: "Orders (test)", ...
    uv run baserow_schema.py apply                      create the real tables
    uv run baserow_schema.py teardown --suffix " (test)" --yes   trash the test copies (only test copies)
    uv run baserow_schema.py --schema planning plan     the same for Financial Planning; apply also
                                                        creates the database if it does not exist

Safe to re-run: tables, fields, links, options and views that exist are left alone. Nothing is
ever deleted or renamed except (a) reverse-link names are set to the schema's names, (b) missing
select options are added next to the existing ones, (c) a view missing some of its filters or
its sort gets them added and (d) the schema's MIGRATIONS, run first: a field renamed in place,
or a select field's options set to the schema's list (renamed options keep their ids, so rows
keep their values; an option is dropped only when no row uses it, else the run stops). Type
mismatches are reported, never "fixed".
`plan` and `apply` run the same code; plan just stops before each change. If Baserow drops the
connection mid-run, a fresh pass (up to 3) checks what exists and carries on from there.

Auth (same convention as ../baserow/deploy.py):
  plan   BASEROW_TOKEN (database token) if set, else a login.
  apply  a login (JWT): Baserow only lets a logged-in user create tables and fields. BASEROW_EMAIL /
         BASEROW_PASSWORD from .env, or you are prompted; the password is never stored or printed.

Target: the Shopping database, BASEROW_SHOPPING_DATABASE_ID (default 566971). With a login the
script refuses to run unless that database is really named "Shopping" (--database-name to change).
"""

from __future__ import annotations

import argparse
import getpass
import importlib
import os
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

import httpx
from dotenv import load_dotenv

import commerce_schema as S
from schema_kit import SELECT_TYPES

SCHEMAS = {"commerce": "commerce_schema", "planning": "planning_schema"}

HERE = Path(__file__).resolve().parent
COLORS = ["blue", "green", "orange", "red", "purple", "yellow", "brown", "cyan", "pink", "gray"]
API_TYPE = {"datetime": "date"}  # spec type -> Baserow field type where they differ


class ApiError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body}")
        self.status, self.body = status, body


# Gateway errors in front of api.baserow.io (Cloudflare 52x, proxy 502-504). The server may or
# may not have acted, so they are handled like a dropped connection: a fresh pass re-reads what
# exists and carries on, rather than blindly repeating a write.
TRANSIENT_STATUS = {502, 503, 504, 520, 521, 522, 523, 524}


def transient(e: BaseException) -> bool:
    return isinstance(e, httpx.TransportError) or (isinstance(e, ApiError) and e.status in TRANSIENT_STATUS)


def describe(e: BaseException) -> str:
    return f"HTTP {e.status}" if isinstance(e, ApiError) else (str(e) or type(e).__name__)


def retry_after(r: httpx.Response, attempt: int) -> float:
    """How long a 429 asks us to wait: Retry-After in seconds when given as a number, else
    exponential back-off (1, 2, 4, 8, 16 s). Baserow Cloud's documented limit is 10 concurrent
    requests; these scripts send one at a time, so a 429 means the server is shedding load."""
    try:
        return max(float(r.headers["Retry-After"]), 1.0)
    except (KeyError, ValueError):   # absent, or an HTTP date
        return float(2 ** attempt)


class Api(Protocol):
    mode: str
    def tables(self, refresh: bool = False) -> list[dict[str, Any]]: ...
    def create_table(self, name: str, key: str) -> dict[str, Any]: ...
    def delete_table(self, table_id: int) -> None: ...
    def fields(self, table_id: int, refresh: bool = False) -> list[dict[str, Any]]: ...
    def create_field(self, table_id: int, payload: dict[str, Any]) -> dict[str, Any]: ...
    def update_field(self, field_id: int, payload: dict[str, Any]) -> dict[str, Any]: ...
    def views(self, table_id: int) -> list[dict[str, Any]]: ...
    def create_view(self, table_id: int, payload: dict[str, Any]) -> dict[str, Any]: ...
    def create_filter(self, view_id: int, payload: dict[str, Any]) -> None: ...
    def create_sort(self, view_id: int, payload: dict[str, Any]) -> None: ...
    def view_filters(self, view_id: int) -> list[dict[str, Any]]: ...
    def view_sorts(self, view_id: int) -> list[dict[str, Any]]: ...
    def count_rows(self, table_id: int, field: dict[str, Any], option_id: int) -> int: ...
    def reset_cache(self) -> None: ...
    def database_name(self) -> str | None: ...


# ----------------------------------------------------------------------------------------------
# HTTP client (patterns from ../baserow/deploy.py, plus JWT refresh and 429 back-off)
# ----------------------------------------------------------------------------------------------
class Baserow:
    def __init__(self, base: str, db_id: int | None, needs_jwt: bool) -> None:
        self.c = httpx.Client(base_url=base.rstrip("/"), timeout=60)
        self.db_id = db_id
        self.refresh_token: str | None = None
        tok = os.environ.get("BASEROW_TOKEN")
        if needs_jwt or not tok:
            self._login()
        else:
            self.c.headers["Authorization"] = f"Token {tok}"
            self.mode = "token"
        self._tables: list[dict[str, Any]] | None = None
        self._fields: dict[int, list[dict[str, Any]]] = {}

    def _login(self) -> None:
        email = os.environ.get("BASEROW_EMAIL")
        pw = os.environ.get("BASEROW_PASSWORD")
        if not (email and pw):
            if not sys.stdin.isatty():
                sys.exit("This needs a Baserow login (JWT): set BASEROW_EMAIL and BASEROW_PASSWORD in .env, "
                         "or run it in a terminal to be prompted.")
            print("Creating tables and fields needs a Baserow login (database tokens cover rows only).")
            email = email or input("Baserow email: ").strip()
            pw = pw or getpass.getpass("Baserow password (not stored, not shown): ")
        r = self.c.post("/api/user/token-auth/", json={"email": email, "password": pw})
        if r.status_code >= 400:
            sys.exit(f"Login failed: HTTP {r.status_code}")
        j = r.json()
        self.c.headers["Authorization"] = f"JWT {j.get('access_token') or j.get('token')}"
        self.refresh_token = j.get("refresh_token")
        self.mode = "jwt"

    def _refresh(self) -> bool:
        if not self.refresh_token:
            return False
        r = self.c.post("/api/user/token-refresh/", json={"refresh_token": self.refresh_token})
        if r.status_code >= 400:
            return False
        j = r.json()
        self.c.headers["Authorization"] = f"JWT {j.get('access_token') or j.get('token')}"
        return True

    def req(self, method: str, path: str, **kw: Any) -> Any:
        for attempt in range(5):
            r = self.c.request(method, path, **kw)
            if r.status_code == 401 and self.mode == "jwt" and attempt == 0 and self._refresh():
                continue  # access tokens are short-lived; refresh once and retry
            if r.status_code == 429:
                time.sleep(retry_after(r, attempt))
                continue
            if r.status_code >= 400:
                raise ApiError(r.status_code, r.text)
            return r.json() if r.content else None
        raise ApiError(429, "rate limited five times in a row")

    def database_name(self) -> str | None:
        if self.mode != "jwt":
            return None  # a database token cannot read application details
        try:
            return str(self.req("GET", f"/api/applications/{self.db_id}/")["name"])
        except ApiError as e:
            print(f"(could not read the database name: HTTP {e.status})")
            return None

    def databases(self) -> list[dict[str, Any]]:
        """Every database this login can see, with its workspace (a login only)."""
        return [a for a in self.req("GET", "/api/applications/") if a.get("type") == "database"]

    def create_database(self, workspace_id: int, name: str) -> int:
        return int(self.req("POST", f"/api/applications/workspace/{workspace_id}/",
                            json={"name": name, "type": "database"})["id"])

    def tables(self, refresh: bool = False) -> list[dict[str, Any]]:
        if self.db_id is None:
            return []  # the database does not exist yet (a plan before its first apply)
        if self._tables is None or refresh:
            if self.mode == "token":
                self._tables = [t for t in self.req("GET", "/api/database/tables/all-tables/")
                                if t["database_id"] == self.db_id]
            else:
                self._tables = self.req("GET", f"/api/database/tables/database/{self.db_id}/")
        return self._tables or []

    def create_table(self, name: str, key: str) -> dict[str, Any]:
        # Header-only data: the table starts with exactly one field, the primary key, named `key`.
        t = self.req("POST", f"/api/database/tables/database/{self.db_id}/",
                     json={"name": name, "data": [[key]], "first_row_header": True})
        self.tables(refresh=True)
        return dict(t)

    def delete_table(self, table_id: int) -> None:
        self.req("DELETE", f"/api/database/tables/{table_id}/")
        self.tables(refresh=True)

    def fields(self, table_id: int, refresh: bool = False) -> list[dict[str, Any]]:
        if table_id not in self._fields or refresh:
            self._fields[table_id] = self.req("GET", f"/api/database/fields/table/{table_id}/")
        return self._fields[table_id]

    def create_field(self, table_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        fd = self.req("POST", f"/api/database/fields/table/{table_id}/", json=payload)
        self._fields.clear()  # a link also adds a field to the other table
        return dict(fd)

    def update_field(self, field_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        fd = self.req("PATCH", f"/api/database/fields/{field_id}/", json=payload)
        self._fields.clear()
        return dict(fd)

    def views(self, table_id: int) -> list[dict[str, Any]]:
        return list(self.req("GET", f"/api/database/views/table/{table_id}/"))

    def create_view(self, table_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.req("POST", f"/api/database/views/table/{table_id}/", json=payload))

    def create_filter(self, view_id: int, payload: dict[str, Any]) -> None:
        self.req("POST", f"/api/database/views/{view_id}/filters/", json=payload)

    def create_sort(self, view_id: int, payload: dict[str, Any]) -> None:
        self.req("POST", f"/api/database/views/{view_id}/sortings/", json=payload)

    def view_filters(self, view_id: int) -> list[dict[str, Any]]:
        return list(self.req("GET", f"/api/database/views/{view_id}/filters/"))

    def view_sorts(self, view_id: int) -> list[dict[str, Any]]:
        return list(self.req("GET", f"/api/database/views/{view_id}/sortings/"))

    def count_rows(self, table_id: int, field: dict[str, Any], option_id: int) -> int:
        """How many rows hold the option in a select field (rows in the trash are not counted)."""
        op = "multiple_select_has" if field["type"] == "multiple_select" else "single_select_equal"
        page = self.req("GET", f"/api/database/rows/table/{table_id}/",
                        params={"size": 1, f"filter__field_{field['id']}__{op}": option_id})
        return int(page["count"])

    def reset_cache(self) -> None:
        """Forget cached tables and fields: after a dropped connection they may be stale."""
        self._tables, self._fields = None, {}


# ----------------------------------------------------------------------------------------------
# Reconciler: one code path for plan and apply
# ----------------------------------------------------------------------------------------------
class Reconciler:
    def __init__(self, api: Api, suffix: str, apply: bool, schema: ModuleType = S) -> None:
        self.api, self.suffix, self.apply, self.S = api, suffix, apply, schema
        self.changes: list[str] = []
        self.warnings: list[str] = []
        self.ok = 0
        # A plan makes no changes, so it remembers what each migration would do to a field (by id)
        # and checks the rest of the schema against that, the way apply would find it.
        self.pending: dict[int, dict[str, Any]] = {}

    # names / lookups -------------------------------------------------------------------------
    def tname(self, name: str) -> str:
        return name + self.suffix

    def table(self, name: str) -> dict[str, Any] | None:
        return next((t for t in self.api.tables() if t["name"] == self.tname(name)), None)

    def tid(self, name: str) -> int | None:
        t = self.table(name)
        return int(t["id"]) if t else None

    def planned(self, fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The fields as they are once this run's migrations are made (differs only in a plan)."""
        return [{**x, **self.pending[x["id"]]} if x["id"] in self.pending else x for x in fields]

    def field(self, table: str, name: str) -> dict[str, Any] | None:
        tid = self.tid(table)
        if tid is None:
            return None
        return next((x for x in self.planned(self.api.fields(tid)) if x["name"] == name), None)

    def change(self, what: str) -> bool:
        """Record a change; True means go ahead and make it."""
        self.changes.append(what)
        print(("  + " if self.apply else "  would ") + what)
        return self.apply

    def warn(self, what: str) -> None:
        self.warnings.append(what)
        print(f"  ! {what}")

    # migrations (schema_kit.rename_field / set_options) --------------------------------------------
    def migrate(self, m: dict[str, Any]) -> None:
        if self.tid(m["table"]) is None:
            return  # the table does not exist yet: it is created under the schema's names
        if m["kind"] == "rename_field":
            self.rename_field(m["table"], m["old"], m["new"])
        else:
            self.set_options(m["table"], m["field"], m["renames"])

    def rename_field(self, table: str, old: str, new: str) -> None:
        if self.field(table, new):
            self.ok += 1
            return
        fd = self.field(table, old)
        if fd is None:
            return  # nothing to rename: the field is created under its new name
        if self.change(f"rename {self.tname(table)}.{old} -> {new}"):
            self.api.update_field(int(fd["id"]), {"name": new})
        else:
            self.pending.setdefault(int(fd["id"]), {})["name"] = new

    def set_options(self, table: str, name: str, renames: dict[str, str]) -> None:
        fd, tid = self.field(table, name), self.tid(table)
        if fd is None or tid is None:
            return  # the field is created later, with the schema's options
        spec = next(x for t in self.S.TABLES if t["name"] == table for x in t["fields"] if x["name"] == name)
        have: list[dict[str, Any]] = fd.get("select_options") or []
        by_value = {o["value"]: o for o in have}
        was = {new: old for old, new in renames.items()}
        target: list[dict[str, Any]] = []
        for i, value in enumerate(spec["options"]):
            o = by_value.get(value) or by_value.get(was.get(value, ""))
            target.append({"id": o["id"], "value": value, "color": o["color"]} if o
                          else {"value": value, "color": COLORS[i % len(COLORS)]})
        if [(o.get("id"), o["value"]) for o in target] == [(o["id"], o["value"]) for o in have]:
            self.ok += 1
            return
        kept = {o["id"] for o in target if "id" in o}
        dropped = [o for o in have if o["id"] not in kept]
        used = {o["value"]: n for o in dropped if (n := self.api.count_rows(tid, fd, int(o["id"])))}
        if used:
            sys.exit(f"{self.tname(table)}.{name}: rows still use options the schema no longer lists "
                     f"{used} (option: rows). Move those rows to a listed option, or put the option back "
                     "in the schema, then run again. Nothing in this field was changed.")
        old_value = {o["id"]: o["value"] for o in have}
        renamed = [f"{old_value[o['id']]} -> {o['value']}" for o in target
                   if "id" in o and old_value[o["id"]] != o["value"]]
        added = [o["value"] for o in target if "id" not in o]
        parts = ([f"rename {', '.join(renamed)}"] if renamed else []) \
            + ([f"add {', '.join(added)}"] if added else []) \
            + ([f"drop {', '.join(o['value'] for o in dropped)} (no rows use them)"] if dropped else [])
        if self.change(f"set {self.tname(table)}.{name} options to the schema's list: " + ("; ".join(parts) or "reorder")):
            self.api.update_field(int(fd["id"]), {"select_options": target})
        else:
            self.pending.setdefault(int(fd["id"]), {})["select_options"] = target

    # tables ----------------------------------------------------------------------------------
    def ensure_table(self, spec: dict[str, Any]) -> None:
        t = self.table(spec["name"])
        if t is None:
            if self.change(f"create table {self.tname(spec['name'])!r} (key field {spec['key']!r})"):
                self.api.create_table(self.tname(spec["name"]), spec["key"])
            return
        self.ok += 1
        primary = next((x for x in self.planned(self.api.fields(int(t["id"]))) if x.get("primary")), None)
        if primary and primary["name"] != spec["key"]:
            self.warn(f"{t['name']}: primary field is {primary['name']!r}, schema expects {spec['key']!r} "
                      "(left as is; the loader keys on the schema name)")

    # plain fields ------------------------------------------------------------------------------
    def payload(self, fd: dict[str, Any]) -> dict[str, Any]:
        t = fd["type"]
        base: dict[str, Any] = {"name": fd["name"], "type": API_TYPE.get(t, t)}
        if t == "number":
            base.update(number_decimal_places=fd["dp"], number_negative=True)
        elif t == "date":
            base.update(date_include_time=False, date_format="ISO")
        elif t == "datetime":
            base.update(date_include_time=True, date_format="ISO", date_time_format="24")
        elif t in SELECT_TYPES:
            base["select_options"] = [{"value": o, "color": COLORS[i % len(COLORS)]} for i, o in enumerate(fd["options"])]
        elif t in ("created_on", "last_modified"):
            base.update(date_include_time=True, date_format="ISO", date_time_format="24")
        elif t == "formula":
            base["formula"] = fd["formula"]
            if fd.get("dp") is not None:
                base["number_decimal_places"] = fd["dp"]
        return base

    def check_existing(self, table: str, existing: dict[str, Any], fd: dict[str, Any]) -> None:
        want = API_TYPE.get(fd["type"], fd["type"])
        if existing["type"] != want and not (fd["type"] == "rollup" and existing["type"] == "formula"):
            self.warn(f"{self.tname(table)}.{fd['name']}: is {existing['type']}, schema says {want} (left as is)")
            return
        if fd["type"] in ("date", "datetime") and bool(existing.get("date_include_time")) != (fd["type"] == "datetime"):
            self.warn(f"{self.tname(table)}.{fd['name']}: date/time setting differs from schema (left as is)")
        if fd["type"] in SELECT_TYPES:
            have = existing.get("select_options") or []
            missing = [o for o in fd["options"] if o not in {x["value"] for x in have}]
            if missing and self.change(f"add options {missing} to {self.tname(table)}.{fd['name']}"):
                keep = [{"id": x["id"], "value": x["value"], "color": x["color"]} for x in have]
                new = [{"value": o, "color": COLORS[(len(have) + i) % len(COLORS)]} for i, o in enumerate(missing)]
                self.api.update_field(int(existing["id"]), {"select_options": keep + new})
                return
        self.ok += 1

    def ensure_field(self, table: str, fd: dict[str, Any]) -> None:
        existing = self.field(table, fd["name"])
        if existing:
            self.check_existing(table, existing, fd)
            return
        if self.change(f"create {self.tname(table)}.{fd['name']} ({fd['type']})"):
            tid = self.tid(table)
            assert tid is not None
            self.api.create_field(tid, self.payload(fd))

    # links -----------------------------------------------------------------------------------
    def ensure_link(self, table: str, fd: dict[str, Any]) -> None:
        target, related = fd["to"], fd["related"]
        existing = self.field(table, fd["name"])
        if existing is None:
            if not self.change(f"link {self.tname(table)}.{fd['name']} -> {self.tname(target)} "
                               + (f"(reverse field {related!r})" if related else "(no reverse field)")):
                return
            src, dst = self.tid(table), self.tid(target)
            assert src is not None and dst is not None
            if related and self.field(target, related):
                sys.exit(f"{self.tname(target)} already has a field named {related!r}; "
                         "rename it before linking, so the reverse link can take that name")
            existing = self.api.create_field(src, {"name": fd["name"], "type": "link_row",
                                                   "link_row_table_id": dst, "has_related_field": bool(related)})
        elif existing["type"] != "link_row":
            self.warn(f"{self.tname(table)}.{fd['name']}: is {existing['type']}, schema says link_row (left as is)")
            return
        elif existing.get("link_row_table_id") != self.tid(target):
            self.warn(f"{self.tname(table)}.{fd['name']}: links to table {existing.get('link_row_table_id')}, "
                      f"schema says {self.tname(target)} (left as is)")
            return
        else:
            self.ok += 1
        if not related:
            return  # a link without a reverse field (e.g. a table linking to itself)
        # the reverse field: make sure it exists and carries the schema's name
        rel_id = existing.get("link_row_related_field_id")
        dst = self.tid(target)
        if not rel_id or dst is None:
            self.warn(f"{self.tname(table)}.{fd['name']}: has no reverse field in {self.tname(target)}")
            return
        rel = next((x for x in self.api.fields(dst, refresh=True) if x["id"] == rel_id), None)
        if rel and rel["name"] != fd["related"]:
            if self.field(target, fd["related"]):
                self.warn(f"{self.tname(target)}: cannot name reverse link {fd['related']!r}, the name is taken")
            elif self.change(f"name reverse link {self.tname(target)}.{rel['name']!r} -> {fd['related']!r}"):
                self.api.update_field(int(rel_id), {"name": fd["related"]})

    # derived fields ------------------------------------------------------------------------------
    def ensure_derived(self, table: str, fd: dict[str, Any]) -> None:
        existing = self.field(table, fd["name"])
        if existing:
            self.check_existing(table, existing, fd)
            return
        if not self.change(f"create {self.tname(table)}.{fd['name']} ({fd['type']})"):
            return
        tid = self.tid(table)
        assert tid is not None
        candidates: list[dict[str, Any]]
        if fd["type"] == "count":
            candidates = [{"type": "count", "through_field_id": self.fid(table, fd["through"])}]
        elif fd["type"] in ("rollup", "lookup"):
            target_table = self.S.link_target(table, fd["through"])
            assert target_table is not None
            ids = {"through_field_id": self.fid(table, fd["through"]),
                   "target_field_id": self.fid(target_table, fd["target"])}
            if fd["type"] == "lookup":
                candidates = [{"type": "lookup", **ids},
                              {"type": "formula", "formula": f"lookup('{fd['through']}', '{fd['target']}')"}]
            else:
                candidates = [{"type": "rollup", **ids, "rollup_function": fd["fn"]},
                              {"type": "formula", "formula": f"{fd['fn']}(lookup('{fd['through']}', '{fd['target']}'))"}]
        else:
            candidates = [self.payload(fd)]
            if "number_decimal_places" in candidates[0]:   # the display format is optional, the formula is not
                candidates.append({k: v for k, v in candidates[0].items() if k != "number_decimal_places"})
        errors = []
        for c in candidates:
            try:
                self.api.create_field(tid, {"name": fd["name"], **c})
                if c["type"] != fd["type"]:
                    self.warn(f"{self.tname(table)}.{fd['name']}: created as {c['type']} fallback")
                elif fd.get("dp") is not None and "number_decimal_places" not in c:
                    self.warn(f"{self.tname(table)}.{fd['name']}: created without its decimal-places format")
                return
            except ApiError as e:
                if e.status in TRANSIENT_STATUS:
                    raise  # the server hiccuped; not a verdict on this variant
                errors.append(e.body[:300])
        if fd.get("optional"):
            self.warn(f"{self.tname(table)}.{fd['name']}: skipped, no variant accepted: {errors[-1]}")
            return
        sys.exit(f"{self.tname(table)}.{fd['name']}: every variant rejected:\n" + "\n".join(errors))

    def ensure_key_formula(self, spec: dict[str, Any]) -> None:
        """A table whose key (primary) field is a formula over its own fields: the table is created
        with a text key, and once those fields exist the key becomes the formula."""
        expr = spec.get("key_formula")
        if not expr:
            return
        key = self.field(spec["name"], spec["key"])
        if key is not None and key["type"] == "formula":
            if key.get("formula") != expr:
                self.warn(f"{self.tname(spec['name'])}.{spec['key']}: formula differs from schema (left as is)")
            else:
                self.ok += 1
            return
        if self.change(f"make {self.tname(spec['name'])}.{spec['key']} the formula {expr}"):
            assert key is not None
            self.api.update_field(int(key["id"]), {"type": "formula", "formula": expr})

    def fid(self, table: str, name: str) -> int:
        fd = self.field(table, name)
        if fd is None:
            raise SystemExit(f"{self.tname(table)}.{name} not found")
        return int(fd["id"])

    # views -----------------------------------------------------------------------------------
    def ensure_view(self, table: str, v: dict[str, Any]) -> None:
        tid = self.tid(table)
        if tid is not None and self.api.mode == "token":
            return  # database tokens cannot list views; checked with a login
        where = f"view {self.tname(table)} / {v['name']!r}"
        view = next((x for x in self.api.views(tid) if x["name"] == v["name"]), None) if tid is not None else None
        have_filters: list[dict[str, Any]] = []
        have_sorts: list[dict[str, Any]] = []
        new = view is None
        if view is None:
            if not self.change(f"create {where}"):
                return
            assert tid is not None
            view = self.api.create_view(tid, {"name": v["name"], "type": "grid", "filter_type": v["filter_type"]})
        else:
            self.ok += 1
            have_filters, have_sorts = self.api.view_filters(int(view["id"])), self.api.view_sorts(int(view["id"]))
        # A run cut short can leave a view without some filters or its sort: add whatever is missing.
        for fname, ftype, want in v["filters"]:
            fd = self.field(table, fname)
            if fd is None:
                self.warn(f"{where}: field {fname!r} missing, filter skipped")
                continue
            value = want
            if ftype.startswith("single_select"):
                opt = next((o for o in fd.get("select_options") or [] if o["value"] == want), None)
                if opt is None:
                    self.warn(f"{where}: option {want!r} missing on {fname!r}, filter skipped")
                    continue
                value = str(opt["id"])
            if any(f["field"] == fd["id"] and f["type"] == ftype and str(f["value"]) == value for f in have_filters):
                continue
            if not new and not self.change(f"add filter {fname} {ftype} {want!r} to {where}"):
                continue
            try:
                self.api.create_filter(int(view["id"]), {"field": fd["id"], "type": ftype, "value": value})
            except ApiError as e:
                if e.status in TRANSIENT_STATUS:
                    raise
                self.warn(f"{where}: filter {fname} {ftype} rejected: {e.body[:200]}")
        if v["sort"]:
            fd = self.field(table, v["sort"][0])
            if fd is not None and not any(s["field"] == fd["id"] for s in have_sorts):
                if new or self.change(f"add sort {v['sort'][0]} {v['sort'][1]} to {where}"):
                    self.api.create_sort(int(view["id"]), {"field": fd["id"], "order": v["sort"][1]})

    # the whole schema --------------------------------------------------------------------------
    def run(self) -> None:
        migrations = getattr(self.S, "MIGRATIONS", [])
        if migrations:
            print("Migrations")
            for m in migrations:
                self.migrate(m)
        print("Tables")
        for t in self.S.TABLES:
            self.ensure_table(t)
        for t in self.S.TABLES:
            plain = [fd for fd in t["fields"] if fd["type"] != "link_row"]
            if plain:
                print(self.tname(t["name"]))
            for fd in plain:
                self.ensure_field(t["name"], fd)
        if any(t.get("key_formula") for t in self.S.TABLES):
            print("Key formulas")
            for t in self.S.TABLES:
                self.ensure_key_formula(t)
        print("Links")
        for t in self.S.TABLES:
            for fd in t["fields"]:
                if fd["type"] == "link_row":
                    self.ensure_link(t["name"], fd)
        print("Counts, rollups, lookups, formulas")
        for tname, fds in self.S.DERIVED.items():
            for fd in fds:
                self.ensure_derived(tname, fd)
        print("Views")
        for t in self.S.TABLES:
            for v in t["views"] + self.S.EXTRA_VIEWS.get(t["name"], []):
                self.ensure_view(t["name"], v)


def run_passes(api: Api, suffix: str, apply: bool, passes: int = 3, pause: float = 5.0,
               schema: ModuleType = S) -> tuple[Reconciler, int]:
    """Run the reconciler; if Baserow drops the connection, start a fresh pass. Every pass first
    checks what exists by name, so it carries on where the last one stopped and duplicates
    nothing (a write whose reply was lost is simply found in place)."""
    changes: list[str] = []
    warnings: list[str] = []
    for n in range(1, passes + 1):
        r = Reconciler(api, suffix, apply, schema)
        try:
            r.run()
        except (httpx.TransportError, ApiError) as e:
            if not transient(e):
                raise
            changes += [c for c in r.changes if c not in changes]
            warnings += [w for w in r.warnings if w not in warnings]
            if n == passes:
                sys.exit(f"\nBaserow failed {passes} times in a row ({describe(e)}). Nothing is lost: "
                         "run the same command again and it carries on from here.")
            print(f"\n! Baserow failed ({describe(e)}); pass {n + 1} of {passes} checks what exists and carries on\n")
            api.reset_cache()
            time.sleep(pause * n)
            continue
        r.changes = changes + [c for c in r.changes if c not in changes]
        r.warnings = warnings + [w for w in r.warnings if w not in warnings]
        return r, n
    raise AssertionError("unreachable")


def teardown(api: Api, suffix: str, yes: bool, schema: ModuleType = S) -> None:
    if not suffix.strip():
        sys.exit("teardown only removes test copies: pass --suffix (e.g. --suffix ' (test)'). "
                 "The real tables are never removed by this script.")
    names = {t["name"] + suffix for t in schema.TABLES}
    doomed = [t for t in api.tables() if t["name"] in names]
    if not doomed:
        print(f"no tables ending in {suffix!r}")
        return
    for t in doomed:
        print(("  trashing " if yes else "  would trash ") + t["name"])
        if yes:
            api.delete_table(int(t["id"]))
    if not yes:
        print("add --yes to trash them (recoverable from Baserow's trash)")


def resolve_database(api: Baserow, name: str, create: bool, workspace_id: int | None) -> int | None:
    """The id of the database called `name`. When it does not exist: apply creates it (in your
    only workspace, or --workspace-id), plan reports that it would and returns None."""
    dbs = api.databases()
    found = [d for d in dbs if d["name"] == name]
    if len(found) > 1:
        sys.exit(f"{len(found)} databases are named {name!r}; pass --database-id to pick one")
    if found:
        return int(found[0]["id"])
    if workspace_id is None:
        workspaces = sorted({int(ws["id"]) for d in dbs if (ws := d.get("workspace") or d.get("group"))})
        if len(workspaces) != 1:
            sys.exit(f"no database named {name!r}, and your databases are in {len(workspaces)} workspaces: "
                     "pass --workspace-id to say where to create it")
        workspace_id = workspaces[0]
    if not create:
        print(f"  would create database {name!r} in workspace {workspace_id}\n")
        return None
    print(f"  + create database {name!r} in workspace {workspace_id}\n")
    return api.create_database(workspace_id, name)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "apply", "teardown"])
    ap.add_argument("--schema", choices=sorted(SCHEMAS), default="commerce",
                    help="commerce (database Shopping) or planning (database Financial Planning)")
    ap.add_argument("--suffix", default="", help="table name suffix for a test copy, e.g. ' (test)'")
    ap.add_argument("--database-id", type=int, default=None)
    ap.add_argument("--database-name", default=None,
                    help="refuse to run unless the database has this name (default: the schema's database)")
    ap.add_argument("--workspace-id", type=int, default=None,
                    help="where apply creates the database when it does not exist (needed only with several workspaces)")
    ap.add_argument("--env", type=Path, default=HERE / ".env", help="dotenv file with BASEROW_* settings")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    schema = importlib.import_module(SCHEMAS[args.schema])
    problems = schema.validate()
    if problems:
        sys.exit(f"{SCHEMAS[args.schema]}.py is invalid:\n  " + "\n  ".join(problems))
    load_dotenv(args.env)
    db_name = args.database_name or schema.DATABASE
    id_env = getattr(schema, "DATABASE_ID_ENV", None)
    db_id: int | None = args.database_id or (int(os.environ.get(id_env, schema.DATABASE_ID_DEFAULT)) if id_env else None)
    base = os.environ.get("BASEROW_URL", "https://api.baserow.io")
    # Finding a database by name (and creating it) takes a login; a database token sees one database.
    api = Baserow(base, db_id, needs_jwt=args.command in ("apply", "teardown") or db_id is None)
    if db_id is None:
        db_id = api.db_id = resolve_database(api, db_name, args.command == "apply", args.workspace_id)

    name = api.database_name() if db_id is not None else None
    if name is not None and name != db_name:
        sys.exit(f"database {db_id} is named {name!r}, not {db_name!r}; refusing to touch it")
    existing = sorted(t["name"] for t in api.tables())
    print(f"Server {base} | database {db_id if db_id is not None else '(new)'}" + (f" ({name})" if name else "")
          + f" | auth {api.mode}")
    print(f"Existing tables: {', '.join(existing) or '(none visible)'}\n")

    if name is None and args.command in ("apply", "teardown") and not args.yes:
        # Unverified target: never write without an explicit yes from the person running it.
        if not sys.stdin.isatty():
            sys.exit(f"could not confirm database {db_id} is {args.database_name!r}; re-run with --yes to proceed")
        if input(f"Database name unverified. Proceed with database {db_id} (tables listed above)? [y/N] ").strip().lower() != "y":
            sys.exit("stopped; nothing changed")
    if args.command == "teardown":
        teardown(api, args.suffix, args.yes, schema)
        return
    r, passes = run_passes(api, args.suffix, apply=args.command == "apply", schema=schema)
    verb = "made" if r.apply else "needed"
    if passes == 1:
        print(f"\n{len(r.changes)} change(s) {verb}, {r.ok} already in place, {len(r.warnings)} warning(s).")
    else:
        print(f"\n{len(r.changes)} change(s) {verb} over {passes} passes (the connection dropped), "
              f"{len(r.warnings)} warning(s).")
    if api.mode == "token" and args.command == "plan":
        print("(views are not visible to a database token; `apply` checks them with a login)")


if __name__ == "__main__":
    main()
