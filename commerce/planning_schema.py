"""The Financial Planning schema: what you intend to buy (goods or services), anchored to the
date the money leaves. Small on purpose, so it fits in one prompt and an LLM working over MCP
never has to guess what a field means. baserow_schema.py --schema planning creates it.

Plans rules
  * Key fields: Item, Description, Who's it for, Price, Tax, Fees, Amount, Notes.
  * Item is the short name; Description says in a line what exactly it is (model, size,
    configuration; for a service, what it covers). Notes hold every other detail, with the source
    (quote, bag, product page) captured verbatim, not paraphrased.
  * Price is what the item or service costs before tax. Tax and Fees are entered on their own
    (Fees = shipping, delivery, recycling fees and other charges on the invoice, e.g. AppleCare+
    bought with a device); blank means none or not known yet. Amount = Price + Tax + Fees is
    computed, so Amount is always the total. Never enter per-month figures; Months spreads it.
  * One purchase = one row. Parts that together make one thing (a playpen with its barrier, mats
    and wall mount) are one purchase: one row, Price = all the parts, every part named in
    Description and detailed in Notes. A checkout with several separate items (e.g. devices for
    different people): one row per item and no umbrella row; split the checkout's tax and fees
    across the items (tax in proportion to price, a fee to the item it belongs to) so the rows add
    up to the invoice, and put the whole checkout in each row's Notes.
  * Monthly = Amount / Months. Ends = the date of the last installment.
  * Paid over time (a loan, 0% installments, a service): Target Date = first payment, Months =
    payments planned (12 = a year of an ongoing service), Price = all of them together.
  * An umbrella row groups spending spread over several dates (a Japan trip: flights, lodging,
    ...): Price blank, Target Date = its first outflow; its children point at it through Part Of
    and carry the money. Never for a single purchase or checkout.
  * Every write sets Item, Description, Target Date, Price, Category, Who's it for, Purpose,
    Status (Price may be blank on an umbrella row). Rescheduling = changing Target Date; there is
    no "deferred" status.
  * Bought (for a service: signed up) needs Bought Date and Actual Amount. Variance = Actual
    Amount - Amount, computed when asked, never stored. Order Key is the Shopping database's
    key for the order, e.g. amazon|114-1881572-4059413.
  * A range question ("what's coming in Q1?"): a row is in the range when Target Date <= range
    end and Ends >= range start; it counts Monthly for each month inside the range.
  * Who's it for: Home for shared things, Other for anyone not listed. Purpose is the why, never who.
  * Tags are cross-cutting facets only (big-ticket, financed, ...), lowercase-hyphenated. Needing
    a tag to say who, why or what means a missing option or category instead.
  * Categories are two levels, Group › Name. A third level goes in the Name as "Sub: Name"
    (Insurance › Property: Auto, Home › Services: Lawn), so each thing has exactly one home.
  * Categories are rows: add one when nothing fits. New select options (a tag, a person, a
    Group) are added here and applied with `baserow_schema.py --schema planning apply`, because
    the MCP writes rows, not options. To rename a field or an option, rename it here and declare
    it in MIGRATIONS, so Baserow renames it in place and no value is lost.
"""

from __future__ import annotations

from typing import Any

import schema_kit as K
from schema_kit import (created_on, date, formula, last_modified, link, long_text, lookup, multi_select, number,
                        select, table, text, url, view)

DATABASE = "Financial Planning"

GROUPS = ["Baby", "Home", "Kitchen", "Health", "Tech", "Transport", "Travel", "Work", "Insurance"]
PEOPLE = ["Home", "Anil", "Nandana", "Vedanth", "Buddy", "Clyde", "Pragna", "Rishi", "Vimala", "Krishna Das", "Other"]
PURPOSE = ["Health", "Safety", "Convenience", "Enjoyment", "Learning", "Work", "Obligation"]
PRIORITY = ["Must", "Should", "Nice"]
STATUS = ["Idea", "Planned", "Bought", "Dropped"]
TAGS = ["big-ticket", "needs-research", "wait-for-sale", "replaces-existing", "gift", "black-friday", "financed"]

