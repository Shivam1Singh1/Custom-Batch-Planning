import frappe

MONTHS_ORDER = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December", "Unknown"
]

@frappe.whitelist()
def get_monthly_reconciliation(year=None, month=None, employee_function=None):
    data = frappe.request.get_json() or {}

    year             = year             or data.get("year")             or "2026"
    month            = month            or data.get("month")            or ""
    employee_function = employee_function or data.get("employee_function") or ""

    month_filter = f"AND bp.month = %(month)s"         if month             else ""
    ef_filter    = f"AND bp.employee_function = %(ef)s" if employee_function else ""

    result = frappe.db.sql(f"""
        SELECT
            bp.month,
            COUNT(DISTINCT bp.batch_planning_id)                                   AS bp_count,
            COUNT(DISTINCT mr.name)                                                AS mr_count,
            COUNT(DISTINCT po.name)                                                AS po_count,
            COUNT(DISTINCT pr.name)                                                AS grn_count,
            COALESCE(SUM(pri.qty), 0)                                              AS grn_qty,
            COUNT(DISTINCT pi.name)                                                AS pi_count,
            COUNT(DISTINCT ma.name)                                                AS ma_count,
            COUNT(DISTINCT CASE
                WHEN po.docstatus = 1 AND po.per_received < 100 THEN po.name
            END)                                                                   AS po_pending,
            COUNT(DISTINCT se.name)                                                AS issue_count,
            COALESCE(SUM(sed.qty), 0)                                              AS issued_qty,
            COALESCE(SUM(sed.basic_amount), 0)                                     AS issued_value
        FROM `tabBatches Planned` bp
        LEFT JOIN `tabMaterial Request`    mr  ON mr.custom_batch_planning_no = bp.batch_planning
                                               AND mr.docstatus IN (0, 1)
        LEFT JOIN `tabPurchase Order`      po  ON po.custom_batch_planning_no = bp.batch_planning
        LEFT JOIN `tabPurchase Receipt`    pr  ON pr.custom_batch_planning_no = bp.batch_planning
                                               AND pr.docstatus = 1
        LEFT JOIN `tabPurchase Receipt Item` pri ON pri.parent = pr.name
        LEFT JOIN `tabPurchase Invoice`    pi  ON pi.custom_batch_planning_no = bp.batch_planning
                                               AND pi.docstatus IN (0, 1)
        -- Joined on the Batch Planning link, not on the old comma-joined
        -- "Planned Batch References" text field. That field held a list like
        -- "SO-...-PD-07, SO-...-PD-06, ...", so `= bp.name` could never match a
        -- single Batches Planned and ma_count was silently 0 for every row.
        -- A Material Allocation is raised per Batch Planning anyway, covering
        -- all of its batches, so this is the link that was always meant.
        LEFT JOIN `tabMaterial Allocation` ma  ON ma.batch_planning           = bp.batch_planning
                                               AND ma.docstatus = 1
        LEFT JOIN `tabStock Entry`         se  ON se.custom_batch_planning_no = bp.batch_planning
                                               AND se.stock_entry_type = 'Material Issue'
                                               AND se.docstatus = 1
        LEFT JOIN `tabStock Entry Detail`  sed ON sed.parent = se.name
        WHERE YEAR(bp.slot_booking_date) = %(year)s
        {month_filter}
        {ef_filter}
        GROUP BY bp.month
    """, {
        "year":  year,
        "month": month,
        "ef":    employee_function,
    }, as_dict=True)

    for r in result:
        r["grn_qty"]          = round(float(r.get("grn_qty")       or 0), 2)
        r["issued_qty"]       = round(float(r.get("issued_qty")     or 0), 2)
        r["issued_value"]     = round(float(r.get("issued_value")   or 0), 2)
        r["batches_planned"]  = r.get("bp_count") or 0
        r["batches_cancelled"] = 0
        r["total_slots"]      = 0
        r["planning_capacity"]     = 0
        r["vacant_slots"]     = 0

    result.sort(key=lambda x: (
        MONTHS_ORDER.index(x.get("month", "Unknown"))
        if x.get("month", "Unknown") in MONTHS_ORDER else 99
    ))

    frappe.response["message"] = result
