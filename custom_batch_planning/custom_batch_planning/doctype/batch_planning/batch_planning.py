import json
import frappe
from frappe.model.document import Document
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
                {
                    "slot_opening": self.slot_opening,
                    "name": ["!=", self.name],
                    "docstatus": ["!=", 2],
                },
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
                continue
            if not (row.slot_opening_id and row.batch_type):
                frappe.throw(
                    f"Row {row.idx}: cannot generate a Batch Planning ID without both "
                    f"Slot Opening and Batch Type. Please set them and save again."
                )
            assigned = [
                r.batch_planning_id
                for r in self.custom_batch_details
                if r.batch_planning_id and r is not row
            ]
            row.batch_planning_id = get_next_batch_counter(
                row.slot_opening_id, row.batch_type, json.dumps(assigned)
            )

        for row in self.custom_batch_details or []:
            if row.batch_planning_id:
                existing_bc = frappe.db.get_value(
                    "Batches Planned",
                    {"batch_planning_id": row.batch_planning_id},
                    "batch_planning",
                )
                if (
                    existing_bc
                    and existing_bc != self.name
                    and not _is_superseded(existing_bc)
                ):
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
            if not row.bom_list:
                frappe.throw(
                    f"Row {row.idx}: Bom List is required. A batch cannot be planned "
                    f"without a BOM."
                )

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

    def _batches_planned_values(self, row):
        return {
            "batch_planning_id": row.batch_planning_id,
            "slot_opening_id": row.slot_opening_id,
            "project": frappe.db.get_value(
                "Slot Opening", row.slot_opening_id, "project"
            ) if row.slot_opening_id else None,
            "employee_function": self.custom_employee_function,
            "employee_name": self.custom_employee_headname,
            "month": self.month,
            "batch_type": row.batch_type,
            "finished_item": row.finished_item,
            "slot_booking_date": row.slot_booking_date,
            "batch_planning": self.name,
        }

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
                if existing.batch_planning and not _is_superseded(
                    existing.batch_planning
                ):
                    frappe.throw(
                        f"⚠️ Batch Planning ID <b>{row.batch_planning_id}</b> "
                        f"already exists under <b>{existing.batch_planning}</b>."
                    )

                values = self._batches_planned_values(row)
                values.update(_batches_planned_status(self.workflow_state))
                frappe.db.set_value(
                    "Batches Planned",
                    existing.name,
                    values,
                    update_modified=False,
                )
                count += 1
                continue

            bp = frappe.new_doc("Batches Planned")
            bp.update(self._batches_planned_values(row))

            bp.flags.ignore_permissions = True
            bp.flags.ignore_validate = True
            bp.flags.ignore_mandatory = True
            bp.flags.ignore_workflow = True

            bp.insert(ignore_permissions=True, ignore_mandatory=True)

            _update_sct_batches_planned(
                row.slot_opening_id, row.slot_booking_date, +1
            )

            frappe.db.set_value(
                "Batches Planned",
                bp.name,
                _batches_planned_status(self.workflow_state),
                update_modified=False,
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
    lab_covered = []
    already_covered = []

    already = _bp_allocated_by_item(batch_planning_name)

    for item in consolidated_items:
        item_code = item["item_code"]
        qty_required = flt(item["qty"])
        allocated_already = already.get(item_code, 0.0)

        lab_stock = _stock_qty(
            item_code,
            warehouse,
            parent_doc.project,
            batch_planning_name,
            "BP",
            in_main=False,
        )
        outstanding = max(qty_required - lab_stock - allocated_already, 0.0)
        if outstanding <= 0:
            if allocated_already > 0:
                already_covered.append(item_code)
            else:
                lab_covered.append(item_code)
            continue

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

        allocate_qty = min(outstanding, cap)
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

        row = {
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
        }
        ma_data["material_allocation"].append(row)

    if not ma_data["material_allocation"]:
        if already_covered and not lab_covered:
            frappe.throw(
                "Nothing to allocate — every item on this Batch Planning is already "
                "fully allocated. Deallocate an existing Material Allocation first "
                "if you need to reissue it."
            )
        if lab_covered or already_covered:
            frappe.throw(
                "Nothing to allocate — every item on this Batch Planning is already "
                "covered, either by stock this batch holds in the lab or by an "
                "existing allocation."
            )
        frappe.throw(
            "No free stock is available for any item on this Batch Planning — "
            "neither its own nor the shared pool. Material Allocation cannot be created."
        )

    ma_data["shared_stock_rows"] = shared_rows
    ma_data["shared_stock_required"] = round(shared_total, 2)

    if warning_message:
        ma_data["warning"] = warning_message
    return ma_data

def _bp_allocated_by_item(batch_planning, exclude_parent=None):
    exclude_sql = "AND ma.name <> %(exclude)s" if exclude_parent else ""
    rows = frappe.db.sql(
        f"""
        SELECT mai.item_code AS item_code,
               IFNULL(SUM(mai.allocate_qty), 0) AS qty
        FROM `tabMaterial Allocation Item` mai
        INNER JOIN `tabMaterial Allocation` ma ON ma.name = mai.parent
        WHERE ma.batch_planning = %(bp)s
          {exclude_sql}
          AND ma.docstatus <> 2
          AND ma.allocation_status NOT IN ('Deallocated', 'Stock Entry Done')
        GROUP BY mai.item_code
        """,
        {"bp": batch_planning, "exclude": exclude_parent},
        as_dict=True,
    )
    return {r.item_code: flt(r.qty) for r in rows}


def _is_superseded(batch_planning):
    if not batch_planning:
        return False

    return frappe.db.get_value("Batch Planning", batch_planning, "docstatus") == 2

def _batches_planned_status(batch_planning_state):
    state = batch_planning_state or "Approved"

    workflow_name = frappe.db.get_value(
        "Workflow", {"document_type": "Batches Planned", "is_active": 1}, "name"
    )
    docstatus = frappe.db.get_value(
        "Workflow Document State",
        {"parent": workflow_name, "state": state},
        "doc_status",
    ) if workflow_name else None

    if docstatus is None:
        state = "Approved"
        docstatus = 1

    values = {"workflow_state": state}
    if int(docstatus):
        values["docstatus"] = int(docstatus)

    return values

def get_default_workflow_state(doctype, docstatus):
    workflow_name = frappe.db.get_value(
        "Workflow", {"document_type": doctype, "is_active": 1}, "name"
    )
    if not workflow_name:
        return None

    return frappe.db.get_value(
        "Workflow Document State",
        {"parent": workflow_name, "doc_status": str(docstatus)},
        "state",
        order_by="idx asc",
    )

@frappe.whitelist()
def create_batches_planned(doc_name):
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

def get_bom_store_name(batch_key):
    if not batch_key:
        return None

    names = frappe.get_all(
        "Batch BOM Store after Edit",
        filters={"batch_id": batch_key},
        pluck="name",
        order_by="modified desc",
        limit=1,
    )
    return names[0] if names else None

@frappe.whitelist()
def get_batch_bom_store(batch_key):
    name = get_bom_store_name(batch_key)
    if not name:
        return None

    doc = frappe.get_doc("Batch BOM Store after Edit", name)
    return {
        "name": doc.name,
        "bom_name": doc.bom_name,
        "bom_components": [
            {
                "item_code": row.item_code,
                "item_name": row.item_name,
                "uom": row.uom,
                "qty": flt(row.qty),
            }
            for row in (doc.bom_components or [])
        ],
    }

@frappe.whitelist()
def save_batch_bom_store(batch_key, bom_name, components):
    if isinstance(components, str):
        components = json.loads(components)

    if not batch_key:
        frappe.throw("Cannot store BOM edits without a batch key.")
    if not components:
        frappe.throw("BOM must have at least one item.")

    names = frappe.get_all(
        "Batch BOM Store after Edit",
        filters={"batch_id": batch_key},
        pluck="name",
        order_by="modified desc",
    )

    for stale in names[1:]:
        frappe.delete_doc(
            "Batch BOM Store after Edit", stale, ignore_permissions=True, force=True
        )

    if names:
        doc = frappe.get_doc("Batch BOM Store after Edit", names[0])
    else:
        doc = frappe.new_doc("Batch BOM Store after Edit")
        doc.batch_id = batch_key

    doc.bom_name = bom_name
    doc.set("bom_components", [])
    for item in components:
        doc.append(
            "bom_components",
            {
                "item_code": item.get("item_code"),
                "item_name": item.get("item_name"),
                "uom": item.get("uom"),
                "qty": flt(item.get("qty")),
            },
        )

    doc.save(ignore_permissions=True)
    return doc.name

@frappe.whitelist()
def rekey_batch_bom_store(local_name, doc_name):
    if not local_name or not doc_name or local_name == doc_name:
        return 0

    rows = frappe.get_all(
        "Batch BOM Store after Edit",
        filters={"batch_id": ["like", f"{local_name}-%"]},
        fields=["name", "batch_id"],
    )

    moved = 0
    for row in rows:
        idx = row.batch_id[len(local_name) + 1 :]
        if not idx.isdigit():
            continue

        new_key = f"{doc_name}-{idx}"
        for clash in frappe.get_all(
            "Batch BOM Store after Edit", filters={"batch_id": new_key}, pluck="name"
        ):
            frappe.delete_doc(
                "Batch BOM Store after Edit", clash, ignore_permissions=True, force=True
            )

        frappe.db.set_value(
            "Batch BOM Store after Edit", row.name, "batch_id", new_key
        )
        moved += 1

    return moved

@frappe.whitelist()
def get_consolidated_bom_components(doc_name, scale="per_unit"):
    doc = frappe.get_doc("Batch Planning", doc_name)
    components = {}

    for row in doc.custom_batch_details or []:
        if not row.bom_list:
            continue
        
        batch_key = f"{doc.name}-{row.idx}"
        bom_store = get_bom_store_name(batch_key)
        
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
            if use_store:
                qty = flt(item.qty)
            elif scale == "stock":
                qty = flt(item.stock_qty or item.qty)
            else:
                qty = flt(item.qty_consumed_per_unit or item.stock_qty or item.qty)
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


def _live_columns(doctype):
    cache = getattr(frappe.local, "_bp_live_columns", None)
    if cache is None:
        cache = {}
        frappe.local._bp_live_columns = cache
    if doctype not in cache:
        try:
            cache[doctype] = set(frappe.db.get_table_columns(doctype))
        except Exception:
            cache[doctype] = set()
    return cache[doctype]


class _SchemaExpr:

    __slots__ = ("_build",)

    def __init__(self, build):
        self._build = build

    def __str__(self):
        return self._build()

    def __format__(self, spec):
        return format(str(self), spec)

    def __contains__(self, needle):
        return needle in str(self)

    def __eq__(self, other):
        return str(self) == other

    def __hash__(self):
        return hash(str(self))

    def __getattr__(self, name):
        return getattr(str(self), name)

    def __repr__(self):
        return "<%s %s>" % (type(self).__name__, self._build())


def _coalesce_existing(*candidates):

    def build():
        parts = [
            "NULLIF(%s.%s,'')" % (alias, field)
            for alias, doctype, field in candidates
            if field in _live_columns(doctype)
        ]
        if not parts:
            return "NULL"
        if len(parts) == 1:
            return parts[0]
        return "COALESCE(" + ", ".join(parts) + ")"

    return _SchemaExpr(build)


def _approved(alias, doctype):

    def build():
        base = "%s.docstatus = 1" % alias
        if "workflow_state" in _live_columns(doctype):
            return base + " AND %s.workflow_state LIKE 'Approve%%%%'" % alias
        return base

    return _SchemaExpr(build)


MR_APPROVED = _approved("mr", "Material Request")
PO_APPROVED = _approved("po", "Purchase Order")
PR_APPROVED = _approved("pr", "Purchase Receipt")

MR_PURCHASE_ONLY = "mr.material_request_type = 'Purchase'"


def _pr_unapproved():

    def build():
        if "workflow_state" not in _live_columns("Purchase Receipt"):
            return "pr.docstatus = 0 AND 1 = 0"
        return (
            "pr.docstatus = 0 "
            "AND (pr.workflow_state IS NULL OR ("
            "pr.workflow_state NOT LIKE 'Approve%%' "
            "AND pr.workflow_state NOT LIKE 'Reject%%' "
            "AND pr.workflow_state NOT LIKE 'Cancel%%'))"
        )

    return _SchemaExpr(build)


PR_UNAPPROVED = _pr_unapproved()

MR_EF = _coalesce_existing(
    ("mri", "Material Request Item", "employee_function"),
    ("mr", "Material Request", "custom_employee_function"),
)
MR_PROJECT = _coalesce_existing(
    ("mri", "Material Request Item", "project"),
    ("mr", "Material Request", "project"),
)
PO_EF = _coalesce_existing(
    ("poi", "Purchase Order Item", "employee_function"),
    ("poi", "Purchase Order Item", "custom_employee_functions"),
    ("po", "Purchase Order", "employee_function"),
    ("po", "Purchase Order", "custom_employee_functions"),
)
PO_PROJECT = _coalesce_existing(
    ("poi", "Purchase Order Item", "project"),
    ("po", "Purchase Order", "project"),
)
PR_EF = _coalesce_existing(
    ("pri", "Purchase Receipt Item", "employee_function"),
    ("pr", "Purchase Receipt", "employee_function"),
)
PR_PROJECT = _coalesce_existing(
    ("pri", "Purchase Receipt Item", "project"),
    ("pr", "Purchase Receipt", "project"),
)

MR_BP = _coalesce_existing(
    ("mri", "Material Request Item", "batch_planning_id"),
    ("mri", "Material Request Item", "custom_batch_planning_no"),
    ("mr", "Material Request", "custom_batch_planning_no"),
)
PO_BP = _coalesce_existing(
    ("poi", "Purchase Order Item", "batch_planning_id"),
    ("poi", "Purchase Order Item", "custom_batch_planning_no"),
    ("po", "Purchase Order", "custom_batch_planning_no"),
)
PR_BP = _coalesce_existing(
    ("pri", "Purchase Receipt Item", "batch_planning_id"),
    ("pr", "Purchase Receipt", "custom_batch_planning_no"),
)


def _project_clause(project_expr, project):
    return f"AND {project_expr} = %(project)s" if project else ""


def _bp_predicate(alias, mode, bp_expr=None):
    if mode == "UNTAGGED":
        expr = bp_expr or f"NULLIF({alias}.batch_planning_id, '')"
        return f"({expr}) IS NULL"
    if mode == "GEN":
        return (
            f"({alias}.batch_planning_id IS NOT NULL "
            f"AND {alias}.batch_planning_id <> '' "
            f"AND {alias}.batch_planning_id <> %(bp)s)"
        )
    return f"{alias}.batch_planning_id = %(bp)s"


def _bucket(rows, count_all=False):
    total = 0.0
    docs = []
    for r in rows:
        qty = flt(r.open_qty)
        total += qty
        if (qty > 0.001 or count_all) and r.doc and r.doc not in docs:
            docs.append(r.doc)
    return round(total, 2), len(docs), docs


def _open_mr(item_code, ef, project, bp, mode):
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
          {_project_clause(MR_PROJECT, project)}
          AND {_bp_predicate('mri', mode, MR_BP)}
        """,
        {"item_code": item_code, "ef": ef, "project": project, "bp": bp},
        as_dict=True,
    )
    return _bucket(rows)


def _open_po(item_code, ef, project, bp, mode):
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
          {_project_clause(PO_PROJECT, project)}
          AND {_bp_predicate('poi', mode, PO_BP)}
        """,
        {"item_code": item_code, "ef": ef, "project": project, "bp": bp},
        as_dict=True,
    )
    return _bucket(rows)


