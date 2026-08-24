import frappe

def execute():
    bp_name = "TEST-BP-003"
    project = "PLTP-2025-0001"
    item_code = "CN02010007"
    warehouse = "Stores - MS"
    
    frappe.db.sql("DELETE FROM `tabMaterial Request` WHERE custom_batch_planning_no = %s", bp_name)
    frappe.db.sql("DELETE FROM `tabMaterial Request Item` WHERE batch_planning_id = %s", bp_name)
    frappe.db.sql("DELETE FROM `tabPurchase Order` WHERE project = %s AND name LIKE 'TEST-PO%%'", project)
    frappe.db.sql("DELETE FROM `tabPurchase Order Item` WHERE batch_planning_id = %s", bp_name)
    frappe.db.sql("DELETE FROM `tabPurchase Receipt` WHERE project = %s AND name LIKE 'TEST-PR%%'", project)
    frappe.db.sql("DELETE FROM `tabPurchase Receipt Item` WHERE batch_planning_id = %s", bp_name)
    frappe.db.sql("DELETE FROM `tabStock Ledger Entry` WHERE batch_planning_id = %s", bp_name)
    frappe.db.commit()

    print("\n" + "="*50)
    print("STEP 1: Create & Submit MR for qty = 50")
    print("="*50)
    frappe.db.sql("""
        INSERT INTO `tabMaterial Request` (name, docstatus, project, custom_batch_planning_no, status, material_request_type, creation, modified)
        VALUES ('TEST-MR-1', 1, %s, %s, 'Pending', 'Purchase', NOW(), NOW())
    """, (project, bp_name))
    
    frappe.db.sql("""
        INSERT INTO `tabMaterial Request Item` (name, parent, item_code, qty, ordered_qty, batch_planning_id, creation, modified)
        VALUES ('TEST-MR-ITM-1', 'TEST-MR-1', %s, 50.0, 0.0, %s, NOW(), NOW())
    """, (item_code, bp_name))
    frappe.db.commit()
    print("Created MR: TEST-MR-1")
    check_status(item_code, bp_name, project, warehouse)

    
    print("\n" + "="*50)
    print("STEP 2: Create & Submit PO for qty = 30 (Converted from MR)")
    print("="*50)
    frappe.db.sql("""
        INSERT INTO `tabPurchase Order` (name, docstatus, project, status, creation, modified)
        VALUES ('TEST-PO-1', 1, %s, 'Pending', NOW(), NOW())
    """, (project,))
    
    frappe.db.sql("""
        INSERT INTO `tabPurchase Order Item` (name, parent, item_code, qty, received_qty, batch_planning_id, material_request, material_request_item, creation, modified)
        VALUES ('TEST-PO-ITM-1', 'TEST-PO-1', %s, 30.0, 0.0, %s, 'TEST-MR-1', 'TEST-MR-ITM-1', NOW(), NOW())
    """, (item_code, bp_name))
    
    frappe.db.sql("UPDATE `tabMaterial Request Item` SET ordered_qty = 30.0 WHERE name = 'TEST-MR-ITM-1'")
    frappe.db.commit()
    print("Created PO: TEST-PO-1")
    check_status(item_code, bp_name, project, warehouse)

    
    print("\n" + "="*50)
    print("STEP 3: Create & Submit GRN (PR) for qty = 10 (Approved)")
    print("="*50)
    frappe.db.sql("""
        INSERT INTO `tabPurchase Receipt` (name, docstatus, project, status, creation, modified)
        VALUES ('TEST-PR-1', 1, %s, 'Pending', NOW(), NOW())
    """, (project,))
    
    frappe.db.sql("""
        INSERT INTO `tabPurchase Receipt Item` (name, parent, item_code, qty, batch_planning_id, purchase_order, purchase_order_item, creation, modified)
        VALUES ('TEST-PR-ITM-1', 'TEST-PR-1', %s, 10.0, %s, 'TEST-PO-1', 'TEST-PO-ITM-1', NOW(), NOW())
    """, (item_code, bp_name))
    
    frappe.db.sql("UPDATE `tabPurchase Order Item` SET received_qty = 10.0 WHERE name = 'TEST-PO-ITM-1'")
    
    frappe.db.sql("""
        INSERT INTO `tabStock Ledger Entry` (name, item_code, warehouse, actual_qty, batch_planning_id, project, is_cancelled, creation, modified, voucher_type, voucher_no)
        VALUES ('TEST-SLE-1', %s, %s, 10.0, %s, %s, 0, NOW(), NOW(), 'Purchase Receipt', 'TEST-PR-1')
    """, (item_code, warehouse, bp_name, project))
    frappe.db.commit()
    print("Created PR/GRN: TEST-PR-1 (Approved, docstatus=1)")
    check_status(item_code, bp_name, project, warehouse)

def check_status(item_code, bp_name, project, warehouse):
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

    qty_required = 100.0
    net_req = qty_required - main_stock - bp_mr_qty - bp_po_qty - bp_pr_qty

    print(f"  > main_stock: {main_stock}")
    print(f"  > bp_mr_qty: {bp_mr_qty}")
    print(f"  > bp_po_qty: {bp_po_qty}")
    print(f"  > bp_pr_qty: {bp_pr_qty}")
    print(f"  > net_requirement: {net_req}")
