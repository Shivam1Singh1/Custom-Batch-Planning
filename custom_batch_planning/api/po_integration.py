import frappe
from custom_batch_planning.api.pr_integration import consolidate_items_table

def validate_purchase_order(doc, method):

    consolidate_items_table(doc)

    if not doc.get("custom_batch_planning_no"):
        bp_nos = []
        for item in doc.items:
            if item.get("custom_batch_planning_no"):
                bp_nos.append(item.custom_batch_planning_no)
            elif item.get("material_request"):
                mr_bp_no = frappe.db.get_value(
                    "Material Request",
                    item.material_request,
                    "custom_batch_planning_no"
                )
                if mr_bp_no:
                    bp_nos.append(mr_bp_no)
        
        if bp_nos:
            unique_bp_nos = []
            for b in bp_nos:
                if b and b not in unique_bp_nos:
                    unique_bp_nos.append(b)
            doc.custom_batch_planning_no = ", ".join(unique_bp_nos)

    if hasattr(doc, "calculate_taxes_and_totals"):
        doc.calculate_taxes_and_totals()