def _open_pr_grn(item_code, ef, project, bp, mode):
    rows = frappe.db.sql(
        f"""
        SELECT pr.name AS doc,
               GREATEST(pri.qty - IFNULL(pri.returned_qty, 0), 0) AS open_qty
        FROM `tabPurchase Receipt Item` pri
        JOIN `tabPurchase Receipt` pr ON pr.name = pri.parent
        WHERE pri.item_code = %(item_code)s
          AND {PR_UNAPPROVED}
          AND {PR_EF} = %(ef)s
          {_project_clause(PR_PROJECT, project)}
          AND {_bp_predicate('pri', mode, PR_BP)}
        """,
        {"item_code": item_code, "ef": ef, "project": project, "bp": bp},
        as_dict=True,
    )
    return _bucket(rows, count_all=True)


def _ef_lab_warehouses(ef_doc):
    return [r.lab_warehouse for r in (ef_doc.table_szrn or []) if r.lab_warehouse]


def _stock_qty(item_code, warehouse, project, bp, mode, in_main=True, warehouses=None):
    if warehouses is not None:
        if not warehouses:
            return 0.0
        wh_clause = "AND sle.warehouse IN %(warehouses)s"
    else:
        wh_clause = f"AND sle.warehouse {'=' if in_main else '<>'} %(warehouse)s"

    return flt(
        frappe.db.sql(
            f"""
        SELECT IFNULL(SUM(sle.actual_qty), 0)
        FROM `tabStock Ledger Entry` sle
        WHERE sle.item_code = %(item_code)s
          {wh_clause}
          {_project_clause('sle.project', project)}
          AND sle.is_cancelled = 0
          AND {_bp_predicate('sle', mode)}
        """,
            {
                "item_code": item_code,
                "warehouse": warehouse,
                "warehouses": tuple(warehouses) if warehouses else None,
                "project": project,
                "bp": bp,
            },
        )[0][0]
        or 0.0
    )


