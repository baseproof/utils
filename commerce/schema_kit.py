"""Building blocks shared by the Baserow schemas (commerce_schema.py, planning_schema.py): field
constructors, and the checks run before any API call. A schema module defines DATABASE, TABLES,
DERIVED and EXTRA_VIEWS (and, when something was renamed, MIGRATIONS) and wraps validate /
all_fields / reverse_fields / link_target around them; baserow_schema.py builds whichever schema
it is given.

Field ownership (what a loader may write):
  loader           written on every load (source data)
  loader_if_empty  proposed once; never overwritten after you edit it
  user             never written by a loader (your notes, categories, reviews)
  derived          computed by Baserow (counts, rollups, lookups, formulas, created/modified)
Reverse links (the other side of a link) are created by Baserow and never written directly.
"""

from __future__ import annotations

import re
from typing import Any

RESERVED_FIELD_NAMES = {"id", "order"}  # Baserow, case-sensitive (fields/constants.py)
OWNERS = ("loader", "loader_if_empty", "user", "derived")
SELECT_TYPES = ("single_select", "multiple_select")


# ---------------------------------------------------------------------------------------------
# Field constructors
# ---------------------------------------------------------------------------------------------
def f(name: str, type_: str, owner: str = "loader", **params: Any) -> dict[str, Any]:
    return {"name": name, "type": type_, "owner": owner, **params}


def text(name: str, owner: str = "loader") -> dict[str, Any]: return f(name, "text", owner)
def long_text(name: str, owner: str = "loader") -> dict[str, Any]: return f(name, "long_text", owner)
def url(name: str, owner: str = "loader") -> dict[str, Any]: return f(name, "url", owner)
def boolean(name: str, owner: str = "loader") -> dict[str, Any]: return f(name, "boolean", owner)
def number(name: str, dp: int = 2, owner: str = "loader") -> dict[str, Any]: return f(name, "number", owner, dp=dp)
def date(name: str, owner: str = "loader") -> dict[str, Any]: return f(name, "date", owner)
def datetime(name: str, owner: str = "loader") -> dict[str, Any]: return f(name, "datetime", owner)
def file(name: str, owner: str = "loader") -> dict[str, Any]: return f(name, "file", owner)   # any number of files
def created_on(name: str) -> dict[str, Any]: return f(name, "created_on", "derived")
def last_modified(name: str) -> dict[str, Any]: return f(name, "last_modified", "derived")


def select(name: str, options: list[str], owner: str = "loader") -> dict[str, Any]:
    return f(name, "single_select", owner, options=list(options))


def multi_select(name: str, options: list[str], owner: str = "loader") -> dict[str, Any]:
    return f(name, "multiple_select", owner, options=list(options))


def link(name: str, to: str, related: str | None, owner: str = "loader") -> dict[str, Any]:
    """Link to another table; `related` names the reverse field Baserow adds to `to`. None: no
    reverse field (a table linking to itself, e.g. a plan that is part of another plan)."""
    return f(name, "link_row", owner, to=to, related=related)


def count(name: str, through: str) -> dict[str, Any]:
    return f(name, "count", "derived", through=through)


def rollup(name: str, through: str, target: str, fn: str, optional: bool = False) -> dict[str, Any]:
    return f(name, "rollup", "derived", through=through, target=target, fn=fn, optional=optional)


def lookup(name: str, through: str, target: str, optional: bool = False) -> dict[str, Any]:
    return f(name, "lookup", "derived", through=through, target=target, optional=optional)


def formula(name: str, expr: str, optional: bool = False, dp: int | None = None) -> dict[str, Any]:
    """dp: decimal places to show when the formula is a number."""
    return f(name, "formula", "derived", formula=expr, optional=optional, dp=dp)


def is_one_of(field: str, values: list[str]) -> str:
    """Baserow formula: totext(field) equals any of values (or() takes two arguments)."""
    terms = [f"totext(field('{field}')) = '{v}'" for v in values]
    expr = terms[0]
    for t in terms[1:]:
        expr = f"or({expr}, {t})"
    return expr


def table(name: str, key: str, doc: str, fields: list[dict[str, Any]],
          views: list[dict[str, Any]] | None = None, key_formula: str | None = None) -> dict[str, Any]:
    """key_formula: make the key (primary) field a formula over the table's own fields, e.g.
    concat(totext(field('Group')), ' › ', field('Name'))."""
    return {"name": name, "key": key, "doc": doc, "fields": fields, "views": views or [], "key_formula": key_formula}


def view(name: str, filters: list[tuple[str, str, str]] = (), filter_type: str = "AND",  # type: ignore[assignment]
         sort: tuple[str, str] | None = None) -> dict[str, Any]:
    """filters: (field, Baserow filter type, value). For single_select filters the value is
    the option's text; baserow_schema.py resolves it to the option id."""
    return {"name": name, "filters": list(filters), "filter_type": filter_type, "sort": sort}


# ---------------------------------------------------------------------------------------------
# Migrations -- the changes baserow_schema.py never infers (it only adds): declared in a schema's
# MIGRATIONS and applied in place, idempotently, before everything else.
# ---------------------------------------------------------------------------------------------
def rename_field(table: str, old: str, new: str) -> dict[str, Any]:
    """Rename a field, the key field included. In place: its id, values, view filters and formula
    references are kept."""
    return {"kind": "rename_field", "table": table, "old": old, "new": new}


def set_options(table: str, field: str, renames: dict[str, str] | None = None) -> dict[str, Any]:
    """Make a select field's options exactly the schema's list, in its order. renames (old value
    -> new value) keep the option's id, so rows keep it; an option the schema no longer lists is
    dropped only when no row uses it."""
    return {"kind": "set_options", "table": table, "field": field, "renames": dict(renames or {})}