# Seed rows for Categories (Group -> Names). Small on purpose; add rows when nothing fits.
SEED_CATEGORIES: dict[str, list[str]] = {
    "Baby": ["Feeding", "Sleep", "Play", "Gear", "Safety"],
    "Home": ["Furniture", "Appliances", "Cleaning", "Air & water",
             "Services: Cleaning", "Services: Pet Related", "Services: Pest", "Services: Lawn",
             "Services: Handy/Home Related", "Services: Moving", "Services: Other"],
    "Kitchen": ["Appliances", "Cookware"],
    "Health": ["Wearables", "Sleep", "Fitness"],
    "Tech": ["Computers", "Tablets & phones", "Accessories"],
    "Transport": ["Vehicle", "Vehicle add-ons", "Registration & fees"],   # auto insurance: Insurance › Property: Auto
    "Travel": ["Flights", "Lodging", "Ground", "Activities"],
    "Work": ["Equipment", "Software"],
    "Insurance": ["Property: Auto", "Property: Life", "Property: Disability", "Property: Rental", "Property: Home",
                  "Property: Home/Umbrella", "Property: Other",
                  "Wellness: Pet", "Wellness: Health", "Wellness: Dental", "Wellness: Vision", "Wellness: Other",
                  "Professional: Medical Liability", "Professional: Legal", "Professional: Other",
                  "Family Safety: Death", "Family Safety: Disability", "Family Safety: Life", "Family Safety: Travel",
                  "Family Safety: Other"],
}

TABLES: list[dict[str, Any]] = [
    table("Categories", "Category",
          "Two levels, YNAB style: Group › Name (e.g. Health › Sleep). Add a row when nothing fits.",
          [select("Group", GROUPS), text("Name")],
          key_formula="concat(totext(field('Group')), ' › ', field('Name'))"),

    table("Plans", "Item",
          "One row = one intended spend (goods or a service), anchored to Target Date. See the rules above.",
          [text("Description"), multi_select("Who's it for", PEOPLE),
           number("Price", 2), number("Tax", 2), number("Fees", 2), long_text("Notes"),
           date("Target Date"), number("Months", 0),
           link("Category", "Categories", related="Plans"),
           select("Purpose", PURPOSE), select("Priority", PRIORITY),
           select("Status", STATUS), multi_select("Tags", TAGS),
           link("Part Of", "Plans", related=None),
           text("Vendor"), url("URL"),
           date("Bought Date"), number("Actual Amount", 2), text("Order Key"),
           created_on("Created"), last_modified("Modified")],
          views=[view("Upcoming", sort=("Target Date", "ASC"),
                      filters=[("Status", "single_select_not_equal", "Bought"),
                               ("Status", "single_select_not_equal", "Dropped")]),
                 view("Bought", sort=("Bought Date", "DESC"),
                      filters=[("Status", "single_select_equal", "Bought")])]),
]

DERIVED: dict[str, list[dict[str, Any]]] = {
    "Plans": [   # in creation order: Monthly uses Amount
        formula("Amount", "field('Price') + when_empty(field('Tax'), 0) + when_empty(field('Fees'), 0)", dp=2),
        formula("Monthly", "if(field('Months') > 1, field('Amount') / field('Months'), field('Amount'))", dp=2),
        formula("Month", "datetime_format(field('Target Date'), 'YYYY-MM')"),
        formula("Ends", "if(field('Months') > 1, field('Target Date') + "
                        "date_interval(concat(totext(field('Months') - 1), ' months')), field('Target Date'))"),
        lookup("Group", "Category", "Group"),
    ],
}

EXTRA_VIEWS: dict[str, list[dict[str, Any]]] = {}

# Renames made in place before anything else (rows, option ids and view filters are kept).
# 2026-09-23: the key fields named the way you name them; Household is now Home.
MIGRATIONS: list[dict[str, Any]] = [
    K.rename_field("Plans", "Name", "Item"),
    K.rename_field("Plans", "For", "Who's it for"),
    K.set_options("Plans", "Who's it for", renames={"Household": "Home"}),
]


def all_fields(t: dict[str, Any]) -> list[dict[str, Any]]:
    return K.all_fields(t, DERIVED)


def reverse_fields() -> dict[str, list[str]]:
    return K.reverse_fields(TABLES)


def validate() -> list[str]:
    errors = K.validate(TABLES, DERIVED, EXTRA_VIEWS, MIGRATIONS)
    errors += [f"SEED_CATEGORIES: unknown Group {g!r}" for g in SEED_CATEGORIES if g not in GROUPS]
    errors += [f"SEED_CATEGORIES: {g} lists {n!r} twice" for g, names in SEED_CATEGORIES.items()
               for n in set(names) if names.count(n) > 1]
    return errors


def link_target(table_name: str, link_name: str) -> str | None:
    return K.link_target(TABLES, table_name, link_name)


if __name__ == "__main__":
    problems = validate()
    print(f"{DATABASE}: {len(TABLES)} tables, {sum(len(v) for v in SEED_CATEGORIES.values())} seed categories")
    print("\n".join(problems) if problems else "valid")
    raise SystemExit(1 if problems else 0)
