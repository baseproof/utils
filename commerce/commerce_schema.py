"""The retailer-neutral commerce schema -- the single source of truth for every table,
field, link, allowed value and view. baserow_schema.py creates it in Baserow; the loader
(later) reads the same definitions, so nothing is declared twice.

Rules that keep it retailer-neutral:
  * No table or field names a retailer. Retailer specifics live in extractor code and in
    data rows (Platforms, Payment Aliases, label -> category mappings).
  * Every key is namespaced by platform ("amazon|111-...", "target|...", "shop|allbirds.com|#1001"),
    so identifiers can never collide across retailers.
  * Select options are the canonical vocabulary. Extractors map what a site shows onto them
    through reviewed mappings; anything unmapped becomes an Issue, never a guess.
  * Orders is every purchase from any vendor; Purchase Receipts holds the receipts behind them (the
    files, their text, and the key facts as printed). Every order has a Purchase Type, decided by
    an LLM reading the receipt (yours to correct; your correction stays). Retail Purchase, Food
    Order and Demand Delivery keep their lines and Items, and every fee line (with any crossed-out
    price) in Order Charges; Hotel Stay and Travel Receipt keep the order only (totals, tip, fees,
    card, transaction id). An LLM that cannot decide leaves it empty and raises an Issue.

Field ownership (what the loader may write):
  loader           written on every load (source data)
  loader_if_empty  proposed once; never overwritten after you edit it
  user             never written by the loader (your notes, categories, reviews)
Reverse links (the other side of a link) are created by Baserow and never written directly.
"""

from __future__ import annotations

from typing import Any

import schema_kit as K
from schema_kit import (boolean, count, date, datetime, file, formula, is_one_of, link, long_text, lookup, number,
                        rollup, select, table, text, url, view)

SCHEMA_VERSION = 1
DATABASE = "Shopping"
DATABASE_ID_ENV = "BASEROW_SHOPPING_DATABASE_ID"
DATABASE_ID_DEFAULT = 566971
RESERVED_FIELD_NAMES = K.RESERVED_FIELD_NAMES

# ---------------------------------------------------------------------------------------------
# Canonical vocabularies (single_select options). Adding a value is safe: re-running
# `baserow_schema.py apply` adds missing options and never removes or renames existing ones.
# ---------------------------------------------------------------------------------------------
PLATFORM_KIND = ["marketplace", "retailer", "grocery", "pet", "delivery", "aggregator", "other"]
MERCHANT_KIND = ["platform", "marketplace_seller", "store", "restaurant", "shop", "other"]
PAYMENT_KIND = ["credit_card", "debit_card", "store_card", "gift_card", "points", "wallet",
                "financing", "cash", "other", "unknown"]
CARD_KINDS = ["credit_card", "debit_card", "store_card"]
ADDRESS_KIND = ["home", "work", "store", "restaurant", "pickup_point", "digital", "other", "unknown"]
FULFILLMENT = ["ship", "pickup", "in_store", "delivery", "digital", "subscription", "mixed", "unknown"]
PAYMENT_SOURCE = ["ledger", "order_page", "statement", "export", "manual", "none", "receipt"]
AMOUNT_SOURCE = ["ledger", "export_total", "order_page", "statement", "manual", "none", "receipt"]
RECON_STATUS = ["reconciled", "reconciled_group", "paid_by_credit", "zero_total", "order_page_only",
                "paid_outside", "payment_unavailable", "payment_unknown", "foreign_currency", "needs_source", "unpaid", "underpaid",
                "overpaid", "group_mismatch", "manual", "reviewed"]
# Settled outcomes (nothing to do): matched to the cent, taken as the source provides it,
# or decided by you. Everything else shows in Orders' "Needs review".
RECON_OK = ["reconciled", "reconciled_group", "paid_by_credit", "zero_total", "foreign_currency",
            "order_page_only", "payment_unknown", "manual", "reviewed"]
ITEM_STATUS = ["ordered", "shipped", "delivered", "returned", "refunded", "substituted", "cancelled", "unknown"]
CHARGE_CATEGORY = ["subtotal", "shipping", "discount", "tax", "tip", "delivery_fee", "service_fee",
                   "other_fee", "gift_card", "points", "credit", "refund", "total", "info", "unknown"]