def _global_main_stock(item_code, warehouse, employee_function, project, cutover):
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

_POOL_COLUMN = "COALESCE(NULLIF(mai.source_pool, ''), 'Tagged')"

_HOLDS_STOCK = (
    "ma.allocation_status IN ('Allocated', 'Material Request Done')"
)


def _allocated_qty(
    item_code,
    project,
    batch_planning=None,
    employee_function=None,
    exclude_batch_planning=None,
    exclude_parent=None,
    for_update=False,
    source=None,
    pool="Tagged",
):
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
          AND {_POOL_COLUMN} = %(pool)s
          AND {_HOLDS_STOCK}
          AND ma.docstatus != 2
        {lock_sql}
        """,
            {
                "item_code": item_code,
                "project": project,
                "scope": scope,
                "exclude": exclude_parent,
                "exclude_bp": exclude_batch_planning,
                "pool": pool,
            },
        )[0][0]
        or 0.0
    )


def split_local_first(quantities, local_free, global_free):
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


def split_lab_first(quantities, lab_free, main_free):
    split = split_local_first(quantities, lab_free, main_free)
    return {
        "lab_free": split["local_free"],
        "main_free": split["global_free"],
        "capacity": split["capacity"],
        "requested": split["requested"],
        "shortfall": split["shortfall"],
        "rows": [
            {"from_lab": r["from_local"], "from_main": r["from_global"]}
            for r in split["rows"]
        ],
    }


def untagged_free_figures(
    item_code,
    warehouse,
    lab_warehouses,
    employee_function,
    batch_planning,
    exclude_parent=None,
    for_update=False,
):
    main_stock = _stock_qty(
        item_code, warehouse, None, None, "UNTAGGED", in_main=True
    )
    lab_stock = _stock_qty(
        item_code, warehouse, None, None, "UNTAGGED", warehouses=lab_warehouses
    )

    lab_allocated = _untagged_allocated_qty(
        item_code, employee_function, batch_planning, "global",
        component="lab", exclude_parent=exclude_parent, for_update=for_update,
    )
    main_allocated = _untagged_allocated_qty(
        item_code, employee_function, batch_planning, "global",
        component="main", exclude_parent=exclude_parent, for_update=for_update,
    )

    return {
        "main_stock": main_stock,
        "lab_stock": lab_stock,
        "lab_allocated": lab_allocated,
        "main_allocated": main_allocated,
        "lab_free": lab_stock - lab_allocated,
        "main_free": main_stock - main_allocated,
    }


def settle_cross_batch_draw(bp_main, other_main):
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
        bom_store = get_bom_store_name(batch_key)
        
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

        bp_untagged_allocated = _untagged_allocated_qty(
            item_code, employee_function, doc.name, "current"
        )

        net_requirement = max(
            qty_required
            - bp_total_stock
            - bp_global_allocated
            - bp_untagged_allocated
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

_UNTAGGED_COLUMN = {
    "total": "mai.allocate_qty",
    "lab": "IFNULL(mai.lab_allocated_qty, 0)",
    "main": "IFNULL(mai.main_allocated_qty, 0)",
}


def _untagged_allocated_qty(
    item_code,
    employee_function,
    batch_planning,
    scope,
    component="total",
    exclude_parent=None,
    for_update=False,
):
    if scope == "current":
        scope_sql = "AND ma.batch_planning = %(scope)s"
        scope_value = batch_planning
    else:
        scope_sql = "AND ma.employee_function = %(scope)s"
        scope_value = employee_function

    exclude_sql = "AND ma.name <> %(exclude)s" if exclude_parent else ""
    lock_sql = "FOR UPDATE" if for_update else ""

    return flt(
        frappe.db.sql(
            f"""
        SELECT IFNULL(SUM({_UNTAGGED_COLUMN[component]}), 0)
        FROM `tabMaterial Allocation Item` mai
        INNER JOIN `tabMaterial Allocation` ma ON ma.name = mai.parent
        WHERE mai.item_code = %(item_code)s
          {scope_sql}
          {exclude_sql}
          AND {_POOL_COLUMN} = 'Untagged'
          AND {_HOLDS_STOCK}
          AND ma.docstatus != 2
        {lock_sql}
        """,
            {
                "item_code": item_code,
                "scope": scope_value,
                "exclude": exclude_parent,
            },
        )[0][0]
        or 0.0
    )


@frappe.whitelist()
def get_untagged_material_data(doc_name):
    doc = frappe.get_doc("Batch Planning", doc_name)
    employee_function = doc.custom_employee_function
    if not employee_function:
        frappe.throw("Employee Function is not set on this document.")

    ef_doc = frappe.get_doc("Employee Function", employee_function)
    warehouse = next(
        (r.store_warehouse for r in (ef_doc.table_bukm or []) if r.store_warehouse),
        None,
    )
    if not warehouse:
        frappe.throw(
            f"No store warehouse found in Employee Function '{employee_function}'."
        )

    lab_warehouses = _ef_lab_warehouses(ef_doc)

    consolidated_items = get_consolidated_bom_components(doc_name, scale="stock")

    res = []
    for item in consolidated_items:
        item_code = item.get("item_code")
        qty_required = flt(item.get("qty"))

        main_stock = _stock_qty(
            item_code, warehouse, None, None, "UNTAGGED", in_main=True
        )
        lab_stock = _stock_qty(
            item_code, warehouse, None, None, "UNTAGGED", warehouses=lab_warehouses
        )

        global_allocated = _untagged_allocated_qty(
            item_code, employee_function, doc.name, "global"
        )
        current_allocated = _untagged_allocated_qty(
            item_code, employee_function, doc.name, "current"
        )

        current_main_allocated = _untagged_allocated_qty(
            item_code, employee_function, doc.name, "current", component="main"
        )

        lab_allocated = _untagged_allocated_qty(
            item_code, employee_function, doc.name, "global", component="lab"
        )

        current_lab_allocated = _untagged_allocated_qty(
            item_code, employee_function, doc.name, "current", component="lab"
        )

        global_main_allocated = global_allocated - lab_allocated

        total_stock = main_stock + lab_stock

        lab_available = lab_stock - lab_allocated

        free_qty = total_stock - global_allocated

        lab_after_alloc = lab_stock + current_main_allocated

        mr_qty, mr_count, mr_docs = _open_mr(
            item_code, employee_function, None, None, "UNTAGGED"
        )
        po_qty, po_count, po_docs = _open_po(
            item_code, employee_function, None, None, "UNTAGGED"
        )
        pr_qty, pr_count, pr_docs = _open_pr_grn(
            item_code, employee_function, None, None, "UNTAGGED"
        )

        net_requirement = max(
            qty_required - free_qty - mr_qty - po_qty, 0.0
        )

        res.append(
            {
                "item_code": item_code,
                "item_name": item.get("item_name"),
                "uom": item.get("uom"),
                "qty_required": round(qty_required, 2),
                "total_stock": round(total_stock, 2),
                "main_stock": round(main_stock, 2),
                "global_allocated": round(global_allocated, 2),
                "current_allocated": round(current_allocated, 2),
                "current_main_allocated": round(current_main_allocated, 2),
                "global_main_allocated": round(global_main_allocated, 2),
                "free_qty": round(free_qty, 2),
                "lab_stock": round(lab_stock, 2),
                "lab_available": round(lab_available, 2),
                "labware_qty": round(current_lab_allocated, 2),
                "lab_allocated_global": round(lab_allocated, 2),
                "lab_after_alloc": round(lab_after_alloc, 2),
                "mr_qty": mr_qty,
                "mr_count": mr_count,
                "mr_docs": mr_docs,
                "po_qty": po_qty,
                "po_count": po_count,
                "po_docs": po_docs,
                "pr_qty": pr_qty,
                "pr_count": pr_count,
                "pr_docs": pr_docs,
                "net_requirement": round(net_requirement, 2),
            }
        )

    return {
        "results": res,
        "warehouse": warehouse,
        "employee_function": employee_function,
        "allocation_pending": False,
    }


@frappe.whitelist()
def create_untagged_material_allocation(batch_planning_name):
    parent_doc = frappe.get_doc("Batch Planning", batch_planning_name)
    if getattr(parent_doc, "workflow_state", None) != "Approved":
        frappe.throw("Document is not in Approved state.")
    if parent_doc.docstatus != 1:
        frappe.throw("Document is not submitted yet.")

    if not parent_doc.custom_employee_function:
        frappe.throw("Employee Function is not set on Batch Planning.")

    employee_function = parent_doc.custom_employee_function
    ef_doc = frappe.get_doc("Employee Function", employee_function)
    warehouse = next(
        (r.store_warehouse for r in (ef_doc.table_bukm or []) if r.store_warehouse),
        None,
    )
    if not warehouse:
        frappe.throw(
            f"No store warehouse found for Employee Function {employee_function}"
        )

    lab_warehouses = _ef_lab_warehouses(ef_doc)

    warning_message = ""
    existing = frappe.db.sql(
        """
        SELECT DISTINCT ma.name
        FROM `tabMaterial Allocation` ma
        INNER JOIN `tabMaterial Allocation Item` mai ON mai.parent = ma.name
        WHERE ma.batch_planning = %(bp)s
          AND COALESCE(NULLIF(mai.source_pool, ''), 'Tagged') = 'Untagged'
          AND ma.allocation_status <> 'Deallocated'
          AND ma.docstatus <> 2
        LIMIT 1
        """,
        {"bp": batch_planning_name},
    )
    if existing:
        warning_message = (
            f"Note: an untagged Material Allocation ({existing[0][0]}) already "
            f"exists for Batch Planning {batch_planning_name}. Anything it holds "
            f"is already out of the pool below."
        )

    report = get_untagged_material_data(batch_planning_name)

    rows = []
    skipped_no_stock = []
    skipped_below_one = []
    skipped_already_allocated = []
    skipped_lab_covered = []

    already = _bp_allocated_by_item(batch_planning_name)

    for item in report["results"]:
        item_code = item["item_code"]
        qty_required = flt(item["qty_required"])
        if qty_required <= 0:
            continue

        if flt(item["free_qty"]) <= 0:
            skipped_no_stock.append(item_code)
            continue

        bp_lab_stock = _stock_qty(
            item_code,
            warehouse,
            parent_doc.project,
            batch_planning_name,
            "BP",
            in_main=False,
        )
        reserved = already.get(item_code, 0.0)

        outstanding = qty_required - bp_lab_stock - reserved
        if outstanding <= 0:
            if bp_lab_stock > 0:
                skipped_lab_covered.append(item_code)
            else:
                skipped_already_allocated.append(item_code)
            continue

        figures = untagged_free_figures(
            item_code,
            warehouse,
            lab_warehouses,
            employee_function,
            batch_planning_name,
        )

        lab_free = max(figures["lab_free"], 0.0)
        main_free = max(figures["main_free"], 0.0)
        capacity = lab_free + main_free

        allocate_qty = min(outstanding, capacity)

        if allocate_qty <= 0:
            skipped_no_stock.append(item_code)
            continue
        if allocate_qty < 1:
            skipped_below_one.append(item_code)
            continue

        split = split_lab_first([allocate_qty], lab_free, main_free)

        if split["shortfall"] > 0:
            skipped_no_stock.append(item_code)
            continue

        from_lab = split["rows"][0]["from_lab"]
        from_main = split["rows"][0]["from_main"]

        row = {
            "doctype": "Material Allocation Item",
            "parenttype": "Material Allocation",
            "parentfield": "material_allocation",
            "item_code": item_code,
            "item_name": item.get("item_name"),
            "uom": item.get("uom"),
            "quantity_required": qty_required,
            "allocate_qty": round(allocate_qty, 2),
            "stock_available": round(capacity, 2),
            "source_pool": "Untagged",
            "lab_allocated_qty": round(from_lab, 2),
            "main_allocated_qty": round(from_main, 2),
        }

        rows.append(row)

    if not rows:
        covered = skipped_already_allocated or skipped_lab_covered
        if covered and not (skipped_no_stock or skipped_below_one):
            frappe.throw(
                "Nothing to allocate — every item on this Batch Planning is already "
                "covered, either by stock this batch holds in its lab or by an "
                "existing allocation. Deallocate one first if you need to reissue it."
            )
        if (skipped_no_stock or skipped_below_one or skipped_already_allocated
                or skipped_lab_covered):
            frappe.throw(
                "Nothing to allocate — no untagged stock is free for any item on "
                "this Batch Planning. Check Free Qty on the Untagged Materials tab."
            )
        frappe.throw("No items found to allocate.")

    notes = []
    if warning_message:
        notes.append(warning_message)
    if skipped_no_stock:
        notes.append(
            f"{len(skipped_no_stock)} item(s) skipped — no free untagged stock: "
            + ", ".join(skipped_no_stock[:10])
            + ("..." if len(skipped_no_stock) > 10 else "")
        )
    if skipped_below_one:
        notes.append(
            f"{len(skipped_below_one)} item(s) skipped — under 1 unit free, which "
            "Material Allocation will not accept: "
            + ", ".join(skipped_below_one[:10])
            + ("..." if len(skipped_below_one) > 10 else "")
        )
    if skipped_lab_covered:
        notes.append(
            f"{len(skipped_lab_covered)} item(s) skipped — already delivered into "
            "this batch's lab by a completed allocation: "
            + ", ".join(skipped_lab_covered[:10])
            + ("..." if len(skipped_lab_covered) > 10 else "")
        )
    if skipped_already_allocated:
        notes.append(
            f"{len(skipped_already_allocated)} item(s) skipped — already reserved "
            "in full on this Batch Planning: "
            + ", ".join(skipped_already_allocated[:10])
            + ("..." if len(skipped_already_allocated) > 10 else "")
        )

    ma_data = {
        "doctype": "Material Allocation",
        "batch_planning": batch_planning_name,
        "employee_function": employee_function,
        "project_id": parent_doc.project,
        "project_name": (
            frappe.db.get_value("Project", parent_doc.project, "project_name")
            if parent_doc.project
            else ""
        ),
        "workflow_state": "Draft",
        "material_allocation": rows,
    }
    if notes:
        ma_data["warning"] = "<br>".join(notes)
    return ma_data


@frappe.whitelist()
def get_untagged_lab_breakdown(doc_name, item_code):
    doc = frappe.get_doc("Batch Planning", doc_name)
    employee_function = doc.custom_employee_function
    if not employee_function:
        frappe.throw("Employee Function is not set on this document.")

    ef_doc = frappe.get_doc("Employee Function", employee_function)
    warehouse = next(
        (r.store_warehouse for r in (ef_doc.table_bukm or []) if r.store_warehouse),
        None,
    )
    lab_warehouses = _ef_lab_warehouses(ef_doc)

    rows = []
    for lab in lab_warehouses:
        qty = _stock_qty(
            item_code, warehouse, None, None, "UNTAGGED", warehouses=[lab]
        )
        rows.append({"warehouse": lab, "qty": round(qty, 2)})

    gross = sum(r["qty"] for r in rows)
    allocated = _untagged_allocated_qty(
        item_code, employee_function, doc_name, "global", component="lab"
    )
    return {
        "item_code": item_code,
        "employee_function": employee_function,
        "rows": rows,
        "total": round(gross, 2),
        "allocated": round(allocated, 2),
        "available": round(gross - allocated, 2),
    }


@frappe.whitelist()
def get_untagged_main_breakdown(doc_name, item_code):
    doc = frappe.get_doc("Batch Planning", doc_name)
    employee_function = doc.custom_employee_function
    if not employee_function:
        frappe.throw("Employee Function is not set on this document.")

    ef_doc = frappe.get_doc("Employee Function", employee_function)
    warehouse = next(
        (r.store_warehouse for r in (ef_doc.table_bukm or []) if r.store_warehouse),
        None,
    )
    if not warehouse:
        frappe.throw(
            f"No store warehouse found in Employee Function '{employee_function}'."
        )

    this_ef = frappe.db.sql(
        """
        SELECT COALESCE(NULLIF(sle.project, ''), '(no project)') AS project_id,
               COALESCE(MAX(p.project_name), '') AS project_name,
               ROUND(SUM(sle.actual_qty), 2) AS qty
        FROM `tabStock Ledger Entry` sle
        LEFT JOIN `tabProject` p ON p.name = sle.project
        WHERE sle.item_code = %(item)s
          AND sle.warehouse = %(warehouse)s
          AND sle.is_cancelled = 0
          AND (sle.batch_planning_id IS NULL OR sle.batch_planning_id = '')
        GROUP BY project_id
        HAVING SUM(sle.actual_qty) <> 0
        ORDER BY (project_id = '(no project)') ASC,
                 project_name ASC,
                 project_id ASC
        """,
        {"item": item_code, "warehouse": warehouse},
        as_dict=True,
    )

    total = round(sum(flt(r.qty) for r in this_ef), 2)

    negatives = sum(flt(r.qty) for r in this_ef if flt(r.qty) < 0)
    if negatives:
        unattributed = next(
            (r for r in this_ef if r.project_id == "(no project)"), None
        )
        if unattributed and flt(unattributed.qty) + negatives >= 0:
            unattributed.qty = round(flt(unattributed.qty) + negatives, 2)
            this_ef = [
                r for r in this_ef
                if flt(r.qty) > 0 or r.project_id == "(no project)"
            ]

    return {
        "item_code": item_code,
        "employee_function": employee_function,
        "warehouse": warehouse,
        "this_ef": this_ef,
        "this_ef_total": total,
    }


@frappe.whitelist()
def make_material_request(doc_name):
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
    planning = get_material_planning_data(doc_name)
    schedule_date = frappe.utils.add_days(frappe.utils.today(), 1)

    shortages = []
    for row in planning["results"]:
        net_requirement = flt(row.get("net_requirement"))
        if net_requirement <= 0:
            continue

        shortages.append({
            "item_code": row["item_code"],
            "item_name": row.get("item_name"),
            "qty": net_requirement,
            "uom": row.get("uom"),
            "custom_batch_planning_no": doc.name,
            "schedule_date": schedule_date,
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
