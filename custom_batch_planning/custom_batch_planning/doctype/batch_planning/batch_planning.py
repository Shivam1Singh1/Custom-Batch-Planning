import json
import frappe
from frappe.model.document import Document
from frappe.model.naming import make_autoname
from frappe.utils import getdate, flt, add_months

from custom_batch_planning.custom_batch_planning.doctype.batch_planning_settings.batch_planning_settings import (
    get_stock_cutover_datetime,
)

ENABLE_AFTER_SUBMIT_LOGIC = True

MONTH_MAP = {
    "january": "01",
    "february": "02",
    "march": "03",
    "april": "04",
    "may": "05",
    "june": "06",
    "july": "07",
    "august": "08",
    "september": "09",
    "october": "10",
    "november": "11",
    "december": "12",
}

def _update_sct_batches_planned(slot_opening_id, slot_booking_date, delta):
    """
    Increment or decrement batches_planned in Slot Capacity Detail.
    delta = +1 for increment, -1 for decrement.
    Uses direct DB set_value for performance (no heavy parent doc save).
    """
    if not slot_opening_id or not slot_booking_date:
        return

    slot_master = frappe.db.get_value(
        "Slot Opening", slot_opening_id, "slot_master"
    )
    if not slot_master:
        return

    sct_name = frappe.db.get_value(
        "Slot Capacity Tracker", {"slot_master": slot_master}, "name"
    )
    if not sct_name:
        return

    sct_detail = frappe.db.get_value(
        "Slot Capacity Detail",
        {
            "parent": sct_name,
            "parenttype": "Slot Capacity Tracker",
            "date": slot_booking_date,
            },
        ["name", "batches_planned"],
        as_dict=True,
    )

    if not sct_detail:
        frappe.log_error(
            message=f"Date {slot_booking_date} not found in SCT {sct_name}",
            title="SCT batches_planned update failed",
        )
        return

    new_planned = max(0, int(sct_detail.batches_planned or 0) + delta)
    frappe.db.set_value(
        "Slot Capacity Detail", sct_detail.name, "batches_planned", new_planned
    )

@frappe.whitelist()
def get_valid_slot_openings(employee_function, current_doc=None):
    """
    Returns Slot Openings where at least one date still has
    remaining capacity (per-date check).
    """
    today = frappe.utils.today()

    valid = frappe.db.sql(
        """
        SELECT DISTINCT so.name
        FROM `tabSlot Opening` so
        INNER JOIN `tabSlot Booking CT` sb ON sb.parent = so.name
        WHERE so.employee_function = %s
          AND sb.slot_booking_date >= %s
          AND EXISTS (
              SELECT 1
              FROM `tabSlot Booking CT` sb2
              WHERE sb2.parent = so.name
                AND sb2.slot_booking_date >= %s
                AND (
                    SELECT COUNT(*)
                    FROM `tabBatches Planned` bp
                    WHERE bp.slot_opening_id = so.name
                      AND bp.slot_booking_date = sb2.slot_booking_date
                ) < sb2.planning_capacity
          )
    """,
        (employee_function, today, today),
        as_dict=True,
    )

    return [r.name for r in valid]

@frappe.whitelist()
def get_next_batch_counter(slot_opening_id, batch_type, exclude_ids=None):
    """
    Returns the next Batch Planning ID for a given Slot Opening + Batch Type.
    MAX-based (not COUNT-based) to avoid reuse of deleted numbers.
    """
    exclude_ids = json.loads(exclude_ids) if exclude_ids else []

    if not slot_opening_id or not batch_type:
        return ""

    type_map = {
        "Manufacturing": "MFG",
        "Process Development": "PD",
        "Machine Trial": "MT",
    }
    short_code = type_map.get(batch_type, "EXP")

    max_committed = (
        frappe.db.sql(
            """
        SELECT COALESCE(MAX(
            FLOOR(CAST(SUBSTRING_INDEX(batch_planning_id, '-', -1) AS DECIMAL(10,0)))
        ), 0)
        FROM `tabBatches Planned`
        WHERE slot_opening_id = %s AND batch_type = %s
          AND batch_planning_id REGEXP '^.+-[0-9]+$'
    """,
            (slot_opening_id, batch_type),
        )[0][0]
        or 0
    )

    max_draft = (
        frappe.db.sql(
            """
        SELECT COALESCE(MAX(
            FLOOR(CAST(SUBSTRING_INDEX(bpd.batch_planning_id, '-', -1) AS DECIMAL(10,0)))
        ), 0)
        FROM `tabBatch Planning Detail` bpd
        JOIN `tabBatch Planning` bc ON bpd.parent = bc.name
        WHERE bpd.slot_opening_id = %s AND bpd.batch_type = %s
          AND bc.docstatus != 2
          AND bpd.batch_planning_id REGEXP '^.+-[0-9]+$'
    """,
            (slot_opening_id, batch_type),
        )[0][0]
        or 0
    )

    next_num = max(int(max_committed), int(max_draft)) + 1

    if exclude_ids:
        while (
            f"{slot_opening_id}-{short_code}-{str(next_num).zfill(2)}"
            in exclude_ids
        ):
            next_num += 1

    return f"{slot_opening_id}-{short_code}-{str(next_num).zfill(2)}"