PAYMENT_DIRECTION = ["charge", "refund", "credit", "hold", "release"]
PAYMENT_EVIDENCE = ["ledger", "giftcard_ledger", "order_page", "statement", "manual", "receipt"]
# The kind of code a store prints for a product: global (asin, upc, ean, plu) or the store's own
# (merchant_item: Costco item #, Target DPCI...; merchant_desc: no code, the normalized description).
ID_TYPE = ["asin", "upc", "ean", "plu", "merchant_item", "merchant_desc"]
# What kind of purchase an order is; the first three keep their lines and Items.
PURCHASE_TYPE = ["Retail Purchase", "Food Order", "Demand Delivery", "Hotel Stay", "Travel Receipt"]
ITEMIZED = ["Retail Purchase", "Food Order", "Demand Delivery"]
UNIT = ["ea", "lb", "oz", "kg", "g"]
ALIAS_STATUS = ["mapped", "needs_review"]
ISSUE_SEVERITY = ["info", "review", "error"]
ISSUE_RESOLUTION = ["open", "accepted", "resolved", "wont_fix"]


# ---------------------------------------------------------------------------------------------
# Tables, in creation order (links may point forward; they are created after every table exists)
# ---------------------------------------------------------------------------------------------
TABLES: list[dict[str, Any]] = [
    table("Platforms", "Platform Key",
          "Where you order from: amazon, target, walmart, chewy, doordash, shop. One row per platform.",
          [text("Name"), select("Kind", PLATFORM_KIND), url("Website"),
           text("Order URL Template"), text("Product URL Template"),   # e.g. https://www.amazon.com/dp/{id}
           select("Default ID Type", ID_TYPE, "loader_if_empty"),     # the code its receipts print
           long_text("Notes", "user")]),

    table("Addresses", "Address Key",
          "Ship-to and store locations, keyed by normalized street | city/postal.",
          [text("Recipient"), text("Street"), text("City Postal"), text("Country"),
           select("Kind", ADDRESS_KIND, "loader_if_empty"),
           text("Label", "user"),                                         # e.g. "Eatontown house"
           long_text("Notes", "user")]),

    table("Merchants", "Merchant Key",
          "Who actually sold or fulfilled: the platform itself, a marketplace seller, a store, "
          "a restaurant (DoorDash) or a shop (Shopify via Shop).",
          [text("Name"), select("Kind", MERCHANT_KIND),
           link("Platform", "Platforms", related="Merchants"),
           link("Location", "Addresses", related="Merchants"),
           url("Website", "loader_if_empty"), long_text("Notes", "user")]),

    table("Payment Methods", "Method Key",
          "One row per real card / wallet / gift card / points program, e.g. amex-1007.",
          [text("Label", "loader_if_empty"), select("Kind", PAYMENT_KIND, "loader_if_empty"),
           text("Network", "loader_if_empty"), text("Last4", "loader_if_empty"),
           text("Issuer", "user"), text("Nickname", "user"), boolean("Active", "user"),
           long_text("Notes", "user")],
          views=[view("Cards", filters=[("Is Card", "boolean", "1")], sort=("Order Spend", "DESC"))]),

    table("Payment Aliases", "Alias Key",
          "Every way a platform displays a payment method (\"AmazonPLCC ••••9611\", an Apple Pay "
          "device number...) mapped to the real Payment Method. Unmapped aliases wait for review.",
          [text("Display Text"), link("Platform", "Platforms", related="Payment Aliases"),
           text("Last4 Seen"),
           link("Payment Method", "Payment Methods", related="Aliases", owner="loader_if_empty"),
           select("Status", ALIAS_STATUS), number("Times Seen", 0), date("First Seen"), date("Last Seen"),
           long_text("Notes", "user")],
          views=[view("Needs mapping", filters=[("Payment Method", "empty", "")])]),

    table("Items", "Item Key",
          "The thing you buy, once, whichever store sold it and whatever code it printed; every Product "
          "(a store's code for it) links here. Key: item|<the Product Key that first named it>. Merge two "
          "Items by relinking one's Products to the other, then delete the empty one.",
          [text("Name", "loader_if_empty"), text("Brand", "loader_if_empty"),
           text("Category", "loader_if_empty"),          # seeded from the receipt's department, then yours
           text("Unit Note", "user"),                     # '325 ct', '2x4 oz'
           long_text("Possible Duplicates"),              # similar names, found by the extractor
           long_text("Notes", "user")],
          views=[view("Possible duplicates", filters=[("Possible Duplicates", "not_empty", "")])]),

    table("Products", "Product Key",
          "One row per code a platform uses for a product: amazon|<ASIN>, costco|<item #>, "
          "target|dpci:<id>, walmart|upc:<upc>, whole_foods|desc:<normalized description>, "
          "shop|<store>|<variant>, doordash|<store>|<item>. Item says what it is, across stores.",
          [link("Platform", "Platforms", related="Products"), text("Native ID"), text("Title"),
           select("ID Type", ID_TYPE),
           text("Brand", "loader_if_empty"), text("GTIN", "loader_if_empty"),   # UPC/EAN: cross-retailer match
           link("Item", "Items", related="Products", owner="loader_if_empty"),   # proposed once; merges are yours
           url("Product URL"), text("Category", "user"), long_text("Notes", "user")]),

    table("Orders", "Order Key",
          "One row per order on any platform. Money fields are the canonical breakdown; the exact "
          "lines as shown are in Order Charges.",
          [link("Platform", "Platforms", related="Orders"),
           link("Merchant", "Merchants", related="Orders"),
           select("Purchase Type", PURCHASE_TYPE, "loader_if_empty"),   # an LLM's call; yours to correct
           text("External Order ID"), date("Order Date"), datetime("Ordered At"), text("Order Status"),
           datetime("Delivered At"),                                      # e.g. DoorDash "Order Complete"
           select("Fulfillment", FULFILLMENT), link("Ship To", "Addresses", related="Orders"),
           text("Currency"),
           number("Items Subtotal"), number("Shipping"), number("Discounts"), number("Tax"), number("Tip"),
           number("Fees"), number("Credits Applied"), number("Grand Total"), number("Order Value"),
           link("Payment Method", "Payment Methods", related="Orders"),
           number("Card Amount"), select("Payment Source", PAYMENT_SOURCE), select("Amount Source", AMOUNT_SOURCE),
           select("Recon Status", RECON_STATUS), number("Recon Diff"),
           # Denormalized copies of link keys so views can group/filter on plain text.
           text("Platform Key"), text("Method Key"), text("Ship To Key"),
           url("Order URL"), text("Evidence"), datetime("Last Synced"), text("Sync Run"),
           boolean("Missing From Source"),
           boolean("Reviewed", "user"), long_text("Notes", "user")],
          views=[view("Needs review", sort=("Order Date", "DESC"),
                      filters=[("Recon Status", "single_select_not_equal", s) for s in RECON_OK]),
                 view("Recent", sort=("Order Date", "DESC"))]),

    table("Purchase Receipts", "Receipt Key",
          "One row per receipt you add -- photo, PDF, email or text; 1 to 10 files (pages) in page order -- "
          "with its key facts as printed. Order is the purchase it proves; an order can have several "
          "(an itemized check and its card slip, a hotel folio and a charge slip). Key: rcpt|<fingerprint "
          "of its first file>, so adding the same file twice changes nothing.",
          [link("Order", "Orders", related="Receipts", owner="loader_if_empty"),
           file("Files", "loader_if_empty"), long_text("Text"),       # every page's text, in page order
           date("Receipt Date"), text("Time As Printed"),
           text("Transaction ID"), text("Receipt Barcode"), text("Auth Code"), text("Card As Printed"),
           number("Total As Printed"), number("Tax Rate %", 3), number("Items Printed", 0),
           text("Read By"),                                            # the LLM that read it
           long_text("Notes", "user")],
          views=[view("Unmatched", filters=[("Order", "empty", "")]),
                 view("Recent", sort=("Receipt Date", "DESC"))]),

    table("Order Items", "Item Key",
          "One row per order line: <order key>|<line>.",
          [link("Order", "Orders", related="Items"),
           link("Product", "Products", related="Order Items"),
           link("Sold By", "Merchants", related="Items Sold"),
           number("Line", 0), text("Title As Ordered"), number("Quantity", 3), number("Unit Price"),
           number("Line Total"), number("Discount"), select("Item Status", ITEM_STATUS),
           text("Substituted For"), long_text("Options"),                 # DoorDash modifiers etc.
           boolean("Subscription"),                                       # Subscribe & Save, Autoship
           # Denormalized from the order so rollups and views work without lookups.
           date("Order Date"), text("Platform Key"), text("Method Key"), text("Ship To Key"),
           # Receipt lines, as printed: '0.94 lb @ 26.99', department, second line, tax flag...
           select("Unit", UNIT), text("Department"), text("Title Long"), text("Tax Flag"), boolean("Taxable"),
           boolean("FSA Eligible"), date("Return By"), number("Confidence", 3),   # parse confidence 0-1
           boolean("Missing From Source"),                # a re-read receipt no longer has this line
           long_text("Notes", "user")],
          views=[view("Return window", sort=("Return By", "ASC"), filters=[("Return By", "not_empty", "")]),
                 view("FSA eligible", sort=("Order Date", "DESC"), filters=[("FSA Eligible", "boolean", "1")])]),

    table("Order Charges", "Charge Key",
          "Every order-summary line exactly as the site shows it, with its canonical category.",
          [link("Order", "Orders", related="Charges"), number("Position", 0), text("Label"),
           select("Category", CHARGE_CATEGORY), number("Amount"),
           number("Original Amount")]),                                   # the crossed-out price, if shown

    table("Payments", "Payment Key",
          "Every money movement: card charges, refunds, gift card use, credits. Payments with no "
          "order (digital subscriptions, in-store) are allowed.",
          [date("Date"), link("Payment Method", "Payment Methods", related="Payments"),
           select("Direction", PAYMENT_DIRECTION), number("Amount"), number("Signed Amount"),
           link("Orders", "Orders", related="Payments"),
           link("Platform", "Platforms", related="Payments"),
           text("Descriptor"), text("Refs As Shown"), select("Source", PAYMENT_EVIDENCE), text("Evidence"),
           long_text("Notes", "user")]),

    table("Issues", "Issue Key",
          "One row per flag per order. Resolution is yours; the loader only updates Active and Last Seen.",
          [link("Order", "Orders", related="Issues"), text("Code"), select("Severity", ISSUE_SEVERITY),
           long_text("Detail"), text("Evidence"), date("First Seen", "loader_if_empty"), date("Last Seen"),
           boolean("Active"),
           select("Resolution", ISSUE_RESOLUTION, "user"), long_text("Resolution Note", "user")],
          views=[view("Open", filters=[("Active", "boolean", "1")]
                      + [("Resolution", "single_select_not_equal", s) for s in ("accepted", "resolved", "wont_fix")])]),

    table("Sync Runs", "Run ID",
          "One row per load: what ran, from which files, and what changed.",
          [text("Platform Key"), text("Extractor"), text("Extractor Version"),
           datetime("Started"), datetime("Finished"), long_text("Source Files"),
           number("Created", 0), number("Updated", 0), number("Unchanged", 0), number("Missing", 0),
           number("Issues", 0), long_text("Notes", "user")]),
]

