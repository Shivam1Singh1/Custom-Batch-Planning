import frappe
from frappe.utils import today

def execute():
    print("--- STARTING MR->PO->GRN CHAIN TEST ---")

    bp_doc = frappe.get_doc("Batch Planning", "BP-26-07-001")
    from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import get_material_planning_data
    
    test_item = "CN02010007"
    print(f"Using Item Code: {test_item}")
    
    
    bp_name = "TEST-BP-001"
    project = bp_doc.project
    
    mr = frappe.new_doc("Material Request")
    mr.material_request_type = "Purchase"
    mr.custom_batch_planning_no = bp_name
    mr.project = project
    mr.append("items", {
        "item_code": test_item,
        "qty": 50.0,
        "schedule_date": today(),
        "batch_planning_id": bp_name,
        "uom": "Nos"
    })
    mr.flags.ignore_mandatory = True
    mr.flags.ignore_links = True
    mr.flags.ignore_validate = True
    mr.insert()
    mr.submit()
    print(f"Step 1: Submitted MR {mr.name} for 50 qty")
    
    check_status("Step 1", test_item, bp_name, project)
    
    po = frappe.new_doc("Purchase Order")
    po.supplier = frappe.db.get_value("Supplier", {"disabled": 0}, "name")
    po.project = project
    po.append("items", {
        "item_code": test_item,
        "qty": 30.0,
        "schedule_date": today(),
        "batch_planning_id": bp_name,
        "material_request": mr.name,
        "material_request_item": mr.items[0].name,
        "rate": 100,
        "uom": "Nos",
        "conversion_factor": 1
    })
    po.flags.ignore_mandatory = True
    po.flags.ignore_links = True
    po.flags.ignore_validate = True
    po.insert()
    po.submit()
    print(f"Step 2: Submitted PO {po.name} for 30 qty")
    
    check_status("Step 2", test_item, bp_name, project)
    
    pr = frappe.new_doc("Purchase Receipt")
    pr.supplier = po.supplier
    pr.project = project
    pr.append("items", {
        "item_code": test_item,
        "qty": 10.0,
        "received_qty": 10.0,
        "accepted_qty": 10.0,
        "rejected_qty": 0.0,
        "purchase_order": po.name,
        "purchase_order_item": po.items[0].name,
        "batch_planning_id": bp_name,
        "rate": 100,
        "warehouse": "Stores - MS",
        "uom": "Nos",
        "conversion_factor": 1
    })
    
    pr.items[0].warehouse = frappe.db.get_value("Warehouse", {"is_group": 0}, "name")
    
    pr.flags.ignore_mandatory = True
    pr.flags.ignore_links = True
    pr.flags.ignore_validate = True
    pr.insert()
    pr.submit()
    print(f"Step 3: Submitted PR {pr.name} for 10 qty")
    
    check_status("Step 3", test_item, bp_name, project, pr.items[0].warehouse)
    

def check_status(step, item_code, bp_name, project, warehouse=None):
    if not warehouse:
        warehouse = frappe.db.get_value("Warehouse", {"is_group": 0}, "name")
        
    main_stock = frappe.db.sql("""
        SELECT IFNULL(SUM(actual_qty), 0)
        FROM `tabStock Ledger Entry`
        WHERE item_code = %s
        AND warehouse = %s
        AND batch_planning_id = %s
        AND project = %s
        AND is_cancelled = 0
    """, (item_code, warehouse, bp_name, project))[0][0] or 0.0

    bp_mr_qty = frappe.db.sql("""
        SELECT IFNULL(SUM(mri.qty - mri.ordered_qty), 0)
        FROM `tabMaterial Request Item` mri
        JOIN `tabMaterial Request` mr ON mr.name = mri.parent
        WHERE mri.item_code = %s
        AND mri.batch_planning_id = %s
        AND mr.project = %s
        AND mr.docstatus = 1
        AND mr.status NOT IN ('Ordered', 'Stopped', 'Cancelled')
        AND mri.qty > mri.ordered_qty
    """, (item_code, bp_name, project))[0][0] or 0.0

    bp_po_qty = frappe.db.sql("""
        SELECT IFNULL(SUM(poi.qty - poi.received_qty), 0)
        FROM `tabPurchase Order Item` poi
        JOIN `tabPurchase Order` po ON po.name = poi.parent
        WHERE poi.item_code = %s
        AND poi.batch_planning_id = %s
        AND po.project = %s
        AND po.docstatus = 1
        AND po.status NOT IN ('Completed', 'Cancelled')
        AND poi.qty > poi.received_qty
    """, (item_code, bp_name, project))[0][0] or 0.0

    bp_pr_qty = frappe.db.sql("""
        SELECT IFNULL(SUM(pri.qty), 0)
        FROM `tabPurchase Receipt Item` pri
        JOIN `tabPurchase Receipt` pr ON pr.name = pri.parent
        WHERE pri.item_code = %s
        AND pri.batch_planning_id = %s
        AND pr.project = %s
        AND pr.docstatus = 0
    """, (item_code, bp_name, project))[0][0] or 0.0

    print(f"--- {step} Results ---")
    print(f"Open MR: {bp_mr_qty}")
    print(f"Open PO: {bp_po_qty}")
    print(f"Open PR: {bp_pr_qty}")
    print(f"Main Stock: {main_stock}")
    print("-" * 30)

