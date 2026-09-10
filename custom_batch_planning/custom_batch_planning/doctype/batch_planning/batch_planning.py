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
            # Cancelled plans do not hold their Slot Opening. Without the
            # docstatus filter a cancelled Batch Planning kept the opening
            # reserved forever: it could be neither amended nor replaced by a
            # fresh plan, because the cancelled row itself answered this check
            # and the slot could never be planned again.
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

        # Second safety net behind the before_save handler in batch_planning.js,
        # which waits for the async counter calls to land. If a row still has no
        # id by the time the document reaches the server, generate it here
        # through the SAME function the client calls, so the format and its
        # scoping (Slot Opening + Batch Type) are identical either way.
        #
        # This replaces a BC-{yy}-{mm}-{###} fallback that used to sit further
        # down, after the duplicate checks. That fallback was NOT dead code: it
        # fired whenever the client lost the race and stamped a second,
        # incompatible format onto rows that should have read
        # SO-26-08-006-MFG-01. It left no trace in the database only because the
        # client normally wins. Confirmed 0 rows carry a BC- id before removal.
        #
        # Runs BEFORE the duplicate checks below, so a generated id is validated
        # exactly like a client-assigned one — the old fallback sat after them
        # and skipped both.
        for row in self.custom_batch_details or []:
            if row.batch_planning_id:
                continue
            if not (row.slot_opening_id and row.batch_type):
                frappe.throw(
                    f"Row {row.idx}: cannot generate a Batch Planning ID without both "
                    f"Slot Opening and Batch Type. Please set them and save again."
                )
            # Identity, not `r.name != row.name`: an unsaved child row has no
            # name yet, so comparing names made every row fail to exclude itself
            # and two rows of the same batch type both received MFG-03.
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
                # A cancelled plan no longer owns its Batches Planned - the plan
                # amending it takes them over on approval. Without the exemption
                # an amended plan could not even be saved, since it inherits the
                # very ids the cancelled plan still points at.
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
            # Belt to the mandatory flag on the field. Every material calculation
            # downstream - the consolidated components, the material planning
            # figures, the Batches Planned record - reads bom_list and quietly
            # skips the row when it is blank, so a row that lost its BOM plans a
            # batch that needs no materials at all and says nothing about it.
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
        """Field values a Batches Planned carries for one detail row.

        Shared by the create and the take-over paths so an amended plan refreshes
        exactly the fields a freshly created one would have written.

        No bom_list: Batches Planned has no such field, so the assignment that
        used to sit here never reached the database. The form reads its BOM back
        through the parent plan's detail row instead.
        """
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

                # Amending a cancelled plan: this batch already exists, so correct
                # it in place rather than planning a second one for the same slot.
                # No capacity change either - the slot was counted when the record
                # was first created and cancelling never gave it back.
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
    lab_covered = []
    already_covered = []

    # What this plan has already reserved, whichever flow reserved it. See
    # _bp_allocated_by_item: the BOM requirement is shared, so an untagged
    # allocation consumes this builder's headroom just as a tagged one does.
    already = _bp_allocated_by_item(batch_planning_name)

    for item in consolidated_items:
        item_code = item["item_code"]
        qty_required = flt(item["qty"])
        allocated_already = already.get(item_code, 0.0)

        # Lab Wise stock is material this batch ALREADY HOLDS — issued out of the
        # store under its own tag. Allocating against the gross BOM quantity
        # ignored it and reserved the same units twice: BP-29-08-001 needed 100
        # of CN02010004 with all 100 sitting in its own lab, and still proposed
        # borrowing 100 from the global pool, taking them off other batches that
        # had nothing. Across submitted plans that was 586 units over-reserved on
        # 9 of 17 lines.
        #
        # Only the shortfall is allocatable. Where the lab covers the requirement
        # outright there is nothing to reserve and the item is dropped entirely.
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
            # Two different reasons for the same skip, reported apart because
            # they mean opposite things to whoever reads the note: one says the
            # batch already holds the material, the other says the paperwork is
            # already done.
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

        # quantity_required stays the GROSS BOM figure: it is what the batch
        # consumes, and Material Allocation.validate uses it as the ceiling.
        #
        # Reason is deliberately NOT filled in, even though that same validate
        # demands one whenever allocate_qty differs from quantity_required, so a
        # partially lab-covered row will not save until someone types it. It used
        # to be written here from the lab-stock and already-allocated figures;
        # the sentence was then the system's, not the user's, and the grid tint
        # that marks an explained row lit up before anyone had explained
        # anything. See apply_reason_highlight in material_allocation.js.
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
    """Qty already reserved on this plan, per item, across BOTH pools.

    This is the ceiling check_batch_planning_allocation_limit enforces at save —
    total allocated per item across every live allocation on the plan may not
    exceed quantity_required — read here so a builder never PROPOSES a quantity
    that is guaranteed to be rejected. The two must stay in step: same status
    filters, same docstatus filter, same sum over allocate_qty. If this reads
    less than the check does, the draft fails on save; if it reads more, the
    builder silently under-allocates.

    DELIBERATELY POOL-BLIND, and that is the whole point. Tagged and untagged
    allocations draw on different stock, but they satisfy the SAME BOM
    requirement — 50 units needed is 50 units needed however they are sourced.
    A plan that reserved 50 from the tagged pool has nothing left to reserve
    from the untagged one, and offering it again produced exactly the confusion
    this fixes: an item showing as fully allocated in the Allocated Items dialog
    while a fresh draft proposed its full quantity over again.

    One grouped query rather than one per item: a fifty-row plan would otherwise
    pay fifty round trips before the builder has done any work.

    COUNTS DRAFTS, unlike the free-stock figures. This is the BOM ceiling, not a
    stock reservation: two drafts that together exceed Qty Required should be
    caught while they are still drafts, and the untagged builder must not
    re-offer an item an unallocated draft already covers. See _HOLDS_STOCK for
    the narrower gate the pool queries use and why the two differ.
    """
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
    """True when this Batch Planning has been cancelled.

    A cancelled plan keeps its rows and its Batches Planned on record, but it no
    longer owns them: the plan amending it takes them over. Both uniqueness
    guards consult this so an amendment is not mistaken for a duplicate.
    """
    if not batch_planning:
        return False

    return frappe.db.get_value("Batch Planning", batch_planning, "docstatus") == 2