class BatchPlanning(Document):

    def autoname(self):
        mm = None
        yy = None

        if self.month:
            mm = MONTH_MAP.get(self.month.strip().lower())

        if self.slot_opening:
            first_date = frappe.db.sql(
                """
                SELECT MIN(slot_booking_date) AS d
                FROM `tabSlot Booking CT`
                WHERE parent = %s
            """,
                self.slot_opening,
                as_dict=True,
            )

            if first_date and first_date[0].d:
                dt = getdate(first_date[0].d)
                if not mm:
                    mm = str(dt.month).zfill(2)
                if not yy:
                    yy = str(dt.year)[2:]

        if not mm:
            dt = getdate(frappe.utils.today())
            mm = str(dt.month).zfill(2)
            yy = str(dt.year)[2:]

        if not yy:
            yy = str(getdate(frappe.utils.today()).year)[2:]

        prefix = f"BP-{yy}-{mm}-"

        current = frappe.db.sql(
            "SELECT `current` FROM `tabSeries` WHERE name = %s", prefix
        )
        next_num = int(current[0][0]) + 1 if current else 1
        candidate = f"{prefix}{str(next_num).zfill(3)}"

        while frappe.db.exists("Batch Planning", candidate):
            next_num += 1
            candidate = f"{prefix}{str(next_num).zfill(3)}"

        frappe.db.sql(
            """
            INSERT INTO `tabSeries` (name, `current`) VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE `current` = %s
        """,
            (prefix, next_num, next_num),
        )

        self.name = candidate

    def validate(self):
        if not self.slot_opening:
            frappe.throw("Slot Opening is mandatory.")

        if self.slot_opening and not self.custom_employee_function:
            frappe.throw(
                "Please select an Employee Function first before selecting a Slot Opening."
            )

        if self.slot_opening:
            slot_opening_data = frappe.db.get_value(
                "Slot Opening",
                self.slot_opening,
                ["project", "batch_start_date", "slot_master"],
                as_dict=True
            )
            if slot_opening_data:
                self.project = slot_opening_data.get("project")
                self.custom_slot_master = slot_opening_data.get("slot_master")
                batch_start_date = slot_opening_data.get("batch_start_date")
                if batch_start_date:
                    import calendar
                    dt = getdate(batch_start_date)
                    self.month = calendar.month_name[dt.month]
        if self.slot_opening:
            existing = frappe.db.get_value(
                "Batch Planning",
                {"slot_opening": self.slot_opening, "name": ["!=", self.name]},
                "name",
            )
            if existing:
                frappe.throw(
                    f"Slot Opening {self.slot_opening} is already linked to Batch Planning {existing}. Only one Batch Planning per Slot Opening allowed."
                )
        
        if self.custom_employee_function:
            self.custom_employee_headname = frappe.db.get_value(
                "Employee Function",
                self.custom_employee_function,
                "function_head_name"
            )

        for row in self.custom_batch_details or []:
            if row.batch_planning_id:
                existing_bc = frappe.db.get_value(
                    "Batches Planned",
                    {"batch_planning_id": row.batch_planning_id},
                    "batch_planning",
                )
                if existing_bc and existing_bc != self.name:
                    frappe.throw(
                        f"⚠️ Duplicate Batch Planning ID Detected!\n\n"
                        f"<b>{row.batch_planning_id}</b> (Row {row.idx}) is already linked to "
                        f"Batches Planned under <b>{existing_bc}</b>.\n\n"
                        f"Each Batch Planning ID must be unique."
                    )

        seen_ids = []
        for row in self.custom_batch_details or []:
            if row.batch_planning_id:
                if row.batch_planning_id in seen_ids:
                    frappe.throw(
                        f"⚠️ Duplicate Batch Planning ID <b>{row.batch_planning_id}</b> "
                        f"found in Row {row.idx}. Each row must have a unique ID."
                    )
                seen_ids.append(row.batch_planning_id)

        for row in self.custom_batch_details or []:
            if row.finished_item and not row.batch_type:
                frappe.throw(
                    f"Row {row.idx}: Please select a Batch Type before selecting a Finished Item."
                )
            if row.bom_list and not row.finished_item:
                frappe.throw(
                    f"Row {row.idx}: Please select a Finished Item before selecting a BOM."
                )

        for row in self.custom_batch_details or []:
            if not row.batch_planning_id and row.slot_booking_date:
                try:
                    parsed_date = frappe.utils.getdate(row.slot_booking_date)
                    year = parsed_date.strftime("%y")
                    month = parsed_date.strftime("%m")
                    prefix = f"BC-{year}-{month}-.###"
                    row.batch_planning_id = make_autoname(prefix)
                except Exception:
                    pass

        if self.slot_opening:
            planning_capacity_data = frappe.get_all(
                "Slot Booking CT",
                filters={"parent": self.slot_opening},
                fields=["slot_booking_date", "planning_capacity"]
            )
            booked_map = {}
            for d in planning_capacity_data:
                date_key = frappe.utils.getdate(d.slot_booking_date)
                booked_map[date_key] = booked_map.get(date_key, 0) + (d.planning_capacity or 0)
            
            planned_map = {}
            for row in self.custom_batch_details or []:
                if row.slot_booking_date:
                    date_key = frappe.utils.getdate(row.slot_booking_date)
                    planned_map[date_key] = planned_map.get(date_key, 0) + 1
            
            for d_key, count in planned_map.items():
                allowed = booked_map.get(d_key, 0)
                if count > allowed:
                    frappe.throw(
                        f"Cannot create {count} batches for {d_key}. You only booked "
                        f"{allowed} slot(s) for this date on Slot Opening {self.slot_opening}."
                    )

    def create_batches_planned_records(self):
        count = 0

        for row in self.custom_batch_details or []:
            existing = frappe.db.get_value(
                "Batches Planned",
                {"batch_planning_id": row.batch_planning_id},
                ["name", "batch_planning"],
                as_dict=True,
            )

            if existing:
                if existing.batch_planning == self.name:
                    continue
                elif existing.batch_planning:
                    frappe.throw(
                        f"⚠️ Batch Planning ID <b>{row.batch_planning_id}</b> "
                        f"already exists under <b>{existing.batch_planning}</b>."
                    )
                else:
                    frappe.db.set_value(
                        "Batches Planned",
                        existing.name,
                        "batch_planning",
                        self.name,
                        update_modified=False,
                    )
                    continue

            batch_key = f"{self.name}-{row.idx}"
            bom_store = frappe.db.get_value(
                "Batch BOM Store after Edit",
                {"batch_id": batch_key},
                "bom_name",
            )

            bp = frappe.new_doc("Batches Planned")
            bp.batch_planning_id = row.batch_planning_id
            bp.slot_opening_id = row.slot_opening_id
            if row.slot_opening_id:
                bp.project = frappe.db.get_value("Slot Opening", row.slot_opening_id, "project")

            bp.employee_function = self.custom_employee_function
            bp.employee_name = self.custom_employee_headname
            bp.month = self.month
            bp.batch_type = row.batch_type
            bp.finished_item = row.finished_item
            bp.slot_booking_date = row.slot_booking_date
            bp.batch_planning = self.name
            bp.bom_list = bom_store if bom_store else row.bom_list

            bp.flags.ignore_permissions = True
            bp.flags.ignore_validate = True
            bp.flags.ignore_mandatory = True
            bp.flags.ignore_workflow = True

            bp.insert(ignore_permissions=True, ignore_mandatory=True)

            _update_sct_batches_planned(
                row.slot_opening_id, row.slot_booking_date, +1
            )

            update_data = {
                "workflow_state": getattr(row, 'status', None),
            }
            if getattr(row, 'status', None) == "Approved":
                update_data["docstatus"] = 1
            elif getattr(row, 'status', None) == "Cancelled":
                update_data["docstatus"] = 2

            frappe.db.set_value(
                "Batches Planned", bp.name, update_data, update_modified=False
            )
            count += 1

        frappe.db.commit()
        return count

    def on_submit(self):
        if not ENABLE_AFTER_SUBMIT_LOGIC:
            return
        if getattr(self, 'workflow_state', None) != "Approved":
            return
        self.create_batches_planned_records()

    def on_trash(self):
        bp_list = frappe.get_all(
            "Batches Planned",
            filters={"batch_planning": self.name},
            fields=["name", "slot_opening_id", "slot_booking_date"],
        )

        for bp in bp_list:
            _update_sct_batches_planned(
                bp.slot_opening_id, bp.slot_booking_date, -1
            )

        frappe.flags.skip_sct_decrement = True
        try:
            for bp in bp_list:
                frappe.delete_doc(
                    "Batches Planned",
                    bp.name,
                    ignore_permissions=True,
                    force=True,
                )
        finally:
            frappe.flags.skip_sct_decrement = False

@frappe.whitelist()
def create_bulk_material_allocations(batch_planning_name):
    """
    Consolidated Flow:
    Combine all BOM items from all Batches Planned under this BP into a single Material Allocation doc.
    """
    parent_doc = frappe.get_doc("Batch Planning", batch_planning_name)
    if getattr(parent_doc, 'workflow_state', None) != "Approved":
        frappe.throw("Document is not in Approved state.")
    if parent_doc.docstatus != 1:
        frappe.throw("Document is not submitted yet.")

    warning_message = ""
    exists = frappe.db.exists(
        "Material Allocation",
        {
            "batch_planning": batch_planning_name,
            "allocation_status": ["!=", "Deallocated"],
            "docstatus": ["!=", 2]
        }
    )
    if exists:
        warning_message = f"Note: A Material Allocation ({exists}) already exists for Batch Planning {batch_planning_name}."

    if not parent_doc.custom_employee_function:
        frappe.throw("Employee Function is not set on Batch Planning.")

    ef = frappe.get_doc("Employee Function", parent_doc.custom_employee_function)
    warehouse = next(
        (r.store_warehouse for r in (ef.table_bukm or []) if r.store_warehouse),
        None,
    )

    if not warehouse:
        frappe.throw(f"No store warehouse found for Employee Function {parent_doc.custom_employee_function}")

    consolidated_items = get_consolidated_bom_components(batch_planning_name)
    if not consolidated_items:
        frappe.throw("No items found to allocate.")

    ma_data = {
        "doctype": "Material Allocation",
        "batch_planning": batch_planning_name,
        "employee_function": parent_doc.custom_employee_function,
        "project_id": parent_doc.project,
        "project_name": frappe.db.get_value("Project", parent_doc.project, "project_name") if parent_doc.project else "",
        "workflow_state": "Draft",
        "material_allocation": []
    }

    cutover = get_stock_cutover_datetime()

    shared_rows = []
    shared_total = 0.0

    for item in consolidated_items:
        item_code = item["item_code"]
        qty_required = flt(item["qty"])

        figures = free_stock_figures(
            item_code,
            warehouse,
            parent_doc.custom_employee_function,
            parent_doc.project,
            batch_planning_name,
            cutover,
        )

        pools = split_local_first(
            [], figures["bp_free_stock"], figures["other_free_stock"]
        )
        own_free = pools["local_free"]
        other_free = pools["global_free"]
        cap = pools["capacity"]

        allocate_qty = min(qty_required, cap)
        if allocate_qty <= 0:
            continue

        split = split_local_first([allocate_qty], own_free, other_free)["rows"][0]
        from_own = split["from_local"]
        from_shared = split["from_global"]

        if from_shared > 0:
            shared_total += from_shared
            shared_rows.append({
                "item_code": item_code,
                "item_name": item["item_name"],
                "uom": item["uom"],
                "required": round(qty_required, 2),
                "own_free": round(own_free, 2),
                "global_free": round(other_free, 2),
                "shared_qty": round(from_shared, 2),
            })

        ma_data["material_allocation"].append({
            "doctype": "Material Allocation Item",
            "parenttype": "Material Allocation",
            "parentfield": "material_allocation",
            "item_code": item_code,
            "item_name": item["item_name"],
            "uom": item["uom"],
            "quantity_required": qty_required,
            "allocate_qty": round(allocate_qty, 2),
            "local_free_qty": round(own_free, 2),
            "global_free_qty": round(other_free, 2),
            "local_allocated_qty": round(from_own, 2),
            "global_allocated_qty": round(from_shared, 2),
            "stock_available": round(cap, 2),
        })

    if not ma_data["material_allocation"]:
        frappe.throw(
            "No free stock is available for any item on this Batch Planning — "
            "neither its own nor the shared pool. Material Allocation cannot be created."
        )

    ma_data["shared_stock_rows"] = shared_rows
    ma_data["shared_stock_required"] = round(shared_total, 2)

    if warning_message:
        ma_data["warning"] = warning_message
    return ma_data

