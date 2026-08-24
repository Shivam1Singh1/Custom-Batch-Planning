app_name = "custom_batch_planning"
app_title = "Custom Batch Planning"
app_publisher = "Shivam Singh"
app_description = "Custom Batch Planning Module"
app_email = "shivam.singh@microcrispr.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "custom_batch_planning",
# 		"logo": "/assets/custom_batch_planning/logo.png",
# 		"title": "Custom Batch Planning",
# 		"route": "/custom_batch_planning",
# 		"has_permission": "custom_batch_planning.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/custom_batch_planning/css/custom_batch_planning.css"
app_include_js = "/assets/custom_batch_planning/js/custom_batch_planning.js"

# include js, css files in header of web template
# web_include_css = "/assets/custom_batch_planning/css/custom_batch_planning.css"
# web_include_js = "/assets/custom_batch_planning/js/custom_batch_planning.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "custom_batch_planning/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "custom_batch_planning/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "custom_batch_planning.utils.jinja_methods",
# 	"filters": "custom_batch_planning.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "custom_batch_planning.install.before_install"
# after_install = "custom_batch_planning.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "custom_batch_planning.uninstall.before_uninstall"
# after_uninstall = "custom_batch_planning.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "custom_batch_planning.utils.before_app_install"
# after_app_install = "custom_batch_planning.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "custom_batch_planning.utils.before_app_uninstall"
# after_app_uninstall = "custom_batch_planning.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "custom_batch_planning.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"custom_batch_planning.tasks.all"
# 	],
# 	"daily": [
# 		"custom_batch_planning.tasks.daily"
# 	],
# 	"hourly": [
# 		"custom_batch_planning.tasks.hourly"
# 	],
# 	"weekly": [
# 		"custom_batch_planning.tasks.weekly"
# 	],
# 	"monthly": [
# 		"custom_batch_planning.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "custom_batch_planning.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "custom_batch_planning.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "custom_batch_planning.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["custom_batch_planning.utils.before_request"]
# after_request = ["custom_batch_planning.utils.after_request"]

# Job Events
# ----------
# before_job = ["custom_batch_planning.utils.before_job"]
# after_job = ["custom_batch_planning.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"custom_batch_planning.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

fixtures = [
    # 1. Custom Fields
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
                "batch_planning_id"
            ]]
        ]
    },
    # 2. Inventory Dimensions
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

# Tagging enforcement is wired on `validate`, never `before_insert`.
#
# before_insert fires once, at creation. A document created before the cutover
# as a draft and edited or amended afterwards would keep its blanks forever.
# validate runs on every save and on submit (frappe runs validate then
# before_save / before_submit), so the rule holds for the whole life of the
# document, which is the only way the post-cutover guarantee is actually a
# guarantee.
#
# On Stock Entry this also puts enforcement ahead of map_stock_entry_fields,
# which is a before_save hook: the copy-down then has valid values to copy.
doc_events = {
    "Material Request": {
        "validate": [
            "custom_batch_planning.api.pr_integration.validate_material_request",
            "custom_batch_planning.api.tagging_enforcement.validate_tagging",
        ]
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
            # Flips the linked allocation to "Stock Entry Done". Server-side so it
            # fires for every submit, not only when the form happens to be open.
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