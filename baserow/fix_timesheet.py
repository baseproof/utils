#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "python-dotenv>=1.0"]
# ///
"""
fix_timesheet.py — make Start/End the single source of truth in Timesheet.

    uv run fix_timesheet.py                 dry run: show every change, write nothing
    uv run fix_timesheet.py --apply         step 1: re-date Start/End onto each row's real day   (token)
    uv run fix_timesheet.py --derive-date   step 2: turn Date into a formula computed from Start (login)

Step 1, per row: Start and End move onto the day in Date, keeping their times of day, so Total
Duration is provably unchanged. Row 3 (whose Start was moved alone in the UI) is restored to the
statement's 9:14 AM – 5:21 PM. A row that is already consistent is left alone.

Step 2: a row's key is (Start, End) — a day can have several shifts — so the primary label becomes
    Shift = "2026-09-12 08:32 – 12:45"   (formula from Start and End, unique per shift)
and Date is demoted to a derived, read-only formula (the day of Start in America/Los_Angeles) so the
Workdays mismatch check and the rows API keep working. From then on the only things anyone enters
are Start and End. Step 2 refuses to run until every row passes step 1, and verifies afterwards that
the derived Date equals the Date you had on every row.

Both steps re-read the table afterwards and refuse to report success if anything else changed.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = os.environ.get("BASEROW_URL", "https://api.baserow.io").rstrip("/")
TIMESHEET_ID = int(os.environ.get("BASEROW_TIMESHEET_TABLE_ID", "1215333"))
WORKDAYS_ID = int(os.environ.get("BASEROW_WORKDAYS_TABLE_ID", "1215498"))
LA = ZoneInfo("America/Los_Angeles")

# Rows to restore outright, with the values from the Sep 21 statement. Row 3's Start was changed
# alone in the UI (to 2:16 PM on Sep 9) while End stayed on Sep 22, so a plain re-date can't fix it.
RESTORE = {3: {"Start": "2026-09-09T16:14:00Z", "End": "2026-09-10T00:21:00Z", "seconds": 29220}}

SHIFT_FORMULA = ("concat(datetime_format_tz(field('Start'), 'YYYY-MM-DD HH24:MI', 'America/Los_Angeles'), "
                 "' – ', datetime_format_tz(field('End'), 'HH24:MI', 'America/Los_Angeles'))")
DATE_FORMULA = "todate(datetime_format_tz(field('Start'), 'YYYYMMDD', 'America/Los_Angeles'), 'YYYYMMDD')"
# Fallback if the instance rejects todate(): Date becomes text, and Workdays.Date Check is rewritten to match.
DATE_FORMULA_TEXT = "datetime_format_tz(field('Start'), 'YYYY-MM-DD', 'America/Los_Angeles')"
DATE_CHECK_TEXT = ("if(count(filter(lookup('Shifts', 'Date'), lookup('Shifts', 'Date') != "
                   "datetime_format(field('Date'), 'YYYY-MM-DD'))) > 0, 'DATE MISMATCH', '')")

CHECK_FIELDS = ("Notes", "Travel", "Caretaker", "Source")   # must never change


def parse(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local(s: str) -> dt.datetime:
    return parse(s).astimezone(LA)


def fmt(s: str) -> str:
    return local(s).strftime("%m-%d %I:%M %p")


def hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 86400}d {s % 86400 // 3600}h {s % 3600 // 60:02d}m {s % 60:02d}s" if s >= 86400 \
        else f"{s // 3600}h {s % 3600 // 60:02d}m" + (f" {s % 60:02d}s" if s % 60 else "")


class Api:
    def __init__(self, jwt: bool) -> None:
        self.c = httpx.Client(base_url=BASE, timeout=60)
        if jwt:
            email, pw = os.environ.get("BASEROW_EMAIL"), os.environ.get("BASEROW_PASSWORD")
            if not (email and pw):
                sys.exit("--derive-date changes a field's type, which Baserow only allows with a login.\n"
                         "Add BASEROW_EMAIL and BASEROW_PASSWORD to .env for this run.")
            r = self.c.post("/api/user/token-auth/", json={"email": email, "password": pw})
            if r.status_code >= 400:
                sys.exit(f"Login failed: {r.text}")
            j = r.json()
            self.c.headers["Authorization"] = f"JWT {j.get('access_token') or j.get('token')}"
        else:
            tok = os.environ.get("BASEROW_TOKEN")
            if not tok:
                sys.exit("BASEROW_TOKEN missing from .env.")
            self.c.headers["Authorization"] = f"Token {tok}"

    def req(self, method: str, path: str, **kw):
        r = self.c.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} {method} {path}: {r.text}")
        return r.json() if r.content else None

    def rows(self, table: int) -> dict[int, dict]:
        j = self.req("GET", f"/api/database/rows/table/{table}/", params={"user_field_names": "true", "size": 200})
        return {r["id"]: r for r in j["results"]}


# ----------------------------------------------------------------------------------------------
# step 1 — re-date Start/End
# ----------------------------------------------------------------------------------------------


def plan_redate(rows: dict[int, dict]) -> tuple[list[dict], list[str]]:
    """Return (patches, report lines). A patch carries the expected duration for post-verification."""
    patches, lines = [], []
    for rid, r in sorted(rows.items()):
        day = dt.date.fromisoformat(str(r["Date"])[:10])
        start, end, dur = r["Start"], r["End"], r["Total Duration"]
        if rid in RESTORE:
            want = RESTORE[rid]
            if start == want["Start"] and end == want["End"]:
                lines.append(f"{rid:>3}  {day}  {fmt(start)} – {fmt(end)}  {hms(dur):>12}  already restored")
            else:
                lines.append(f"{rid:>3}  {day}  {fmt(start)} – {fmt(end)}  {hms(dur):>12}  → RESTORE "
                             f"{fmt(want['Start'])} – {fmt(want['End'])} = {hms(want['seconds'])} (statement)")
                patches.append({"id": rid, "Start": want["Start"], "End": want["End"], "_expect": want["seconds"]})
            continue
        s_loc, e_loc = local(start), local(end)
        if s_loc.date() == day:
            lines.append(f"{rid:>3}  {day}  {fmt(start)} – {fmt(end)}  {hms(dur):>12}  ok")
            continue
        delta = day - s_loc.date()
        new_s = s_loc + delta   # aware datetimes: wall-clock shift by whole days
        new_e = e_loc + delta
        new_dur = (new_e - new_s).total_seconds()
        if new_dur != dur:
            lines.append(f"{rid:>3}  {day}  {fmt(start)} – {fmt(end)}  {hms(dur):>12}  SKIP — shift would change "
                         f"duration to {hms(new_dur)} (DST edge); fix by hand")
            continue
        lines.append(f"{rid:>3}  {day}  {fmt(start)} – {fmt(end)}  {hms(dur):>12}  → {fmt(iso(new_s))} – "
                     f"{fmt(iso(new_e))}  ({delta.days:+d} days, duration unchanged)")
        patches.append({"id": rid, "Start": iso(new_s), "End": iso(new_e), "_expect": dur})
    return patches, lines


def step_redate(apply: bool) -> None:
    api = Api(jwt=False)
    before = api.rows(TIMESHEET_ID)
    patches, lines = plan_redate(before)
    print(f"Timesheet: {len(before)} rows\n")
    print(f"{'row':>3}  {'Date':<10}  {'Start – End (now, PDT)':<34}{'Duration':>12}  action")
    print("\n".join(lines))

    if not patches:
        print("\nAll rows consistent — Start/End already on the right days. Ready for --derive-date.")
        return
    if not apply:
        print(f"\nDry run: {len(patches)} row(s) would change. Re-run with --apply to write.")
        return

    api.req("PATCH", f"/api/database/rows/table/{TIMESHEET_ID}/batch/", params={"user_field_names": "true"},
            json={"items": [{k: v for k, v in p.items() if not k.startswith("_")} for p in patches]})
    print(f"\nWrote {len(patches)} row(s). Verifying…")

    after = api.rows(TIMESHEET_ID)
    problems = []
    for p in patches:
        got = after[p["id"]]["Total Duration"]
        if got != p["_expect"]:
            problems.append(f"row {p['id']}: duration {hms(got)} ≠ expected {hms(p['_expect'])}")
    for rid in before:
        for k in CHECK_FIELDS + ("Date",):
            if before[rid].get(k) != after[rid].get(k):
                problems.append(f"row {rid}: {k} changed")
        if rid not in {p["id"] for p in patches} and (before[rid]["Start"], before[rid]["End"]) != (after[rid]["Start"], after[rid]["End"]):
            problems.append(f"row {rid}: Start/End changed but was not in the plan")
    if problems:
        sys.exit("VERIFICATION FAILED:\n  " + "\n  ".join(problems))
    print("Verified: every changed row has exactly the expected duration; Date, Notes, Travel, Caretaker, Source untouched.")
    _workdays_check(api)


# ----------------------------------------------------------------------------------------------
# step 2 — derive Date from Start
# ----------------------------------------------------------------------------------------------


def step_derive() -> None:
    api = Api(jwt=True)
    rows = api.rows(TIMESHEET_ID)
    bad = [rid for rid, r in rows.items() if local(r["Start"]).date().isoformat() != str(r["Date"])[:10]]
    if bad:
        sys.exit(f"Rows {bad} have a Start whose day differs from Date. Run `--apply` first; refusing to derive.")
    expected = {rid: str(r["Date"])[:10] for rid, r in rows.items()}

    def fields() -> list[dict]:
        return api.req("GET", f"/api/database/fields/table/{TIMESHEET_ID}/")

    def by_name(name: str) -> dict | None:
        return next((f for f in fields() if f["name"] == name), None)

    # 1. Shift label field
    shift = by_name("Shift")
    if shift is None:
        shift = api.req("POST", f"/api/database/fields/table/{TIMESHEET_ID}/",
                        json={"name": "Shift", "type": "formula", "formula": SHIFT_FORMULA})
        print("Created Shift:", SHIFT_FORMULA)
    else:
        print("Shift exists.")

    # 2. Make Shift the primary field
    primary = next(f for f in fields() if f["primary"])
    if primary["id"] != shift["id"]:
        try:
            api.req("POST", f"/api/database/fields/table/{TIMESHEET_ID}/change-primary-field/",
                    json={"new_primary_field_id": shift["id"]})
            print("Shift is now the primary field.")
        except RuntimeError as e:
            sys.exit("This Baserow version can't change the primary field via API "
                     f"({str(e)[:100]}…).\nIn the UI: Timesheet → Shift column ▾ → 'Set as primary field', then re-run.")
    else:
        print("Shift is already primary.")

    # 3. Date becomes derived
    date_f = by_name("Date")
    if date_f is None:
        sys.exit("No field named 'Date' found — stopping rather than guessing.")
    if date_f["type"] == "formula":
        print("Date is already a formula:", date_f["formula"])
    else:
        try:
            api.req("PATCH", f"/api/database/fields/{date_f['id']}/", json={"type": "formula", "formula": DATE_FORMULA})
            print("Date → formula (date type):", DATE_FORMULA)
        except RuntimeError as e:
            print(f"todate() form rejected ({str(e)[:100]}…); using text form.")
            api.req("PATCH", f"/api/database/fields/{date_f['id']}/", json={"type": "formula", "formula": DATE_FORMULA_TEXT})
            dc = next((f for f in api.req("GET", f"/api/database/fields/table/{WORKDAYS_ID}/") if f["name"] == "Date Check"), None)
            if dc:
                api.req("PATCH", f"/api/database/fields/{dc['id']}/", json={"formula": DATE_CHECK_TEXT})
                print("Workdays.Date Check rewritten for text dates.")

    # 4. Verify
    after = api.rows(TIMESHEET_ID)
    drift = {rid: (expected[rid], str(after[rid]["Date"])[:10]) for rid in expected if str(after[rid]["Date"])[:10] != expected[rid]}
    if drift:
        sys.exit(f"VERIFICATION FAILED — derived Date differs from the previous value: {drift}")
    print(f"Verified: derived Date equals the previous Date on all {len(after)} rows.")
    labels = [r["Shift"] for r in after.values()]
    dupes = sorted({l for l in labels if labels.count(l) > 1})
    print("Shift labels unique." if not dupes else f"DUPLICATE shifts (same Start and End): {dupes}")
    print("Sample labels:", ", ".join(sorted(labels)[:3]), "…")
    _workdays_check(api)
    print("\nDone. From now on enter only Start and End; Shift and Date fill themselves.")


def _workdays_check(api: Api) -> None:
    flagged = [w["Name"] for w in api.rows(WORKDAYS_ID).values() if w.get("Date Check")]
    print("Workdays: " + (f"DATE MISMATCH on {', '.join(flagged)}" if flagged else "Date Check clear on every row."))


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if "--derive-date" in args:
        step_derive()
    else:
        step_redate(apply="--apply" in args)
