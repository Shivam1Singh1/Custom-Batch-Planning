import frappe
from frappe.model.document import Document
from frappe.model.naming import make_autoname

@frappe.whitelist()
def get_sct_details(slot_master=None, date=None):

    if not slot_master:
        return []

    sct_name = frappe.db.get_value(
        "Slot Capacity Tracker",
        {"slot_master": slot_master},
        "name"
    )

    if not sct_name:
        return []

    filters = {"parent": sct_name}

    if date:
        filters["date"] = date

    return frappe.get_all(
        "Slot Capacity Detail",
        filters=filters,
        fields=[
            "name",
            "date",
            "total_capacity",
            "capacity_booked",
            "capacity_available",
            "batches_planned"
        ],
        ignore_permissions=True
    )

@frappe.whitelist()
def get_slot_opening_usage(slot_opening=None):
    """Per date: how many of THIS Slot Opening's booked slots are still unused.

    Slot Capacity Detail.batches_planned cannot answer this. It is keyed on
    (slot master, date) alone, so every batch planned against that date
    increments it no matter which Slot Opening produced it. Comparing that
    shared counter against one Slot Opening's own planning_capacity — which is
    what the Create Batch button used to do — makes a neighbour's batch consume
    this opening's button: two slots on a date, another opening plans one, and
    this one is told it is full while holding an untouched booking.

    Counted from the batches themselves instead. Batches Planned carries the
    Batch Planning that made it, and Batch Planning carries the Slot Opening it
    came from, so the chain back to one opening is exact and needs no counter to
    stay in step. Cancelled batches (docstatus 2) do not hold a slot.

    Returns one row per booked date:
        date, booked, planned, remaining
    """
    if not slot_opening:
        return []

    bookings = frappe.db.sql(
        """
        SELECT slot_booking_date AS date, IFNULL(planning_capacity, 0) AS booked
        FROM `tabSlot Booking CT`
        WHERE parent = %(so)s AND parenttype = 'Slot Opening'
          AND slot_booking_date IS NOT NULL
        """,
        {"so": slot_opening},
        as_dict=True,
    )
    if not bookings:
        return []

    used = dict(
        frappe.db.sql(
            """
            SELECT bp.slot_booking_date, COUNT(*)
            FROM `tabBatches Planned` bp
            INNER JOIN `tabBatch Planning` p ON p.name = bp.batch_planning
            WHERE p.slot_opening = %(so)s
              AND bp.docstatus <> 2
              AND p.docstatus <> 2
            GROUP BY bp.slot_booking_date
            """,
            {"so": slot_opening},
        )
        or []
    )

    out = []
    for row in bookings:
        booked = int(row.booked or 0)
        planned = int(used.get(row.date, 0) or 0)
        out.append({
            "date": str(row.date),
            "booked": booked,
            "planned": planned,
            "remaining": max(booked - planned, 0),
        })
    return out


@frappe.whitelist()
def get_calendar_data(employee_function=None, project=None):

    if not employee_function:
        return []

    conditions = ["sct.employee_function = %(employee_function)s", "sct.docstatus != 2"]
    args = {"employee_function": employee_function}

    if project:
        conditions.append("sm.project = %(project)s")
        args["project"] = project

    where_clause = " AND ".join(conditions)

    return frappe.db.sql(
        f"""
        SELECT
            sct.name AS sct_name,
            sct.slot_master,
            sct.employee_headname,
            sm.project,
            scd.date,
            scd.total_capacity,
            scd.capacity_booked,
            scd.capacity_available

        FROM
            `tabSlot Capacity Tracker` sct

        JOIN
            `tabSlot Capacity Detail` scd
            ON scd.parent = sct.name

        INNER JOIN
            `tabSlot Master List` sm
            ON sm.name = sct.slot_master

        WHERE
            {where_clause}

        ORDER BY
            scd.date ASC
        """,
        args,
        as_dict=True
    )

