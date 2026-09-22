import frappe


def stamp_material_allocation(doc, method=None):
    allocation = doc.get("custom_material_allocation")
    if not allocation:
        return

    if not frappe.db.exists("Material Allocation", allocation):
        return

    values = {"material_request": doc.name}

    if frappe.db.get_value("Material Allocation", allocation,
                           "allocation_status") == "Allocated":
        values["allocation_status"] = "Material Request Done"

    frappe.db.set_value(
        "Material Allocation",
        allocation,
        values,
        update_modified=False,
    )
