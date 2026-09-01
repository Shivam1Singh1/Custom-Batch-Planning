import json
import frappe
from frappe.model.document import Document
from frappe.utils import flt, today

from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
    get_bom_store_name,
)

class BatchesPlanned(Document):

    def on_trash(self):
        if frappe.db.exists(
            "Material Allocation",
            {
                "batch_planning": self.batch_planning,
                "allocation_status": ["not in", ("Deallocated", "Stock Entry Done")],
                "docstatus": ["!=", 2],
            },
        ):
            frappe.throw(
                f"Cannot delete Batches Planned <b>{self.name}</b>. "
                f"A live Material Allocation exists for Batch Planning "
                f"<b>{self.batch_planning}</b>."
            )

        if frappe.flags.get("skip_sct_decrement"):
            return

        if self.slot_opening_id and self.slot_booking_date:
            slot_master = frappe.db.get_value(
                "Slot Opening", self.slot_opening_id, "slot_master"
            )
            if slot_master:
                sct_name = frappe.db.get_value(
                    "Slot Capacity Tracker", {"slot_master": slot_master}, "name"
                )
                if sct_name:
                    sct_detail = frappe.db.get_value(
                        "Slot Capacity Detail",
                        {
                            "parent": sct_name,
                            "parenttype": "Slot Capacity Tracker",
                            "date": self.slot_booking_date,
                        },
                        ["name", "batches_planned"],
                        as_dict=True,
                    )
                    if sct_detail:
                        new_planned = max(0, int(sct_detail.batches_planned or 0) - 1)
                        frappe.db.set_value(
                            "Slot Capacity Detail",
                            sct_detail.name,
                            "batches_planned",
                            new_planned,
                        )