class SlotOpening(Document):

    def autoname(self):

        if not self.batch_start_date and self.slot_master:
            self.batch_start_date = frappe.db.get_value(
                "Slot Master List",
                self.slot_master,
                "batch_start_date"
            )

        if not self.batch_start_date:
            frappe.throw("Planning Start Date is required.")

        yy, mm = str(self.batch_start_date).split("-")[:2]

        self.name = make_autoname(
            f"SO-{yy[2:]}-{mm}-.###"
        )

    def before_save(self):

        if self.slot_master:
            slot_master_data = frappe.db.get_value(
                "Slot Master List",
                self.slot_master,
                ["project", "batch_capacity"],
                as_dict=True
            )
            if slot_master_data:
                self.project = slot_master_data.get("project")
                batch_capacity = slot_master_data.get("batch_capacity") or 0
                for row in self.slot_booking:
                    row.total_slots = batch_capacity

        if self.slot_master and not self.employee_function:
            frappe.throw(
                "Please select an Employee Function first before selecting a Slot Master."
            )

        self._validate_slot_dates()

        self._check_duplicate_full_capacity()

    def _validate_slot_dates(self):

        if not self.slot_master:
            return

        sm = frappe.get_doc(
            "Slot Master List",
            self.slot_master
        )

        start_date = str(sm.batch_start_date)
        end_date = str(sm.batch_end_date)

        for row in self.slot_booking:

            if not row.slot_booking_date:
                frappe.throw(
                    f"Row {row.idx}: Slot Booking Date is mandatory!"
                )

            booking_date = str(row.slot_booking_date)

            if booking_date < start_date:

                frappe.throw(
                    f"Row {row.idx}: Slot Booking Date "
                    f"<b>{booking_date}</b> cannot be before "
                    f"Planning Start Date <b>{start_date}</b>!"
                )

            if booking_date > end_date:

                frappe.throw(
                    f"Row {row.idx}: Slot Booking Date "
                    f"<b>{booking_date}</b> cannot be after "
                    f"Planning End Date <b>{end_date}</b>!"
                )

    def _check_duplicate_full_capacity(self):
        current_dates = [row.slot_booking_date for row in self.slot_booking]

        for date in current_dates:
            conflict = frappe.db.sql("""
                SELECT so.name
                FROM `tabSlot Opening` so
                JOIN `tabSlot Booking CT` sbc ON sbc.parent = so.name
                WHERE so.slot_master = %s
                AND so.employee_function = %s
                AND so.name != %s
                AND so.docstatus != 2
                AND sbc.slot_booking_date = %s
                LIMIT 1
            """, (self.slot_master, self.employee_function, self.name, date))

            if not conflict:
                continue

            sct_available = frappe.db.sql("""
                SELECT IFNULL(SUM(scd.capacity_available), 0)
                FROM `tabSlot Capacity Detail` scd
                JOIN `tabSlot Capacity Tracker` sct ON sct.name = scd.parent
                WHERE sct.slot_master = %s
                AND scd.date = %s
            """, (self.slot_master, date))[0][0]

            current_booked = next(
                (int(r.planning_capacity or 0) for r in self.slot_booking
                 if str(r.slot_booking_date) == str(date)), 0
            )

            if not self.is_new():
                old_booked = frappe.db.get_value(
                    "Slot Booking CT",
                    {"parent": self.name, "slot_booking_date": date},
                    "planning_capacity"
                ) or 0
                net_booking = current_booked - int(old_booked)
            else:
                net_booking = current_booked

            if (int(sct_available) - net_booking) < 0:
                frappe.throw(
                    f"Slot Opening <b>{conflict[0][0]}</b> already exists "
                    f"for this Employee Function on <b>{date}</b> "
                    f"and capacity is full."
                )

    def _update_sct(self):

        if not self.slot_master:
            return

        sct_name = frappe.db.get_value(
            "Slot Capacity Tracker",
            {"slot_master": self.slot_master},
            "name"
        )

        if not sct_name:
            return

        sct_doc = frappe.get_doc(
            "Slot Capacity Tracker",
            sct_name
        )

        for row in self.slot_booking:

            if not row.slot_booking_date:
                continue

            if not row.planning_capacity:
                continue

            booked = int(row.planning_capacity)

            sct_detail = next(
                (
                    d
                    for d in sct_doc.slot_capacity_detail
                    if str(d.date) == str(row.slot_booking_date)
                ),
                None
            )

            if not sct_detail:

                frappe.throw(
                    f"Date {row.slot_booking_date} "
                    f"not found in SCT ({sct_name})."
                )

            diff = booked

            available = int(sct_detail.capacity_available or 0)

            if diff > available:

                frappe.throw(
                    f"Date {row.slot_booking_date} has only "
                    f"{available} additional slot(s) available."
                )

            sct_detail.capacity_booked = (
                int(sct_detail.capacity_booked or 0) + diff
            )

            sct_detail.capacity_available = (
                int(sct_detail.total_capacity or 0)
                - int(sct_detail.capacity_booked)
            )

        sct_doc.flags.ignore_permissions = True
        sct_doc.flags.ignore_validate = True

        sct_doc.save()


    def on_submit(self):
        if getattr(self, "workflow_state", None) == "Approved":
            self._update_sct()

    def on_cancel(self):

        self._reverse_sct()

    def on_trash(self):

        if frappe.db.exists(
            "Batch Planning",
            {"slot_opening": self.name}
        ):

            frappe.throw(
                f"Cannot delete Slot Opening <b>{self.name}</b>. "
                f"Batch Planning exists for it."
            )

        if self.docstatus != 2:
            self._reverse_sct()

    def _reverse_sct(self):

        if not self.slot_master:
            return

        sct_name = frappe.db.get_value(
            "Slot Capacity Tracker",
            {"slot_master": self.slot_master},
            "name"
        )

        if not sct_name:
            return

        sct_doc = frappe.get_doc(
            "Slot Capacity Tracker",
            sct_name
        )

        for row in self.slot_booking:

            if not row.slot_booking_date:
                continue

            if not row.planning_capacity:
                continue

            booked = int(row.planning_capacity)

            sct_detail = next(
                (
                    d
                    for d in sct_doc.slot_capacity_detail
                    if str(d.date) == str(row.slot_booking_date)
                ),
                None
            )

            if not sct_detail:

                frappe.log_error(
                    f"Date {row.slot_booking_date} not found in SCT ({sct_name})",
                    "Slot Opening Cancel"
                )

                continue

            sct_detail.capacity_booked = max(
                0,
                int(sct_detail.capacity_booked or 0)
                - booked
            )

            sct_detail.capacity_available = (
                int(sct_detail.total_capacity or 0)
                - int(sct_detail.capacity_booked)
            )

        sct_doc.flags.ignore_permissions = True
        sct_doc.flags.ignore_validate = True

        sct_doc.save()


