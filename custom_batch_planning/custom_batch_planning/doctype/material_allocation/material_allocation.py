import frappe
import json
from frappe.model.document import Document
from frappe.utils import flt, getdate, today, now

class MaterialAllocation(Document):

    def validate(self):
        self.validate_planning_window()
        self.check_batch_planning_allocation_limit()
        self.check_global_free_stock_limit()
        self.set_pool_flags()
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

    def set_pool_flags(self):
        has_untagged = False
        transferable = 0.0

        for item in self.material_allocation:
            qty = flt(item.allocate_qty)
            if qty <= 0:
                continue
            if (item.source_pool or "Tagged") == "Untagged":
                has_untagged = True
                transferable += flt(item.main_allocated_qty)
            else:
                transferable += qty

        self.has_untagged_items = 1 if has_untagged else 0
        self.requires_material_request = 1 if transferable > 0 else 0

    def clear_source_split(self):
        for item in self.material_allocation:
            item.local_free_qty = 0
            item.global_free_qty = 0
            item.local_allocated_qty = 0
            item.global_allocated_qty = 0
            item.lab_allocated_qty = 0
            item.main_allocated_qty = 0

    def check_global_free_stock_limit(self):
        from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
            _ef_lab_warehouses,
            free_stock_figures,
            split_lab_first,
            split_local_first,
            untagged_free_figures,
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

        tagged_by_item = {}
        untagged_by_item = {}
        for item in self.material_allocation:
            if flt(item.allocate_qty) <= 0:
                item.local_allocated_qty = 0
                item.global_allocated_qty = 0
                item.lab_allocated_qty = 0
                item.main_allocated_qty = 0
                continue
            if (item.source_pool or "Tagged") == "Untagged":
                untagged_by_item.setdefault(item.item_code, []).append(item)
            else:
                tagged_by_item.setdefault(item.item_code, []).append(item)

        for item_code, rows in tagged_by_item.items():
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
                    f"(current batch {split['local_free']} + global batch {split['global_free']}).",
                    title="Insufficient Free Stock",
                )

            for row, row_split in zip(rows, split["rows"]):
                row.local_free_qty = split["local_free"]
                row.global_free_qty = split["global_free"]
                row.local_allocated_qty = row_split["from_local"]
                row.global_allocated_qty = row_split["from_global"]
                row.stock_available = split["capacity"]
                row.lab_allocated_qty = 0
                row.main_allocated_qty = 0

        if not untagged_by_item:
            return

        lab_warehouses = _ef_lab_warehouses(
            frappe.get_doc("Employee Function", self.employee_function)
        )

        for item_code, rows in untagged_by_item.items():
            figures = untagged_free_figures(
                item_code,
                warehouse,
                lab_warehouses,
                self.employee_function,
                self.batch_planning,
                exclude_parent=self.name,
                for_update=True,
            )

            split = split_lab_first(
                [flt(r.allocate_qty) for r in rows],
                figures["lab_free"],
                figures["main_free"],
            )

            if split["shortfall"] > 0:
                rows_note = f" across {len(rows)} rows" if len(rows) > 1 else ""
                frappe.throw(
                    f"<b>{item_code}</b>: need <b>{split['requested']}</b>{rows_note}, "
                    f"only <b>{split['capacity']}</b> free in the untagged pool "
                    f"(lab {split['lab_free']} + main store {split['main_free']}).",
                    title="Insufficient Untagged Stock",
                )

            for row, row_split in zip(rows, split["rows"]):
                row.lab_allocated_qty = row_split["from_lab"]
                row.main_allocated_qty = row_split["from_main"]
                row.stock_available = split["capacity"]
                row.local_free_qty = 0
                row.global_free_qty = 0
                row.local_allocated_qty = 0
                row.global_allocated_qty = 0

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
        from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
            _ef_lab_warehouses,
            split_lab_first,
            untagged_free_figures,
        )

        if self.workflow_state != "Approved":
            frappe.throw("Allocation requires the document to be Approved.")

        if self.docstatus == 2:
            frappe.throw("Document is cancelled.")

        warehouse = self.get_warehouse()
        if not warehouse:
            frappe.throw(f"No store warehouse found for Employee Function: {self.employee_function}")

        lab_warehouses = None

        for item in self.material_allocation:
            item.qty_allocated = 0
            item.shortage = 0
            item.set("batch_details", [])

            qty_needed = flt(item.allocate_qty) if flt(item.allocate_qty) > 0 else flt(item.quantity_required)
            if qty_needed <= 0:
                continue

            untagged = (item.source_pool or "Tagged") == "Untagged"

            if untagged:
                if lab_warehouses is None:
                    lab_warehouses = _ef_lab_warehouses(
                        frappe.get_doc("Employee Function", self.employee_function)
                    )

                item.stock_available = self._bin_qty(
                    item.item_code, [warehouse] + lab_warehouses
                )

                figures = untagged_free_figures(
                    item.item_code,
                    warehouse,
                    lab_warehouses,
                    self.employee_function,
                    self.batch_planning,
                    exclude_parent=self.name,
                )
                split = split_lab_first(
                    [qty_needed],
                    max(flt(figures["lab_free"]), 0.0),
                    max(flt(figures["main_free"]), 0.0),
                )
                item.lab_allocated_qty = split["rows"][0]["from_lab"]
                item.main_allocated_qty = split["rows"][0]["from_main"]

                total_allocated = self._fill_from_batches(
                    item, lab_warehouses, item.lab_allocated_qty
                )
                total_allocated += self._fill_from_batches(
                    item, [warehouse], item.main_allocated_qty
                )
            else:
                item.stock_available = self._bin_qty(item.item_code, [warehouse])
                total_allocated = self._fill_from_batches(
                    item, [warehouse], qty_needed
                )

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

    def _bin_qty(self, item_code, warehouses):
        if not warehouses:
            return 0.0
        rows = frappe.db.get_all(
            "Bin",
            filters={"item_code": item_code, "warehouse": ["in", warehouses]},
            pluck="actual_qty",
        )
        return sum(flt(q) for q in rows)

    def _fill_from_batches(self, item, warehouses, qty_needed):
        qty_needed = flt(qty_needed)
        if qty_needed <= 0 or not warehouses:
            return 0.0

        total = 0.0
        for b in self.get_batches(item.item_code, warehouses):
            if total >= qty_needed:
                break
            available = flt(b.actual_qty)
            if available <= 0:
                continue
            take = min(available, qty_needed - total)
            item.append("batch_details", {
                "batch_no": b.batch_no,
                "expiry_date": b.expiry_date,
                "qty_available": available,
                "qty_allocated": take,
            })
            total += take
        return total

    def get_linked_stock_entry(self):
        if not self.stock_entry:
            return None

        se = frappe.db.get_value(
            "Stock Entry", self.stock_entry, ["name", "docstatus"], as_dict=True
        )
        if not se or se.docstatus == 2:
            self.db_set("stock_entry", None, update_modified=False)
            return None

        return se

    def get_linked_material_request(self):
        if not self.material_request:
            return None

        mr = frappe.db.get_value(
            "Material Request",
            self.material_request,
            ["name", "docstatus", "workflow_state"],
            as_dict=True,
        )
        if not mr or mr.docstatus == 2:
            self.db_set("material_request", None, update_modified=False)
            return None

        return mr

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

        existing_mr = self.get_linked_material_request()
        if existing_mr:
            if existing_mr.docstatus == 1:
                frappe.throw(
                    f"Material Request <b>{existing_mr.name}</b> is submitted. Cancel it first."
                )
            else:
                frappe.throw(
                    f"Draft Material Request <b>{existing_mr.name}</b> exists. Delete or submit it first."
                )

        for item in self.material_allocation:
            item.qty_allocated = 0
            item.shortage = flt(item.quantity_required)
            item.set("batch_details", [])

            if (item.source_pool or "Tagged") == "Untagged":
                item.lab_allocated_qty = 0
                item.main_allocated_qty = 0

        self.allocation_status = "Deallocated"
        if self.docstatus == 1:
            self.flags.ignore_validate_update_after_submit = True

        self.save()

        self.save_allocation_log("Deallocated")

        return True

    @frappe.whitelist()
    def make_material_request(self):
        if self.docstatus != 1:
            frappe.throw("Submit the Material Allocation first.")

        existing_mr = self.get_linked_material_request()
        if existing_mr:
            frappe.throw(
                f"Material Request <b>{existing_mr.name}</b> already exists. Only one is allowed."
            )

        ef = self.get_employee_function_defaults()
        missing = [
            label
            for label, value in (
                ("store warehouse", ef.from_warehouse),
                ("lab warehouse", ef.to_warehouse),
                ("segment", ef.segment),
                ("cost center", ef.cost_center),
            )
            if not value
        ]
        if missing:
            frappe.throw(
                f"Employee Function {self.employee_function} has no "
                + ", ".join(missing)
                + "."
            )

        schedule_date = today()

        mr = frappe.new_doc("Material Request")
        mr.material_request_type = "Material Transfer"
        mr.transaction_date = today()
        mr.schedule_date = schedule_date
        company = frappe.defaults.get_user_default("Company")
        if company:
            mr.company = company
        mr.set_from_warehouse = ef.from_warehouse
        mr.set_warehouse = ef.to_warehouse
        mr.custom_batch_planning_no = self.batch_planning
        mr.custom_material_allocation = self.name

        mr.custom_employee_function = self.employee_function
        mr.project = self.project_id

        mr.custom_function_head_name = (
            self.function_head_name or ef.function_head_name
        )

        for row in self.material_allocation:
            if (row.source_pool or "Tagged") == "Untagged":
                qty = flt(row.main_allocated_qty)
            else:
                qty = flt(row.allocate_qty)
            if qty <= 0:
                continue
            mr.append("items", {
                "item_code": row.item_code,
                "item_name": row.item_name,
                "qty": qty,
                "uom": row.uom,
                "schedule_date": schedule_date,
                "from_warehouse": ef.from_warehouse,
                "warehouse": ef.to_warehouse,
                "segment": ef.segment,
                "cost_center": ef.cost_center,
                "batch_planning_id": self.batch_planning,
                "employee_function": self.employee_function,
                "project": self.project_id,
            })

        if not mr.items:
            frappe.throw(
                "No allocated quantities to transfer. An allocation covered "
                "entirely from untagged lab stock has nothing to move — those "
                "units are already standing in the lab."
            )

        return mr

    def save_allocation_log(self, status):
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

    def get_employee_function_defaults(self):
        ef_doc = frappe.get_doc("Employee Function", self.employee_function)

        def pick(table, fieldname):
            rows = [r for r in (ef_doc.get(table) or []) if r.get(fieldname)]
            for row in rows:
                if row.get("default"):
                    return row.get(fieldname)
            return rows[0].get(fieldname) if rows else None

        return frappe._dict({
            "from_warehouse": pick("table_bukm", "store_warehouse"),
            "to_warehouse": pick("table_szrn", "lab_warehouse"),
            "segment": pick("table_xlgh", "segment"),
            "cost_center": pick("cost_center", "cost_center"),
            "function_head_name": ef_doc.get("function_head_name"),
        })

    def get_batches(self, item_code, warehouses):
        if not warehouses:
            return []
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
                      AND ma.allocation_status IN ('Allocated', 'Material Request Done')
                      AND ma.docstatus != 2
                ), 0)) AS actual_qty
            FROM `tabStock Ledger Entry` sle
            INNER JOIN `tabBatch` b ON b.name = sle.batch_no
            WHERE sle.item_code = %s
              AND sle.warehouse IN %s
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
                  AND ma.allocation_status IN ('Allocated', 'Material Request Done')
                  AND ma.docstatus != 2
            ), 0)) > 0
            ORDER BY b.expiry_date ASC
        """, (self.name, item_code, tuple(warehouses), self.name), as_dict=True)

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
def ma_get_untagged_free(item_code, employee_function, batch_planning, exclude_parent=None):
    from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
        _ef_lab_warehouses,
        untagged_free_figures,
    )

    ef_doc = frappe.get_doc("Employee Function", employee_function)
    warehouse = next(
        (r.store_warehouse for r in (ef_doc.table_bukm or []) if r.store_warehouse),
        None,
    )
    if not warehouse:
        return {"lab_free": 0.0, "main_free": 0.0, "free_stock": 0.0}

    figures = untagged_free_figures(
        item_code,
        warehouse,
        _ef_lab_warehouses(ef_doc),
        employee_function,
        batch_planning,
        exclude_parent=exclude_parent,
    )

    lab_free = max(flt(figures["lab_free"]), 0.0)
    main_free = max(flt(figures["main_free"]), 0.0)
    return {
        "lab_free": lab_free,
        "main_free": main_free,
        "free_stock": lab_free + main_free,
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
    on_stock_entry_submit(doc.name)


def _allocation_for_stock_entry(stock_entry_name):
    ma_name = frappe.db.get_value(
        "Material Allocation", {"stock_entry": stock_entry_name}, "name"
    )
    if ma_name:
        return ma_name

    material_requests = frappe.db.get_all(
        "Stock Entry Detail",
        filters={"parent": stock_entry_name, "material_request": ["is", "set"]},
        pluck="material_request",
        distinct=True,
    )
    for mr_name in material_requests:
        ma_name = frappe.db.get_value(
            "Material Allocation", {"material_request": mr_name}, "name"
        )
        if ma_name:
            return ma_name

    return None


@frappe.whitelist()
def on_stock_entry_submit(stock_entry_name):
    ma_name = _allocation_for_stock_entry(stock_entry_name)
    if not ma_name:
        return

    ma_doc = frappe.get_doc("Material Allocation", ma_name)

    if ma_doc.allocation_status not in ("Allocated", "Material Request Done"):
        return

    if not ma_doc.stock_entry:
        ma_doc.db_set("stock_entry", stock_entry_name, update_modified=False)

    ma_doc.allocation_status = "Stock Entry Done"
    ma_doc.flags.ignore_validate_update_after_submit = True
    ma_doc.save(ignore_permissions=True)
    frappe.db.commit()

@frappe.whitelist()
def get_allocated_items(batch_planning, employee_function):
    LIVE = """
          AND ma.batch_planning = %(bp)s
          AND ma.employee_function = %(ef)s
          AND ma.docstatus <> 2
          AND ma.workflow_state = 'Approved'
          AND IFNULL(ma.allocation_status, '') IN ('Allocated', 'Material Request Done', 'Stock Entry Done')
          AND mai.allocate_qty > 0
    """

    rows = frappe.db.sql(f"""
        SELECT
            mai.item_code                   AS item_code,
            MAX(mai.item_name)              AS item_name,
            MAX(mai.uom)                    AS uom,
            MAX(mai.quantity_required)      AS quantity_required,
            SUM(mai.allocate_qty)           AS qty_allocated
        FROM `tabMaterial Allocation Item` mai
        INNER JOIN `tabMaterial Allocation` ma ON ma.name = mai.parent
        WHERE 1 = 1
          {LIVE}
        GROUP BY mai.item_code
        ORDER BY mai.item_code
    """, {"bp": batch_planning, "ef": employee_function}, as_dict=True)

    ma_count = flt(frappe.db.sql(f"""
        SELECT COUNT(DISTINCT ma.name)
        FROM `tabMaterial Allocation Item` mai
        INNER JOIN `tabMaterial Allocation` ma ON ma.name = mai.parent
        WHERE 1 = 1
          {LIVE}
    """, {"bp": batch_planning, "ef": employee_function})[0][0] or 0)

    for r in rows:
        r["qty_allocated"] = flt(r["qty_allocated"])
        r["quantity_required"] = flt(r["quantity_required"])

    return {"items": rows, "ma_count": int(ma_count)}
