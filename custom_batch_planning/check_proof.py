import frappe
def execute():
    try:
        frappe.clear_cache(doctype="Stock Ledger Entry")
        opt = frappe.get_meta("Stock Ledger Entry").get_field("batch_planning_id").options
        print(f"Option is now: {opt}")
        
        try:
            dims = frappe.get_all("Inventory Dimension", fields=["name", "document_type"])
            print(f"Inventory Dimensions: {dims}")
        except frappe.exceptions.DoesNotExistError:
            print("Inventory Dimension doctype does not exist")
    except Exception as e:
        print(f"Error: {e}")