@frappe.whitelist()
def create_batches_planned(doc_name):
    """Called from a custom JS button on the Batch Planning form."""
    doc = frappe.get_doc("Batch Planning", doc_name)

    if getattr(doc, 'workflow_state', None) != "Approved":
        frappe.throw("Document is not in Approved state.")
    if doc.docstatus != 1:
        frappe.throw("Document is not submitted yet.")

    count = doc.create_batches_planned_records()
    return f"{count} Batches Planned record(s) created successfully."

@frappe.whitelist()
def get_item_details_for_bom(item_codes):
    item_codes = json.loads(item_codes)
    if not item_codes:
        return []

    return frappe.db.sql(
        """
        SELECT name, item_group, min_order_qty, safety_stock
        FROM `tabItem`
        WHERE name IN %(items)s
    """,
        {"items": item_codes},
        as_dict=True,
    )

@frappe.whitelist()
def get_consolidated_bom_components(doc_name):
    doc = frappe.get_doc("Batch Planning", doc_name)
    components = {}

    for row in doc.custom_batch_details or []:
        if not row.bom_list:
            continue
        
        batch_key = f"{doc.name}-{row.idx}"
        bom_store = frappe.db.get_value(
            "Batch BOM Store after Edit", {"batch_id": batch_key}, "name"
        )
        
        use_store = False
        items = []
        if bom_store:
            store_doc = frappe.get_doc("Batch BOM Store after Edit", bom_store)
            items = store_doc.bom_components or []
            use_store = True
        else:
            bom = frappe.get_doc("BOM", row.bom_list)
            items = bom.exploded_items or bom.items or []
            
        for item in items:
            qty = flt(
                item.qty
                if use_store
                else (item.qty_consumed_per_unit or item.stock_qty or item.qty)
            )
            uom = item.uom if use_store else (item.stock_uom or item.uom)
            item_code = item.item_code
            item_name = item.item_name
            
            if item_code not in components:
                components[item_code] = {
                    "item_code": item_code,
                    "item_name": item_name,
                    "uom": uom,
                    "qty": 0.0
                }
            components[item_code]["qty"] += qty

    sorted_components = sorted(components.values(), key=lambda x: x["item_code"])
    return sorted_components


MR_APPROVED = "mr.docstatus = 1 AND mr.workflow_state LIKE 'Approve%%'"
PO_APPROVED = "po.docstatus = 1 AND po.workflow_state LIKE 'Approve%%'"
PR_APPROVED = "pr.docstatus = 1 AND pr.workflow_state LIKE 'Approve%%'"

MR_PURCHASE_ONLY = "mr.material_request_type = 'Purchase'"

PR_UNAPPROVED = (
    "pr.docstatus = 0 "
    "AND (pr.workflow_state IS NULL OR ("
    "pr.workflow_state NOT LIKE 'Approve%%' "
    "AND pr.workflow_state NOT LIKE 'Reject%%' "
    "AND pr.workflow_state NOT LIKE 'Cancel%%'))"
)

MR_EF = "COALESCE(NULLIF(mri.employee_function,''), NULLIF(mr.custom_employee_function,''))"
MR_PROJECT = "COALESCE(NULLIF(mri.project,''), NULLIF(mr.project,''))"
PO_EF = (
    "COALESCE(NULLIF(poi.employee_function,''), NULLIF(poi.custom_employee_functions,''), "
    "NULLIF(po.employee_function,''), NULLIF(po.custom_employee_functions,''))"
)
PO_PROJECT = "COALESCE(NULLIF(poi.project,''), NULLIF(po.project,''))"
PR_EF = "COALESCE(NULLIF(pri.employee_function,''), NULLIF(pr.employee_function,''))"
PR_PROJECT = "COALESCE(NULLIF(pri.project,''), NULLIF(pr.project,''))"


def _bp_predicate(alias, mode):
    """Row filter for the two figures stacked in every open-pipeline cell.

    The two modes select DISJOINT sets of rows — GEN is other batches' demand,
    BP is this batch's demand:

        GEN: tagged to some batch, but explicitly NOT the current one
        BP:  tagged to the current batch

    The `<> %(bp)s` in GEN is what keeps them separate, and it is the whole
    point of this function: without it GEN would swallow BP and the two lines
    in a cell would double-report the current batch's own documents. An item
    with a single open MR of 9 belonging to this batch must read GEN 0 (0) /
    BP 9 (1), never 9 (1) / 9 (1).

    Because they are separate pools they must never be summed, and only BP may
    be used as coverage: the material behind GEN is committed to other batches
    and cannot be drawn on by this one.

    Rows with no batch_planning_id at all were raised outside batch planning
    and belong to no batch, so they match neither mode. Every open-pipeline and
    stock column obeys that rule without exception — Unapproved GRN used to
    take an untagged_in_gen escape hatch that swept untagged receipts into GEN,
    and it was removed so one classification governs all of them.
    """
    if mode == "GEN":
        return (
            f"({alias}.batch_planning_id IS NOT NULL "
            f"AND {alias}.batch_planning_id <> '' "
            f"AND {alias}.batch_planning_id <> %(bp)s)"
        )
    return f"{alias}.batch_planning_id = %(bp)s"


def _bucket(rows, count_all=False):
    """Fold per-row open qty into (qty, doc_count, doc_names).

    Row qty is already floored at 0 by GREATEST() in SQL. Documents are counted
    only when they still carry open qty, unless count_all is set (Unapproved
    GRN, where every waiting receipt counts regardless of net qty).
    """
    total = 0.0
    docs = []
    for r in rows:
        qty = flt(r.open_qty)
        total += qty
        if (qty > 0.001 or count_all) and r.doc and r.doc not in docs:
            docs.append(r.doc)
    return round(total, 2), len(docs), docs


def _open_mr(item_code, ef, project, bp, mode):
    """Approved Purchase-type MR line qty not yet covered by an approved PO.

        Open MR = MR is Approved
                  AND (no PO against it, OR its PO is not itself Approved)

    Draft and pending-approval MRs are excluded outright — they appear in
    neither GEN nor BP, because an unapproved request is not yet demand anyone
    has committed to. Symmetrically, only an APPROVED PO retires the quantity
    from this column; a draft or pending PO is not trusted as coverage since it
    can still be rejected or deleted, leaving the MR needing action. See
    MR_APPROVED / PO_APPROVED for both halves of that rule.

    Restricted to material_request_type = 'Purchase': Material Transfer,
    Material Issue and Manufacture requests share this doctype but are not
    procurement, so they are excluded from GEN and BP alike (see
    MR_PURCHASE_ONLY). Open PO and Open PR/GRN need no equivalent filter —
    Purchase Order and Purchase Receipt are procurement documents by nature.
    """
    rows = frappe.db.sql(
        f"""
        SELECT mr.name AS doc,
               GREATEST(mri.qty - IFNULL(approved_po.po_qty, 0), 0) AS open_qty
        FROM `tabMaterial Request Item` mri
        JOIN `tabMaterial Request` mr ON mr.name = mri.parent
        LEFT JOIN (
            SELECT poi.material_request_item AS mri_name, SUM(poi.qty) AS po_qty
            FROM `tabPurchase Order Item` poi
            JOIN `tabPurchase Order` po ON po.name = poi.parent
            WHERE {PO_APPROVED}
            GROUP BY poi.material_request_item
        ) approved_po ON approved_po.mri_name = mri.name
        WHERE mri.item_code = %(item_code)s
          AND {MR_APPROVED}
          AND {MR_PURCHASE_ONLY}
          AND {MR_EF} = %(ef)s
          AND {MR_PROJECT} = %(project)s
          AND {_bp_predicate('mri', mode)}
        """,
        {"item_code": item_code, "ef": ef, "project": project, "bp": bp},
        as_dict=True,
    )
    return _bucket(rows)


