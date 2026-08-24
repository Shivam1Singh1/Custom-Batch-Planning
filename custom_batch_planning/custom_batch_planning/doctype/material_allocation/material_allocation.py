import frappe
import json
from frappe.model.document import Document
from frappe.utils import flt, getdate, today, now

class MaterialAllocation(Document):

    def validate(self):
        self.validate_planning_window()
        self.check_batch_planning_allocation_limit()
        self.check_global_free_stock_limit()
        for item in self.material_allocation:
            qty_requested = flt(item.allocate_qty)
            bom_qty = flt(item.quantity_required)
            stock = flt(item.stock_available)

            if qty_requested < 1:
                frappe.throw(
                    f"Row #{item.idx} ({item.item_code}): Qty Requested must be at least 1."
                )

            if qty_requested > stock:
                frappe.throw(
                    f"Row #{item.idx} ({item.item_code}): Qty Requested {qty_requested} exceeds Stock Available {stock}."
                )
            if qty_requested > bom_qty:
                frappe.throw(
                    f"Row #{item.idx} ({item.item_code}): Qty Requested {qty_requested} exceeds BOM Qty {bom_qty}."
                )
            if qty_requested != bom_qty and not (item.reason or "").strip():
                frappe.throw(
                    f"Row #{item.idx} ({item.item_code}): Reason is required when Qty Requested {qty_requested} differs from BOM Qty {bom_qty}."
                )

    def clear_source_split(self):
        """Blank the source breakdown when it cannot be derived server-side.

        The four split fields are read-only on the form but still arrive in the
        payload, so leaving whatever the client sent in place would present an
        unverified split as if the server had computed it. Zero is the honest
        answer when there is no pool to compute against.
        """
        for item in self.material_allocation:
            item.local_free_qty = 0
            item.global_free_qty = 0
            item.local_allocated_qty = 0
            item.global_allocated_qty = 0

    def check_global_free_stock_limit(self):
        """Re-read the free pools at save time, enforce local-first, refuse to
        over-issue.

        Allocation priority is enforced here, not in the UI: this batch's own
        free stock is consumed in full before any global stock is touched, and
        the resulting local/global breakdown is written back onto every row.
        Requested 100 against 40 local and 80 global saves as 40 local + 60
        global; requested 30 against the same pools saves as 30 local + 0
        global. Whatever split the client sent is discarded.

        The confirmation dialog on the Batch Planning form is advisory only: it
        reflects the pools as they stood when Material Planning was last run. By
        the time this document is saved another allocation may have consumed
        the same units, so the figure the user agreed to cannot be trusted and
        is deliberately not sent back to the server.

        Rows are locked FOR UPDATE while the pools are read, so two allocations
        racing for the last units serialise instead of both passing.

        This document is excluded from the totals it is being checked against —
        on a re-save it is already in the table, and counting it would make it
        compete with itself.
        """
        from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
            free_stock_figures,
            split_local_first,
        )
        from custom_batch_planning.custom_batch_planning.doctype.batch_planning_settings.batch_planning_settings import (
            get_stock_cutover_datetime,
        )

        if self.allocation_status in ("Deallocated", "Stock Entry Done"):
            return

        if not self.batch_planning or not self.employee_function:
            self.clear_source_split()
            return

        cutover = get_stock_cutover_datetime()

        warehouse = self.get_warehouse()
        if not warehouse:
            self.clear_source_split()
            return

        rows_by_item = {}
        for item in self.material_allocation:
            if flt(item.allocate_qty) <= 0:
                item.local_allocated_qty = 0
                item.global_allocated_qty = 0
                continue
            rows_by_item.setdefault(item.item_code, []).append(item)

        for item_code, rows in rows_by_item.items():
            figures = free_stock_figures(
                item_code,
                warehouse,
                self.employee_function,
                self.project_id,
                self.batch_planning,
                cutover,
                exclude_parent=self.name,
                for_update=True,
            )

            split = split_local_first(
                [flt(r.allocate_qty) for r in rows],
                figures["bp_free_stock"],
                figures["other_free_stock"],
            )

            if split["shortfall"] > 0:
                requested = split["requested"]
                available = split["capacity"]
                rows_note = f" across {len(rows)} rows" if len(rows) > 1 else ""
                frappe.throw(
                    f"<b>{item_code}</b>: need <b>{requested}</b>{rows_note}, "
                    f"only <b>{available}</b> free "
                    f"(this batch {split['local_free']} + global {split['global_free']}).",
                    title="Insufficient Free Stock",
                )

            for row, row_split in zip(rows, split["rows"]):
                row.local_free_qty = split["local_free"]
                row.global_free_qty = split["global_free"]
                row.local_allocated_qty = row_split["from_local"]
                row.global_allocated_qty = row_split["from_global"]
                row.stock_available = split["capacity"]

    def check_batch_planning_allocation_limit(self):
        if not self.batch_planning:
            return

        for item in self.material_allocation:
            query = """
                SELECT IFNULL(SUM(mai.allocate_qty), 0)
                FROM `tabMaterial Allocation Item` mai
                INNER JOIN `tabMaterial Allocation` ma ON ma.name = mai.parent
                WHERE mai.item_code = %s 
                AND ma.batch_planning = %s
                AND ma.name != %s
                AND ma.docstatus != 2
                AND ma.allocation_status NOT IN ('Deallocated', 'Stock Entry Done')
                FOR UPDATE
            """
            
            already_allocated = flt(frappe.db.sql(query, (item.item_code, self.batch_planning, self.name or ""))[0][0])
            total = already_allocated + flt(item.allocate_qty)
            
            if total > flt(item.quantity_required):
                frappe.throw(
                    f"Row #{item.idx} ({item.item_code}): total allocated {total} exceeds required {item.quantity_required} "
                    f"({already_allocated} already allocated elsewhere + {item.allocate_qty} here)."
                )

    def validate_planning_window(self):
        if not self.batch_planning:
            return
        
        latest_date = frappe.db.sql(
            """
            SELECT MAX(slot_booking_date)
            FROM `tabSlot Booking CT`
            WHERE parent = %s AND parenttype = 'Batch Planning'
            """,
            (self.batch_planning,)
        )
        
        if latest_date and latest_date[0][0]:
            d = getdate(latest_date[0][0])
            if d < getdate(today()):
                frappe.throw(
                    f"Planning window for <b>{self.batch_planning}</b> closed on {d}."
                )

    @frappe.whitelist()
    def auto_allocate(self):
        """
        Auto Allocate Flow:
        1. Fetch warehouse from Employee Function.
        2. FEFO based batch allocation.
        3. Fallback for non-batch items.
        """
        if self.workflow_state != "Approved":
            frappe.throw("Allocation requires the document to be Approved.")

        if self.docstatus == 2:
            frappe.throw("Document is cancelled.")

        warehouse = self.get_warehouse()
        if not warehouse:
            frappe.throw(f"No store warehouse found for Employee Function: {self.employee_function}")

        for item in self.material_allocation:
            item.qty_allocated = 0
            item.shortage = 0
            item.set("batch_details", [])

            qty_needed = flt(item.allocate_qty) if flt(item.allocate_qty) > 0 else flt(item.quantity_required)
            if qty_needed <= 0:
                continue

            item.stock_available = flt(frappe.db.get_value("Bin", {"item_code": item.item_code, "warehouse": warehouse}, "actual_qty"))

            batches = self.get_batches(item.item_code, warehouse)
            total_allocated = 0

            for b in batches:
                if total_allocated >= qty_needed:
                    break

                available = flt(b.actual_qty)
                if available <= 0:
                    continue

                allocate_qty = min(available, qty_needed - total_allocated)
                item.append("batch_details", {
                    "batch_no": b.batch_no,
                    "expiry_date": b.expiry_date,
                    "qty_available": available,
                    "qty_allocated": allocate_qty,
                })
                total_allocated += allocate_qty

            if total_allocated == 0 and flt(item.stock_available) >= qty_needed:
                total_allocated = qty_needed

            item.qty_allocated = total_allocated
            item.shortage = max(qty_needed - total_allocated, 0)

        self.allocation_status = "Allocated"
        if self.docstatus == 1:
            self.flags.ignore_validate_update_after_submit = True

        self.save()

        for item in self.material_allocation:
            item.db_update()

        self.save_allocation_log("Allocated")

        return True

    def get_linked_stock_entry(self):
        """
        Returns the live Stock Entry linked to this allocation, or None.

        The link lives on this side (`stock_entry`) rather than on Stock Entry,
        so a cancelled Stock Entry leaves a stale pointer behind. Treat a
        cancelled Stock Entry as no link at all and clear it, which is what
        frees the allocation up for a fresh transfer.
        """
        if not self.stock_entry:
            return None

        se = frappe.db.get_value(
            "Stock Entry", self.stock_entry, ["name", "docstatus"], as_dict=True
        )
        if not se or se.docstatus == 2:
            self.db_set("stock_entry", None, update_modified=False)
            return None

        return se

    @frappe.whitelist()
    def deallocate(self):
        if self.docstatus == 2:
            frappe.throw("Document is cancelled.")

        existing_se = self.get_linked_stock_entry()
        if existing_se:
            if existing_se.docstatus == 1:
                frappe.throw(
                    f"Stock Entry <b>{existing_se.name}</b> is submitted. Cancel it first."
                )
            else:
                frappe.throw(
                    f"Draft Stock Entry <b>{existing_se.name}</b> exists. Delete or submit it first."
                )

        for item in self.material_allocation:
            item.qty_allocated = 0
            item.shortage = flt(item.quantity_required)
            item.set("batch_details", [])

        self.allocation_status = "Deallocated"
        if self.docstatus == 1:
            self.flags.ignore_validate_update_after_submit = True

        self.save()

        self.save_allocation_log("Deallocated")

        return True

    @frappe.whitelist()
    def create_stock_entry(self):
        """
        Builds the Material Transfer Stock Entry for this allocation and records
        it on `stock_entry`.

        This runs server-side rather than via frappe.new_doc on the client
        because the link is now held on this document: the Stock Entry name only
        exists once it has been inserted, so the client cannot stamp it.
        Inserting here also makes the one-Stock-Entry-per-allocation guard
        atomic instead of a read-then-create race.

        The draft is inserted with mandatory checks off. Stock Entry carries two
        required fields this allocation has no answer for — custom_line_of_business
        (labelled "Cost Centre") and custom_cost_centre (labelled "Segment") —
        and guessing a value for either would be worse than leaving them blank:
        they are an accounting decision belonging to whoever posts the transfer.
        Without the flag the insert failed outright with "Value missing for
        Stock Entry: Cost Centre", so no draft could be created at all.

        Nothing is bypassed permanently. The flag lives on this one in-memory
        document; the Stock Entry that gets loaded when the user opens it has no
        such flag, so both fields are enforced normally the moment they try to
        save or submit it — which is exactly where they are meant to be filled.
        """
        if self.docstatus != 1:
            frappe.throw("Submit the Material Allocation first.")

        existing_se = self.get_linked_stock_entry()
        if existing_se:
            frappe.throw(
                f"Stock Entry <b>{existing_se.name}</b> already exists. Only one is allowed."
            )

        ef = frappe.get_doc("Employee Function", self.employee_function)
        from_warehouse = next(
            (r.store_warehouse for r in (ef.table_bukm or []) if r.store_warehouse), None
        )
        if not from_warehouse:
            frappe.throw(
                f"No store warehouse found in Employee Function {self.employee_function}."
            )
        to_warehouse = next(
            (r.lab_warehouse for r in (ef.table_szrn or []) if r.lab_warehouse), None
        )

        bom_no = None
        if self.batch_planning:
            bom_rows = frappe.get_all(
                "Batch Planning Detail",
                filters={
                    "parent": self.batch_planning,
                    "parenttype": "Batch Planning",
                    "bom_list": ["is", "set"],
                },
                fields=["bom_list"],
                order_by="idx asc",
                limit=1,
            )
            bom_no = bom_rows[0].bom_list if bom_rows else None

        se = frappe.new_doc("Stock Entry")
        se.stock_entry_type = "Material Transfer"
        se.custom_batch_planning_no = self.batch_planning
        se.from_warehouse = from_warehouse
        se.to_warehouse = to_warehouse
        se.project = self.project_id
        se.custom_employee_functions = self.employee_function
        if bom_no:
            se.bom_no = bom_no
            se.from_bom = 1

        for row in self.material_allocation:
            qty = flt(row.allocate_qty)
            if qty <= 0:
                continue
            se.append("items", {
                "item_code": row.item_code,
                "item_name": row.item_name,
                "qty": qty,
                "uom": row.uom,
                "s_warehouse": from_warehouse,
                "t_warehouse": to_warehouse,
                "conversion_factor": 1,
                "transfer_qty": qty,
                "batch_planning_id": self.batch_planning,
                "project": self.project_id,
            })

        if not se.items:
            frappe.throw("No allocated quantities to transfer.")

        se.insert(ignore_mandatory=True)
        self.db_set("stock_entry", se.name, update_modified=False)

        return se.name

    def save_allocation_log(self, status):
        """Logs the allocation/deallocation activity to 'Material Allocation Log'."""
        existing = frappe.db.get_value(
            "Material Allocation Log",
            {"batch_planning": self.batch_planning},
            "name",
        )

        if existing:
            log = frappe.get_doc("Material Allocation Log", existing)
        else:
            log = frappe.new_doc("Material Allocation Log")
            log.batch_planning = self.batch_planning
            log.employee_function = self.employee_function
            log.project_id = self.project_id
            log.project_name = self.project_name

        for item in self.material_allocation:
            log.append("table", {
                "allocated_by": frappe.session.user,
                "allocated_on": now(),
                "material_allocation_id": self.name,
                "status": status,
                "item_code": item.item_code,
                "qty_allocated": item.qty_allocated if status == "Allocated" else 0,
            })

        existing_items = {}
        for r in (log.ma_logs or []):
            existing_items[r.item_code] = r

        for item in self.material_allocation:
            if status == "Allocated":
                if item.item_code in existing_items:
                    existing_items[item.item_code].qty_allocated += flt(item.allocate_qty)
                    existing_items[item.item_code].allocate_qty += flt(item.allocate_qty)
                    existing_items[item.item_code].allocated_on = now()
                else:
                    log.append("ma_logs", {
                        "item_code": item.item_code,
                        "item_name": item.item_name,
                        "uom": item.uom,
                        "quantity_required": flt(item.quantity_required),
                        "stock_available": flt(item.stock_available),
                        "allocate_qty": flt(item.allocate_qty),
                        "qty_allocated": flt(item.allocate_qty),
                        "shortage": flt(item.shortage),
                        "open_pr": flt(item.open_pr),
                        "open_po": flt(item.open_po),
                        "grn_qty": flt(item.grn_qty),
                        "status": "Allocated",
                        "allocated_on": now(),
                    })
            elif status == "Deallocated":
                if item.item_code in existing_items:
                    existing_items[item.item_code].qty_allocated -= flt(item.allocate_qty)
                    existing_items[item.item_code].allocate_qty -= flt(item.allocate_qty)
                    if existing_items[item.item_code].qty_allocated < 0:
                        existing_items[item.item_code].qty_allocated = 0
                    if existing_items[item.item_code].allocate_qty < 0:
                        existing_items[item.item_code].allocate_qty = 0

        log.save(ignore_permissions=True)

    def autoname(self):
        if self.batch_planning:
            count = frappe.db.count("Material Allocation", filters={"batch_planning": self.batch_planning})
            counter = str(count + 1).zfill(2)
            self.name = f"MA-{self.batch_planning}-{counter}"
        else:
            frappe.throw("Batch Planning is required.")

    def get_warehouse(self):
        ef_doc = frappe.get_doc("Employee Function", self.employee_function)
        for row in ef_doc.get("table_bukm"):
            if row.store_warehouse:
                return row.store_warehouse
        return None

    def get_batches(self, item_code, warehouse):
        """Fetch batches with FEFO logic and exclude existing allocations."""
        return frappe.db.sql("""
            SELECT
                sle.batch_no,
                b.expiry_date,
                (SUM(sle.actual_qty) - IFNULL((
                    SELECT SUM(mbd.qty_allocated)
                    FROM `tabMA Batch Detail` mbd
                    INNER JOIN `tabMaterial Allocation` ma ON ma.name = mbd.parent
                    WHERE mbd.batch_no = sle.batch_no
                      AND ma.name != %s
                      AND ma.allocation_status = 'Allocated'
                      AND ma.docstatus != 2
                ), 0)) AS actual_qty
            FROM `tabStock Ledger Entry` sle
            INNER JOIN `tabBatch` b ON b.name = sle.batch_no
            WHERE sle.item_code = %s
              AND sle.warehouse = %s
              AND sle.is_cancelled = 0
              AND sle.batch_no IS NOT NULL
              AND sle.batch_no != ''
              AND b.disabled = 0
              AND (b.expiry_date IS NULL OR b.expiry_date >= CURDATE())
            GROUP BY sle.batch_no, b.expiry_date
            HAVING (SUM(sle.actual_qty) - IFNULL((
                SELECT SUM(mbd.qty_allocated)
                FROM `tabMA Batch Detail` mbd
                INNER JOIN `tabMaterial Allocation` ma ON ma.name = mbd.parent
                WHERE mbd.batch_no = sle.batch_no
                  AND ma.name != %s
                  AND ma.allocation_status = 'Allocated'
                  AND ma.docstatus != 2
            ), 0)) > 0
            ORDER BY b.expiry_date ASC
        """, (self.name, item_code, warehouse, self.name), as_dict=True)

