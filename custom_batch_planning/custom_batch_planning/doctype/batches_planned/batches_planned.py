import frappe
from frappe.model.document import Document


class BatchesPlanned(Document):

    def on_trash(self):
        if frappe.db.exists(
            "Material Allocation",
            {
                "batch_planning": self.batch_planning,
                "allocation_status": ["not in", ("Deallocated", "Stock Entry Done")],
                "docstatus": ["!=", 2],
            },
        ):
            frappe.throw(
                f"Cannot delete Batches Planned <b>{self.name}</b>. "
                f"A live Material Allocation exists for Batch Planning "
                f"<b>{self.batch_planning}</b>."
            )

        if frappe.flags.get("skip_sct_decrement"):
            return

        if self.slot_opening_id and self.slot_booking_date:
            slot_master = frappe.db.get_value(
                "Slot Opening", self.slot_opening_id, "slot_master"
            )
            if slot_master:
                sct_name = frappe.db.get_value(
                    "Slot Capacity Tracker", {"slot_master": slot_master}, "name"
                )
                if sct_name:
                    sct_detail = frappe.db.get_value(
                        "Slot Capacity Detail",
                        {
                            "parent": sct_name,
                            "parenttype": "Slot Capacity Tracker",
                            "date": self.slot_booking_date,
                        },
                        ["name", "batches_planned"],
                        as_dict=True,
                    )
                    if sct_detail:
                        new_planned = max(0, int(sct_detail.batches_planned or 0) - 1)
                        frappe.db.set_value(
                            "Slot Capacity Detail",
                            sct_detail.name,
                            "batches_planned",
                            new_planned,
                        )