# Fields that need the links (reverse fields) in place: counts, rollups, formulas.
DERIVED: dict[str, list[dict[str, Any]]] = {
    "Platforms": [count("Order Count", "Orders")],
    "Merchants": [count("Order Count", "Orders")],
    "Addresses": [count("Order Count", "Orders"), rollup("Order Spend", "Orders", "Card Amount", "sum")],
    "Payment Methods": [
        formula("Is Card", is_one_of("Kind", CARD_KINDS)),
        count("Order Count", "Orders"),
        rollup("Order Spend", "Orders", "Card Amount", "sum"),
        rollup("Net Charged", "Payments", "Signed Amount", "sum"),
        rollup("First Order", "Orders", "Order Date", "min", optional=True),
        rollup("Last Order", "Orders", "Order Date", "max", optional=True),
    ],
    # A purchase line (Order Items) links an order to an item: shown on the line, read through its Product.
    # Price stays on each line as printed; no price statistics are kept.
    "Order Items": [
        lookup("Item", "Product", "Item", optional=True),
    ],
    "Products": [
        count("Times Ordered", "Order Items"),
        rollup("Units", "Order Items", "Quantity", "sum"),
        rollup("Item Spend", "Order Items", "Line Total", "sum"),
        rollup("First Ordered", "Order Items", "Order Date", "min", optional=True),
        rollup("Last Ordered", "Order Items", "Order Date", "max", optional=True),
    ],
    "Items": [   # totals over its Products' totals, so after Products
        count("Codes", "Products"),
        rollup("Times Bought", "Products", "Times Ordered", "sum", optional=True),
        rollup("Units", "Products", "Units", "sum", optional=True),
        rollup("Spend", "Products", "Item Spend", "sum", optional=True),
        rollup("First Bought", "Products", "First Ordered", "min", optional=True),
        rollup("Last Bought", "Products", "Last Ordered", "max", optional=True),
    ],
    "Orders": [
        count("Item Count", "Items"),
        count("Issue Count", "Issues"),
        rollup("Charged Total", "Payments", "Signed Amount", "sum"),
    ],
}