@frappe.whitelist()
def ma_get_allocated_qty(item_code, employee_function, batch_planning, project, exclude_parent=None, row_name=None):
    warehouse = None
    ef_doc = frappe.get_doc("Employee Function", employee_function)
    for row in ef_doc.get("table_bukm"):
        if row.store_warehouse:
            warehouse = row.store_warehouse
            break

    if not warehouse:
        return {"free_stock": 0, "allocated_qty": 0}

    from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
        free_stock_figures,
    )
    from custom_batch_planning.custom_batch_planning.doctype.batch_planning_settings.batch_planning_settings import (
        get_stock_cutover_datetime,
    )

    figures = free_stock_figures(
        item_code,
        warehouse,
        employee_function,
        project,
        batch_planning,
        get_stock_cutover_datetime(),
        exclude_parent=exclude_parent,
    )

    local_free = max(flt(figures["bp_free_stock"]), 0.0)
    global_free = max(flt(figures["other_free_stock"]), 0.0)

    return {
        "local_free": local_free,
        "global_free": global_free,
        "free_stock": local_free + global_free,
        "allocated_qty": flt(figures["bp_allocated"]),
    }

@frappe.whitelist()
def get_open_pr_po(item_codes):
    if isinstance(item_codes, str):
        item_codes = json.loads(item_codes)

    result = {}
    for item_code in item_codes:
        open_pr = flt(frappe.db.sql("""
            SELECT SUM(mri.qty - mri.ordered_qty)
            FROM `tabMaterial Request Item` mri
            JOIN `tabMaterial Request` mr ON mr.name = mri.parent
            WHERE mri.item_code = %s AND mr.docstatus = 1 AND mri.ordered_qty < mri.qty
        """, (item_code,))[0][0])

        open_po = flt(frappe.db.sql("""
            SELECT SUM(poi.qty - poi.received_qty)
            FROM `tabPurchase Order Item` poi
            JOIN `tabPurchase Order` po ON po.name = poi.parent
            WHERE poi.item_code = %s AND poi.docstatus = 1 AND poi.received_qty < poi.qty
        """, (item_code,))[0][0])

        result[item_code] = {
            "open_pr": round(open_pr, 2),
            "open_po": round(open_po, 2),
        }

    return result

