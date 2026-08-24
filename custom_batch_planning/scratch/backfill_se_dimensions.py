import frappe

def run():
    print("--- DRY RUN: Backfilling Stock Entry and SLE dimensions ---")
    
    stock_entries = frappe.db.sql("""
        SELECT name, custom_material_allocation, custom_batch_planning_no, project 
        FROM `tabStock Entry` 
        WHERE docstatus = 1 
        AND (custom_batch_planning_no IS NOT NULL AND custom_batch_planning_no != '')
    """, as_dict=True)
    
    if not stock_entries:
        print("No submitted Stock Entries with custom_batch_planning_no found.")
        return
        
    updated_sed = 0
    updated_sle = 0
    
    for se in stock_entries:
        details = frappe.db.sql("""
            SELECT name, item_code, batch_planning_id, project 
            FROM `tabStock Entry Detail` 
            WHERE parent = %s 
            AND (batch_planning_id IS NULL OR batch_planning_id = '' OR project IS NULL OR project = '')
        """, (se.name,), as_dict=True)
        
        sles = frappe.db.sql("""
            SELECT name, item_code, batch_planning_id, project, warehouse, actual_qty
            FROM `tabStock Ledger Entry` 
            WHERE voucher_type = 'Stock Entry' 
            AND voucher_no = %s 
            AND (batch_planning_id IS NULL OR batch_planning_id = '' OR project IS NULL OR project = '')
        """, (se.name,), as_dict=True)
        
        if not details and not sles:
            continue
            
        ma_project = None
        if se.custom_material_allocation:
            ma_project = frappe.db.get_value("Material Allocation", se.custom_material_allocation, "project_id")
            
        bp_id = se.custom_batch_planning_no
        project = ma_project or se.project
        
        print(f"\nFound Stock Entry: {se.name} (Batch Planning: {bp_id}, Project: {project})")
        
        for d in details:
            print(f"  [Dry Run] Would update Stock Entry Detail: {d.name} (Item: {d.item_code}) -> set batch_planning_id='{bp_id}', project='{project}'")
            updated_sed += 1
            
        for sle in sles:
            print(f"  [Dry Run] Would update Stock Ledger Entry: {sle.name} (Item: {sle.item_code}, Warehouse: {sle.warehouse}, Qty: {sle.actual_qty}) -> set batch_planning_id='{bp_id}', project='{project}'")
            updated_sle += 1

    print("\n--- Summary ---")
    print(f"Total Stock Entry Details to update: {updated_sed}")
    print(f"Total Stock Ledger Entries to update: {updated_sle}")

def run_actual():
    print("--- ACTUAL RUN: Backfilling Stock Entry and SLE dimensions ---")
    
    stock_entries = frappe.db.sql("""
        SELECT name, custom_material_allocation, custom_batch_planning_no, project 
        FROM `tabStock Entry` 
        WHERE docstatus = 1 
        AND (custom_batch_planning_no IS NOT NULL AND custom_batch_planning_no != '')
    """, as_dict=True)
    
    if not stock_entries:
        print("No submitted Stock Entries with custom_batch_planning_no found.")
        return
        
    updated_sed = 0
    updated_sle = 0
    
    for se in stock_entries:
        details = frappe.db.sql("""
            SELECT name, item_code, batch_planning_id, project 
            FROM `tabStock Entry Detail` 
            WHERE parent = %s 
            AND (batch_planning_id IS NULL OR batch_planning_id = '' OR project IS NULL OR project = '')
        """, (se.name,), as_dict=True)
        
        sles = frappe.db.sql("""
            SELECT name, item_code, batch_planning_id, project 
            FROM `tabStock Ledger Entry` 
            WHERE voucher_type = 'Stock Entry' 
            AND voucher_no = %s 
            AND (batch_planning_id IS NULL OR batch_planning_id = '' OR project IS NULL OR project = '')
        """, (se.name,), as_dict=True)
        
        if not details and not sles:
            continue
            
        ma_project = None
        if se.custom_material_allocation:
            ma_project = frappe.db.get_value("Material Allocation", se.custom_material_allocation, "project_id")
            
        bp_id = se.custom_batch_planning_no
        project = ma_project or se.project
        
        if details:
            print(f"Updating {len(details)} details for SE {se.name}...")
            for d in details:
                frappe.db.set_value("Stock Entry Detail", d.name, {
                    "batch_planning_id": bp_id,
                    "project": project
                }, update_modified=False)
                updated_sed += 1
                
        if sles:
            print(f"Updating {len(sles)} SLEs for SE {se.name}...")
            for sle in sles:
                frappe.db.set_value("Stock Ledger Entry", sle.name, {
                    "batch_planning_id": bp_id,
                    "project": project
                }, update_modified=False)
                updated_sle += 1
                
    if updated_sed or updated_sle:
        frappe.db.commit()
        print("Changes committed to database.")

    print("\n--- Summary ---")
    print(f"Total Stock Entry Details updated: {updated_sed}")
    print(f"Total Stock Ledger Entries updated: {updated_sle}")