@frappe.whitelist()
def get_material_planning_data(items, warehouse, batch_planning, employee_function):
    if isinstance(items, str):
        items = json.loads(items)

    res = []
    curr_today = today()

    ef_doc = frappe.get_doc("Employee Function", employee_function)
    lab_warehouses = [
        r.lab_warehouse for r in (ef_doc.get("table_szrn") or []) if r.lab_warehouse
    ]

    for item in items:
        item_code = item.get("item_code")
        qty_required = flt(item.get("qty_required"))

        main_stock = flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"))

        lab_stock = 0.0
        for lab_wh in lab_warehouses:
            lab_stock += flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": lab_wh}, "actual_qty"))

        total_stock = main_stock + lab_stock

        allocated_qty = (
            frappe.db.sql("""
                SELECT IFNULL(SUM(mai.allocate_qty), 0)
                FROM `tabMaterial Allocation Item` mai
                INNER JOIN `tabMaterial Allocation` ma ON ma.name = mai.parent
                WHERE mai.item_code = %s
                AND ma.employee_function = %s
                AND ma.allocation_status NOT IN ('Deallocated', 'Stock Entry Done')
                AND ma.docstatus != 2
            """, (item_code, employee_function))[0][0] or 0
        )

        free_stock = flt(
            frappe.db.sql("""
                SELECT IFNULL(SUM(actual_qty), 0)
                FROM `tabStock Ledger Entry`
                WHERE item_code = %s
                AND warehouse = %s
                AND (batch_planning_id IS NULL OR batch_planning_id = '')
                AND is_cancelled = 0
            """, (item_code, warehouse))[0][0] or 0
        )

        bp_tagged_stock = flt(
            frappe.db.sql("""
                SELECT IFNULL(SUM(actual_qty), 0)
                FROM `tabStock Ledger Entry`
                WHERE item_code = %s
                AND warehouse = %s
                AND batch_planning_id = %s
                AND is_cancelled = 0
            """, (item_code, warehouse, batch_planning))[0][0] or 0
        )

        stock_available = free_stock + bp_tagged_stock

        bp_mr_qty = flt(
            frappe.db.sql("""
                SELECT IFNULL(SUM(mri.qty - mri.ordered_qty), 0)
                FROM `tabMaterial Request Item` mri
                JOIN `tabMaterial Request` mr ON mr.name = mri.parent
                WHERE mri.item_code = %s
                AND mri.batch_planning_id = %s
                AND mr.docstatus = 1
                AND mr.status NOT IN ('Ordered', 'Stopped', 'Cancelled')
                AND mri.qty > mri.ordered_qty
            """, (item_code, batch_planning))[0][0] or 0
        )

        global_mr_qty = flt(
            frappe.db.sql("""
                SELECT IFNULL(SUM(mri.qty - mri.ordered_qty), 0)
                FROM `tabMaterial Request Item` mri
                JOIN `tabMaterial Request` mr ON mr.name = mri.parent
                WHERE mri.item_code = %s
                AND mr.custom_employee_function = %s
                AND mr.docstatus = 1
                AND mr.status NOT IN ('Ordered', 'Stopped', 'Cancelled')
                AND mri.qty > mri.ordered_qty
            """, (item_code, employee_function))[0][0] or 0
        )

        bp_po_qty = flt(
            frappe.db.sql("""
                SELECT IFNULL(SUM(poi.qty - poi.received_qty), 0)
                FROM `tabPurchase Order Item` poi
                JOIN `tabPurchase Order` po ON po.name = poi.parent
                WHERE poi.item_code = %s
                AND poi.batch_planning_id = %s
                AND po.docstatus = 1
                AND po.status NOT IN ('Completed', 'Cancelled')
                AND poi.qty > poi.received_qty
            """, (item_code, batch_planning))[0][0] or 0
        )

        global_po_qty = flt(
            frappe.db.sql("""
                SELECT IFNULL(SUM(poi.qty - poi.received_qty), 0)
                FROM `tabPurchase Order Item` poi
                JOIN `tabPurchase Order` po ON po.name = poi.parent
                WHERE poi.item_code = %s
                AND poi.employee_function = %s
                AND po.docstatus = 1
                AND po.status NOT IN ('Completed', 'Cancelled')
                AND poi.qty > poi.received_qty
            """, (item_code, employee_function))[0][0] or 0
        )

        bp_grn_qty = flt(
            frappe.db.sql("""
                SELECT IFNULL(SUM(pri.qty - pri.returned_qty), 0)
                FROM `tabPurchase Receipt Item` pri
                JOIN `tabPurchase Receipt` pr ON pr.name = pri.parent
                WHERE pri.item_code = %s
                AND pri.batch_planning_id = %s
                AND pr.docstatus = 1
                AND pr.status NOT IN ('Completed', 'Cancelled')
                AND pri.qty > pri.returned_qty
            """, (item_code, batch_planning))[0][0] or 0
        )

        global_grn_qty = flt(
            frappe.db.sql("""
                SELECT IFNULL(SUM(pri.qty - pri.returned_qty), 0)
                FROM `tabPurchase Receipt Item` pri
                JOIN `tabPurchase Receipt` pr ON pr.name = pri.parent
                WHERE pri.item_code = %s
                AND pri.employee_function = %s
                AND pr.docstatus = 1
                AND pr.status NOT IN ('Completed', 'Cancelled')
                AND pri.qty > pri.returned_qty
            """, (item_code, employee_function))[0][0] or 0
        )

        net_requirement = max(
            qty_required - (free_stock + bp_mr_qty + bp_po_qty + bp_grn_qty), 0
        )

        batch_info = frappe.db.sql("""
            SELECT b.expiry_date, SUM(sle.actual_qty) AS actual_qty
            FROM `tabStock Ledger Entry` sle
            INNER JOIN `tabBatch` b ON b.name = sle.batch_no
            WHERE sle.item_code = %s AND sle.is_cancelled = 0
            AND (b.expiry_date >= %s OR b.expiry_date IS NULL)
            GROUP BY sle.batch_no, b.expiry_date
            HAVING SUM(sle.actual_qty) > 0
            ORDER BY b.expiry_date ASC LIMIT 1
        """, (item_code, curr_today), as_dict=True)

        expired_qty = (
            frappe.db.sql("""
                SELECT IFNULL(SUM(sle.actual_qty), 0)
                FROM `tabStock Ledger Entry` sle
                INNER JOIN `tabBatch` b ON b.name = sle.batch_no
                WHERE sle.item_code = %s AND sle.is_cancelled = 0
                AND b.expiry_date < %s
            """, (item_code, curr_today))[0][0] or 0
        )

        usable_qty = flt(batch_info[0].actual_qty) if batch_info else 0
        expiry_date = batch_info[0].expiry_date if batch_info else None

        res.append({
            "item_code": item_code,
            "item_name": item.get("item_name"),
            "qty_required": round(qty_required, 2),
            "total_stock": round(total_stock, 2),
            "main_stock": round(main_stock, 2),
            "lab_stock": round(lab_stock, 2),
            "allocated_qty": round(flt(allocated_qty), 2),
            "free_stock": round(free_stock, 2),
            "stock_available": round(stock_available, 2),
            "bp_tagged_stock": round(bp_tagged_stock, 2),
            "bp_mr_qty": round(bp_mr_qty, 2),
            "bp_po_qty": round(bp_po_qty, 2),
            "bp_grn_qty": round(bp_grn_qty, 2),
            "global_mr_qty": round(global_mr_qty, 2),
            "global_po_qty": round(global_po_qty, 2),
            "global_grn_qty": round(global_grn_qty, 2),
            "net_requirement": round(net_requirement, 2),
            "usable_qty": round(usable_qty, 2),
            "expired_qty": round(flt(expired_qty), 2),
            "expiry_date": expiry_date,
        })

    return res