def _batches_planned_status(batch_planning_state):
    """docstatus and workflow_state a Batches Planned inherits from its plan.

    A batch is the same decision as the plan that created it, so it carries the
    plan's state -- and batches are only ever created from an approved plan.

    This used to read a per-row `status` instead. That field no longer exists on
    Batch Planning Detail; only an orphan column survives, and nothing writes it
    any more, so every row created since the field was dropped reported no status
    and its batch was left sitting in Draft underneath an Approved plan. A blank
    status also left the record with no workflow state at all, which is what made
    it unopenable -- "Field workflow_state not found."
    """
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
        # The plan sits in a state the Batches Planned workflow does not define.
        # Approved is the only state batches are ever created under, so use it
        # rather than leaving the record without one.
        state = "Approved"
        docstatus = 1

    values = {"workflow_state": state}
    if int(docstatus):
        values["docstatus"] = int(docstatus)

    return values

def get_default_workflow_state(doctype, docstatus):
    """First state the active workflow defines for this docstatus, or None.

    Mirrors what Workflow.update_default_workflow_status() backfills with, so a
    record created here carries the same state it would have been given had it
    gone through the workflow.
    """
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

def get_bom_store_name(batch_key):
    """Name of the edited-BOM store for one Batch Planning Detail row, if any.

    Ordered by modified so the newest edit always wins. The table can hold more
    than one row per key: the dialog used to insert client side, so a second save
    that raced the first left a duplicate behind, and an unordered get_value would
    hand back whichever the database happened to return first.
    """
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
    """Edited BOM components for one row, or None when the row was never edited."""
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
    """Upsert the edited BOM for one Batch Planning Detail row.

    Get-or-create lives here rather than in the dialog so two saves of the same
    row can never land as two store documents - the dialog used to insert
    whenever its lookup came back empty, which is how the duplicates already in
    this table were made. Any such leftovers are collapsed on the next save.
    """
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
    """Move BOM edits made before the first save onto the real document name.

    Store rows are keyed `<batch planning>-<row idx>`, so a BOM edited while the
    form was still local is filed under the throwaway `new-batch-planning-xxxx`
    name. Nothing rewrote those keys - the old client filter looked for
    `new-batch-creation-%`, a name Frappe never generates for this doctype - so
    the edit was stranded and every later read fell back to the source BOM.
    """
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
    """Consolidated raw-material demand across every batch row on this plan.

    scale picks WHICH BOM quantity is summed, and the two differ by the BOM's
    own output quantity (ERPNext sets qty_consumed_per_unit = stock_qty /
    BOM.quantity — see bom.py):

        "per_unit"  qty_consumed_per_unit — demand for ONE unit of the finished
                    item. The historical default, kept so the Material Planning
                    and BOM Component tabs are untouched by this parameter.

        "stock"     stock_qty — demand for a full BOM's output, which is what a
                    batch actually produces. Used by the Untagged Materials tab.

    THE TWO TABS THEREFORE DISAGREE on any BOM whose quantity is not 1, and on
    this site that is not hypothetical: of 12 submitted BOMs, one is quantity
    400 and one is 20, so the same item reads 400x apart between the tabs. That
    divergence is deliberate and was signed off — "stock" is the correct scale
    and the new tab uses it from day one — but the older tabs have NOT been
    migrated, and doing so is a separate decision.

    The edited-BOM path is unaffected by scale: Batch BOM Store after Edit rows
    were captured from the dialog, which has always read stock_qty, so they are
    already on the "stock" scale whichever way this is called.
    """
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
    """Columns the running site ACTUALLY has for `doctype`.

    Every expression below is assembled from this rather than from the schema
    the author happened to be looking at, because those two are not the same
    thing and the difference is not academic. mr.custom_batch_planning was named
    here for months: it exists as an orphan column on databases old enough to
    predate its rename to custom_batch_planning_no, and on every other site the
    untagged queries died outright with "Unknown column" (1054) — a hard 500 on
    a tab that had nothing to do with a field nobody had written to in a year.

    A missing column is not an error here. It means the site cannot store that
    particular flavour of tag / employee function / project, so no row can match
    on it, so the term is dropped and the rest of the expression still answers.

    Cached on frappe.local: one information_schema read per doctype per request,
    and a bench migrate in another process cannot leave a stale answer behind
    for longer than the request that started it.
    """
    cache = getattr(frappe.local, "_bp_live_columns", None)
    if cache is None:
        cache = {}
        frappe.local._bp_live_columns = cache
    if doctype not in cache:
        try:
            cache[doctype] = set(frappe.db.get_table_columns(doctype))
        except Exception:
            # An unknown or not-yet-created table answers "nothing exists",
            # which drops every term rather than emitting SQL that cannot run.
            cache[doctype] = set()
    return cache[doctype]


class _SchemaExpr:
    """A SQL fragment built on first use, not at import.

    These are consumed exclusively as f-string interpolations into
    frappe.db.sql (`WHERE {MR_APPROVED}`), so __str__ and __format__ are the
    whole interface — every existing call site keeps working untouched while the
    fragment itself becomes a function of the live schema. It cannot be built at
    import time: module import happens without a site bound, long before there
    is a database to ask.
    """

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
        # Anything else a caller does to a SQL fragment — .replace, .endswith,
        # .strip — lands on the built string. These were plain str constants and
        # the callers (tests included) still treat them as such; delegating is
        # what keeps this a substitution rather than a migration.
        return getattr(str(self), name)

    def __repr__(self):
        return "<%s %s>" % (type(self).__name__, self._build())


