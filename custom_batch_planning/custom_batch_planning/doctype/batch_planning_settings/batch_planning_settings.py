import frappe
from frappe.model.document import Document
from frappe.utils import get_datetime


class BatchPlanningSettings(Document):
    pass


_MIN_PLAUSIBLE_CUTOVER_YEAR = 2000


def get_stock_cutover_datetime():
    try:
        value = frappe.db.get_single_value(
            "Batch Planning Settings", "stock_cutover_datetime"
        )
    except Exception:
        return None

    if not value:
        return None

    value = get_datetime(value)
    if value is None or value.year < _MIN_PLAUSIBLE_CUTOVER_YEAR:
        return None

    return value


_DEFAULT_EXEMPT_SE_PURPOSES = (
    "Material Consumption",
    "Repack",
    "Material Issue",
    "Internal Transfer",
    "Manufacture",
    "Material Transfer for Manufacture",
)


def get_exempt_stock_entry_purposes():
    try:
        rows = frappe.db.sql(
            """
            SELECT value FROM `tabSingles`
            WHERE doctype = 'Batch Planning Settings'
              AND field = 'exempt_stock_entry_purposes'
            """
        )
    except Exception:
        return set(_DEFAULT_EXEMPT_SE_PURPOSES)

    if not rows:
        return set(_DEFAULT_EXEMPT_SE_PURPOSES)

    raw = rows[0][0] or ""
    return {line.strip() for line in raw.splitlines() if line.strip()}
