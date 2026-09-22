import frappe

def set_batch_planning_id_on_po(doc, method):
    for item in doc.items:
        if item.batch_planning_id:
            continue

        if item.material_request and item.material_request_item:
            bp_id = frappe.db.get_value(
                "Material Request Item",
                item.material_request_item,
                "batch_planning_id"
            )
            if bp_id:
                item.batch_planning_id = bp_id

def set_batch_planning_id_on_grn(doc, method):
    for item in doc.items:
        if item.batch_planning_id:
            continue

        if item.purchase_order and item.purchase_order_item:
            bp_id = frappe.db.get_value(
                "Purchase Order Item",
                item.purchase_order_item,
                "batch_planning_id"
            )
            if bp_id:
                item.batch_planning_id = bp_id