@frappe.whitelist(allow_guest=True)
def get_item_batch_expiry(item_codes):
    if isinstance(item_codes, str):
        item_codes = json.loads(item_codes)
    if not item_codes:
        return {}

    today_date = getdate(today())
    fmt = ",".join(["%s"] * len(item_codes))

    batches = frappe.db.sql(f"""
        SELECT b.item, b.name as batch_no, b.expiry_date, COALESCE(SUM(sle.actual_qty), 0) as qty
        FROM `tabBatch` b
        LEFT JOIN `tabStock Ledger Entry` sle ON sle.batch_no = b.name AND sle.is_cancelled = 0
        WHERE b.item IN ({fmt}) AND b.expiry_date IS NOT NULL AND b.disabled = 0
        GROUP BY b.item, b.name, b.expiry_date
    """, item_codes, as_dict=True)

    result = {}
    for b in batches:
        expiry = getdate(b.expiry_date)
        days_left = (expiry - today_date).days

        if days_left < 0:
            status, label = "expired", f"Expired ({abs(days_left)}d ago)"
        elif days_left <= 30:
            status, label = "expiring_soon", f"Expiring in {days_left}d"
        else:
            status, label = "ok", f"OK ({days_left}d left)"

        if b.item not in result:
            result[b.item] = {
                "status": status,
                "label": label,
                "days_left": days_left,
                "earliest_expiry": str(expiry),
                "batch_no": b.batch_no,
            }

    return result