def _open_po(item_code, ef, project, bp, mode):
    """Approved PO line qty not yet covered by an approved goods receipt.

        Open PO = PO is Approved
                  AND (no PR/GRN against it, OR its PR/GRN is not itself
                       Approved by Store Head)

    Draft and pending-approval POs are excluded outright, from GEN and BP
    alike, on the same reasoning as Open MR: an unapproved order is not a
    commitment to buy. Symmetrically, only a Store-Head-approved receipt
    retires the quantity from this column — a draft or pending receipt can
    still be rejected or deleted, so it is not proof the goods have actually
    landed. See MR_APPROVED / PO_APPROVED / PR_APPROVED.

    Matching Open MR's gate here is what removes the MR/PO double-count: with
    an existence gate, a draft PO against an approved MR appeared in Open MR
    and Open PO at once.

    Deliberately joins receipt rows rather than reading poi.received_qty, so the
    hand-off point is the receipt document itself rather than ERPNext's own
    received counter.

    OVERLAPS UNAPPROVED GRN BY DESIGN. A pending receipt does not clear its PO,
    so those units show here AND in Unapproved GRN — deliberately, because the
    goods are ordered, have physically arrived, and are still not accepted
    stock. The two columns are therefore not a partition and must never be
    added. Every consumer subtracts THIS column only and leaves Unapproved GRN
    as display: Net Req always did, and get_batch_wise_shortages was corrected
    to, having previously subtracted both and under-reported the shortage.
    """
    rows = frappe.db.sql(
        f"""
        SELECT po.name AS doc,
               GREATEST(poi.qty - IFNULL(approved_pr.pr_qty, 0), 0) AS open_qty
        FROM `tabPurchase Order Item` poi
        JOIN `tabPurchase Order` po ON po.name = poi.parent
        LEFT JOIN (
            SELECT pri.purchase_order_item AS poi_name,
                   SUM(pri.qty - IFNULL(pri.returned_qty, 0)) AS pr_qty
            FROM `tabPurchase Receipt Item` pri
            JOIN `tabPurchase Receipt` pr ON pr.name = pri.parent
            WHERE {PR_APPROVED}
            GROUP BY pri.purchase_order_item
        ) approved_pr ON approved_pr.poi_name = poi.name
        WHERE poi.item_code = %(item_code)s
          AND {PO_APPROVED}
          AND {PO_EF} = %(ef)s
          AND {PO_PROJECT} = %(project)s
          AND {_bp_predicate('poi', mode)}
        """,
        {"item_code": item_code, "ef": ef, "project": project, "bp": bp},
        as_dict=True,
    )
    return _bucket(rows)


def _open_pr_grn(item_code, ef, project, bp, mode):
    """Goods received but not yet approved into stock — the Unapproved GRN column.

    The final in-flight stage. Quantity arrives here when a receipt is raised
    against the PO and leaves the moment Store Head approves it, at which point
    the same units appear as real stock through the Stock Ledger. Nothing is
    ever in both places: approval is the submit (see PR_UNAPPROVED), so an
    unapproved receipt has no ledger entry and an approved one is no longer
    unapproved. That is what keeps this column free of double counting.

    Two independent figures, classified exactly as Open MR and Open PO classify
    theirs, through the same _bp_predicate:

        GEN ("Global Unapproved GRN")  receipts tagged to OTHER Batch Plannings
        BP  ("Local Unapproved GRN")   receipts tagged to THIS one

    Untagged receipts belong to no batch and appear in neither line. This
    column previously folded them into GEN, which made it the one stage that
    classified differently from the rest of the report; that exception was
    removed on request so a single rule governs every column.

    Visibility only. No coverage arithmetic subtracts this column — the units
    behind it are already credited through Open PO, which by design does not
    release a PO until its receipt is approved. Subtracting both would credit
    the same goods twice, which is why Net Req and get_batch_wise_shortages
    each subtract Open PO alone.
    """
    rows = frappe.db.sql(
        f"""
        SELECT pr.name AS doc,
               GREATEST(pri.qty - IFNULL(pri.returned_qty, 0), 0) AS open_qty
        FROM `tabPurchase Receipt Item` pri
        JOIN `tabPurchase Receipt` pr ON pr.name = pri.parent
        WHERE pri.item_code = %(item_code)s
          AND {PR_UNAPPROVED}
          AND {PR_EF} = %(ef)s
          AND {PR_PROJECT} = %(project)s
          AND {_bp_predicate('pri', mode)}
        """,
        {"item_code": item_code, "ef": ef, "project": project, "bp": bp},
        as_dict=True,
    )
    return _bucket(rows, count_all=True)




def _stock_qty(item_code, warehouse, project, bp, mode, in_main=True):
    """Stock Ledger qty for one item, split by batch tag.

    Reuses _bp_predicate so "GEN" means here exactly what it means for the open
    pipeline: tagged to some OTHER batch — never the current one, never
    untagged.

    Employee Function is scoped through the warehouse rather than through
    sle.employee_function. That column exists but is unpopulated on most rows
    (empty on 115k of 152k Stock Ledger Entries, and on 41 of the 147
    batch-tagged ones), so filtering on it would silently drop real stock. A
    store warehouse belongs to exactly one Employee Function, so
    `warehouse = <that EF's store>` is the dependable proxy — and it is what
    this report has always used.

    in_main=False flips to everything OUTSIDE the main store, which is how Lab
    Wise is measured.
    """
    wh_op = "=" if in_main else "<>"
    return flt(
        frappe.db.sql(
            f"""
        SELECT IFNULL(SUM(sle.actual_qty), 0)
        FROM `tabStock Ledger Entry` sle
        WHERE sle.item_code = %(item_code)s
          AND sle.warehouse {wh_op} %(warehouse)s
          AND sle.project = %(project)s
          AND sle.is_cancelled = 0
          AND {_bp_predicate('sle', mode)}
        """,
            {
                "item_code": item_code,
                "warehouse": warehouse,
                "project": project,
                "bp": bp,
            },
        )[0][0]
        or 0.0
    )


def _global_main_stock(item_code, warehouse, employee_function, project, cutover):
    """Pool-wide Main Wh stock, scoped to the post-cutover regime.

    Deliberately NOT gen + bp. The GEN/BP Main Wh columns describe stock
    whenever it was posted; this figure exists only to drive Free Qty, and Free
    Qty is only meaningful over stock that was tagged under go-live discipline.
    The two therefore diverge for any pre-cutover tagged row, and that is the
    intended behaviour, not drift.

    Three independent guards, none of which is redundant:

      project = P            the scope that matters
      employee_function = EF a second dimension on the same row
      posting_datetime >= cutover

    The date cutoff is not implied by the project filter. A legacy row can
    carry a matching EF and Project by accident — a default value, a bulk
    import artefact — while predating any real tagging discipline. The cutoff
    is what makes "this row was tagged deliberately" true rather than hoped.

    Note this filters on sle.employee_function, which the GEN/BP columns
    deliberately avoid because it is empty on ~76% of historical rows. Here
    that is safe and wanted: everything before the cutover is excluded anyway,
    and Employee Function is an Inventory Dimension, so post-cutover rows carry
    it once tagging is enforced at the source. Until that enforcement exists,
    expect this to read 0 — which is why Free Qty stays pending.

    Returns None when no cutover has been declared: there is no honest global
    figure before go-live, and returning 0 would read as "no stock".
    """
    if not cutover:
        return None

    return flt(
        frappe.db.sql(
            """
        SELECT IFNULL(SUM(sle.actual_qty), 0)
        FROM `tabStock Ledger Entry` sle
        WHERE sle.item_code = %(item_code)s
          AND sle.warehouse = %(warehouse)s
          AND sle.employee_function = %(ef)s
          AND sle.project = %(project)s
          AND sle.posting_datetime >= %(cutover)s
          AND sle.is_cancelled = 0
        """,
            {
                "item_code": item_code,
                "warehouse": warehouse,
                "ef": employee_function,
                "project": project,
                "cutover": cutover,
            },
        )[0][0]
        or 0.0
    )


@frappe.whitelist()
def get_legacy_stock(item_code, warehouse, project=None):
    """Pre-cutover / untagged stock, for audit and reporting ONLY.

    This is the frozen bucket the cutover creates: everything posted before the
    go-live marker, plus anything with no Project at all whenever it was
    posted. It is never joined into Global Main Wh, Free Qty, Net Req or any
    other planning figure — that separation is the entire point of the cutover,
    and quietly folding it back in would recreate the mostly-untagged pool the
    cutover exists to escape.

    Real, usable stock does sit in here. It becomes visible to planning only
    when someone deliberately reconciles it with a correction Stock Entry.
    That is a one-time data-cleanup task, not something this query should try
    to infer.
    """
    cutover = get_stock_cutover_datetime()

    conditions = ["sle.item_code = %(item_code)s", "sle.warehouse = %(warehouse)s", "sle.is_cancelled = 0"]
    params = {"item_code": item_code, "warehouse": warehouse, "cutover": cutover}

    if cutover:
        conditions.append(
            "(sle.posting_datetime < %(cutover)s OR sle.project IS NULL OR sle.project = '')"
        )
    else:
        conditions.append("(sle.project IS NULL OR sle.project = '')")

    if project:
        conditions.append("(sle.project = %(project)s OR sle.project IS NULL OR sle.project = '')")
        params["project"] = project

    row = frappe.db.sql(
        f"""
        SELECT IFNULL(SUM(sle.actual_qty), 0) AS qty, COUNT(*) AS rows_counted
        FROM `tabStock Ledger Entry` sle
        WHERE {' AND '.join(conditions)}
        """,
        params,
        as_dict=True,
    )[0]

    return {
        "item_code": item_code,
        "warehouse": warehouse,
        "cutover_datetime": str(cutover) if cutover else None,
        "legacy_qty": round(flt(row.qty), 2),
        "legacy_sle_rows": int(row.rows_counted or 0),
        "note": "Audit only — never counted in Global Main Wh, Free Qty or Net Req.",
    }


