# Nanny pay ledger — Baserow deploy

Two scripts. `deploy.py` builds and fills the ledger and never writes to Timesheet.
`fix_timesheet.py` is the one deliberate exception: it re-dates Start/End and makes Date derived,
guarded and dry-run by default.

```sh
cd ~/baserow
cp .env.example .env          # paste your database token into BASEROW_TOKEN
uv run deploy.py check        # confirms the token works and lists what it can reach
uv run deploy.py schema       # 7 tables, ~90 fields, 4 views — needs BASEROW_EMAIL/PASSWORD for this run only
uv run deploy.py backfill     # Sep 7–21: pay slips, workdays, accounts, 3 Venmo transactions  (token)
uv run deploy.py verify       # expects $1,324.54 / $14.29 and $1,274.57 / $2.04              (token)

uv run fix_timesheet.py           # see the README section below
```

`uv` installs `httpx` and `python-dotenv` on first run; nothing else is needed.

## Which auth each command uses

| Command | Auth | Why |
|---|---|---|
| check, backfill, verify, fix_timesheet --apply | **database token** | Row and field-list endpoints — everything a token can do |
| schema, teardown, fix_timesheet --derive-date | email + password → JWT | Baserow exposes table/field creation only to a logged-in user. The generated API docs list exactly what a token can call: list fields, rows CRUD, upload, all-tables — no create-table or create-field. |

The password goes only to `api.baserow.io/api/user/token-auth/` and is never stored anywhere but your
local `.env`. Run `schema` once, then blank the two lines out; the token handles everything after.

**Token scope.** Baserow tokens are permissioned per table. If yours was created for "all tables" in the
workspace, the seven new tables are covered automatically. If it was created for specific tables, add the
new ones after `schema` runs — `check` tells you which are reachable.

## deploy.py commands

- **check** — prints the auth in use, which of the 8 tables the credentials can reach, and reads
  Timesheet's row count. No writes.
- **schema** — creates Pay Terms, Pay Slip, Workdays, Adjustments, Accounts, Transactions, Allocations in
  dependency order. The only link into Timesheet is `Workdays.Shifts`, created without a related field, so no
  column is added to Timesheet. Where a formula might not be accepted on your Baserow version, the script
  tries a fallback and reports which one it used.
- **backfill** — reads the 12 Timesheet rows, groups them by day, creates one Workday per day linked to its
  shifts, three Pay Slips (Sep 7, 14, 21), Sep 11 and 12 as overnight in San Diego, two Venmo Accounts
  (@AnilCG and @vanessa_lopez), and the three Venmo payments with their allocations.
- **verify** — prints every pay slip's hours, gross, paid and balance and checks the two closed weeks
  against the Sep 21 statement.
- **teardown --yes** — trashes the seven ledger tables (recoverable from Baserow's trash). Never Timesheet.

Every command is idempotent: run it twice and the second run reports "exists" for everything.

## fix_timesheet.py — Start/End become the single source of truth

Timesheet held the same fact three times (Date, Start, End), and that is how both errors happened: four
Dates were typed with day and month swapped, and a Start was re-dated alone in the UI. Baserow won't let
Date be deleted (it's the primary field), so instead it becomes a formula computed from Start. Two steps:

```sh
uv run fix_timesheet.py                 # dry run: prints every change, writes nothing
uv run fix_timesheet.py --apply         # step 1 (token): re-date Start/End onto each row's real day
uv run fix_timesheet.py --derive-date   # step 2 (login): Date = todate(Start in America/Los_Angeles)
```

**Step 1** moves Start and End onto the day in Date, keeping their times of day, so every Total Duration
is unchanged — the script re-reads the table and refuses to report success otherwise. Row 3, whose Start
was moved alone (2:16 PM on Sep 9 against an End still on Sep 22), is restored to the statement's
9:14 AM – 5:21 PM. Rows already consistent are left alone, so it's safe to re-run.

**Step 2** converts the primary field to a formula and refuses to run until every row passes step 1, then
verifies the derived Date equals the old Date on every row. If the instance rejects `todate()`, Date
becomes a text formula and Workdays' *Date Check* is rewritten to match.

Afterwards the only fields anyone enters are Start and End. When editing a datetime cell, type the full
value (`2026-09-09 09:14`) rather than using the picker — the picker fills the time with "now" when you
change only the date, which is what broke row 3.

## Time zones and travel

- **Hours** are always right: Start and End are instants (stored in UTC), so a shift's duration is the
  same whether it happened in Moreno Valley, New York or Hawaii.
- **The workday is Pacific, and it does not travel.** Date and Shift are derived in `America/Los_Angeles`,
  so daily overtime (after 9h) and the Mon–Sun workweek (after 45h) are counted on Pacific days. California
  requires the employer's workday and workweek to be fixed periods that start at the same time every day;
  they can't move to suit a trip, so this is the only correct choice as well as the simplest. DST is handled
  by the same rule (an overnight shift across the November fall-back is one real hour longer, and paid as such).
- **Entering a shift on a trip:** type the local wall-clock time on a device set to the local time zone.
  Baserow stores the instant. Never convert to Pacific in your head.
- **Display:** from home, a 9 AM–6 PM New York shift shows as 6 AM–3 PM Pacific — correct, but confusing on
  a statement. Set `Workdays.Time Zone` for trip days (blank = Pacific); the statement prints her local
  times from it. On a far trip (Europe, Asia) one local day can straddle two Pacific workdays; every hour
  is still paid, daily OT just splits at Pacific midnight.

## The agreement, and paystub drafts

Pay Terms now holds the agreement itself: `Agreed Terms` (the rules in Vanessa's words — $27.22/hr, 1.5× after
9h a day or 45h a week Mon–Sun Pacific, $75 per night away from home, reimbursements), plus the thresholds as
numbers so `Pay Slip.Terms Check` can flag if they ever stop matching the formulas. Overtime is explained
automatically: `Workdays.OT Note` per day, rolled up into `Pay Slip.OT Notes` per week; `Workdays.Notes` and
`Pay Slip.Notes` hold the human context. `PAYSTUB_SPEC.md` maps every line of a stub to the field it must come
from and lists the checks a draft must pass — the model formats, Baserow computes.

To add these fields to the live tables: `uv run deploy.py schema` (login), then `uv run deploy.py backfill`
(token) writes the agreement text and thresholds onto the existing Pay Terms row.

## After it runs

1. Both Venmo accounts are already filled in (@AnilCG, @vanessa_lopez).
2. Add the Venmo transaction IDs and screenshots to the three **Transactions**; until then they sit in
   the *Needs attention* view, which is the point of that view. No payment app is ever called — every
   transaction is metadata you enter.
3. Weekly: create the Pay Slip (Monday date, link Pay Terms) → one Workday per day, link its shifts → when
   you send, one Transaction with its receipt and one Allocation. Split sends are extra Transactions, each
   allocated in full. `uv run deploy.py verify` is the weekly reconciliation.