# ---------------------------------------------------------------------------------------------
# Checks -- run before any API call, and by the tests.
# ---------------------------------------------------------------------------------------------
def formula_refs(expr: str) -> list[str]:
    return re.findall(r"field\('([^']+)'\)", expr)


def all_fields(t: dict[str, Any], derived: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = t["fields"]
    return fields + derived.get(t["name"], [])


def reverse_fields(tables: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {t["name"]: [] for t in tables}
    for t in tables:
        for fd in t["fields"]:
            if fd["type"] == "link_row" and fd["related"]:
                out[fd["to"]].append(fd["related"])
    return out


def link_target(tables: list[dict[str, Any]], table_name: str, link_name: str) -> str | None:
    """The table a link (own or reverse) on `table_name` points to."""
    for t in tables:
        for fd in t["fields"]:
            if fd["type"] != "link_row":
                continue
            if t["name"] == table_name and fd["name"] == link_name:
                return str(fd["to"])
            if fd["to"] == table_name and fd["related"] == link_name:
                return str(t["name"])
    return None


def validate_migrations(tables: list[dict[str, Any]], migrations: list[dict[str, Any]]) -> list[str]:
    """A migration must lead to the schema: renamed fields and options end up under names the
    schema uses, and nothing the schema still lists is renamed away."""
    errors: list[str] = []
    by_name = {t["name"]: t for t in tables}
    for m in migrations:
        t = by_name.get(m["table"])
        if t is None:
            errors.append(f"migration {m['kind']}: unknown table {m['table']!r}")
            continue
        own = [t["key"]] + [fd["name"] for fd in t["fields"]]
        if m["kind"] == "rename_field":
            if m["new"] not in own:
                errors.append(f"migration: {m['table']}.{m['old']} -> {m['new']!r}, which is not in the schema")
            if m["old"] in own:
                errors.append(f"migration: {m['table']}.{m['old']!r} is renamed but still in the schema")
        elif m["kind"] == "set_options":
            fd = next((x for x in t["fields"] if x["name"] == m["field"]), None)
            if fd is None or fd["type"] not in SELECT_TYPES:
                errors.append(f"migration: {m['table']}.{m['field']!r} is not a select field of the schema")
                continue
            for old, new in m["renames"].items():
                if new not in fd["options"] or old in fd["options"]:
                    errors.append(f"migration: {m['table']}.{m['field']} renames {old!r} -> {new!r}; the schema's "
                                  "options must list the new value and not the old one")
        else:
            errors.append(f"migration: unknown kind {m['kind']!r}")
    return errors


def validate(tables: list[dict[str, Any]], derived: dict[str, list[dict[str, Any]]],
             extra_views: dict[str, list[dict[str, Any]]],
             migrations: list[dict[str, Any]] | None = None) -> list[str]:
    errors = validate_migrations(tables, migrations or [])
    names = [t["name"] for t in tables]
    if len(set(names)) != len(names):
        errors.append(f"duplicate table names: {names}")
    rev = reverse_fields(tables)
    by_name = {t["name"]: t for t in tables}
    for t in tables:
        fnames = [t["key"]] + [fd["name"] for fd in all_fields(t, derived)] + rev[t["name"]]
        dupes = sorted({n for n in fnames if fnames.count(n) > 1})
        if dupes:
            errors.append(f"{t['name']}: duplicate field names {dupes} (own fields and reverse links share one namespace)")
        for n in fnames:
            if n in RESERVED_FIELD_NAMES:
                errors.append(f"{t['name']}.{n}: reserved by Baserow")
        if t.get("key_formula"):
            own = {fd["name"] for fd in t["fields"]}
            for ref in formula_refs(t["key_formula"]):
                if ref not in own:
                    errors.append(f"{t['name']}.{t['key']}: key formula uses {ref!r}, not a field of the table")
        for fd in all_fields(t, derived):
            if fd["owner"] not in OWNERS:
                errors.append(f"{t['name']}.{fd['name']}: unknown owner {fd['owner']!r}")
            if fd["type"] == "link_row" and fd["to"] not in by_name:
                errors.append(f"{t['name']}.{fd['name']}: links to unknown table {fd['to']!r}")
            if fd["type"] in ("count", "rollup", "lookup"):
                if fd["through"] not in rev[t["name"]] and not any(
                        x["name"] == fd["through"] and x["type"] == "link_row" for x in t["fields"]):
                    errors.append(f"{t['name']}.{fd['name']}: through {fd['through']!r} is not a link on this table")
            if fd["type"] in ("rollup", "lookup"):
                target_table = link_target(tables, t["name"], fd["through"])
                if target_table:
                    target = by_name[target_table]
                    if fd["target"] not in [target["key"]] + [x["name"] for x in all_fields(target, derived)]:
                        errors.append(f"{t['name']}.{fd['name']}: {target_table} has no field {fd['target']!r}")
            if fd["type"] == "formula":
                for ref in formula_refs(fd["formula"]):
                    if ref not in fnames:
                        errors.append(f"{t['name']}.{fd['name']}: formula uses unknown field {ref!r}")
            if fd["type"] in SELECT_TYPES and len(set(fd["options"])) != len(fd["options"]):
                errors.append(f"{t['name']}.{fd['name']}: duplicate options")
        for v in t["views"] + extra_views.get(t["name"], []):
            for fname, _, _ in v["filters"]:
                if fname not in fnames:
                    errors.append(f"{t['name']} view {v['name']!r}: filter on unknown field {fname!r}")
            if v["sort"] and v["sort"][0] not in fnames:
                errors.append(f"{t['name']} view {v['name']!r}: sort on unknown field {v['sort'][0]!r}")
    return errors
