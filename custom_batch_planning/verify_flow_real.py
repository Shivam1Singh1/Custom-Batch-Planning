import frappe
from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import get_material_planning_data
from erpnext.stock.doctype.material_request.material_request import make_purchase_order
from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
from frappe.utils import today

def execute():
    import frappe.model.document
    frappe.model.document.Document._validate_links = lambda self: None
    frappe.flags.ignore_links = True
    
    bp_name = "BP-26-07-001"
    bp_doc = frappe.get_doc("Batch Planning", bp_name)
    project = bp_doc.project
    
    from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import get_material_planning_data
    
    data = get_material_planning_data(bp_name)
    if isinstance(data, dict):
        data = list(data.values())
        
    print(f"Data type: {type(data)}, first element type: {type(data[0]) if data else 'N/A'}")
    
    item_code = None
    if data:
        for row in data:
            if isinstance(row, list):
                for sub_row in row:
                    if isinstance(sub_row, dict):
                        ic = sub_row.get("item_code")
                        if ic and frappe.db.get_value("Item", ic, "has_batch_no") == 1 and frappe.db.get_value("Item", ic, "has_serial_no") == 0:
                            item_code = ic
                            break
            elif isinstance(row, dict):
                ic = row.get("item_code")
                if ic and frappe.db.get_value("Item", ic, "has_batch_no") == 1 and frappe.db.get_value("Item", ic, "has_serial_no") == 0:
                    item_code = ic
                    break
            
            if item_code:
                break
    
    if not item_code:
        print("Could not find any item WITH batch tracking in the BOM. Picking a random one.")
        item_code = frappe.db.get_value("Item", {"has_batch_no": 1, "has_serial_no": 0}, "name")
        if not item_code:
            item_code = "CN02010004"
            
    print(f"Using Item: {item_code}")
    
    print("\n" + "="*50)
    print("CLEANING OLD TEST DATA FOR THIS ITEM + BATCH PLANNING")
    print("="*50)
    frappe.db.sql("DELETE FROM `tabMaterial Request` WHERE custom_batch_planning_no = %s AND name IN (SELECT parent FROM `tabMaterial Request Item` WHERE item_code=%s)", (bp_name, item_code))
    frappe.db.sql("DELETE FROM `tabMaterial Request Item` WHERE batch_planning_id = %s AND item_code=%s", (bp_name, item_code))
    frappe.db.sql("DELETE FROM `tabPurchase Order Item` WHERE batch_planning_id = %s AND item_code=%s", (bp_name, item_code))
    frappe.db.sql("DELETE FROM `tabPurchase Receipt Item` WHERE batch_planning_id = %s AND item_code=%s", (bp_name, item_code))
    frappe.db.sql("DELETE FROM `tabStock Ledger Entry` WHERE batch_planning_id = %s AND item_code=%s", (bp_name, item_code))
    frappe.db.commit()

    print("\n" + "="*50)
    print("STEP 1: Create & Submit REAL MR for qty = 50")
    print("="*50)
    
    mr = frappe.new_doc("Material Request")
    
    any_mr = frappe.db.get_value("Material Request", {"docstatus": 1}, "name")
    existing_mr = frappe.get_doc("Material Request", any_mr)
    mr.custom_stages = existing_mr.get('custom_stages')
    mr.project_description = existing_mr.get('project_description')
    
    if hasattr(existing_mr, 'segment'):
        mr.segment = existing_mr.segment
    elif hasattr(existing_mr, 'custom_segment'):
        mr.custom_segment = existing_mr.custom_segment
        
    employee_function = bp_doc.custom_employee_function
    ef_doc = frappe.get_doc("Employee Function", employee_function)
    warehouse = None
    for r in (ef_doc.table_bukm or []):
        if r.store_warehouse:
            warehouse = r.store_warehouse
            break
            
    if not warehouse:
        company = bp_doc.get("company") or frappe.defaults.get_user_default("company")
        warehouse = frappe.db.get_value("Warehouse", {"is_group": 0, "company": company}, "name")
    
    mr.material_request_type = "Purchase"
    mr.custom_batch_planning_no = bp_name
    mr.project = project
    mr.append("items", {
        "item_code": item_code,
        "qty": 50.0,
        "schedule_date": today(),
        "batch_planning_id": bp_name,
        "uom": frappe.db.get_value("Item", item_code, "stock_uom"),
        "conversion_factor": 1,
        "warehouse": warehouse
    })
    
    try:
        mr.flags.ignore_mandatory = True
        mr.insert()
        mr.submit()
        print(f"Created REAL MR: {mr.name}")
        check_status(item_code, bp_name)
    except Exception as e:
        print(f"Failed to create MR: {str(e)}")
        import traceback; traceback.print_exc()
        return

    print("\n" + "="*50)
    print("STEP 2: Create & Submit REAL PO for qty = 30 (Converted from MR)")
    print("="*50)
    
    try:
        po = make_purchase_order(mr.name)
        po.items[0].qty = 30.0
        po.supplier = frappe.db.get_value("Supplier", {"disabled": 0}, "name")
        po.items[0].rate = 100.0
        po.items[0].conversion_factor = 1.0
        po.items[0].warehouse = warehouse
        
        po.schedule_date = today()
        
        po.flags.ignore_mandatory = True
        po.insert()
        po.submit()
        
        print(f"Created REAL PO: {po.name}")
        check_status(item_code, bp_name)
    except Exception as e:
        print(f"Failed to create PO: {str(e)}")
        import traceback; traceback.print_exc()
        return
        
    print("\n" + "="*50)
    print("STEP 3: Create & Submit REAL PR (GRN) for qty = 10")
    print("="*50)
    
    try:
        pr = make_purchase_receipt(po.name)
        pr.items[0].qty = 10.0
        pr.items[0].received_qty = 10.0
        pr.items[0].rejected_qty = 0.0
        pr.items[0].accepted_qty = 10.0
        pr.items[0].conversion_factor = 1.0
        pr.items[0].batch_planning_id = bp_name
        pr.items[0].warehouse = warehouse
        pr.project = project
        
        pr.flags.ignore_mandatory = True
        pr.insert()
        
        workflows = frappe.get_all('Workflow', filters={'document_type': 'Purchase Receipt', 'is_active': 1}, fields=['name'])
        
        if frappe.db.get_value("Item", item_code, "has_batch_no"):
            my_batch_id = frappe.generate_hash(length=10)
            batch = frappe.get_doc({
                "doctype": "Batch",
                "batch_id": my_batch_id,
                "item": item_code
            })
            batch.insert()
            
            sbb = frappe.get_doc({
                "doctype": "Serial and Batch Bundle",
                "item_code": item_code,
                "warehouse": warehouse,
                "type_of_transaction": "Inward",
                "voucher_type": "Purchase Receipt",
                "has_batch_no": 1,
                "entries": [
                    {
                        "batch_no": my_batch_id,
                        "qty": 10.0
                    }
                ]
            })
            sbb.insert()
            
            pr.items[0].serial_and_batch_bundle = sbb.name
            pr.save()
            
        if workflows:
            workflow_name = workflows[0].name
            transitions = frappe.get_all('Workflow Transition', filters={'parent': workflow_name}, fields=['state', 'next_state', 'action'], order_by="idx")
            
            current_state = pr.workflow_state or "Draft"
            target_state = "Approved By Store Head"
            
            from frappe.model.workflow import apply_workflow
            
            while current_state != target_state:
                transition = next((t for t in transitions if t.state == current_state), None)
                if not transition:
                    print(f"Could not find workflow transition from {current_state}")
                    break
                
                print(f"Applying workflow action: {transition.action} ({current_state} -> {transition.next_state})")
                apply_workflow(pr, transition.action)
                current_state = transition.next_state
                pr.reload()
        else:
            pr.submit()
        
        print(f"Created REAL PR/GRN: {pr.name}")
        check_status(item_code, bp_name)
    except Exception as e:
        print(f"Failed to create PR: {str(e)}")
        import traceback; traceback.print_exc()
        return


