import frappe

def execute():
    print(frappe.db.exists("DocType", "Inventory Dimension"))
    print(frappe.get_meta("Stock Ledger Entry").get_field("batch_planning_id").options)