def _coalesce_existing(*candidates):
    """COALESCE over the candidate columns that exist here, in the order given.

    candidates are (alias, doctype, fieldname) — the alias as it appears in the
    query, the doctype whose table that alias stands for, and the field to read.

    Yields the literal NULL when nothing survives. That is the honest answer,
    and the callers already handle it: `<expr> = %(ef)s` matches no row (SQL
    equality against NULL is never true), and _project_clause behaves the same.
    _bp_predicate is the one place it flips the other way — `(NULL) IS NULL` is
    TRUE, so a site with nowhere to store a batch tag would read every row as
    untagged. That is the correct reading of such a site, and it cannot arise in
    practice: batch_planning_id is recreated by the Batch Planning ID inventory
    dimension on every migrate.
    """

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
    """Submitted AND workflow-approved, degrading to submitted where no workflow
    exists.

    workflow_state is not this app's field and is not shipped by it: Frappe
    creates it as a Custom Field the moment a Workflow is defined for the
    doctype, and a site with no such workflow has no such column. Naming it
    unconditionally makes every open-pipeline query on that site a 1054.

    Where the column is absent, docstatus = 1 IS the approval — there is no
    other gate on that site for it to disagree with, so the figures stay
    meaningful instead of the tab dying.
    """

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
    """Received but not yet approved by the Store Head.

    Same workflow_state caveat as _approved, inverted: with no workflow column
    there is no such thing as a receipt waiting for approval, so the predicate
    becomes 1 = 0 rather than silently sweeping every draft receipt into the
    Unapproved GRN column.
    """

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
# mr.project is a Custom Field this app does not ship — neither in its fixtures
# nor a stock ERPNext field, so it is present here and absent on any site that
# never had that customisation. mri.project IS standard, which is why the item
# level is read first and why dropping the parent term costs little.
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

# Every place a batch tag can live on a pipeline row, coalesced the same way
# EF and Project already are above.
#
# It is not one field. A document can carry the tag on the ITEM
# (batch_planning_id, or the older custom_batch_planning_no) or on the PARENT
# (custom_batch_planning_no) — three fields for MR and PO, two for PR, which has
# no item-level custom field.
#
# A fourth parent field, custom_batch_planning, used to be read here and was
# removed: it is not a Custom Field on any of the three doctypes and survives
# only as an orphan column on databases old enough to predate its rename to
# custom_batch_planning_no. It can hold no data on a site that lacks the
# leftover, and naming it there was a hard 1054. Old client code such as
# gate_pass_control's banana.js still assigns it; that write goes nowhere.
#
# Checking only the item's batch_planning_id, as the untagged columns first did,
# calls a document untagged when its parent plainly names a Batch Planning. On
# this site that mislabels 58 Material Request lines whose parent is tagged, 68
# more carrying only the item-level custom field, and one row each on PO and PR
# — PR-2026-2027-00002 among them, whose header reads BP-26-11-001 while its
# single item row has no batch_planning_id at all.
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
    """The Project filter, or nothing at all when there is no project to filter on.

    Every tagged column scopes to one Project, and must keep doing so. The
    untagged columns cannot: a row with no batch tag almost never carries a
    Project either, and `<expr> = %(project)s` is never true for NULL — so
    keeping the clause would silently return 0 for exactly the legacy rows the
    Untagged Materials tab exists to surface.

    Dropping the clause is safe there because Employee Function still scopes the
    query, through the store warehouse for stock and through the EF columns for
    the pipeline. It is NOT safe anywhere else, which is why this returns the
    full clause whenever a project is supplied — every existing caller passes
    one and is unaffected.
    """
    return f"AND {project_expr} = %(project)s" if project else ""


def _bp_predicate(alias, mode, bp_expr=None):
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
    if mode == "UNTAGGED":
        # The third pool, and the complement of GEN + BP: rows carrying no batch
        # tag at all. It is what the Untagged Materials tab is built on — stock
        # and pipeline raised before batch planning existed, which every other
        # column in this file deliberately ignores.
        #
        # bp_expr is the full set of places a tag can live for this doctype (see
        # MR_BP / PO_BP / PR_BP). Pass it for anything with a parent document: a
        # row whose header names a Batch Planning is NOT untagged, however empty
        # its own batch_planning_id happens to be. Stock Ledger Entry has no
        # parent and only the one column, so it falls back to the default.
        #
        # Takes no %(bp)s parameter because there is no batch to compare against.
        # Callers still pass one in the params dict and that is harmless: an
        # unused key is ignored, and keeping the signature uniform is what lets
        # _open_mr / _open_po / _open_pr_grn serve all three modes unchanged.
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
          {_project_clause(MR_PROJECT, project)}
          AND {_bp_predicate('mri', mode, MR_BP)}
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
          {_project_clause(PO_PROJECT, project)}
          AND {_bp_predicate('poi', mode, PO_BP)}
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
          {_project_clause(PR_PROJECT, project)}
          AND {_bp_predicate('pri', mode, PR_BP)}
        """,
        {"item_code": item_code, "ef": ef, "project": project, "bp": bp},
        as_dict=True,
    )
    return _bucket(rows, count_all=True)




def _ef_lab_warehouses(ef_doc):
    """The lab warehouses an Employee Function declares, from table_szrn.

    Read the same way get_employee_function_defaults reads it, so the report and
    the Material Request builder agree on what counts as this function's lab.
    """
    return [r.lab_warehouse for r in (ef_doc.table_szrn or []) if r.lab_warehouse]


def _stock_qty(item_code, warehouse, project, bp, mode, in_main=True, warehouses=None):
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
    Wise is measured FOR THE TAGGED MODES ONLY.

    WHY THE NEGATIVE SCOPE IS TAGGED-ONLY. `warehouse <> main` does not really
    scope anything; it only splits an already-scoped set. Under GEN and BP the
    scoping is done by the batch tag, which confines rows to one batch's own
    movements, so "everywhere except the store" means "this batch's labs" in
    practice. Under UNTAGGED there is no tag doing that work, and the same
    clause silently matches EVERY warehouse in the company — other Employee
    Functions' main stores included.

    It shipped that way and overstated untagged Lab Wise by roughly twenty
    times: CN02010004 on VP-LTP-MFG-001 read 130,500, of which only 6,300 was
    in that function's own labs; the other 124,200 belonged to other functions,
    93,000 of it sitting in another function's STORE. Total Stock was then
    provably Employee-Function-independent — the same 175,500 whichever
    function you viewed, because it was simply all untagged stock everywhere.

    Pass `warehouses` to scope POSITIVELY instead: a list of warehouses the row
    must be in, which is what the untagged path now uses (the Employee
    Function's declared lab warehouses, see _ef_lab_warehouses). It overrides
    in_main entirely. This is the shape any new pool should use — a filter that
    only constrains when some other predicate is already doing the real work is
    the same trap _project_clause exists to document.
    """
    if warehouses is not None:
        # An empty list must not become `IN ()`, which is a syntax error. An
        # Employee Function that declares no lab warehouses holds no lab stock,
        # so 0 is also the right answer.
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

