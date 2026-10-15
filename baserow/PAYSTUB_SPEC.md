# Paystub drafts — how an LLM generates them from Baserow, and why they can be trusted

## The one rule

**The model formats; Baserow computes.** Every number on a stub is read from a Baserow field through
the Baserow MCP connector and copied verbatim. The model never adds hours, multiplies rates, or decides
what is overtime. If a number is not in a field, it does not go on the stub. This is the whole reason the
ledger exists: the four errors in the Sep 18 statement were all arithmetic done outside the database.

## What the model reads (all via `list_table_rows`)

| Table | Used for |
|---|---|
| Pay Slip | the week: hours, pay lines, gross, paid, balance, status, `OT Notes`, `Agreed Terms`, `Terms Check` |
| Workdays (linked from the slip) | the Hours table: per-day total, regular, OT, per-diem night, location, time zone, `OT Note`, `Notes` |
| Timesheet (linked from each Workday) | the Work Tracker: each shift's Start / End / Total Duration, Notes |
| Pay Terms (linked from the slip) | rates and the agreement text |
| Allocations → Transactions | the "Less: Payment (date)" lines — amount, date, Venmo ID |
| Adjustments (linked from the slip) | Reimbursements and other lines |

## Line-by-line source

| Stub line | Baserow field | Notes |
|---|---|---|
| Pay period | Pay Slip → `Period Start`, `Period End` | |
| Regular Pay: rate | Pay Slip → `Rate` | |
| Regular Pay: hours | Pay Slip → `Reg Min` − `Weekly OT Min` | display as `h m`; do not recompute |
| Regular Pay: total | Pay Slip → `Regular Pay` | |
| Overtime Pay: rate | Pay Slip → `OT Rate` | |
| Overtime Pay: hours | Pay Slip → `Daily OT Min` + `Weekly OT Min` | |
| Overtime Pay: total | Pay Slip → `OT Pay` | |
| Per-diem: nights, total | Pay Slip → `Nights`, `Per-diem` | rate from Pay Terms → `Per-diem Rate` |
| Reimbursements | Pay Slip → `Reimbursements`; itemise from Adjustments where `Type` = Reimbursement | |
| Gross Pay | Pay Slip → `Gross` | |
| Less: Payment (date) | each Allocation → `Amount`; date and Venmo ID from its Transaction | one line per allocation, only `Status` = Completed |
| Balance Due | Pay Slip → `Balance` | |
| Hours table, one row per day | Workday → `Minutes`, `Reg Min`, `Daily OT Min`, `Overnight`, `Location`, `Time Zone` | days with no Workday print as "—" |
| Work Tracker rows | Timesheet → `Start`, `End`, `Total Duration`, `Notes` | show local time of the Workday's `Time Zone` (blank = Pacific); label it if not Pacific |
| Overtime explanation | Pay Slip → `OT Notes` | auto-written from the day rows; append Workday `Notes` (announced breaks, etc.) |
| "How your pay works" page | Pay Terms → `Agreed Terms` | printed verbatim; this is the agreement |
| Footer | "Generated <now> from Pay Slip '<Name>' (row <id>)" | so any stub can be traced back |

## Guardrails the model must apply before calling a draft done

1. **`Terms Check` is empty.** If it says the formulas assume 9h/45h and the terms say otherwise, stop.
2. **`Monday Check` is empty** and `Period End` = `Period Start` + 6.
3. **Re-add the printed lines** (Regular + OT + Per-diem + Reimbursements + Other) and confirm the sum equals
   the printed `Gross` to the cent; confirm `Gross` − payments = `Balance`. This checks the *transcription*,
   not the ledger.
4. **Every Workday's `Date Check` is empty** and every Transaction on the slip has `Unallocated` = 0.
5. **Every Transaction on the slip has a `Transaction ID`.** If one is blank, the draft is marked DRAFT — ID
   PENDING and is not sent.
6. Nothing on the stub that is not in a field. No estimates, no "approximately", no rounding of hours beyond
   what the field holds.

## After the draft is approved

- Attach the PDF to Pay Slip → `Statements Sent`. That field is the version history: the Sep 18 and Sep 21
  versions both belong there.
- If the stub replaced an earlier one, write what changed and why in Pay Slip → `Notes` (the way the Sep 21
  corrections page did). The model can draft that paragraph from the diff of the two PDFs' numbers, but the
  numbers again come from fields.

## Capturing overtime notes for Vanessa

Three layers, from automatic to human:

1. **Workdays → `OT Note`** (formula, automatic). Written for any day over 9h, e.g.
   `Sep 12: 10h 28m in 2 blocks = 9h regular + 1h 28m overtime`. Always consistent with the numbers because it
   is computed from the same fields.
2. **Pay Slip → `OT Notes`** (formula, automatic). Joins the day notes for the week and adds a line if the
   45-hour weekly threshold also applied. This is the sentence the stub prints under the Hours table.
3. **Workdays → `Notes`** and **Pay Slip → `Notes`** (typed). The context a formula can't know: "break
   12:45–3:00 announced beforehand", "flight delayed, day ran long". The stub prints these after the auto
   note, attributed to the day.

The rule itself — 1.5× after 9h in a day or 45h in a week, Mon–Sun, Pacific — lives once, in
Pay Terms → `Agreed Terms`, and the stub prints it on the "How your pay works" page every time. The thresholds
are also stored as numbers (`Daily OT After (h)` = 9, `Weekly OT After (h)` = 45) so the ledger can check that
what is written matches what the formulas do.

## The prompt

With the Baserow connector attached, the whole request is:

> Draft the paystub for Vanessa's week of Sep 14 following PAYSTUB_SPEC.md. Use the PDF skill for layout,
> matching the Sep 21 statement. Stop and tell me if any guardrail fails.

The per-diem rule ($75 for every night away from home with the family) is captured the same way: the
Workday's `Overnight` box is the fact, `Per-diem Rate` on Pay Terms is the rate, and the sentence explaining it
is in `Agreed Terms`.
