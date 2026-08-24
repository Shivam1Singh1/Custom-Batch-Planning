import frappe
from frappe.model.document import Document
from frappe.utils import get_datetime


class BatchPlanningSettings(Document):
    pass


_MIN_PLAUSIBLE_CUTOVER_YEAR = 2000


def get_stock_cutover_datetime():
    """The go-live marker, or None if the cutover has not been declared yet.

    Deliberately a setting rather than a constant: it is a business decision
    about a specific moment in time, it may need adjusting once if go-live
    slips, and it has to be findable by someone auditing the numbers a year
    from now. A constant in code would be none of those things.

    Every caller may rely on this returning either a real datetime or None —
    never the epoch sentinel described above.
    """
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
    """Stock Entry purposes that may be saved untagged after the cutover.

    Stock Entry is not a batch-planning-only doctype. On this site the purposes
    below are general warehouse operations that are essentially never
    project-scoped — Repack and Material Issue are 100% unprojected, Material
    Consumption 98.7%, Internal Transfer 98% — so demanding a Project on them
    would halt ordinary warehouse work on go-live day rather than improve any
    planning figure.

    Material Transfer is deliberately NOT exempt: it is the only purpose the
    batch-planning flow actually uses, and an untagged Material Transfer moving
    material out of the main store is exactly what makes Global Main Wh
    overstate. Material Receipt is likewise enforced, being a way stock enters
    the pool without a Purchase Receipt.

    A setting rather than a constant for the same reason the cutover marker is:
    it encodes a business decision about which operations are in scope, and it
    must be adjustable without a deploy when that answer changes.

    Read from tabSingles directly, because "never configured" and "deliberately
    cleared" have to be told apart and get_single_value cannot do it: for an
    unset Small Text it hands back "", which is indistinguishable from an admin
    emptying the box. Defaulting on "" would make "exempt nothing" impossible to
    express; treating a fresh site's "" as an empty list would silently enforce
    every Stock Entry purpose and stop general warehouse work on go-live day.
    An absent row means nobody has decided yet, so the documented default
    applies; a present-but-empty row is a decision, and is honoured.
    """
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