# Which pool a reservation drew on. Tagged rows and untagged rows describe
# claims against DIFFERENT physical piles, so no figure may ever mix them: an
# untagged allocation that counted as a tagged reservation would shrink the
# Material Planning tab's free stock by units that pile never held.
#
# Rows written before this field existed are all tagged-flow rows — the untagged
# flow did not exist to write any — so an empty value reads as "Tagged". That is
# what makes the column safe to add with no backfill, unlike the local/global
# split, whose absence is genuinely ambiguous (see
# patches/backfill_allocation_source_split.py).
_POOL_COLUMN = "COALESCE(NULLIF(mai.source_pool, ''), 'Tagged')"

# Which allocations actually hold stock.
#
# "Allocated" is a POSITIVE gate, and it replaced the negative one — status NOT
# IN ('Deallocated', 'Stock Entry Done') — which had a hole in it. A freshly
# saved draft has docstatus 0 and a NULL allocation_status, so it passed both
# halves of that filter and consumed free stock the moment someone pressed Save:
# before approval, and before anyone clicked Allocate. Free Qty dropped for a
# document that had committed to nothing.
#
# The gate now matches where the commitment is actually made. auto_allocate sets
# allocation_status = "Allocated", and it refuses to run unless the workflow has
# reached Approved — so nothing reserves stock until the Allocate button has been
# pressed on an approved document. Deallocated and Stock-Entry-Done fall out for
# free: the first released the stock, the second consumed it into a transfer, and
# neither is "Allocated" any more.
#
# BOTH pre-transfer statuses count. "Material Request Done" is Allocated with a
# transfer request raised against it: the stock has not moved, so the reservation
# is every bit as live. Leaving it out would have released the stock the moment
# somebody raised the request — Free Qty would rise while the material was more
# committed than before, not less.
#
# get_batches has always gated on this same set for its batch-level
# double-reservation guard, so this brings the quantity figures in line with it.
#
# DELIBERATELY NOT USED BY THE BOM CAP. check_batch_planning_allocation_limit and
# _bp_allocated_by_item keep counting drafts, because they protect a different
# thing: total allocated per item may not exceed Qty Required, and two drafts
# that together breach it should be caught while they are still drafts rather
# than at the second Allocate click.
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

    pool is a different axis and both must be set correctly. source splits a
    reservation across the two TAGGED piles; pool decides whether the row is
    counted against the tagged piles at all. It defaults to "Tagged" so every
    existing caller keeps measuring exactly what it always did — the untagged
    pool is opt-in, and _untagged_allocated_qty is the only thing that asks for
    it. Passing pool=None to total both would be meaningless: the two piles are
    disjoint stock under disjoint accounting, and no column displays their sum.

    Scoped on ma.project_id, not ma.project: project_id is the populated field
    on this doctype (set on 23 of 25 allocations; ma.project is empty on all of
    them).

    Only allocations in status "Allocated" count as live reservations — see
    _HOLDS_STOCK. A draft reserves nothing, and neither does an approved document
    nobody has pressed Allocate on; a Deallocated one released its stock and a
    Stock-Entry-Done one consumed it into a transfer.

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


