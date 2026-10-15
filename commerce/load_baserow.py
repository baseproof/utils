#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "python-dotenv>=1.0"]
# ///
"""
load_baserow.py -- load a canonical file (written by a platform extractor, such as
../amazon_orders/extract_canonical.py) into the tables baserow_schema.py created.

    uv run load_baserow.py plan  FILE [--suffix " (test)"]   what would change; writes nothing
    uv run load_baserow.py apply FILE [--suffix " (test)"]   load it

Rows are matched on their key field, so it is safe to re-run: new rows are created, changed
fields updated, nothing is ever deleted. commerce_schema's field owners decide what is written:
`loader` fields always, `loader_if_empty` only while the field is empty (your edits stay),
`user` and derived fields never. When the file is a complete extract, rows of that platform it
no longer contains are marked (Orders: Missing From Source; Issues: Active off), not deleted.
Tables load in schema order, so every link points at rows already loaded; a link to a key no
row has, a value outside a field's options, or a key used by two rows stops the load. Each
apply records a Sync Runs row. If Baserow drops the connection, a fresh pass carries on.

Auth: a Baserow login, as for `baserow_schema.py apply` (BASEROW_EMAIL / BASEROW_PASSWORD
from --env, or prompted; the password is never stored or printed).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import httpx
from dotenv import load_dotenv

import baserow_schema as BS
import commerce_schema as S

HERE = Path(__file__).resolve().parent
BATCH = 200  # Baserow's batch endpoints take at most 200 rows
# Rows of the loaded platform that a complete extract no longer contains: marked, never deleted.
ABSENT = {"Orders": ("Missing From Source", True), "Issues": ("Active", False)}
# Written by the loader whenever it creates or changes a row; not part of the comparison.
TOUCH = ("Last Synced", "Sync Run")
RANK = {"unchanged": 0, "marked": 1, "updated": 2, "created": 3}


class RowApi(Protocol):
    def tables(self, refresh: bool = False) -> list[dict[str, Any]]: ...
    def fields(self, table_id: int, refresh: bool = False) -> list[dict[str, Any]]: ...
    def list_rows(self, table_id: int) -> list[dict[str, Any]]: ...
    def create_rows(self, table_id: int, items: list[dict[str, Any]]) -> list[dict[str, Any]]: ...
    def update_rows(self, table_id: int, items: list[dict[str, Any]]) -> None: ...
    def reset_cache(self) -> None: ...


class BaserowRows(BS.Baserow):
    """The schema script's client plus the row endpoints (user field names, batches)."""

    def __init__(self, base: str, db_id: int, needs_jwt: bool) -> None:
        super().__init__(base, db_id, needs_jwt)
        # A batch of linked rows makes Baserow recompute every count and rollup that depends
        # on them; on the cloud that can take well over a minute. Waiting beats re-reading.
        self.c.timeout = httpx.Timeout(60, read=300)

    def list_rows(self, table_id: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            r = self.req("GET", f"/api/database/rows/table/{table_id}/",
                         params={"user_field_names": "true", "size": BATCH, "page": page})
            out += r["results"]
            if not r.get("next"):
                return out
            page += 1

    def create_rows(self, table_id: int, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        r = self.req("POST", f"/api/database/rows/table/{table_id}/batch/",
                     params={"user_field_names": "true"}, json={"items": items})
        return list(r["items"])

    def update_rows(self, table_id: int, items: list[dict[str, Any]]) -> None:
        self.req("PATCH", f"/api/database/rows/table/{table_id}/batch/",
                 params={"user_field_names": "true"}, json={"items": items})


# ----------------------------------------------------------------------------------------------
# Values: one comparable form for what the file says and what Baserow returns
# ----------------------------------------------------------------------------------------------
def quant(fd: dict[str, Any]) -> Decimal:
    return Decimal(1).scaleb(-int(fd["dp"]))


def norm(fd: dict[str, Any], value: Any) -> Any:
    t = fd["type"]
    if t == "number":
        return None if value in (None, "") else Decimal(str(value)).quantize(quant(fd))
    if t == "single_select":
        return (value.get("value") if isinstance(value, dict) else value) or None
    if t == "link_row":
        return sorted(int(x["id"]) if isinstance(x, dict) else int(x) for x in value or [])
    if t == "boolean":
        return bool(value)
    if t == "date":
        return str(value)[:10] if value else None
    if t == "datetime":
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc) if value else None
    return "" if value is None else str(value)


def empty(v: Any) -> bool:
    return v in (None, "", [], False)


def to_api(fd: dict[str, Any], live: dict[str, Any], value: Any, where: str) -> Any:
    """A (link-resolved) value as Baserow's row endpoints take it."""
    t = fd["type"]
    if value is None:
        return [] if t == "link_row" else None
    if t == "number":
        return str(Decimal(str(value)).quantize(quant(fd)))
    if t == "single_select":
        opt = next((o for o in live.get("select_options") or [] if o["value"] == value), None)
        if opt is None:
            raise SystemExit(f"{where}: {value!r} is not an option in Baserow; add it to commerce_schema.py "
                             "and run baserow_schema.py apply")
        return int(opt["id"])
    return value


# ----------------------------------------------------------------------------------------------
# The file: checked in full before any call to Baserow
# ----------------------------------------------------------------------------------------------
def check_types(fd: dict[str, Any], v: Any) -> str | None:
    t = fd["type"]
    if v is None:
        return None
    ok = {
        "text": isinstance(v, str), "long_text": isinstance(v, str), "url": isinstance(v, str),
        "number": isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": isinstance(v, bool),
        "single_select": isinstance(v, str) and v in fd.get("options", []),
        "link_row": isinstance(v, list) and all(isinstance(x, str) and x for x in v),
        "date": isinstance(v, str) and len(v) == 10,
        "datetime": isinstance(v, str),
    }.get(t, False)
    if not ok:
        return f"{v!r} is not a valid {t}" + (f" (options: {fd['options']})" if t == "single_select" else "")
    if t == "date":
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            return f"{v!r} is not a YYYY-MM-DD date"
    return None


def validate_doc(doc: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for k in ("platform", "complete", "tables"):
        if k not in doc:
            errors.append(f"file has no {k!r}")
    if errors:
        return errors
    specs = {t["name"]: t for t in S.TABLES}
    for name, rows in doc["tables"].items():
        spec = specs.get(name)
        if spec is None:
            errors.append(f"{name}: not a table in commerce_schema")
            continue
        fields = {fd["name"]: fd for fd in spec["fields"]}
        seen: set[str] = set()
        for i, row in enumerate(rows):
            key = row.get(spec["key"])
            if not isinstance(key, str) or not key.strip():
                errors.append(f"{name} row {i}: no {spec['key']}")
                continue
            if key in seen:
                errors.append(f"{name}: key {key!r} appears twice")
            seen.add(key)
            for fname, v in row.items():
                if fname == spec["key"]:
                    continue
                fd = fields.get(fname)
                if fd is None:
                    errors.append(f"{name}.{fname}: not a field the loader can write (unknown, derived or a reverse link)")
                elif fd["owner"] == "user":
                    errors.append(f"{name}.{fname}: yours; the loader never writes it")
                elif fname in TOUCH:
                    errors.append(f"{name}.{fname}: written by the loader itself")
                else:
                    problem = check_types(fd, v)
                    if problem:
                        errors.append(f"{name} {key!r} {fname}: {problem}")
            if len(errors) > 50:
                return errors + ["... (stopped after 50)"]
    return errors


# ----------------------------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------------------------
class Loader:
    def __init__(self, api: RowApi, doc: dict[str, Any], suffix: str, apply: bool, run_id: str, now: str,
                 status: dict[tuple[str, str], str]) -> None:
        self.api, self.doc, self.suffix, self.apply = api, doc, suffix, apply
        self.run_id, self.now, self.status = run_id, now, status
        self.ids: dict[str, dict[str, int]] = {}   # table -> key -> row id (placeholders < 0 in a plan)

    def tid(self, name: str) -> int:
        t = next((t for t in self.api.tables() if t["name"] == name + self.suffix), None)
        if t is None:
            raise SystemExit(f"no table {name + self.suffix!r}: run baserow_schema.py apply"
                             + (f" --suffix {self.suffix!r}" if self.suffix else ""))
        return int(t["id"])

    def mark(self, table: str, key: str, what: str) -> None:
        old = self.status.get((table, key))
        if old is None or RANK[what] > RANK[old]:
            self.status[(table, key)] = what

    def load(self) -> None:
        wanted = set(self.doc["tables"])
        targets = {fd["to"] for t in S.TABLES if t["name"] in wanted for fd in t["fields"] if fd["type"] == "link_row"}
        for spec in S.TABLES:  # schema order: every link points at an earlier table
            if spec["name"] in wanted:
                self.load_table(spec, self.doc["tables"][spec["name"]])
            elif spec["name"] in targets:
                self.index(spec, self.existing(spec))

    def existing(self, spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
        by_key: dict[str, dict[str, Any]] = {}
        dupes: set[str] = set()
        for row in self.api.list_rows(self.tid(spec["name"])):
            key = (row.get(spec["key"]) or "").strip()
            if not key:
                continue  # a blank row someone added by hand: nothing to match
            if key in by_key:
                dupes.add(key)
            by_key[key] = row
        if dupes:
            raise SystemExit(f"{spec['name'] + self.suffix}: key(s) used by more than one row: {sorted(dupes)[:10]}. "
                             "Baserow does not enforce unique keys; remove the duplicates, then re-run.")
        return by_key

    def index(self, spec: dict[str, Any], by_key: dict[str, dict[str, Any]]) -> None:
        self.ids[spec["name"]] = {k: int(r["id"]) for k, r in by_key.items()}

    def resolve(self, table: str, key: str, fd: dict[str, Any], value: Any) -> Any:
        if fd["type"] != "link_row" or value is None:
            return value
        ids = self.ids[fd["to"]]
        missing = [k for k in value if k not in ids]
        if missing:
            raise SystemExit(f"{table} {key!r} {fd['name']}: no {fd['to']} row with key {missing[:3]}")
        return [ids[k] for k in value]

    def load_table(self, spec: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        name, key_field = spec["name"], spec["key"]
        tid = self.tid(name)
        live = {f["name"]: f for f in self.api.fields(tid)}
        fields = {fd["name"]: fd for fd in spec["fields"]}
        touch = [t for t in TOUCH if t in fields]
        written = {fname for row in rows for fname in row if fname != key_field} | set(touch)
        if name in ABSENT and self.doc["complete"]:
            written.add(ABSENT[name][0])
        missing = sorted(written - set(live))
        if missing:
            raise SystemExit(f"{name + self.suffix}: no field(s) {missing} in Baserow: run baserow_schema.py apply")
        by_key = self.existing(spec)
        self.index(spec, by_key)
        stamp = {"Last Synced": self.now, "Sync Run": self.run_id}
        creates: list[dict[str, Any]] = []
        updates: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            key = row[key_field]
            want = {f: self.resolve(name, key, fields[f], v) for f, v in row.items() if f != key_field}
            have = by_key.get(key)
            if have is None:
                item = {key_field: key}
                item.update({f: to_api(fields[f], live[f], v, f"{name}.{f}") for f, v in want.items()
                             if v is not None and v != []})
                item.update({t: stamp[t] for t in touch})
                creates.append(item)
                continue
            change: dict[str, Any] = {}
            for f, v in want.items():
                fd = fields[f]
                current = norm(fd, have.get(f))
                if fd["owner"] == "loader_if_empty" and not empty(current):
                    continue  # proposed once; yours now
                if norm(fd, v) != current:
                    change[f] = to_api(fd, live[f], v, f"{name}.{f}")
            if change:
                change.update({t: stamp[t] for t in touch})
                updates.append((key, {"id": int(have["id"]), **change}))
            else:
                self.mark(name, key, "unchanged")
        marked: list[tuple[str, dict[str, Any]]] = []
        if name in ABSENT and self.doc["complete"]:
            fname, value = ABSENT[name]
            present = {row[key_field] for row in rows}
            for key, have in by_key.items():
                if key.startswith(self.doc["platform"] + "|") and key not in present \
                        and norm(fields[fname], have.get(fname)) != value:
                    marked.append((key, {"id": int(have["id"]), fname: value, **{t: stamp[t] for t in touch}}))
        if not self.apply:
            for n, item in enumerate(creates, 1):
                self.ids[name][item[key_field]] = -n   # placeholder, so later links resolve in a plan
                self.mark(name, item[key_field], "created")
            for key, _ in updates:
                self.mark(name, key, "updated")
            for key, _ in marked:
                self.mark(name, key, "marked")
            return
        for i in range(0, len(creates), BATCH):
            for made in self.api.create_rows(tid, creates[i:i + BATCH]):
                self.ids[name][made[key_field]] = int(made["id"])
                self.mark(name, made[key_field], "created")
            done = min(i + BATCH, len(creates))
            if len(creates) > 1000 and (done % 1000 == 0 or done == len(creates)):
                print(f"  {name + self.suffix}: {done}/{len(creates)} new rows written", flush=True)
        for what, batch in (("updated", updates), ("marked", marked)):
            for i in range(0, len(batch), BATCH):
                chunk = batch[i:i + BATCH]
                self.api.update_rows(tid, [item for _, item in chunk])
                for key, _ in chunk:
                    self.mark(name, key, what)


def load(api: RowApi, doc: dict[str, Any], suffix: str, apply: bool, run_id: str, now: str,
         passes: int = 5, pause: float = 5.0) -> dict[tuple[str, str], str]:
    """Load the file; if Baserow drops the connection or its gateway fails (BS.transient),
    start a fresh pass. Each pass re-reads what exists, so rows a lost reply created are found
    by key rather than made twice."""
    status: dict[tuple[str, str], str] = {}
    for n in range(1, passes + 1):
        try:
            Loader(api, doc, suffix, apply, run_id, now, status).load()
            return status
        except (httpx.TransportError, BS.ApiError) as e:
            if not BS.transient(e):
                raise
            if n == passes:
                sys.exit(f"\nBaserow failed {passes} times in a row ({BS.describe(e)}). Nothing is lost: "
                         "run the same command again and it carries on from here.")
            print(f"! Baserow failed ({BS.describe(e)}); pass {n + 1} of {passes} checks what exists and carries on",
                  flush=True)
            api.reset_cache()
            time.sleep(pause * n)
    raise AssertionError("unreachable")


def summary(doc: dict[str, Any], status: dict[tuple[str, str], str]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for name in doc["tables"]:
        counts = {k: 0 for k in RANK}
        for (table, _), what in status.items():
            if table == name:
                counts[what] += 1
        out[name] = counts
    return out


def main() -> None:
    again = "Nothing is lost: rows already written stay, and running the same command again carries on."
    try:
        run()
    except BS.ApiError as e:   # during a load, transient ones get fresh passes instead
        body = "(an HTML error page)" if e.body.lstrip().startswith("<") else e.body[:500]
        sys.exit(f"\nBaserow refused a request: HTTP {e.status} {body}\n{again}")
    except httpx.TransportError as e:
        sys.exit(f"\nCould not reach Baserow ({BS.describe(e)}).\n{again}")


def run() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "apply"])
    ap.add_argument("file", type=Path, help="canonical file from an extractor")
    ap.add_argument("--suffix", default="", help="load into the test copies, e.g. ' (test)'")
    ap.add_argument("--database-id", type=int, default=None)
    ap.add_argument("--database-name", default="Shopping", help="refuse to run unless the database has this name")
    ap.add_argument("--env", type=Path, default=HERE / ".env", help="dotenv file with BASEROW_* settings")
    args = ap.parse_args()

    doc = json.loads(args.file.read_text())
    problems = S.validate() + validate_doc(doc)
    if problems:
        sys.exit(f"{args.file} cannot be loaded:\n  " + "\n  ".join(problems))
    load_dotenv(args.env)
    db_id = args.database_id or int(os.environ.get("BASEROW_SHOPPING_DATABASE_ID", "566971"))
    base = os.environ.get("BASEROW_URL", "https://api.baserow.io")
    api = BaserowRows(base, db_id, needs_jwt=True)
    name = api.database_name()
    if name != args.database_name:
        sys.exit(f"database {db_id} is named {name!r}, not {args.database_name!r}; refusing to touch it")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    run_id = f"{doc['platform']}|{now}"
    rows = sum(len(r) for r in doc["tables"].values())
    print(f"Server {base} | database {db_id} ({name}) | tables{args.suffix or ' (real)'}")
    print(f"File {args.file.name}: {doc['platform']}, {rows} rows, "
          + ("complete extract" if doc["complete"] else f"a sample (orders since {doc.get('since')}); nothing is marked missing")
          + "\n")

    apply = args.command == "apply"
    runs_tid = Loader(api, doc, args.suffix, apply, run_id, now, {}).tid("Sync Runs")
    run_row: dict[str, Any] = {}
    if apply:
        run_row = api.create_rows(runs_tid, [{
            "Run ID": run_id, "Platform Key": doc["platform"], "Extractor": doc.get("extractor", ""),
            "Extractor Version": str(doc.get("extractorVersion", "")), "Started": now,
            "Source Files": "\n".join([str(args.file)] + list(doc.get("sourceFiles", [])))}])[0]

    status = load(api, doc, args.suffix, apply, run_id, now)
    counts = summary(doc, status)
    verb = {"created": "new", "updated": "changed", "marked": "marked missing/inactive", "unchanged": "unchanged"}
    for table, c in counts.items():
        parts = [f"{c[k]} {verb[k]}" for k in ("created", "updated", "marked", "unchanged") if c[k] or k == "unchanged"]
        print(f"  {table + args.suffix:<24} " + ", ".join(parts))
    total = {k: sum(c[k] for c in counts.values()) for k in RANK}
    changed = total["updated"] + total["marked"]
    if not apply:
        print(f"\n{total['created']} row(s) to create, {changed} to change, {total['unchanged']} unchanged "
              "(plan: nothing written).")
        return
    active = sum(1 for i in doc["tables"].get("Issues", []) if i.get("Active"))
    finished = [{"id": run_row["id"], "Finished": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                 "Created": total["created"], "Updated": total["updated"], "Unchanged": total["unchanged"],
                 "Missing": total["marked"], "Issues": active}]
    for attempt in range(1, 4):   # an update by row id: safe to repeat
        try:
            api.update_rows(runs_tid, finished)
            break
        except (httpx.TransportError, BS.ApiError) as e:
            if not BS.transient(e) or attempt == 3:
                raise
            time.sleep(5 * attempt)
    print(f"\n{total['created']} row(s) created, {changed} changed, {total['unchanged']} unchanged. "
          f"Recorded in Sync Runs{args.suffix}: {run_id}")


if __name__ == "__main__":
    main()