@frappe.whitelist()
def get_bom_items_for_ma(batch_planning):
    bp = frappe.get_doc("Batches Planned", batch_planning)
    if not bp.batch_planning:
        frappe.throw("Batch Planning not linked!")

    bc = frappe.get_doc("Batch Planning", bp.batch_planning)
    matched = next(
        (
            row
            for row in (bc.custom_batch_details or [])
            if row.batch_planning_id
            in (
                batch_planning,
                bp.amended_from,
                batch_planning.rsplit("-", 1)[0],
            )
        ),
        None,
    )

    if not matched or not matched.bom_list:
        frappe.throw(f"BOM not found for: {batch_planning}")

    batch_key = f"{bp.batch_planning}-{matched.idx}"
    use_store = False
    items = []

    if matched.batch_type in ("Process Development", "Machine Trial"):
        bom_store = get_bom_store_name(batch_key)
        if bom_store:
            store_doc = frappe.get_doc("Batch BOM Store after Edit", bom_store)
            items = store_doc.bom_components or []
            use_store = True

    if not use_store:
        bom = frappe.get_doc("BOM", matched.bom_list)
        items = bom.exploded_items or bom.items or []

    ef = frappe.get_doc("Employee Function", bp.employee_function)
    warehouse = next(
        (r.store_warehouse for r in (ef.table_bukm or []) if r.store_warehouse),
        None,
    )

    result = []
    for item in items:
        qty = flt(
            item.qty
            if use_store
            else (item.qty_consumed_per_unit or item.stock_qty or item.qty)
        )
        uom = item.uom if use_store else (item.stock_uom or item.uom)
        item_code = item.item_code

        main_stock = flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"))

        allocated_qty = (
            frappe.db.sql(
                """
            SELECT IFNULL(SUM(mai.allocate_qty), 0)
            FROM `tabMaterial Allocation Item` mai
            JOIN `tabMaterial Allocation` ma ON ma.name = mai.parent
            WHERE mai.item_code = %s AND ma.employee_function = %s
            AND ma.allocation_status NOT IN ('Deallocated', 'Stock Entry Done')
            AND ma.docstatus != 2
        """,
                (item_code, bp.employee_function),
            )[0][0]
            or 0
        )

        free_stock = max(main_stock - flt(allocated_qty), 0)

        result.append(
            {
                "item_code": item_code,
                "item_name": item.item_name,
                "uom": uom,
                "quantity_required": round(qty, 6),
                "stock_available": round(free_stock, 2),
            }
        )

    return result
