app_name = "custom_batch_planning"
app_title = "Custom Batch Planning"
app_publisher = "Shivam Singh"
app_description = "Custom Batch Planning Module"
app_email = "shivam.singh@microcrispr.com"
app_license = "mit"


app_include_js = "/assets/custom_batch_planning/js/custom_batch_planning.js"


fixtures = [
    {
        "dt": "Custom Field",
        "filters": [
            ["fieldname", "in", [
                "custom_batch_no",
                "custom_employee_function",
                "custom_employee_functions",
                "custom_batch_planning",
                "custom_enable_manufacturing_batch",
                "custom_batch_planning_no",
                "custom_material_allocation",
                "batch_planning_id"
            ]]
        ]
    },
    {
        "dt": "Inventory Dimension",
        "filters": [
            ["name", "in", [
                "Batch Planning ID",
                "Employee Function",
                "oligo bank"
            ]]
        ]
    }
]

doctype_js = {
    "Material Request": "public/js/material_request_prefill.js",
    "Purchase Order": "public/js/purchase_order_consolidation.js"
}

doc_events = {
    "Material Request": {
        "validate": [
            "custom_batch_planning.api.pr_integration.validate_material_request",
            "custom_batch_planning.api.tagging_enforcement.validate_tagging",
        ],
        "after_insert": "custom_batch_planning.api.ma_link.stamp_material_allocation",
    },
    "Purchase Order": {
        "validate": [
            "custom_batch_planning.api.po_integration.validate_purchase_order",
            "custom_batch_planning.api.tagging_enforcement.validate_tagging",
        ],
        "before_insert": "custom_batch_planning.hooks_po_grn.set_batch_planning_id_on_po"
    },
    "Purchase Receipt": {
        "validate": "custom_batch_planning.api.tagging_enforcement.validate_tagging",
        "before_save": "custom_batch_planning.api.pr_integration.map_purchase_receipt_fields",
        "before_insert": "custom_batch_planning.hooks_po_grn.set_batch_planning_id_on_grn",
        "on_submit": "custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning.sync_batch_expiry_from_grn"
    },
    "Stock Entry": {
        "validate": "custom_batch_planning.api.tagging_enforcement.validate_tagging",
        "before_save": "custom_batch_planning.api.pr_integration.map_stock_entry_fields",
        "on_submit": [
            "custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning.on_stock_entry_submit",
            "custom_batch_planning.custom_batch_planning.doctype.material_allocation.material_allocation.stock_entry_on_submit",
        ]
    },
    "Purchase Invoice": {
        "validate": "custom_batch_planning.api.pr_integration.map_purchase_invoice_fields"
    },
    "Stock Ledger Entry": {
        "before_insert": "custom_batch_planning.api.pr_integration.map_sle_fields"
    }
}