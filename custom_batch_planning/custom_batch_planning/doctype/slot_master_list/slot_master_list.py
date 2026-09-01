import frappe
from frappe.model.document import Document
from frappe.model.naming import make_autoname
from frappe.utils import getdate, today, add_days

class SlotMasterList(Document):

    def autoname(self):

        if not self.batch_start_date:
            frappe.throw("Planning Start Date is required to generate name.")

        date = getdate(self.batch_start_date)
        yy = str(date.year)[2:]
        mm = str(date.month).zfill(2)

        prefix = f"SM-{yy}-{mm}-"

        self.name = make_autoname(f"{prefix}.##")

        while frappe.db.exists("Slot Master List", self.name):
            self.name = make_autoname(f"{prefix}.##")

    def before_save(self):

        try:
            capacity = int(self.batch_capacity)
        except (TypeError, ValueError):
            frappe.throw("Daily Batch Capacity must be a valid whole number.")

        if capacity < 1:
            frappe.throw("Daily Batch Capacity must be at least 1.")

        self.batch_capacity = capacity

        start_date = str(self.batch_start_date) if self.batch_start_date else None
        end_date = str(self.batch_end_date) if self.batch_end_date else None
        today_date = today()

        if self.is_new():
            if start_date and start_date < today_date:
                frappe.throw("Planning Start Date cannot be a past date.")

            if end_date and end_date < today_date:
                frappe.throw("Planning End Date cannot be a past date.")

        if start_date and end_date:

            if start_date > end_date:
                frappe.throw(
                    "Planning Start Date must be less than or equal to Planning End Date."
                )

            s = getdate(start_date)
            e = getdate(end_date)

            if s.month != e.month or s.year != e.year:
                frappe.throw(
                    "Planning Start Date and Planning End Date must be in the same month."
                )

        existing = frappe.db.exists({
            "doctype": "Slot Master List",
            "employee_function": self.employee_function,
            "project": self.project,
            "name": ["!=", self.name],
            "docstatus": ["!=", 2],
            "batch_start_date": ["<=", self.batch_end_date],
            "batch_end_date": [">=", self.batch_start_date],
        })

        if existing:
            existing_doc = frappe.get_doc("Slot Master List", existing)
            frappe.throw(
                f"Overlapping slot already exists for "
                f"<b>{existing_doc.employee_function_head_name}</b> and Project <b>{existing_doc.project}</b>.<br>"
                f"Existing Slot: <b>{existing_doc.name}</b><br>"
                f"From: "
                f"<b>{getdate(existing_doc.batch_start_date).strftime('%d-%m-%Y')}</b> "
                f"To: "
                f"<b>{getdate(existing_doc.batch_end_date).strftime('%d-%m-%Y')}</b><br>"
                f"Daily Batch Capacity: <b>{existing_doc.batch_capacity}</b>"
            )

    def before_submit(self):

        slot_capacity_tracker = frappe.new_doc("Slot Capacity Tracker")

        slot_capacity_tracker.flags.name_set = True
        slot_capacity_tracker.name = self.name.replace("SM-", "SCT-")

        slot_capacity_tracker.slot_master = self.name
        slot_capacity_tracker.employee_function = self.employee_function
        slot_capacity_tracker.employee_headname = self.employee_function_head_name
        slot_capacity_tracker.batch_start_date = self.batch_start_date
        slot_capacity_tracker.batch_end_date = self.batch_end_date

        current_date = getdate(self.batch_start_date)
        batch_end = getdate(self.batch_end_date)

        while current_date <= batch_end:
            slot_capacity_tracker.append("slot_capacity_detail", {
                "date": current_date,
                "total_capacity": self.batch_capacity,
                "capacity_booked": 0,
                "capacity_available": self.batch_capacity,
            })
            current_date = add_days(current_date, 1)

        slot_capacity_tracker.insert(ignore_permissions=True)

    def on_trash(self):

        if frappe.db.exists(
            "Slot Opening",
            {"slot_master": self.name}
        ):
            frappe.throw(
                f"Cannot delete Slot Master "
                f"<b>{self.name}</b>. "
                f"Slot Openings exist for it (including cancelled). "
                f"Please delete all linked Slot Openings first."
            )

@frappe.whitelist()
def get_employee_function_projects(employee_function):
    if not employee_function:
        return []
    rows = frappe.get_all(
        "Project list",
        filters={"parent": employee_function},
        fields=["projects"],
    )
    return [r.projects for r in rows if r.projects]
