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
    frappe.db.set_value(
        "Material Allocation",
        allocation,
        "material_request",
        doc.name,
        update_modified=False,
    )