def stock_entry_on_submit(doc, method=None):
    """doc_events hook for Stock Entry on_submit."""
    on_stock_entry_submit(doc.name)


@frappe.whitelist()
def on_stock_entry_submit(stock_entry_name):
    """
    Called when Stock Entry linked to a Material Allocation is submitted.
    Updates allocation_status to 'Stock Entry Done'.

    The allocation owns the link (`stock_entry`), so this resolves the
    Material Allocation by reverse lookup rather than reading a field off
    the Stock Entry.
    """
    ma_name = frappe.db.get_value(
        "Material Allocation",
        {"stock_entry": stock_entry_name},
        "name",
    )
    if not ma_name:
        return

    ma_doc = frappe.get_doc("Material Allocation", ma_name)

    if ma_doc.allocation_status != "Allocated":
        return

    ma_doc.allocation_status = "Stock Entry Done"
    ma_doc.flags.ignore_validate_update_after_submit = True
    ma_doc.save(ignore_permissions=True)
    frappe.db.commit()

@frappe.whitelist()
def get_allocated_items(batch_planning, employee_function):
    log_name = frappe.db.get_value(
        "Material Allocation Log",
        {"batch_planning": batch_planning, "employee_function": employee_function},
        "name"
    )
    
    ma_count = frappe.db.count("Material Allocation", filters={
        "batch_planning": batch_planning,
        "employee_function": employee_function,
        "docstatus": 1
    })

    if not log_name:
        return {"items": [], "ma_count": ma_count}

    log_doc = frappe.get_doc("Material Allocation Log", log_name)
    items = [item for item in log_doc.get("ma_logs", []) if item.qty_allocated > 0]
    return {"items": items, "ma_count": ma_count}