_HAS_SPLIT = (
    "(IFNULL(mai.local_allocated_qty, 0) + IFNULL(mai.global_allocated_qty, 0)) > 0"
)
_SOURCE_COLUMN = {
    None: "mai.allocate_qty",
    "local": f"CASE WHEN {_HAS_SPLIT} THEN IFNULL(mai.local_allocated_qty, 0) "
             "ELSE mai.allocate_qty END",
    "global": f"CASE WHEN {_HAS_SPLIT} THEN IFNULL(mai.global_allocated_qty, 0) "
              "ELSE 0 END",
}


def _allocated_qty(
    item_code,
    project,
    batch_planning=None,
    employee_function=None,
    exclude_batch_planning=None,
    exclude_parent=None,
    for_update=False,
    source=None,
):
    """Qty reserved through Material Allocation, at batch scope or pool scope.

    Pass batch_planning for this batch's own reservations (the Allocated
    column); pass employee_function for the pool-wide total that Global Free
    Qty is built from. Same query either way — only the scope line differs —
    so the two figures can never drift apart in their status filters.

    source selects WHICH pool the reservation drew on: None totals
    allocate_qty (every reservation, whatever its source — this is the
    Allocated column), "local" totals only what came out of the batch's own
    stock, "global" only what came out of the shared pool. See _SOURCE_COLUMN
    above for how rows predating the breakdown are handled.

    Scoped on ma.project_id, not ma.project: project_id is the populated field
    on this doctype (set on 23 of 25 allocations; ma.project is empty on all of
    them).

    A Deallocated or Stock-Entry-Done allocation no longer holds stock — the
    first released it, the second consumed it into a Stock Entry — so neither
    counts as a live reservation.

    exclude_parent drops one Material Allocation from the total. It exists for
    the save-time re-check: a document being re-saved is already in the table,
    so counting it would make it compete with itself and reject its own rows.

    for_update takes row locks so a concurrent allocation cannot read the same
    free pool and both pass. Only the save-time check needs it — the report
    must never hold locks.
    """
    if batch_planning:
        scope_sql = "AND ma.batch_planning = %(scope)s"
        scope = batch_planning
    else:
        scope_sql = "AND ma.employee_function = %(scope)s"
        scope = employee_function

    exclude_sql = "AND ma.name <> %(exclude)s" if exclude_parent else ""
    exclude_bp_sql = (
        "AND ma.batch_planning <> %(exclude_bp)s" if exclude_batch_planning else ""
    )
    lock_sql = "FOR UPDATE" if for_update else ""

    return flt(
        frappe.db.sql(
            f"""
        SELECT IFNULL(SUM({_SOURCE_COLUMN[source]}), 0)
        FROM `tabMaterial Allocation Item` mai
        INNER JOIN `tabMaterial Allocation` ma ON ma.name = mai.parent
        WHERE mai.item_code = %(item_code)s
          AND ma.project_id = %(project)s
          {scope_sql}
          {exclude_sql}
          {exclude_bp_sql}
          AND ma.allocation_status NOT IN ('Deallocated', 'Stock Entry Done')
          AND ma.docstatus != 2
        {lock_sql}
        """,
            {
                "item_code": item_code,
                "project": project,
                "scope": scope,
                "exclude": exclude_parent,
                "exclude_bp": exclude_batch_planning,
            },
        )[0][0]
        or 0.0
    )


def split_local_first(quantities, local_free, global_free):
    """Split requested quantities across the two pools, local stock first.

    The allocation priority is a rule, not a preference: every unit of THIS
    batch's own free stock is spent before a single unit is taken from material
    other batches purchased.

        requested 100, local 40, global 80  ->  40 local + 60 global
        requested  30, local 40, global 80  ->  30 local +  0 global

    never 0 local + 100 global, and never any global while local stock remains.

    `quantities` is the list of requested quantities in row order, so several
    rows for the same item drain one shared pool instead of each reading the
    full figure and collectively taking more local stock than exists. The first
    rows fill from local until it runs out; later rows fall to global.

    Both pools are clamped at zero before anything is spent. bp_free reads
    negative when a batch has reserved more than it bought (see
    free_stock_figures), and a negative pool must contribute nothing rather
    than lend the other pool extra room.

    `shortfall` is what the request exceeds the two pools by; it is the
    caller's job to reject on it. When it is non-zero the per-row from_global
    figures deliberately still add up to the full request — they describe what
    was asked for, not what may be issued.
    """
    local_free = round(max(flt(local_free), 0.0), 6)
    global_free = round(max(flt(global_free), 0.0), 6)

    local_remaining = local_free
    requested_total = 0.0
    rows = []

    for qty in quantities:
        qty = max(flt(qty), 0.0)
        requested_total += qty
        from_local = min(qty, local_remaining)
        local_remaining -= from_local
        rows.append({
            "from_local": round(from_local, 6),
            "from_global": round(qty - from_local, 6),
        })

    capacity = round(local_free + global_free, 6)
    requested_total = round(requested_total, 6)

    return {
        "local_free": local_free,
        "global_free": global_free,
        "capacity": capacity,
        "requested": requested_total,
        "shortfall": round(max(requested_total - capacity, 0.0), 6),
        "rows": rows,
    }


def settle_cross_batch_draw(bp_main, other_main):
    """Move a negative main-warehouse balance onto the pile it was taken from.

    A batch's main-warehouse figure is the sum of ledger rows carrying its tag.
    It can only go negative when the batch issued material that never entered
    under its own tag — which is exactly what a borrowed allocation is. The
    Stock Entry stamps the transfer with the batch that CONSUMED the stock,
    while the receipt that put it there carries the batch that BOUGHT it, so
    the issue lands on one line and the receipt on another:

        lender   9,950 own,   −50 other      (bought 10,000, moved 50 to lab)
        borrower   −50 own, 9,950 other      (bought nothing, issued 50)

    Both add up to the 9,900 physically left in the store, but neither says so:
    each shows 9,950 on one line and a negative on the other, and the negative
    is clamped to 0 everywhere it is displayed. So Main Wh reads 9,950 after 50
    units have demonstrably left the building.

    Netting the deficit against the other line makes both batches agree:

        lender   9,900 own,      0 other
        borrower     0 own,  9,900 other

    This also repairs Net Req, which subtracts the batch's own main stock: a
    borrower carrying −50 had that deficit ADDED to its requirement, so a batch
    holding all 50 units it needed in its own lab still asked for 50 more.

    Same conservative rule as free_pools, and the same limit: the ledger does
    not record whose pile a borrowed unit came from, so with three or more
    lenders the deficit is charged to the whole other pool rather than
    apportioned. Errs toward showing less stock, never more.

    Both negative means material with no batch tag at all was consumed; there
    is nothing to net against, so the figures are left as they are.
    """
    bp_main, other_main = flt(bp_main), flt(other_main)

    if bp_main < 0 <= other_main:
        return 0.0, other_main + bp_main
    if other_main < 0 <= bp_main:
        return bp_main + other_main, 0.0
    return bp_main, other_main


def free_pools(
    bp_main,
    bp_local_allocated,
    bp_global_allocated,
    other_main,
    other_local_allocated,
    other_global_allocated,
):
    """Free Qty for both pools: every reservation charged to the stock it took.

    The single rule the Free Qty columns rest on. A LOCAL draw comes off the
    drawing batch's own tagged stock. A GLOBAL draw comes off everyone else's —
    so it is subtracted from the OTHER line of whoever is looking, whichever
    side of the loan they are on:

        bp_free    = bp_main    − my local draws    − other batches' GLOBAL draws
        other_free = other_main − their local draws − my GLOBAL draws

    Both halves matter, and they are mirror images. Without the first, a lender
    kept offering stock a borrower had already reserved: batch A holding 9,950
    lent 50 to batch B, and still showed 9,950 free — so A could allocate all
    9,950 while B held 50 of the same units, reserving 10,000 units of a 9,950
    unit pile. Without the second, a global allocation was invisible to the
    batch that made it.

    Worked through, from both ends of that loan:

        A (lender)   9950 − 0 − 50 = 9900 own,  0    − 0 − 0  =    0 global
        B (borrower)    0 − 0 − 0  =    0 own,  9950 − 0 − 50 = 9900 global

    Both now report the same 9,900 free against the same physical pile, which
    is the property the old arithmetic lacked: the two batches disagreed by
    exactly the borrowed quantity.

    THREE OR MORE LENDERS. An allocation records how much it borrowed, never
    from whom, so a global draw is charged to every possible lender's own line
    rather than apportioned across them. With two batches that is exact. With
    three it is conservative — each lender's own free reads low by what the
    others lent — which errs toward refusing an allocation rather than
    double-issuing stock. Recording the lender on the allocation row is what
    would make it exact.

    Returned unclamped; the caller decides whether a negative reads as zero.
    """
    return {
        "bp_free": flt(bp_main) - flt(bp_local_allocated) - flt(other_global_allocated),
        "other_free": flt(other_main) - flt(other_local_allocated) - flt(bp_global_allocated),
    }