def split_lab_first(quantities, lab_free, main_free):
    """Split untagged draws across the two untagged piles, LAB FIRST.

    A rename of split_local_first, not a second implementation - same clamping,
    same shared-pool draining across several rows of one item, and the same rule
    that `rows` still sums to the full request when `shortfall` is non-zero, so
    the caller must reject on shortfall rather than trust the rows.

    The rename earns its place because the priority means something completely
    different on each axis, and reading "local" as "lab" is exactly the mistake
    that would go unnoticed. On the tagged axis local-first is about OWNERSHIP:
    spend your own batch's stock before borrowing another batch's. Here it is
    about LOCATION, and about what an allocation costs downstream. Lab stock is
    already standing where the batch needs it, so consuming it first reserves
    the units that require no transfer at all; only the remainder falls to the
    store, where it becomes a physical Main -> Lab movement someone has to make.

    Allocating main-first would reserve store units while lab units sat idle,
    manufacturing transfer work out of nothing.
    """
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
    """The untagged pool's two halves, each net of what has been drawn from it.

    The untagged counterpart of free_stock_figures, and deliberately much
    smaller: there is one pile here, not two competing ones, so there is no
    cross-batch lending to settle and no local/global mirror to keep consistent.

        lab_free  = untagged stock in this function's labs  - lab-sourced draws
        main_free = untagged stock in its store             - main-sourced draws

    EACH DRAW IS CHARGED TO THE HALF IT CAME FROM, which is only possible
    because every untagged row records lab_allocated_qty and main_allocated_qty
    separately. Charging both to one half would let the other keep offering
    units that are already reserved - the same failure free_pools exists to
    prevent on the tagged axis.

    Summed, the two halves equal the report's Free Qty exactly:
    (main + lab) - total untagged allocated. That identity is the point; the
    allocator and the tab must not be able to disagree about how much is free.

    Scoped by Employee Function and NOT by project, matching
    get_untagged_material_data and _untagged_allocated_qty. See the note there:
    untagged rows almost never carry a Project, so a project filter would
    measure the reservations against a pool that was counted without one.

    Returned unclamped, like free_pools. Legacy untagged rows cannot exist - the
    flow that writes them shipped with the split fields - but stock moving out
    from under a live reservation can still drive a half negative, and the
    caller decides whether that reads as zero.
    """
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

    WHICH RECONCILIATION HOLDS, AND WHICH DOES NOT. The Allocated column stacks
    a pool-side figure over a batch-side one, and only the first of them closes
    against Main Wh and Free Qty:

        GLOBAL line   other_main_stock - other_allocated_total = other_free_stock

    holds in every state, because other_allocated_total is defined as everything
    drawn out of that pile — other batches' own draws PLUS this batch's
    borrowing.

        CURRENT line  bp_main_stock - bp_allocated = bp_free_stock

    does NOT hold, and no display choice can make it. bp_allocated is what this
    batch reserved wherever it came from, so it counts borrowed units that are
    physically in the other batches' pile and absent from bp_main_stock. A batch
    owning nothing and borrowing 10 reads main 0, allocated 10, free 0.

    The universal invariant on this line is pool-side, not batch-side, and it is
    exactly what free_pools computes:

        bp_main_stock = bp_local_allocated + other_global_allocated + bp_free_stock

    i.e. this batch's stock equals what it reserved out of itself, plus what
    other batches borrowed from it, plus what is left. Every term is a claim
    against the same physical pile. Anyone reconciling the Allocated column by
    hand should use this, not the displayed Current figure.
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

        # ONLY the global allocation is credited. The two sources are not
        # symmetric, and treating them as if they were is what made this figure
        # under-state the requirement:
        #
        #   bp_global_allocated  borrowed from OTHER batches' tagged stock.
        #                        Those units sit outside bp_main_stock, so this
        #                        term is the only place they are credited. It
        #                        must be subtracted.
        #
        #   bp_local_allocated   drawn from this batch's OWN stock, so the units
        #                        are ALREADY inside bp_total_stock. Subtracting
        #                        it as well credited the same material twice.
        #
        # Worked through — BOM 100, own main 40, lab 0, 40 reserved locally and
        # 10 borrowed. The batch physically controls 50, so Net Req is 50. The
        # old form read 100 - 40 - 0 - 10 - 40 = 10, understating by the whole
        # local reservation. Grouping Lab into Total Stock does not fix this:
        # 100 - (40+0) - (40+10) is the same 10, term for term.
        #
        # Consequence, accepted deliberately: Net Req rises on every item with a
        # local allocation, and it feeds get_batch_wise_shortages, so Material
        # Request quantities rise with it. That is the honest number.
        net_requirement = max(
            qty_required
            - bp_total_stock
            - bp_global_allocated
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
    """Reservations drawn from the UNTAGGED pool.

    This returned a hardcoded 0.0 until Material Allocation Item gained
    source_pool. It now counts real rows, and the two figures it feeds — Free
    Qty and Lab Item (After Alloc) — move for the first time.

    scope is "global" (everything drawn out of the untagged pile by this
    Employee Function, inclusive of this batch's own draw) or "current" (this
    batch's own draw).

    component picks WHICH PART of the reservation to total:

        "total"  allocate_qty — the whole untagged draw. What Free Qty subtracts.
        "lab"    the portion taken from stock already sitting in the labs.
        "main"   the portion reserved out of the main store, which is the only
                 part that will ever physically move.

    NO PROJECT FILTER, deliberately, and this is not an oversight copied from
    _allocated_qty — that function scopes on ma.project_id because every figure
    it feeds is project-scoped. This tab is not: untagged rows almost never
    carry a Project, which is the whole reason the tab exists (see
    get_untagged_material_data and _project_clause). Adding one here would
    subtract nothing from a Free Qty that was measured without one, and the
    Allocated column would read 0 against stock that is plainly reserved.

    Employee Function is the scope that does the real work, matching how the
    stock side of this tab is bounded.

    Counts only allocations in status "Allocated", the same gate _allocated_qty
    uses — see _HOLDS_STOCK. A saved draft reserves no untagged stock, so Free
    Qty and Labware on the tab do not move until Allocate is pressed.

    exclude_parent and for_update exist for the save-time re-check, exactly as
    they do on _allocated_qty: a document being re-saved is already in the
    table, so counting it would make it compete with itself, and the pools must
    be locked while they are read or two allocations racing for the last
    untagged units would both pass. The report passes neither - it must never
    hold locks.
    """
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
    """Untagged Materials tab — stock and pipeline carrying no batch tag.

    The same items as Material Planning, measured against the opposite pool.
    Where that tab shows what THIS batch and other batches have claimed, this
    one shows what nobody has: rows raised before batch planning existed, which
    every tagged column deliberately excludes.

    SCOPED BY EMPLOYEE FUNCTION ONLY. No project filter anywhere — see
    _project_clause. Untagged rows almost never carry a Project, so filtering on
    one returns zero for precisely the stock this tab exists to surface. The EF
    still bounds the query, through its store warehouse for stock and through
    the EF columns for MR/PO/GRN.

    ROWS ARE THE CONSOLIDATED BOM ITEMS, not every item holding untagged stock.
    Two reasons. Qty Req and Net Req are only meaningful against demand, and the
    volume settles it: this site carries 860-3,549 distinct items with untagged
    ledger rows per warehouse, against 50-64 BOM items on a typical plan. An
    item-driven table would be four figures of rows, almost all irrelevant to
    the batch being planned.

    FREE QTY CREDITS LAB (decided 2026-09-07). Free Qty is
    (Main Wh + Lab Item) - Allocated(Global): every untagged unit counts as
    available wherever it physically sits, until a Material Allocation reserves
    it. The tab shipped with lab excluded, on the reasoning that untagged lab
    stock was old unclaimed material and not this batch's until formally
    allocated; the untagged allocation flow is that formal claim, so the
    allocatable pool and the coverage figure now agree. Net Req drops on any row
    holding untagged lab stock, which is the intended consequence.

    Allocation figures are real from this release. They were hardcoded 0 while
    nothing could reserve untagged stock — see _untagged_allocated_qty, which
    now counts Material Allocation Item rows carrying source_pool = Untagged.
    """
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

        # project=None drops the Project predicate; bp=None is unused by the
        # UNTAGGED branch of _bp_predicate and is passed only to keep the
        # shared signature.
        main_stock = _stock_qty(
            item_code, warehouse, None, None, "UNTAGGED", in_main=True
        )
        # Positively scoped to this function's declared lab warehouses. NOT
        # in_main=False — with no batch tag to confine the rows, that clause
        # matches every warehouse in the company. See _stock_qty.
        lab_stock = _stock_qty(
            item_code, warehouse, None, None, "UNTAGGED", warehouses=lab_warehouses
        )

        global_allocated = _untagged_allocated_qty(
            item_code, employee_function, doc.name, "global"
        )
        current_allocated = _untagged_allocated_qty(
            item_code, employee_function, doc.name, "current"
        )

        # Only the main-sourced share of this batch's own draw will ever move —
        # see lab_after_alloc below for why that distinction matters.
        current_main_allocated = _untagged_allocated_qty(
            item_code, employee_function, doc.name, "current", component="main"
        )

        # The lab-sourced share of every untagged draw on this pool. It drives
        # the SYMBOLIC lab_item -> Labware move: units claimed out of the lab
        # stop counting as free Lab Item and appear under Labware instead.
        #
        # Symbolic in the strict sense — no Stock Entry, no ledger row, nothing
        # physically relocates. Those units were always standing in the lab
        # warehouses; allocation only records who they belong to. Labware is the
        # readout of that claim, not a warehouse. See the lock recorded on
        # MaterialAllocation.deallocate.
        #
        # GLOBAL, and it must stay global: it feeds lab_available and
        # global_main_allocated, both of which are pool arithmetic. Every batch
        # drawing on this pool has to see the whole claim or two of them would
        # reserve the same units. It is NOT what the Labware column shows —
        # see current_lab_allocated below.
        lab_allocated = _untagged_allocated_qty(
            item_code, employee_function, doc.name, "global", component="lab"
        )

        # What the LABWARE column shows: THIS batch's own lab-sourced draw.
        #
        # Scoped to doc.name because the claim carries a batch tag even though
        # the stock does not. Allocating from the untagged pool writes no ledger
        # row — the units stay untagged in Stock Ledger Entry — but the Material
        # Allocation that records the claim names its Batch Planning, and that
        # is the tag Labware reports.
        #
        # It read the global figure until this was split, which put another
        # batch's claim on every row: one 2-unit untagged allocation on
        # BP-26-10-001 showed as Labware 2 on all fourteen approved plans under
        # VP-LTP-MFG-001, including plans whose own untagged draw was zero.
        # Labware sits on a per-batch row beside per-batch figures, so it is
        # read as per-batch whatever the tooltip says.
        #
        # Other batches' claims are still visible — in Lab Item, which is net of
        # the global figure. They are simply no longer attributed to this batch.
        current_lab_allocated = _untagged_allocated_qty(
            item_code, employee_function, doc.name, "current", component="lab"
        )

        # What the ALLOCATED column shows: the MAIN-sourced share only.
        #
        # The lab-sourced share is reported under Labware instead, and showing it
        # in both places read as double the reservation. The split is also what
        # the two columns MEAN: Labware is stock the batch already has standing
        # in the lab, while Allocated is stock still sitting in the store waiting
        # on a physical transfer. Only the second is outstanding work.
        #
        # Derived, not queried — global total minus its lab half — so the column
        # costs nothing extra on a fifty-row plan.
        #
        # Free Qty still subtracts the WHOLE reservation (see below); it is the
        # display that splits, not the arithmetic. The row still reconciles:
        #     Main Wh + Lab Item - Allocated = Free Qty
        # because Lab Item has already given up the lab-sourced share.
        global_main_allocated = global_allocated - lab_allocated

        # GROSS, deliberately, and it stays gross once Labware starts moving.
        # Total Stock is the reconciliation against what is physically on the
        # shelf, and every unit is still there whoever has claimed it. Netting
        # the allocation out here would make the column disagree with the
        # warehouse and would double-count it, since Free Qty already subtracts
        # the same reservation.
        total_stock = main_stock + lab_stock

        # What Lab Item DISPLAYS: the unclaimed remainder. lab_stock stays the
        # gross figure above so Total Stock = Main Wh + lab_stock still holds and
        # the per-warehouse drill-down still reconciles.
        lab_available = lab_stock - lab_allocated

        # One pool, so one subtraction. The tagged tab needs free_pools because
        # it has two piles and must charge each draw to the one it came from;
        # here there is only the untagged pile, and everything reserved out of
        # it is in Allocated (Global) by definition.
        #
        # Both halves of that pile are offered: stock in the store and stock
        # already standing in the labs are equally unclaimed while untagged, so
        # Free Qty is measured against Total Stock, not against Main Wh alone.
        free_qty = total_stock - global_allocated

        # Projection only, never persisted: what the lab would hold once the
        # current reservation is physically transferred.
        #
        # ONLY THE MAIN-SOURCED PORTION IS ADDED, and using current_allocated
        # here instead would be a double count. Lab-sourced allocation moves
        # nothing: those units are already standing in the lab, so they are
        # already inside lab_stock. Adding the whole reservation would promise a
        # lab holding that no transfer will ever deliver — on an item covered
        # entirely from lab stock it would project double what is there.
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

        # Credits Free Qty, NOT Total Stock — so untagged lab stock is not
        # counted as coverage. This is the specified formula and it differs
        # from the tagged tab, which does credit its own lab stock. Unapproved
        # GRN is display-only here exactly as it is there: those units are
        # already credited through Open PO, which does not release until the
        # receipt is approved.
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
                # The pool-wide lab claim, kept for the drill-down: Lab Item is
                # net of THIS, not of labware_qty, so the dialog needs it to
                # reconcile the subtraction it shows.
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
        # Untagged stock can be reserved from this release, so the tab no longer
        # prints the "allocation not yet available" caveat and the Lab Item cell
        # goes back to the stacked Existing / After Alloc pair — the two figures
        # can now differ.
        "allocation_pending": False,
    }