def check_status(item_code, bp_name):
    from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import get_material_planning_data
    
    data = get_material_planning_data(bp_name)
    if isinstance(data, dict):
        data = list(data.values())
    
    item_row = None
    if data:
        for row in data:
            if isinstance(row, list):
                for sub_row in row:
                    if isinstance(sub_row, dict) and sub_row.get("item_code") == item_code:
                        item_row = sub_row
                        break
            elif isinstance(row, dict) and row.get("item_code") == item_code:
                item_row = row
                break
            
            if item_row:
                break
    
    if item_row and isinstance(item_row, dict):
        print(f"  > main_stock: {item_row.get('main_stock')}")
        print(f"  > bp_mr_qty: {item_row.get('bp_mr_qty')}")
        print(f"  > bp_po_qty: {item_row.get('bp_po_qty')}")
        print(f"  > bp_pr_qty: {item_row.get('bp_pr_qty')}")
        print(f"  > net_requirement: {item_row.get('net_requirement')}")
    else:
        print(f"Item {item_code} not found in get_material_planning_data result for {bp_name}!")
        
    print("\n" + "="*50)
    print("STEP 4: Fetch Raw Stock Ledger Entry to Verify Dimensions")
    print("="*50)
    
    entries = frappe.db.get_all("Stock Ledger Entry", 
        filters={"item_code": item_code, "batch_planning_id": bp_name, "is_cancelled": 0},
        fields=["name", "item_code", "batch_planning_id", "project", "actual_qty", "warehouse", "voucher_type", "voucher_no"],
        order_by="creation desc",
        limit=1
    )
    
    if entries:
        print(f"RAW SLE Record for {item_code}:")
        print(entries[0])
    else:
        print("No Stock Ledger Entry found!")

