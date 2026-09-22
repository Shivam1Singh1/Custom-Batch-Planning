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
