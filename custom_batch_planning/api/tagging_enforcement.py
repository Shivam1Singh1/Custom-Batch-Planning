"""Mandatory Employee Function + Project tagging, from the cutover onward.

One validator, four call sites. The alternative — a bespoke check written into
each doctype — rots the moment somebody adds a fifth entry point, because the
new doctype is unguarded by default and nothing says so. Here, adding a doctype
means adding a row to DOC_DATE_FIELDS and TAGGING_FIELDS, and forgetting to do
that leaves the doctype visibly absent from a table rather than invisibly
unprotected.

Everything downstream depends on this holding. Global Main Wh, Global Free Qty
and eventually Net Req are all scoped to `posting_datetime >= cutover AND
employee_function = EF AND project = P`. That scoping is only meaningful if no
untagged row can be created after the cutover — one bypass and the pool
silently under-counts again, which is the problem the cutover exists to end.
"""

import frappe
from frappe.utils import get_datetime, getdate

from custom_batch_planning.custom_batch_planning.doctype.batch_planning_settings.batch_planning_settings import (
    get_exempt_stock_entry_purposes,
    get_stock_cutover_datetime,
)

DOC_DATE_FIELDS = {
    "Material Request": ("transaction_date", None),
    "Purchase Order": ("transaction_date", None),
    "Purchase Receipt": ("posting_date", "posting_time"),
    "Stock Entry": ("posting_date", "posting_time"),
}

TAGGING_FIELDS = {
    "Material Request": {
        "items_field": "items",
        "parent_ef": ("custom_employee_function",),
        "item_ef": ("employee_function",),
        "parent_project": ("project",),
        "item_project": ("project",),
    },
    "Purchase Order": {
        "items_field": "items",
        "parent_ef": ("employee_function", "custom_employee_functions"),
        "item_ef": ("employee_function", "custom_employee_functions"),
        "parent_project": ("project",),
        "item_project": ("project",),
    },
    "Purchase Receipt": {
        "items_field": "items",
        "parent_ef": ("employee_function",),
        "item_ef": ("employee_function",),
        "parent_project": ("project",),
        "item_project": ("project",),
    },
    "Stock Entry": {
        "items_field": "items",
        "parent_ef": ("employee_function", "custom_employee_functions"),
        "item_ef": ("employee_function",),
        "parent_project": ("project",),
        "item_project": ("project",),
    },
}


def _value(source, fieldnames):
    """First non-blank value among fieldnames, treating whitespace as blank."""
    for fieldname in fieldnames:
        value = source.get(fieldname)
        if isinstance(value, str):
            value = value.strip()
        if value:
            return value
    return None


def is_post_cutover(doc, cutover):
    """Whether this document falls under the post-cutover tagging regime.

    Date-only doctypes (MR, PO) are compared on the DATE alone, not against the
    cutover's time of day. If the cutover is declared at 09:00 on go-live day,
    a Material Request dated that day is enforced regardless of the hour it was
    raised. Comparing it at midnight instead would let every document raised on
    the morning of go-live through untagged — a whole day of leakage through
    the one gap the cutover is supposed to close. Being a few hours strict
    costs someone two fields; being lax costs the pool its integrity.

    A document with no date at all is treated as post-cutover. That only
    happens on a malformed document, and the safe reading of "I don't know when
    this is" is "enforce".
    """
    date_field, time_field = DOC_DATE_FIELDS[doc.doctype]
    raw_date = doc.get(date_field)
    if not raw_date:
        return True

    if time_field:
        doc_datetime = get_datetime(
            f"{getdate(raw_date)} {doc.get(time_field) or '00:00:00'}"
        )
        return doc_datetime >= get_datetime(cutover)

    return getdate(raw_date) >= getdate(cutover)


def is_exempt(doc):
    """Stock Entries doing general warehouse work are outside this discipline.

    See get_exempt_stock_entry_purposes for why the exemption exists and why
    Material Transfer is not in it. No other doctype has an exemption: a
    Material Request, Purchase Order or Purchase Receipt raised after the
    cutover is always procurement for somebody, so it always has an owner and
    a project.
    """
    if doc.doctype != "Stock Entry":
        return False

    purpose = doc.get("stock_entry_type") or doc.get("purpose")
    return bool(purpose) and purpose in get_exempt_stock_entry_purposes()


def enforce_ef_project_tagging(doc, cutover_dt=None):
    """Reject a post-cutover document missing Employee Function or Project.

    Silent no-op before the cutover is declared, and for documents dated before
    it. Enforcement is forward-only by design: nothing here backfills or
    rejects historical data, which stays in the legacy bucket.

    cutover_dt is injectable so tests can exercise both regimes without writing
    to the live setting; production callers leave it out and get the guarded
    accessor, which is the only read of stock_cutover_datetime that correctly
    treats Frappe's truthy datetime(1, 1, 1) sentinel as "not set".
    """
    config = TAGGING_FIELDS.get(doc.doctype)
    if not config:
        return

    cutover = cutover_dt or get_stock_cutover_datetime()
    if not cutover:
        return

    if not is_post_cutover(doc, cutover):
        return

    if is_exempt(doc):
        return

    parent_ef = _value(doc, config["parent_ef"])
    parent_project = _value(doc, config["parent_project"])
    items = doc.get(config["items_field"]) or []

    missing = []
    if not items:
        if not parent_ef:
            missing.append("Employee Function")
        if not parent_project:
            missing.append("Project")
    else:
        for item in items:
            if not (_value(item, config["item_ef"]) or parent_ef):
                missing.append(f"Row #{item.idx}: Employee Function")
            if not (_value(item, config["item_project"]) or parent_project):
                missing.append(f"Row #{item.idx}: Project")

    if not missing:
        return

    shown = missing[:10]
    overflow = len(missing) - len(shown)
    detail = "<br>".join(f"• {m}" for m in shown)
    if overflow:
        detail += f"<br>• …and {overflow} more"

    frappe.throw(
        f"<b>Employee Function and Project are mandatory on {doc.doctype} "
        f"from {getdate(cutover)} onward.</b><br><br>"
        f"Missing:<br>{detail}<br><br>"
        f"These fields are what make stock visible to batch planning. Without "
        f"them this document's stock cannot be counted in Free Qty or Net Req, "
        f"and will sit in the legacy bucket unreachable by planning.",
        title="Tagging Required",
    )


def validate_tagging(doc, method=None):
    """doc_events entry point. Thin on purpose — all logic lives above."""
    enforce_ef_project_tagging(doc)
