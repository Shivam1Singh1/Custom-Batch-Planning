"""
Adds `custom_material_allocation` to Material Request.

Material Allocation.make_material_request returns an unsaved request, so the
allocation can only be linked once the user saves it. This field is what
carries the allocation across that gap, and api/ma_link.py reads it on insert.

Not mandatory, and shown only for Material Transfer: a request raised by hand,
or for a Purchase, has no allocation behind it and should not be asked for one.
Read only because it is filled by the button, not typed.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
    create_custom_fields(
        {
            "Material Request": [
                {
                    "fieldname": "custom_material_allocation",
                    "label": "Material Allocation",
                    "fieldtype": "Link",
                    "options": "Material Allocation",
                    "insert_after": "custom_batch_planning_no",
                    "depends_on": 'eval:doc.material_request_type=="Material Transfer"',
                    "read_only": 1,
                    "no_copy": 1,
                    "reqd": 0,
                }
            ]
        },
        ignore_validate=True,
    )
    frappe.clear_cache(doctype="Material Request")
