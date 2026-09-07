"""
Keeps `Material Allocation.material_request` in step with the request the user
actually saves.

Material Allocation.make_material_request hands the client an unsaved Material
Request, so no name exists at build time and the allocation cannot be stamped
there. The unsaved doc carries `custom_material_allocation` instead; this fires
on insert and writes the link back.

Only the stamping direction needs a hook. A request that is later cancelled or
deleted leaves a stale pointer behind, and get_linked_material_request on the
allocation already clears that on read - so there is deliberately no on_cancel
or on_trash handler here to be kept in sync with it.
"""

import frappe


def stamp_material_allocation(doc, method=None):
    allocation = doc.get("custom_material_allocation")
    if not allocation:
        return

    if not frappe.db.exists("Material Allocation", allocation):
        return

    # update_modified=False because the user did not touch the allocation.
    # Bumping its timestamp would make every open Material Allocation form
    # report a document-changed conflict on the next save.
    values = {"material_request": doc.name}

    # Advance the lifecycle, but only from "Allocated". Saving a request must not
    # drag a Deallocated or already-transferred allocation backwards, and this
    # hook fires on insert of any request carrying the link — including one
    # raised against an allocation whose state has since moved on.
    #
    # The status change does NOT release the stock: _HOLDS_STOCK counts
    # "Material Request Done" exactly as it counts "Allocated", because nothing
    # has physically moved yet.
    if frappe.db.get_value("Material Allocation", allocation,
                           "allocation_status") == "Allocated":
        values["allocation_status"] = "Material Request Done"

    frappe.db.set_value(
        "Material Allocation",
        allocation,
        values,
        update_modified=False,
    )