def free_stock_figures(
    item_code,
    warehouse,
    employee_function,
    project,
    batch_planning,
    cutover,
    exclude_parent=None,
    for_update=False,
):
    """The one place Free Qty is computed. Report and allocator both call it.

    Returns two DISJOINT scopes, both narrowed to the same
    Item + Project + Main Warehouse:

        this batch   stock tagged to THIS Batch Planning    − what has been reserved OUT OF IT
        other        stock tagged to ANY OTHER Batch Planning − what has been reserved OUT OF IT

    Only batch-tagged stock counts. Untagged Stock Ledger rows belong to no
    batch and appear in neither figure, so nothing can be allocated out of
    material that was never claimed by batch planning in the first place.

    Because the two sets share no row, they ARE additive: the allocatable
    ceiling is this batch's free stock plus the other batches' free stock, and
    that sum is returned as total_free. This is the opposite of a pool-scoped
    model, where the batch figure sits inside the global one and adding them
    would double-count.

    EACH RESERVATION IS SUBTRACTED FROM THE POOL IT WAS DRAWN FROM, not from
    the pool of whichever batch made it. This is the whole point of the
    local/global breakdown recorded on every allocation row. This batch
    allocating 10 with no stock of its own takes those 10 out of OTHER, because
    that is where they physically came from:

        bp_free    = bp_main    − this batch's LOCAL draws
        other_free = other_main − other batches' draws − this batch's GLOBAL draws

    Subtracting a batch's whole reservation from its own tagged stock instead
    used to leave the global pool unchanged after a global allocation — the
    borrowed units stayed on offer to the borrower, who could then take them
    again — while pushing this batch's own figure negative to compensate. Since
    the negative was clamped away at every display and at the allocation
    ceiling, the compensation never actually arrived.

    Neither figure can now go negative through cross-batch borrowing: a draw
    only ever reduces the pool that holds the stock. (Legacy rows predating the
    breakdown can still produce one — see _SOURCE_COLUMN — so callers keep
    their clamps.)

    The cutover-scoped pool figures are still returned under their global_*
    names for existing consumers, but no longer drive any column: they were
    needed to make untagged legacy stock safe to reason about, and untagged
    stock is now excluded outright.
    """
    bp_main = _stock_qty(item_code, warehouse, project, batch_planning, "BP", in_main=True)

    bp_allocated = _allocated_qty(
        item_code,
        project,
        batch_planning=batch_planning,
        exclude_parent=exclude_parent,
        for_update=for_update,
    )
    bp_local_allocated = _allocated_qty(
        item_code,
        project,
        batch_planning=batch_planning,
        exclude_parent=exclude_parent,
        for_update=for_update,
        source="local",
    )
    bp_global_allocated = _allocated_qty(
        item_code,
        project,
        batch_planning=batch_planning,
        exclude_parent=exclude_parent,
        for_update=for_update,
        source="global",
    )

    other_main = _stock_qty(
        item_code, warehouse, project, batch_planning, "GEN", in_main=True
    )

    bp_main, other_main = settle_cross_batch_draw(bp_main, other_main)
    other_local_allocated = _allocated_qty(
        item_code,
        project,
        employee_function=employee_function,
        exclude_batch_planning=batch_planning,
        exclude_parent=exclude_parent,
        for_update=for_update,
        source="local",
    )
    other_global_allocated = _allocated_qty(
        item_code,
        project,
        employee_function=employee_function,
        exclude_batch_planning=batch_planning,
        exclude_parent=exclude_parent,
        for_update=for_update,
        source="global",
    )
    other_allocated = other_local_allocated + other_global_allocated

    pools = free_pools(
        bp_main,
        bp_local_allocated,
        bp_global_allocated,
        other_main,
        other_local_allocated,
        other_global_allocated,
    )
    bp_free = pools["bp_free"]
    other_free = pools["other_free"]

    global_main = _global_main_stock(
        item_code, warehouse, employee_function, project, cutover
    )
    global_allocated = _allocated_qty(
        item_code,
        project,
        employee_function=employee_function,
        exclude_parent=exclude_parent,
        for_update=for_update,
    )

    return {
        "bp_main_stock": bp_main,
        "bp_allocated": bp_allocated,
        "bp_local_allocated": bp_local_allocated,
        "bp_global_allocated": bp_global_allocated,
        "bp_free_stock": bp_free,
        "other_main_stock": other_main,
        "other_allocated": other_allocated,
        "other_local_allocated": other_local_allocated,
        "other_global_allocated": other_global_allocated,
        "other_allocated_total": other_local_allocated + bp_global_allocated,
        "other_free_stock": other_free,
        "total_free_stock": max(bp_free, 0.0) + max(other_free, 0.0),
        "global_main_stock": global_main,
        "global_allocated": global_allocated,
        "global_free_stock": (
            None if global_main is None else global_main - global_allocated
        ),
    }


