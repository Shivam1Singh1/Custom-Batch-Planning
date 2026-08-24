import frappe
from frappe.utils import flt

def consolidate_items_table(doc):
    if not doc.get("items"):
        return

    for item in doc.items:
        if hasattr(item, "custom_batch_reference"):
            setattr(item, "custom_batch_reference", None)

    grouped_items = {}
    for item in doc.items:
        key = item.item_code
        if not key:
            continue
        if key not in grouped_items:
            grouped_items[key] = []
        grouped_items[key].append(item)

    new_items = []
    for item_code, rows in grouped_items.items():
        if len(rows) == 1:
            row = rows[0]
            if hasattr(row, "custom_batch_reference"):
                setattr(row, "custom_batch_reference", None)
            new_items.append(row)
        else:
            target = rows[0]
            if hasattr(target, "custom_batch_reference"):
                setattr(target, "custom_batch_reference", None)

            total_qty = sum(flt(r.qty) for r in rows)
            target.qty = total_qty

            if hasattr(target, "stock_qty"):
                cf = flt(target.get("conversion_factor") or 1)
                target.stock_qty = total_qty * cf

            if hasattr(target, "transfer_qty"):
                cf = flt(target.get("conversion_factor") or 1)
                target.transfer_qty = total_qty * cf

            if hasattr(target, "rate") and hasattr(target, "amount"):
                target.amount = flt(target.qty * flt(target.rate or 0))

            new_items.append(target)

    doc.items = new_items

    if hasattr(doc, "calculate_taxes_and_totals") and getattr(doc, "currency", None):
        doc.calculate_taxes_and_totals()

def validate_material_request(doc, method=None):
    """Consolidate child items and remove custom_batch_reference."""
    consolidate_items_table(doc)

def map_purchase_receipt_fields(doc, method=None):
    """
    Populate parent Purchase Receipt (GRN) `custom_batch_planning_no`
    from the linked Purchase Order or Material Request parent header.

    Also consolidate duplicate items and remove custom_batch_reference.
    """
    consolidate_items_table(doc)

    if doc.get("custom_batch_planning_no"):
        return

    po_names = []
    mr_names = []
    for item in doc.items or []:
        if item.get("purchase_order") and item.purchase_order not in po_names:
            po_names.append(item.purchase_order)
        if item.get("material_request") and item.material_request not in mr_names:
            mr_names.append(item.material_request)

    for po in po_names:
        val = frappe.db.get_value("Purchase Order", po, "custom_batch_planning_no")
        if val and frappe.db.exists("Batch Planning", val):
            doc.custom_batch_planning_no = val
            return

    for mr in mr_names:
        val = frappe.db.get_value("Material Request", mr, "custom_batch_planning_no")
        if val and frappe.db.exists("Batch Planning", val):
            doc.custom_batch_planning_no = val
            return

def map_stock_entry_fields(doc, method=None):
    """
    Populate parent Stock Entry `custom_batch_planning_no`
    from the linked Purchase Receipt (GRN) parent header.

    Also consolidate duplicate items and remove custom_batch_reference.
    """
    consolidate_items_table(doc)

    for item in doc.items:
        if item.batch_planning_id and not item.to_batch_planning_id:
            item.to_batch_planning_id = item.batch_planning_id
            
    frappe.log_error(title="map_stock_entry_fields hook", message=f"SE {doc.name}, items: {[{'bp': i.batch_planning_id, 'to_bp': i.to_batch_planning_id} for i in doc.items]}")

    if doc.get("custom_batch_planning_no"):
        return

    pr_name = doc.get("purchase_receipt_no")
    if not pr_name:
        for item in doc.items or []:
            if item.get("reference_purchase_receipt"):
                pr_name = item.reference_purchase_receipt
                break

    if pr_name:
        val = frappe.db.get_value("Purchase Receipt", pr_name, "custom_batch_planning_no")
        if val and frappe.db.exists("Batch Planning", val):
            doc.custom_batch_planning_no = val

def map_purchase_invoice_fields(doc, method=None):
    """
    Populate parent Purchase Invoice `custom_batch_planning_no`
    from the linked Purchase Receipt, Purchase Order, or Material Request.

    Also consolidate duplicate items and remove custom_batch_reference.
    """
    consolidate_items_table(doc)

    if doc.get("custom_batch_planning_no"):
        return

    pr_names = []
    po_names = []
    mr_names = []

    for item in doc.items or []:
        if item.get("purchase_receipt") and item.purchase_receipt not in pr_names:
            pr_names.append(item.purchase_receipt)
        if item.get("purchase_order") and item.purchase_order not in po_names:
            po_names.append(item.purchase_order)
        if item.get("material_request") and item.material_request not in mr_names:
            mr_names.append(item.material_request)

    for pr in pr_names:
        val = frappe.db.get_value("Purchase Receipt", pr, "custom_batch_planning_no")
        if val and frappe.db.exists("Batch Planning", val):
            doc.custom_batch_planning_no = val
            return

    for po in po_names:
        val = frappe.db.get_value("Purchase Order", po, "custom_batch_planning_no")
        if val and frappe.db.exists("Batch Planning", val):
            doc.custom_batch_planning_no = val
            return

    for mr in mr_names:
        val = frappe.db.get_value("Material Request", mr, "custom_batch_planning_no")
        if val and frappe.db.exists("Batch Planning", val):
            doc.custom_batch_planning_no = val
            return

def map_sle_fields(doc, method=None):
    """
    Ensure custom dimensions like batch_planning_id propagate to both legs (s_warehouse and t_warehouse)
    of the Stock Ledger Entry for Stock Entries (e.g. Material Transfers).
    """
    if doc.voucher_type == "Stock Entry" and doc.voucher_detail_no:
        bp_before = doc.batch_planning_id
        if not doc.batch_planning_id:
            bp_id = frappe.db.get_value("Stock Entry Detail", doc.voucher_detail_no, "batch_planning_id")
            if bp_id:
                doc.batch_planning_id = bp_id
        if not doc.project:
            proj = frappe.db.get_value("Stock Entry Detail", doc.voucher_detail_no, "project")
            proj = frappe.db.get_value("Stock Entry Detail", doc.voucher_detail_no, "project")
            if proj:
                doc.project = proj
