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
    for fieldname in fieldnames:
        value = source.get(fieldname)
        if isinstance(value, str):
            value = value.strip()
        if value:
            return value
    return None


def is_post_cutover(doc, cutover):
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
    if doc.doctype != "Stock Entry":
        return False

    purpose = doc.get("stock_entry_type") or doc.get("purpose")
    return bool(purpose) and purpose in get_exempt_stock_entry_purposes()


def enforce_ef_project_tagging(doc, cutover_dt=None):
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
    enforce_ef_project_tagging(doc)