# Views that sort on derived fields are attached to tables above; they are created last.
EXTRA_VIEWS: dict[str, list[dict[str, Any]]] = {
    "Products": [view("Most ordered", sort=("Times Ordered", "DESC"))],
    "Items": [view("Most bought", sort=("Times Bought", "DESC"))],
}


# ---------------------------------------------------------------------------------------------
# Checks (schema_kit's, over this schema's tables)
# ---------------------------------------------------------------------------------------------
def all_fields(t: dict[str, Any]) -> list[dict[str, Any]]:
    return K.all_fields(t, DERIVED)


def reverse_fields() -> dict[str, list[str]]:
    return K.reverse_fields(TABLES)


def validate() -> list[str]:
    return K.validate(TABLES, DERIVED, EXTRA_VIEWS)


def link_target(table_name: str, link_name: str) -> str | None:
    return K.link_target(TABLES, table_name, link_name)


if __name__ == "__main__":
    problems = validate()
    n_fields = sum(len(all_fields(t)) + 1 for t in TABLES) + sum(len(v) for v in reverse_fields().values())
    print(f"schema v{SCHEMA_VERSION}: {len(TABLES)} tables, {n_fields} fields (incl. keys and reverse links)")
    print("\n".join(problems) if problems else "valid")
    raise SystemExit(1 if problems else 0)