@frappe.whitelist()
def get_material_planning_data(doc_name):
    doc = frappe.get_doc("Batch Planning", doc_name)
    employee_function = doc.custom_employee_function
    if not employee_function:
        frappe.throw("Employee Function is not set on this document.")

    ef_doc = frappe.get_doc("Employee Function", employee_function)

    warehouse = None
    for r in (ef_doc.table_bukm or []):
        if r.store_warehouse:
            warehouse = r.store_warehouse
            break

    if not warehouse:
        frappe.throw(f"No store warehouse found in Employee Function '{employee_function}'.")



    batches_data = []
    for row in doc.custom_batch_details or []:
        if not row.bom_list or not row.batch_planning_id:
            continue
        
        batch_key = f"{doc.name}-{row.idx}"
        bom_store = frappe.db.get_value(
            "Batch BOM Store after Edit", {"batch_id": batch_key}, "name"
        )
        
        use_store = False
        components = []
        if bom_store:
            store_doc = frappe.get_doc("Batch BOM Store after Edit", bom_store)
            components = store_doc.bom_components or []
            use_store = True
        else:
            bom = frappe.get_doc("BOM", row.bom_list)
            components = bom.exploded_items or bom.items or []
            
        batch_items = {}
        for comp in components:
            item_code = comp.item_code
            qty = flt(
                comp.qty if use_store
                else (comp.qty_consumed_per_unit or comp.stock_qty or comp.qty)
            )
            batch_items[item_code] = batch_items.get(item_code, 0.0) + qty
            
        batches_data.append({
            "batch_planning_id": row.batch_planning_id,
            "items": batch_items
        })

    consolidated_items = get_consolidated_bom_components(doc_name)

    res = []
    curr_today = frappe.utils.today()

    cutover = get_stock_cutover_datetime()

    for item in consolidated_items:
        item_code = item.get("item_code")
        qty_required = flt(item.get("qty"))

        figures = free_stock_figures(
            item_code, warehouse, employee_function, doc.project, doc.name, cutover
        )
        bp_main_stock = figures["bp_main_stock"]
        global_main_stock = figures["global_main_stock"]
        gen_main_stock = figures["other_main_stock"]

        lab_stock = _stock_qty(
            item_code, warehouse, doc.project, doc.name, "BP", in_main=False
        )

        bp_total_stock = bp_main_stock + lab_stock
        gen_total_stock = gen_main_stock
        global_total_stock = gen_main_stock

        allocated_qty = figures["bp_allocated"]
        global_allocated = figures["other_allocated_total"]
        bp_global_allocated = figures["bp_global_allocated"]
        bp_local_allocated = figures["bp_local_allocated"]

        global_free_stock = figures["other_free_stock"]
        bp_free_stock = figures["bp_free_stock"]
        total_free_stock = figures["total_free_stock"]
        cutoff_date = getdate(add_months(frappe.utils.today(), 3))
        batch_rows = frappe.db.sql(
            """
            SELECT b.expiry_date, IFNULL(SUM(sbe.qty), 0) AS qty
            FROM `tabStock Ledger Entry` sle
            INNER JOIN `tabSerial and Batch Entry` sbe
                ON sbe.parent = sle.serial_and_batch_bundle
            INNER JOIN `tabBatch` b ON b.name = sbe.batch_no
            WHERE sle.item_code = %s
              AND sle.batch_planning_id = %s
              AND sle.project = %s
              AND sle.is_cancelled = 0
              AND sle.serial_and_batch_bundle IS NOT NULL
            GROUP BY b.expiry_date

            UNION ALL

            SELECT b.expiry_date, IFNULL(SUM(sle.actual_qty), 0) AS qty
            FROM `tabStock Ledger Entry` sle
            INNER JOIN `tabBatch` b ON b.name = sle.batch_no
            WHERE sle.item_code = %s
              AND sle.batch_planning_id = %s
              AND sle.project = %s
              AND sle.is_cancelled = 0
              AND sle.batch_no IS NOT NULL
            GROUP BY b.expiry_date
            ORDER BY expiry_date ASC
            """,
            (item_code, doc.name, doc.project, item_code, doc.name, doc.project),
            as_dict=True,
        )
        usable_qty = 0.0
        expired_qty = 0.0
        for row in batch_rows:
            qty = flt(row.qty)
            if not row.expiry_date or getdate(row.expiry_date) < cutoff_date:
                expired_qty += qty
            else:
                usable_qty += qty

        gen_mr_qty, gen_mr_count, gen_mr_docs = _open_mr(
            item_code, employee_function, doc.project, doc.name, "GEN"
        )
        bp_mr_qty, bp_mr_count, bp_mr_docs = _open_mr(
            item_code, employee_function, doc.project, doc.name, "BP"
        )

        gen_po_qty, gen_po_count, gen_po_docs = _open_po(
            item_code, employee_function, doc.project, doc.name, "GEN"
        )
        bp_po_qty, bp_po_count, bp_po_docs = _open_po(
            item_code, employee_function, doc.project, doc.name, "BP"
        )

        gen_pr_qty, gen_pr_count, gen_pr_docs = _open_pr_grn(
            item_code, employee_function, doc.project, doc.name, "GEN"
        )
        bp_pr_qty, bp_pr_count, bp_pr_docs = _open_pr_grn(
            item_code, employee_function, doc.project, doc.name, "BP"
        )

        # BOTH allocation sources are credited, by explicit business decision:
        # an allocation locks material to this batch, and locked material is not
        # to be purchased again. The two terms are NOT symmetric, though, and the
        # difference matters when reading this figure:
        #
        #   bp_global_allocated  borrowed from other batches' tagged stock. Those
        #                        units are outside bp_main_stock, so this term is
        #                        the only place they are credited.
        #
        #   bp_local_allocated   drawn from this batch's OWN free stock, so these
        #                        units are already inside bp_main_stock and are
        #                        credited a second time here. Deliberate. On any
        #                        row where bp_main_stock < qty_required, Net Req
        #                        therefore reads low by the locally-allocated qty
        #                        and under-states what must be bought.
        #
        # Anyone reconciling a purchase shortfall against this column should
        # start here: subtract Allocated's local line back out to get the
        # physically-backed requirement.
        net_requirement = max(
            qty_required
            - bp_main_stock
            - lab_stock
            - bp_global_allocated
            - bp_local_allocated
            - bp_mr_qty
            - bp_po_qty,
            0.0,
        )

        res.append(
            {
                "item_code": item_code,
                "item_name": item.get("item_name"),
                "uom": item.get("uom"),
                "qty_required": round(qty_required, 2),
                "gen_total_stock": round(gen_total_stock, 2),
                "bp_total_stock": round(bp_total_stock, 2),
                "gen_main_stock": round(gen_main_stock, 2),
                "bp_main_stock": round(bp_main_stock, 2),
                "global_main_stock": (
                    None if global_main_stock is None else round(global_main_stock, 2)
                ),
                "global_total_stock": round(global_total_stock, 2),
                "total_free_stock": round(total_free_stock, 2),
                "lab_stock": round(lab_stock, 2),
                "allocated_qty": round(allocated_qty, 2),
                "local_allocated_qty": round(bp_local_allocated, 2),
                "global_allocated_qty": round(bp_global_allocated, 2),
                "bp_free_stock": round(bp_free_stock, 2),
                "global_allocated": round(global_allocated, 2),
                "global_free_stock": (
                    None if global_free_stock is None else round(global_free_stock, 2)
                ),
                "main_stock": round(bp_main_stock, 2),
                "total_stock": round(bp_total_stock, 2),
                "free_stock": round(total_free_stock, 2),
                "stock_available": round(total_free_stock, 2),
                "gen_mr_qty": gen_mr_qty,
                "gen_mr_count": gen_mr_count,
                "gen_mr_docs": gen_mr_docs,
                "bp_mr_qty": bp_mr_qty,
                "bp_mr_count": bp_mr_count,
                "bp_mr_docs": bp_mr_docs,
                "gen_po_qty": gen_po_qty,
                "gen_po_count": gen_po_count,
                "gen_po_docs": gen_po_docs,
                "bp_po_qty": bp_po_qty,
                "bp_po_count": bp_po_count,
                "bp_po_docs": bp_po_docs,
                "gen_pr_qty": gen_pr_qty,
                "gen_pr_count": gen_pr_count,
                "gen_pr_docs": gen_pr_docs,
                "bp_pr_qty": bp_pr_qty,
                "bp_pr_count": bp_pr_count,
                "bp_pr_docs": bp_pr_docs,
                "net_requirement": round(net_requirement, 2),
                "usable_qty": round(usable_qty, 2),
                "expired_qty": round(flt(expired_qty), 2),
            }
        )

    return {
        "results": res,
        "warehouse": warehouse,
        "cutover_datetime": str(cutover) if cutover else None,
        "free_qty_pending": not cutover,
    }

@frappe.whitelist()
def make_material_request(doc_name):
    """Create a consolidated Material Request from Batch Planning, with warehouse auto-filled for each item."""
    doc = frappe.get_doc("Batch Planning", doc_name)
    if not doc.custom_employee_function:
        frappe.throw("Employee Function is not set on this Batch Planning.")
    ef_doc = frappe.get_doc("Employee Function", doc.custom_employee_function)
    warehouse = None
    for r in (ef_doc.table_bukm or []):
        if r.store_warehouse:
            warehouse = r.store_warehouse
            break
    if not warehouse:
        frappe.throw(f"No store warehouse found in Employee Function '{doc.custom_employee_function}'.")
    items = get_consolidated_bom_components(doc_name)
    if not items:
        frappe.throw("No items found to create Material Request.")
    mr = frappe.new_doc("Material Request")
    mr.material_request_type = "Purchase"
    mr.custom_employee_function = doc.custom_employee_function
    mr.project = doc.project
    mr.custom_batch_planning_no = doc.name
    mr.flags.ignore_permissions = True
    for comp in items:
        row = mr.append("items", {})
        row.item_code = comp.get("item_code")
        row.qty = comp.get("qty")
        row.uom = comp.get("uom")
        row.warehouse = warehouse
        row.conversion_factor = 1
        row.batch_planning_id = doc.name
    mr.insert(ignore_permissions=True)
    frappe.db.commit()
    return mr.name

@frappe.whitelist()
def temp_db_fix():
    frappe.db.sql("DROP TABLE IF EXISTS `tabBatch Planning`")
    frappe.db.sql("RENAME TABLE `tabBatch Creation` TO `tabBatch Planning`")
    columns = [c[0] for c in frappe.db.sql("DESC `tabBatches Planned`")]
    if "batch_creation" in columns and "batch_planning" not in columns:
        frappe.db.sql("ALTER TABLE `tabBatches Planned` CHANGE COLUMN `batch_creation` `batch_planning` VARCHAR(255)")
    
    return {
        "status": "Fix completed successfully!",
        "tabBatch Planning count": frappe.db.sql("select count(*) from `tabBatch Planning`")[0][0],
        "tabBatches Planned columns": [c[0] for c in frappe.db.sql("DESC `tabBatches Planned`")]
    }