@frappe.whitelist()
def get_active_slot_masters(doctype, txt, searchfield, start=0, page_len=20, filters=None):
    if isinstance(filters, str):
        import json
        try:
            filters = json.loads(filters)
        except Exception:
            filters = {}

    employee_function = filters.get("employee_function") if filters else None

    if not employee_function:
        return []

    try:
        start = int(start)
    except (ValueError, TypeError):
        start = 0

    try:
        page_len = int(page_len)
    except (ValueError, TypeError):
        page_len = 20

    query = """
        SELECT
            sm.name, sm.employee_function, sm.batch_start_date, sm.batch_end_date
        FROM
            `tabSlot Master List` sm
        INNER JOIN
            `tabSlot Capacity Tracker` sct ON sct.slot_master = sm.name
        WHERE
            sm.docstatus = 1
            AND sm.workflow_state = 'Approved'
            AND sm.batch_end_date >= CURDATE()
            AND sct.docstatus != 2
    """

    args = {}
    if employee_function:
        query += " AND sm.employee_function = %(employee_function)s"
        args["employee_function"] = employee_function

    if txt:
        query += " AND sm.name LIKE %(txt)s"
        args["txt"] = f"%{txt}%"

    query += """
        AND (
            SELECT SUM(scd.capacity_available)
            FROM `tabSlot Capacity Detail` scd
            WHERE scd.parent = sct.name
        ) > 0
    """

    query += " ORDER BY sm.name ASC LIMIT %(start)s, %(page_len)s"
    args["start"] = start
    args["page_len"] = page_len

    return frappe.db.sql(query, args, as_list=1)