@frappe.whitelist()
def create_untagged_material_allocation(batch_planning_name):
    """Bulk allocation against the UNTAGGED pool, every item in one pass.

    The sibling of create_bulk_material_allocations, not a variant of it. The
    two share a shape — consolidate the BOM, size each row against a pool,
    return an UNSAVED document for the user to review — and share nothing else,
    because they draw on different stock under different accounting:

        create_bulk_material_allocations   batch-tagged pools, local-first,
                                           free_stock_figures, project-scoped
        this                               the untagged pile, LAB-FIRST,
                                           untagged_free_figures, EF-scoped

    Kept apart rather than merged behind a flag. Every line of the tagged
    builder that looks reusable — the lab_stock deduction, split_local_first,
    the shared-stock warning — is answering a question this pool does not ask,
    and a single function serving both would have to be read twice to be read
    at all.

    NOTHING IS QUERIED FROM THE CLIENT. get_untagged_material_data is re-run
    here, on the server, so the quantities allocated are the ones true at the
    moment of the click and not whatever the tab was painted with — which may be
    minutes old and may predate another batch's allocation.

    LAB FIRST, then main store. See split_lab_first for why that order is a rule
    rather than a preference. The two portions are recorded separately on every
    row (lab_allocated_qty / main_allocated_qty) because they behave differently
    forever after: the lab portion is symbolic and never moves — those units are
    already standing in the lab — while the main portion is a reservation that
    a Stock Entry will later turn into a physical Main -> Lab transfer.

    NO CROSS-FLOW CHECK, deliberately. A Batch Planning may carry both a tagged
    and an untagged allocation, and this does not look at what the tagged flow
    has already reserved. check_batch_planning_allocation_limit is the single
    ceiling — total allocated per item across every allocation on the plan may
    not exceed quantity_required — and it is enforced at save on both flows. If
    the two together exceed the BOM requirement the second one to be saved is
    rejected there, with the arithmetic spelled out, which is the intended
    behaviour rather than a gap.

    Returned UNSAVED, like create_bulk_material_allocations and
    make_material_request. Nothing is inserted, so a draft the user abandons
    leaves no record to clean up.
    """
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

    # Fresh, server-side, and the same call the tab makes — so Qty Req and the
    # free figures on the draft cannot disagree with what the user was looking at
    # for any reason other than the passage of time.
    report = get_untagged_material_data(batch_planning_name)

    rows = []
    skipped_no_stock = []
    skipped_below_one = []
    skipped_already_allocated = []
    skipped_lab_covered = []

    # Shared with the tagged builder on purpose — see _bp_allocated_by_item. An
    # item this plan has already reserved in full has no headroom left under
    # check_batch_planning_allocation_limit, whichever pool did the reserving.
    already = _bp_allocated_by_item(batch_planning_name)

    for item in report["results"]:
        item_code = item["item_code"]
        qty_required = flt(item["qty_required"])
        if qty_required <= 0:
            continue

        # ONLY ROWS THE TAB SHOWS AS HAVING SOMETHING FREE, and taken from the
        # report row rather than recomputed, so the button takes exactly the
        # items someone reading the Free Qty column expects it to take.
        #
        # Not the same test as `capacity > 0` below, and the difference is not
        # academic. Free Qty is (main + lab) - allocated as ONE figure, while
        # capacity clamps each half at zero before adding them. An item whose
        # lab half has gone negative — stock moved out from under a live
        # reservation — reads as covered on one and available on the other:
        # lab -10 with main +5 is Free Qty -5 on the tab and capacity 5 here.
        # Allocating that would hand out units against a row the user was just
        # told had nothing.
        #
        # Cheap, too: skipping here avoids the four queries untagged_free_figures
        # costs, on every one of fifty rows that has nothing to give.
        if flt(item["free_qty"]) <= 0:
            skipped_no_stock.append(item_code)
            continue

        # WHAT THIS BATCH ALREADY HOLDS UNDER ITS OWN TAG, in its labs. This is
        # the half that live reservations do not describe, and missing it is
        # what kept fully-supplied items on offer.
        #
        # A finished allocation leaves NO live reservation behind: on Stock Entry
        # submit allocation_status becomes 'Stock Entry Done', which
        # _bp_allocated_by_item excludes by the same rule every other consumer in
        # this app uses. That exclusion is right — the reservation is over — but
        # the material did not disappear, it ARRIVED. It is now tagged to this
        # batch and standing in the lab, and it satisfies the BOM line as
        # completely as a reservation does.
        #
        # BP-26-10-001 is the worked example: CN02010004 needs 50, its allocation
        # reached Stock Entry Done, and 50 units now sit in the lab under this
        # batch's tag. Counting only reservations read that as "nothing
        # allocated" and offered all 50 again, against an untagged pile of
        # 45,000 that has no idea the requirement is already met.
        #
        # Not double counting: an untransferred allocation has a reservation and
        # no lab stock, a transferred one has lab stock and no reservation. The
        # two terms are disjoint by construction, which is why they sum. This is
        # the same lab_stock deduction create_bulk_material_allocations has
        # always made — it was only ever missing here.
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
            # Reported apart because they read as opposite things: one says the
            # material is already standing in the lab, the other says it is
            # spoken for but has not moved yet.
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

        # Material Allocation.validate rejects anything under 1, so a row that
        # would carry 0 must not be emitted at all rather than emitted and left
        # for the user to delete. Nothing is free for these items; that is what
        # Net Req on the tab is for.
        #
        # The Free Qty gate above already dropped the empty rows. This still runs
        # because the report and untagged_free_figures are separate reads: a
        # concurrent allocation between them can empty a pool that had stock a
        # moment ago, and emitting a zero row would fail at save instead of here.
        if allocate_qty <= 0:
            skipped_no_stock.append(item_code)
            continue
        if allocate_qty < 1:
            skipped_below_one.append(item_code)
            continue

        split = split_lab_first([allocate_qty], lab_free, main_free)

        # Cannot trigger as written — allocate_qty is clamped to capacity above —
        # but split_lab_first's per-row figures deliberately still sum to the
        # full request when the pools cannot cover it, so a caller that trusts
        # them without checking is one edit away from silently over-reserving.
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

        # Reason is left EMPTY, and that is a deliberate cost. validate demands
        # one whenever Qty Requested differs from BOM Qty, and partial coverage
        # is the normal case here — the untagged pile is whatever happened to be
        # bought without a batch tag, not a pile sized to this plan — so most
        # rows now need a reason typed before the allocation will save. The
        # sentence this used to generate from the pool figures explained the
        # arithmetic, not the decision, and it tinted every such row on arrival
        # as though a person had already justified it. Same reasoning as
        # create_bulk_material_allocations.
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
    """The per-warehouse split behind one row's Lab Item figure.

    Called on demand from the Untagged Materials tab, not folded into
    get_untagged_material_data: that would cost one query per item per lab
    warehouse on every load (50 items x 5-7 warehouses = 250-350 queries) to
    populate a panel almost nobody opens. Here it is 5-7 queries, once, on click.

    Deliberately loops _stock_qty per warehouse rather than issuing a single
    GROUP BY. One extra query per lab is cheap; a second copy of "what counts as
    untagged stock" is not. The summed figure on the row and the parts in this
    breakdown come from the identical code path, so they cannot drift — which is
    the entire reason the drill-down exists, since its job is to let someone
    check the sum instead of trusting it.
    """
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
    # Lab Item on the row is now net of the lab-sourced claim, so the
    # per-warehouse rows alone no longer sum to it. Both figures are returned so
    # the dialog can show the subtraction rather than appear to contradict the
    # table it exists to explain.
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
    """This item's untagged store stock, split by Project.

    Scoped to the Batch Planning's OWN Employee Function and its store
    warehouse — the same warehouse and the same untagged predicate the Main Wh
    column uses — so the rows always sum to the figure on the row. That
    reconciliation is the point: the drill-down exists to let someone check the
    number, not to decorate it.

    Other Employee Functions are deliberately not reported. Their stock is not
    this batch's to plan against, and showing it invited the reading that the
    two could be added together.

    Project is COALESCEd to a visible label rather than dropped. Untagged rows
    are overwhelmingly project-less — that is what makes them untagged — so a
    breakdown that hid the NULL bucket would show almost nothing and imply the
    stock did not exist.
    """
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

    # project_name comes from a LEFT join, not a lookup per row: the drill-down
    # is opened on click and should stay one query however many projects hold the
    # item. LEFT rather than INNER because the NULL-project bucket is usually the
    # biggest one here — untagged rows overwhelmingly carry no project, which is
    # what makes them untagged — and an inner join would silently drop it.
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
        -- Aliased project_id, NOT project. `project` is also a real column on
        -- Stock Ledger Entry, and MariaDB binds an ambiguous name in GROUP BY
        -- and ORDER BY to the COLUMN, not the select alias. That silently broke
        -- both: the ordering test compared the raw value against the placeholder
        -- text and never matched, and grouping by the raw column would split
        -- NULL and '' into two rows both labelled "(no project)".
        GROUP BY project_id
        HAVING SUM(sle.actual_qty) <> 0
        -- By project NAME, with the nameless bucket last. Ordering by quantity
        -- put whichever project happened to hold most at the top, so the same
        -- item reordered itself between visits and there was nowhere predictable
        -- to look for a given project. "(no project)" sorts to the bottom
        -- because it is not a project; it is the absence of one, and it is
        -- usually the largest bucket here, which would otherwise pin an
        -- uninformative row to the top of every drill-down.
        ORDER BY (project_id = '(no project)') ASC,
                 project_name ASC,
                 project_id ASC
        """,
        {"item": item_code, "warehouse": warehouse},
        as_dict=True,
    )

    # NEGATIVE BUCKETS ARE FOLDED INTO "(no project)" RATHER THAN DISPLAYED.
    #
    # A negative bucket means stock left the store stamped with that project but
    # never arrived under it: `project` on a ledger row records what a movement
    # was FOR, not which pile it came from. Goods received unattributed and later
    # issued against a project leave the issue stranded in that project's bucket
    # with nothing to offset it. It reads as a shortage and is not one.
    #
    # Simply hiding those rows would be worse: the visible rows would sum past
    # the total, and reconciling against Main Wh is the only job this drill-down
    # has. Clamping them to zero has the same effect.
    #
    # So the unmatched issue is charged back to the pile that actually funded it.
    # "(no project)" is the only honest destination — it is not a claim about any
    # project, it is the absence of one, and unattributed stock is by definition
    # where an unattributed receipt sits. The total is unchanged and still equals
    # Main Wh on the row.
    #
    # If folding would drive "(no project)" itself negative, the fold is abandoned
    # and every row is shown as-is. That means more was issued against projects
    # than was ever received unattributed, which is a real data problem worth
    # seeing rather than smoothing away.
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
    """MR lines = exactly the rows Material Planning shows with Net Req > 0.

    Deliberately delegates to get_material_planning_data rather than recomputing
    the requirement. This function used to carry its own formula, which had
    drifted from net_requirement in four terms:

      Main Wh   read straight off the Stock Ledger instead of the settled figure
                free_stock_figures returns, so a batch carrying a cross-batch
                deficit (see settle_cross_batch_draw) never saw it. BP-26-10-001
                read Main Wh as +2 where planning read -58, and reported no
                shortage on an item planning wanted 60 of.

      Lab Wise  omitted entirely, and omitted TWICE OVER: issuing stock from the
                store to a lab leaves a negative on Main Wh, which was then
                subtracted from the requirement — i.e. added to it. A batch
                needing 40 with all 40 already in its own lab asked to buy 80.

      Allocated  both the local and global terms were missing, so material
                 locked to this batch would have been purchased a second time
                 once any allocation existed.

    Reusing the single formula is what keeps the MR button and the Material
    Planning tab from disagreeing again; the guards for a missing Employee
    Function and a missing store warehouse are preserved because
    get_material_planning_data raises the same two.

    Lab stock counts as coverage, exactly as Net Req has it: material already
    sitting in this batch's lab is not re-purchased.
    """
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