@frappe.whitelist()
def get_batch_wise_shortages(doc_name):
    doc = frappe.get_doc("Batch Planning", doc_name)
    
    item_requirements = {}
    for row in doc.custom_batch_details or []:
        if not row.bom_list or not row.batch_planning_id:
            continue

        batch_key = f"{doc.name}-{row.idx}"
        bom_store = frappe.db.get_value(
            "Batch BOM Store after Edit", {"batch_id": batch_key}, "name"
        )

        use_store = False
        items = []
        if bom_store:
            store_doc = frappe.get_doc("Batch BOM Store after Edit", bom_store)
            items = store_doc.bom_components or []
            use_store = True
        else:
            bom = frappe.get_doc("BOM", row.bom_list)
            items = bom.exploded_items or bom.items or []

        for item in items:
            item_code = item.item_code
            item_name = item.item_name
            uom = item.uom if use_store else (item.stock_uom or item.uom)
            qty_needed = flt(
                item.qty if use_store
                else (item.qty_consumed_per_unit or item.stock_qty or item.qty)
            )
            
            if item_code not in item_requirements:
                item_requirements[item_code] = {
                    "item_code": item_code,
                    "item_name": item_name,
                    "uom": uom,
                    "qty_needed": 0.0
                }
            item_requirements[item_code]["qty_needed"] += qty_needed

    employee_function = doc.custom_employee_function
    if not employee_function:
        frappe.throw("Employee Function is not set on this document.")

    ef_doc = frappe.get_doc("Employee Function", employee_function)
    warehouse = next(
        (r.store_warehouse for r in (ef_doc.table_bukm or []) if r.store_warehouse),
        None,
    )
    if not warehouse:
        frappe.throw(f"No store warehouse found in Employee Function '{employee_function}'.")

    shortages = []
    for item_code, req in item_requirements.items():
        main_stock = flt(frappe.db.sql(
            """
            SELECT IFNULL(SUM(actual_qty), 0)
            FROM `tabStock Ledger Entry`
            WHERE item_code = %s
            AND warehouse = %s
            AND batch_planning_id = %s
            AND project = %s
            AND is_cancelled = 0
            """,
            (item_code, warehouse, doc.name, doc.project)
        )[0][0] or 0.0)

        bp_mr_qty, _mr_count, _mr_docs = _open_mr(
            item_code, employee_function, doc.project, doc.name, "BP"
        )
        bp_po_qty, _po_count, _po_docs = _open_po(
            item_code, employee_function, doc.project, doc.name, "BP"
        )

        shortage_qty = req["qty_needed"] - main_stock - bp_mr_qty - bp_po_qty

        if shortage_qty <= 0:
            continue

        shortages.append({
            "item_code": item_code,
            "item_name": req["item_name"],
            "qty": round(shortage_qty, 4),
            "uom": req["uom"],
            "custom_batch_planning_no": doc.name,
            "schedule_date": frappe.utils.add_days(frappe.utils.today(), 1)
        })

    return sorted(shortages, key=lambda x: x["item_code"])

@frappe.whitelist()
def get_project_finished_items(doctype, txt, searchfield, start, page_len, filters):
    project = filters.get("project") if filters else None
    
    if project:
        query = """
            SELECT DISTINCT bom.item
            FROM `tabBOM` bom
            INNER JOIN `tabItem` item ON item.name = bom.item
            WHERE bom.project = %(project)s
              AND bom.docstatus = 1
              AND bom.is_active = 1
              AND item.item_group = 'Finish Goods'
        """
        params = {"project": project}
        if txt:
            query += " AND bom.item LIKE %(txt)s"
            params["txt"] = f"%{txt}%"
        
        query += f" LIMIT {int(start)}, {int(page_len)}"
        return frappe.db.sql(query, params, as_dict=False)
    else:
        query = """
            SELECT name
            FROM `tabItem`
            WHERE item_group = 'Finish Goods'
              AND disabled = 0
        """
        params = {}
        if txt:
            query += " AND (name LIKE %(txt)s OR item_name LIKE %(txt)s)"
            params["txt"] = f"%{txt}%"
            
        query += f" LIMIT {int(start)}, {int(page_len)}"
        return frappe.db.sql(query, params, as_dict=False)

@frappe.whitelist()
def get_stock_entry_items(batch_planning):
    """
    Fetches all Stock Entries linked to the batch planning ID,
    extracts the underlying child items, and returns a merged list with combined quantities.
    """
    entries = frappe.get_all(
        "Stock Entry",
        filters={"custom_batch_planning_no": batch_planning},
        fields=["name"],
    )

    merged = {}
    for se in entries:
        items = frappe.get_all(
            "Stock Entry Detail",
            filters={"parent": se.name},
            fields=["item_code", "item_name", "qty", "uom", "s_warehouse", "t_warehouse"],
            ignore_permissions=True,
        )
        for item in items:
            if not item.item_code:
                continue
            if item.item_code in merged:
                merged[item.item_code]["qty"] += item.qty
            else:
                merged[item.item_code] = dict(item)

    return list(merged.values())

@frappe.whitelist()
def get_item_issue_data(batch_planning):
    """
    Fetches all SUBMITTED Stock Entries linked to this Batch Planning,
    extracts child items, and returns a merged/deduplicated list.
    Duplicate items are merged by item_code with quantities summed.
    """
    entries = frappe.get_all(
        "Stock Entry",
        filters={
            "custom_batch_planning_no": batch_planning,
            "docstatus": 1,
        },
        fields=["name"],
    )

    if not entries:
        return []

    se_names = [e.name for e in entries]
    items = frappe.db.sql(
        """
        SELECT
            sed.item_code,
            sed.item_name,
            sed.qty,
            sed.uom,
            sed.s_warehouse,
            sed.t_warehouse
        FROM `tabStock Entry Detail` sed
        WHERE sed.parent IN %s
        AND sed.item_code IS NOT NULL
        AND sed.item_code != ''
        ORDER BY sed.item_code
        """,
        (se_names,),
        as_dict=True,
    )

    merged = {}
    for item in items:
        code = item.item_code
        if code in merged:
            merged[code]["qty"] = flt(merged[code]["qty"]) + flt(item.qty)
        else:
            merged[code] = {
                "item_code": item.item_code,
                "item_name": item.item_name,
                "qty": flt(item.qty),
                "uom": item.uom,
                "s_warehouse": item.s_warehouse,
                "t_warehouse": item.t_warehouse,
            }

    result = []
    for item in merged.values():
        item["qty"] = round(item["qty"], 3)
        result.append(item)

    return result

@frappe.whitelist()
def on_stock_entry_submit(doc, method):
    """
    When Stock Entry is submitted:
    1. Add one row to stock_entry_log child table on Batch Planning
    2. Add one row per item to item_issue_log child table on Batch Planning, aggregating qty for same item
    """
    batch_planning = doc.get("custom_batch_planning_no") or doc.get("custom_batch_planning")
    if not batch_planning:
        return

    if not frappe.db.exists("Batch Planning", batch_planning):
        return

    bp = frappe.get_doc("Batch Planning", batch_planning)

    existing_se = [r.stock_entry for r in (bp.stock_entry_log or [])]
    if doc.name in existing_se:
        return

    bp.append("stock_entry_log", {
        "stock_entry": doc.name,
        "date": doc.posting_date,
        "from_warehouse": doc.from_warehouse,
        "to_warehouse": doc.to_warehouse,
        "status": "Submitted"
    })

    existing_items = {}
    for r in (bp.item_issue_log or []):
        existing_items[r.item_code] = r

    for item in doc.items:
        if not item.item_code:
            continue
            
        if item.item_code in existing_items:
            existing_row = existing_items[item.item_code]
            existing_row.qty += item.qty
            if doc.name not in (existing_row.stock_entry or ""):
                existing_row.stock_entry = f"{existing_row.stock_entry}, {doc.name}" if existing_row.stock_entry else doc.name
        else:
            new_row = bp.append("item_issue_log", {
                "item_code": item.item_code,
                "item_name": item.item_name,
                "qty": item.qty,
                "uom": item.uom,
                "from_warehouse": item.s_warehouse,
                "to_warehouse": item.t_warehouse,
                "stock_entry": doc.name
            })
            existing_items[item.item_code] = new_row

    bp.flags.ignore_permissions = True
    bp.flags.ignore_validate_update_after_submit = True
    bp.save(ignore_permissions=True)
    frappe.db.commit()


def sync_batch_expiry_from_grn(doc, method):
    """
    After GRN submit, force Batch.expiry_date to match the supplier-provided
    expiry date entered on the PR Item, overriding any auto-calculated
    shelf-life-based date.
    """
    for item in doc.items:
        supplier_expiry = item.get("custom_supplier_expiry_date")
        if not supplier_expiry:
            continue

        batch_no = item.get("batch_no")
        if not batch_no and item.get("serial_and_batch_bundle"):
            batch_no = frappe.db.get_value(
                "Serial and Batch Entry",
                {"parent": item.get("serial_and_batch_bundle")},
                "batch_no"
            )

        if not batch_no:
            continue

        current_expiry = frappe.db.get_value("Batch", batch_no, "expiry_date")
        if current_expiry != getdate(supplier_expiry):
            frappe.db.set_value("Batch", batch_no, "expiry_date", supplier_expiry, update_modified=False)
