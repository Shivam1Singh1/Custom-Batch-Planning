import frappe
import json
from frappe.model.document import Document
from frappe.utils import flt, getdate, today, now

class MaterialAllocation(Document):

    def validate(self):
        self.validate_planning_window()
        self.check_batch_planning_allocation_limit()
        self.check_global_free_stock_limit()
        self.set_pool_flags()
        for item in self.material_allocation:
            qty_requested = flt(item.allocate_qty)
            bom_qty = flt(item.quantity_required)
            stock = flt(item.stock_available)

            if qty_requested < 1:
                frappe.throw(
                    f"Row #{item.idx} ({item.item_code}): Qty Requested must be at least 1."
                )

            if qty_requested > stock:
                frappe.throw(
                    f"Row #{item.idx} ({item.item_code}): Qty Requested {qty_requested} exceeds Stock Available {stock}."
                )
            if qty_requested > bom_qty:
                frappe.throw(
                    f"Row #{item.idx} ({item.item_code}): Qty Requested {qty_requested} exceeds BOM Qty {bom_qty}."
                )
            if qty_requested != bom_qty and not (item.reason or "").strip():
                frappe.throw(
                    f"Row #{item.idx} ({item.item_code}): Reason is required when Qty Requested {qty_requested} differs from BOM Qty {bom_qty}."
                )

    def set_pool_flags(self):
        """Stamp the two hidden pool flags from the rows.

        Both are DERIVED, never entered, and recomputed on every save — a stored
        flag that can disagree with the rows it summarises is the same trap
        Material Allocation Log fell into.

        Runs after check_global_free_stock_limit so it reads the split that
        method just wrote, not whatever the client sent.

        has_untagged_items answers "does this allocation touch the untagged
        pool", for filtering and reporting.

        requires_material_request answers the operational question, and it is
        NOT the inverse of the first. An untagged allocation still needs a
        transfer for its MAIN-sourced share: that stock is sitting in the store
        and has to physically reach the lab. Only the lab-sourced share moves
        nothing, because those units are already standing in the lab.

            all lab-sourced untagged   -> nothing to move, no request
            part main-sourced untagged -> the main share must move
            tagged                     -> the whole reservation must move

        Hiding the request button on every untagged allocation would strand the
        main-sourced quantity: reserved indefinitely, with no way to move it.
        This flag is exactly the condition under which make_material_request
        would raise "No allocated quantities to transfer", so the button and the
        server agree instead of the button offering an action that throws.
        """
        has_untagged = False
        transferable = 0.0

        for item in self.material_allocation:
            qty = flt(item.allocate_qty)
            if qty <= 0:
                continue
            if (item.source_pool or "Tagged") == "Untagged":
                has_untagged = True
                transferable += flt(item.main_allocated_qty)
            else:
                transferable += qty

        self.has_untagged_items = 1 if has_untagged else 0
        self.requires_material_request = 1 if transferable > 0 else 0

    def clear_source_split(self):
        """Blank the source breakdown when it cannot be derived server-side.

        The four split fields are read-only on the form but still arrive in the
        payload, so leaving whatever the client sent in place would present an
        unverified split as if the server had computed it. Zero is the honest
        answer when there is no pool to compute against.
        """
        for item in self.material_allocation:
            item.local_free_qty = 0
            item.global_free_qty = 0
            item.local_allocated_qty = 0
            item.global_allocated_qty = 0
            item.lab_allocated_qty = 0
            item.main_allocated_qty = 0

    def check_global_free_stock_limit(self):
        """Re-read the free pools at save time, enforce local-first, refuse to
        over-issue.

        Allocation priority is enforced here, not in the UI: this batch's own
        free stock is consumed in full before any global stock is touched, and
        the resulting local/global breakdown is written back onto every row.
        Requested 100 against 40 local and 80 global saves as 40 local + 60
        global; requested 30 against the same pools saves as 30 local + 0
        global. Whatever split the client sent is discarded.

        The confirmation dialog on the Batch Planning form is advisory only: it
        reflects the pools as they stood when Material Planning was last run. By
        the time this document is saved another allocation may have consumed
        the same units, so the figure the user agreed to cannot be trusted and
        is deliberately not sent back to the server.

        Rows are locked FOR UPDATE while the pools are read, so two allocations
        racing for the last units serialise instead of both passing.

        This document is excluded from the totals it is being checked against —
        on a re-save it is already in the table, and counting it would make it
        compete with itself.

        TWO POOLS, CHOSEN PER ROW BY source_pool. Tagged rows are checked
        exactly as they always were, against the batch-tagged pools
        free_stock_figures measures. Untagged rows are checked against the
        untagged pile instead — untagged_free_figures — and split LAB FIRST
        rather than local first.

        The pools must never be mixed, in either direction. Checking an untagged
        row against the tagged pools reads ~0 free and rejects an allocation
        that is fully covered; checking a tagged row against the untagged pile
        offers it stock that was never claimed by batch planning. Rows carrying
        no source_pool are legacy tagged rows — see _POOL_COLUMN — so the
        default here must stay "Tagged".
        """
        from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
            _ef_lab_warehouses,
            free_stock_figures,
            split_lab_first,
            split_local_first,
            untagged_free_figures,
        )
        from custom_batch_planning.custom_batch_planning.doctype.batch_planning_settings.batch_planning_settings import (
            get_stock_cutover_datetime,
        )

        if self.allocation_status in ("Deallocated", "Stock Entry Done"):
            return

        if not self.batch_planning or not self.employee_function:
            self.clear_source_split()
            return

        cutover = get_stock_cutover_datetime()

        warehouse = self.get_warehouse()
        if not warehouse:
            self.clear_source_split()
            return

        tagged_by_item = {}
        untagged_by_item = {}
        for item in self.material_allocation:
            if flt(item.allocate_qty) <= 0:
                item.local_allocated_qty = 0
                item.global_allocated_qty = 0
                item.lab_allocated_qty = 0
                item.main_allocated_qty = 0
                continue
            if (item.source_pool or "Tagged") == "Untagged":
                untagged_by_item.setdefault(item.item_code, []).append(item)
            else:
                tagged_by_item.setdefault(item.item_code, []).append(item)

        for item_code, rows in tagged_by_item.items():
            figures = free_stock_figures(
                item_code,
                warehouse,
                self.employee_function,
                self.project_id,
                self.batch_planning,
                cutover,
                exclude_parent=self.name,
                for_update=True,
            )

            split = split_local_first(
                [flt(r.allocate_qty) for r in rows],
                figures["bp_free_stock"],
                figures["other_free_stock"],
            )

            if split["shortfall"] > 0:
                requested = split["requested"]
                available = split["capacity"]
                rows_note = f" across {len(rows)} rows" if len(rows) > 1 else ""
                frappe.throw(
                    f"<b>{item_code}</b>: need <b>{requested}</b>{rows_note}, "
                    f"only <b>{available}</b> free "
                    f"(current batch {split['local_free']} + global batch {split['global_free']}).",
                    title="Insufficient Free Stock",
                )

            for row, row_split in zip(rows, split["rows"]):
                row.local_free_qty = split["local_free"]
                row.global_free_qty = split["global_free"]
                row.local_allocated_qty = row_split["from_local"]
                row.global_allocated_qty = row_split["from_global"]
                row.stock_available = split["capacity"]
                # A tagged row draws on neither untagged half. Blanked rather
                # than left alone so a row switched between pools cannot carry a
                # stale breakdown that _untagged_allocated_qty would then count.
                row.lab_allocated_qty = 0
                row.main_allocated_qty = 0

        if not untagged_by_item:
            return

        lab_warehouses = _ef_lab_warehouses(
            frappe.get_doc("Employee Function", self.employee_function)
        )

        for item_code, rows in untagged_by_item.items():
            figures = untagged_free_figures(
                item_code,
                warehouse,
                lab_warehouses,
                self.employee_function,
                self.batch_planning,
                exclude_parent=self.name,
                for_update=True,
            )

            split = split_lab_first(
                [flt(r.allocate_qty) for r in rows],
                figures["lab_free"],
                figures["main_free"],
            )

            if split["shortfall"] > 0:
                rows_note = f" across {len(rows)} rows" if len(rows) > 1 else ""
                frappe.throw(
                    f"<b>{item_code}</b>: need <b>{split['requested']}</b>{rows_note}, "
                    f"only <b>{split['capacity']}</b> free in the untagged pool "
                    f"(lab {split['lab_free']} + main store {split['main_free']}).",
                    title="Insufficient Untagged Stock",
                )

            for row, row_split in zip(rows, split["rows"]):
                row.lab_allocated_qty = row_split["from_lab"]
                row.main_allocated_qty = row_split["from_main"]
                # stock_available is what validate() checks Qty Requested
                # against, and validate() runs this method first precisely so the
                # ceiling it enforces is the server's, not the client's.
                row.stock_available = split["capacity"]
                # The tagged breakdown is meaningless on an untagged row, and
                # leaving whatever the client sent would let _allocated_qty
                # attribute untagged units to a tagged pool if source_pool were
                # ever cleared.
                row.local_free_qty = 0
                row.global_free_qty = 0
                row.local_allocated_qty = 0
                row.global_allocated_qty = 0

    def check_batch_planning_allocation_limit(self):
        if not self.batch_planning:
            return

        for item in self.material_allocation:
            query = """
                SELECT IFNULL(SUM(mai.allocate_qty), 0)
                FROM `tabMaterial Allocation Item` mai
                INNER JOIN `tabMaterial Allocation` ma ON ma.name = mai.parent
                WHERE mai.item_code = %s 
                AND ma.batch_planning = %s
                AND ma.name != %s
                AND ma.docstatus != 2
                AND ma.allocation_status NOT IN ('Deallocated', 'Stock Entry Done')
                FOR UPDATE
            """
            
            already_allocated = flt(frappe.db.sql(query, (item.item_code, self.batch_planning, self.name or ""))[0][0])
            total = already_allocated + flt(item.allocate_qty)
            
            if total > flt(item.quantity_required):
                frappe.throw(
                    f"Row #{item.idx} ({item.item_code}): total allocated {total} exceeds required {item.quantity_required} "
                    f"({already_allocated} already allocated elsewhere + {item.allocate_qty} here)."
                )

    def validate_planning_window(self):
        if not self.batch_planning:
            return
        
        latest_date = frappe.db.sql(
            """
            SELECT MAX(slot_booking_date)
            FROM `tabSlot Booking CT`
            WHERE parent = %s AND parenttype = 'Batch Planning'
            """,
            (self.batch_planning,)
        )
        
        if latest_date and latest_date[0][0]:
            d = getdate(latest_date[0][0])
            if d < getdate(today()):
                frappe.throw(
                    f"Planning window for <b>{self.batch_planning}</b> closed on {d}."
                )

    @frappe.whitelist()
    def auto_allocate(self):
        """
        Auto Allocate Flow:
        1. Fetch warehouse from Employee Function.
        2. FEFO based batch allocation.
        3. Fallback for non-batch items.

        TAGGED AND UNTAGGED ROWS SCAN DIFFERENT WAREHOUSES. A tagged row is
        filled from the store, as it always was. An untagged row is filled in
        two passes — its lab-sourced portion from the Employee Function's lab
        warehouses, its main-sourced portion from the store — because that is
        where each portion was reserved.

        Filling an untagged row entirely from the store would name store batches
        for units physically standing in a lab, and would leave the lab-sourced
        portion with no batch rows at all. That second failure is not cosmetic:
        get_batches subtracts MA Batch Detail quantities held by other live
        allocations, so a portion that writes no rows is invisible to the
        batch-level double-reservation guard.

        NOT an expiry check on the pool. get_batches skips disabled and expired
        batches, so nothing expired is NAMED here — but the untagged stock
        queries apply no expiry filter, so those units were already counted as
        free and offered on the tab. Closing that gap moves Free Qty and is a
        separate decision (deferred 2026-09-07).
        """
        from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
            _ef_lab_warehouses,
            split_lab_first,
            untagged_free_figures,
        )

        if self.workflow_state != "Approved":
            frappe.throw("Allocation requires the document to be Approved.")

        if self.docstatus == 2:
            frappe.throw("Document is cancelled.")

        warehouse = self.get_warehouse()
        if not warehouse:
            frappe.throw(f"No store warehouse found for Employee Function: {self.employee_function}")

        # Resolved on first use only. Most allocations are tagged and never need
        # it, and this runs on a document the user has already waited on.
        lab_warehouses = None

        for item in self.material_allocation:
            item.qty_allocated = 0
            item.shortage = 0
            item.set("batch_details", [])

            qty_needed = flt(item.allocate_qty) if flt(item.allocate_qty) > 0 else flt(item.quantity_required)
            if qty_needed <= 0:
                continue

            untagged = (item.source_pool or "Tagged") == "Untagged"

            if untagged:
                if lab_warehouses is None:
                    lab_warehouses = _ef_lab_warehouses(
                        frappe.get_doc("Employee Function", self.employee_function)
                    )

                # Both halves of the pool this row drew on. The store Bin alone
                # would understate what is available to an untagged row and send
                # the non-batch fallback below to the wrong answer.
                item.stock_available = self._bin_qty(
                    item.item_code, [warehouse] + lab_warehouses
                )

                # THE SPLIT IS RECOMPUTED HERE, not read off the row, and that
                # is the difference between this working and quietly doing
                # nothing.
                #
                # Reading the stored fields broke re-allocation. deallocate()
                # zeroes lab_allocated_qty and main_allocated_qty, so on
                # deallocate -> Allocate again both halves arrived as 0, both
                # _fill_from_batches calls were handed qty_needed = 0 and wrote
                # no batch rows, and the non-batch fallback below then set
                # qty_allocated with an EMPTY batch_details. The split was
                # restored moments later by check_global_free_stock_limit on
                # save — long after the FEFO pass that needed it.
                #
                # Recomputing also keeps this honest when the draft is old: the
                # pool may have moved since the builder sized the row, and the
                # batches named here should reflect what is free now.
                #
                # check_global_free_stock_limit runs the same two functions with
                # the same arguments on the save below, so it lands on the same
                # numbers rather than overwriting these with different ones.
                figures = untagged_free_figures(
                    item.item_code,
                    warehouse,
                    lab_warehouses,
                    self.employee_function,
                    self.batch_planning,
                    exclude_parent=self.name,
                )
                split = split_lab_first(
                    [qty_needed],
                    max(flt(figures["lab_free"]), 0.0),
                    max(flt(figures["main_free"]), 0.0),
                )
                item.lab_allocated_qty = split["rows"][0]["from_lab"]
                item.main_allocated_qty = split["rows"][0]["from_main"]

                # Each portion filled from where it was actually reserved, and
                # lab first — matching split_lab_first, so the batches named here
                # are the ones the allocation is accounted against.
                total_allocated = self._fill_from_batches(
                    item, lab_warehouses, item.lab_allocated_qty
                )
                total_allocated += self._fill_from_batches(
                    item, [warehouse], item.main_allocated_qty
                )
            else:
                item.stock_available = self._bin_qty(item.item_code, [warehouse])
                total_allocated = self._fill_from_batches(
                    item, [warehouse], qty_needed
                )

            # Non-batch items have no batch rows to find, so a zero fill against
            # sufficient stock means "not batch-tracked", not "nothing there".
            if total_allocated == 0 and flt(item.stock_available) >= qty_needed:
                total_allocated = qty_needed

            item.qty_allocated = total_allocated
            item.shortage = max(qty_needed - total_allocated, 0)

        self.allocation_status = "Allocated"
        if self.docstatus == 1:
            self.flags.ignore_validate_update_after_submit = True

        self.save()

        for item in self.material_allocation:
            item.db_update()

        self.save_allocation_log("Allocated")

        return True

    def _bin_qty(self, item_code, warehouses):
        """Actual qty across a set of warehouses, from Bin."""
        if not warehouses:
            return 0.0
        rows = frappe.db.get_all(
            "Bin",
            filters={"item_code": item_code, "warehouse": ["in", warehouses]},
            pluck="actual_qty",
        )
        return sum(flt(q) for q in rows)

    def _fill_from_batches(self, item, warehouses, qty_needed):
        """FEFO-fill batch_details for ONE portion of a row. Returns qty covered.

        Split out of auto_allocate because an untagged row has two portions
        drawn from two different places, and each must be filled from the
        warehouses it was actually reserved against. Filling the whole row from
        the store would name store batches for units standing in a lab.

        Appends to batch_details rather than replacing it, so the caller can run
        it twice for one row. MA Batch Detail carries no warehouse column, so
        the two passes are indistinguishable once written — acceptable because
        batch_no identifies the material, and get_batches reads the rows back
        purely to avoid double-reserving a batch.
        """
        qty_needed = flt(qty_needed)
        if qty_needed <= 0 or not warehouses:
            return 0.0

        total = 0.0
        for b in self.get_batches(item.item_code, warehouses):
            if total >= qty_needed:
                break
            available = flt(b.actual_qty)
            if available <= 0:
                continue
            take = min(available, qty_needed - total)
            item.append("batch_details", {
                "batch_no": b.batch_no,
                "expiry_date": b.expiry_date,
                "qty_available": available,
                "qty_allocated": take,
            })
            total += take
        return total

    def get_linked_stock_entry(self):
        """
        Returns the live Stock Entry linked to this allocation, or None.

        The link lives on this side (`stock_entry`) rather than on Stock Entry,
        so a cancelled Stock Entry leaves a stale pointer behind. Treat a
        cancelled Stock Entry as no link at all and clear it, which is what
        frees the allocation up for a fresh transfer.
        """
        if not self.stock_entry:
            return None

        se = frappe.db.get_value(
            "Stock Entry", self.stock_entry, ["name", "docstatus"], as_dict=True
        )
        if not se or se.docstatus == 2:
            self.db_set("stock_entry", None, update_modified=False)
            return None

        return se

    def get_linked_material_request(self):
        """
        Returns the live Material Request raised from this allocation, or None.

        Same self-healing shape as get_linked_stock_entry: the link lives on
        this side (`material_request`), so a cancelled Material Request leaves a
        stale pointer behind. A cancelled request is treated as no link at all
        and cleared, which frees the allocation to raise a fresh one.
        """
        if not self.material_request:
            return None

        mr = frappe.db.get_value(
            "Material Request",
            self.material_request,
            ["name", "docstatus", "workflow_state"],
            as_dict=True,
        )
        if not mr or mr.docstatus == 2:
            self.db_set("material_request", None, update_modified=False)
            return None

        return mr

    @frappe.whitelist()
    def deallocate(self):
        """Release this allocation's hold, whichever pool it drew on.

        ONE MECHANISM SERVES BOTH POOLS, and that is by design rather than by
        omission. What releases stock is the status: allocation_status becomes
        "Deallocated", and every figure in the app that means "still holding
        stock" filters it out by the same rule — _allocated_qty for the tagged
        pools, _untagged_allocated_qty for the untagged pile, the guards in
        check_batch_planning_allocation_limit and _bp_allocated_by_item. Flip the
        status and Free Qty rises on whichever tab was counting the reservation.

        THE LAB-SOURCED PORTION NEEDS NO LEDGER REVERSAL. This is the part that
        looks like a missing step and is not. Allocating untagged lab stock posts
        nothing: those units were already standing in the lab, so the claim was
        recorded on this document and nowhere else. There is no Stock Entry to
        cancel, no Stock Ledger row to reverse, and adding one here would invent
        a movement that never happened. Releasing the claim IS the whole
        reversal.

        The main-sourced portion behaves exactly like a tagged reservation: the
        stock never left the store, so releasing the claim returns it to the
        untagged pool with nothing to undo. If it had already been transferred,
        the Stock Entry guard below refuses the deallocation until that entry is
        cancelled — and cancelling it reverses the tagged ledger rows, which is
        what puts the units back in the untagged pile.

        The guards therefore need no pool awareness. An allocation covered
        entirely from lab stock can never have a Material Request in the first
        place: make_material_request skips lab-sourced quantity, so a fully
        lab-sourced allocation raises "nothing to transfer" instead of a request.
        Both checks below simply never fire for it.
        """
        if self.docstatus == 2:
            frappe.throw("Document is cancelled.")

        existing_se = self.get_linked_stock_entry()
        if existing_se:
            if existing_se.docstatus == 1:
                frappe.throw(
                    f"Stock Entry <b>{existing_se.name}</b> is submitted. Cancel it first."
                )
            else:
                frappe.throw(
                    f"Draft Stock Entry <b>{existing_se.name}</b> exists. Delete or submit it first."
                )

        # A Material Request now sits between the allocation and the Stock
        # Entry, which opens a window the Stock Entry check alone cannot see:
        # the request is raised and approved, but nobody has pressed Create >
        # Stock Entry yet. The stock is committed at that point even though no
        # Stock Entry exists, so deallocating would release quantities an
        # approved request is already counting on.
        #
        # docstatus is the gate rather than workflow_state. Where a Material
        # Request workflow is installed it only reaches docstatus 1 on its
        # approve transition (the pattern Slot Opening uses), so submitted and
        # approved coincide; where none is installed, docstatus 1 is the only
        # signal there is. Reading workflow_state instead would silently pass
        # everything on a site with no workflow.
        existing_mr = self.get_linked_material_request()
        if existing_mr:
            if existing_mr.docstatus == 1:
                frappe.throw(
                    f"Material Request <b>{existing_mr.name}</b> is submitted. Cancel it first."
                )
            else:
                frappe.throw(
                    f"Draft Material Request <b>{existing_mr.name}</b> exists. Delete or submit it first."
                )

        for item in self.material_allocation:
            item.qty_allocated = 0
            item.shortage = flt(item.quantity_required)
            item.set("batch_details", [])

            # Clear the untagged breakdown, but NOT the tagged one, and the
            # asymmetry is deliberate.
            #
            # _UNTAGGED_COLUMN sums lab_allocated_qty and main_allocated_qty
            # straight, so zeroing them is belt-and-braces: a future query that
            # forgets the status filter still cannot resurrect a released claim.
            #
            # Doing the same to local_allocated_qty / global_allocated_qty would
            # be a bug. _SOURCE_COLUMN reads a zero pair as "no breakdown
            # recorded" and falls back to charging the WHOLE reservation to the
            # batch's own stock — the exact legacy-row trap
            # patches/backfill_allocation_source_split.py exists to repair. The
            # tagged split is audit evidence of where the units came from and
            # must survive deallocation.
            if (item.source_pool or "Tagged") == "Untagged":
                item.lab_allocated_qty = 0
                item.main_allocated_qty = 0

        self.allocation_status = "Deallocated"
        if self.docstatus == 1:
            self.flags.ignore_validate_update_after_submit = True

        self.save()

        self.save_allocation_log("Deallocated")

        return True

    @frappe.whitelist()
    def make_material_request(self):
        """
        Builds the Material Transfer request for this allocation and returns it
        UNSAVED, for the client to open as a new form.

        Nothing is written here - no insert, no draft, no link stamped. The
        request comes into existence only when the user presses Save on the
        form, which is also where Stage and Project Description get filled in:
        both are reqd on Material Request and neither can be derived from
        anything on this side, so Frappe's own mandatory check on the form is
        what collects them. Returning an unsaved doc is ERPNext's own make_*
        mapper shape - see make_stock_entry in the Material Request doctype.

        Because the request has no name yet, the link back to this allocation
        cannot be stamped here. `custom_material_allocation` on the returned
        doc carries it instead, and stamp_material_allocation in api/ma_link.py
        writes `material_request` back on save. Everything that reads that link
        - the one-request-per-allocation check below, the deallocate guard -
        keeps working, one Save later than it used to.

        The Stock Entry is deliberately NOT built here. Once this request is
        submitted the user presses ERPNext's own Create > Stock Entry on it,
        which runs make_stock_entry in erpnext/stock/doctype/material_request.
        Going through the stock mapper rather than a second hand-rolled builder
        is what makes the target warehouse the user picks, the batch selection
        and the qty-already-transferred arithmetic all behave the way they do
        everywhere else in ERPNext.

        UNTAGGED ROWS TRANSFER ONLY THEIR MAIN-STORE PORTION. The mechanism
        below is unchanged — same mapper, same warehouse pair, same tagging —
        but the quantity for a row drawn from the untagged pool is
        main_allocated_qty, not allocate_qty. Its lab-sourced half is already
        standing in the lab warehouse: those units were allocated where they
        sat, and no ledger movement was ever meant to post for them. Requesting
        the whole reservation would move stock out of a store that does not hold
        it, and would deliver into the lab a second copy of what is already
        there. Tagged rows are untouched — they carry no untagged split and
        transfer their whole reservation exactly as before.

        An allocation covered entirely from lab stock therefore has nothing to
        request, and says so rather than raising an empty transfer.

        Warehouse direction, because ERPNext's field names invite getting this
        backwards: `set_from_warehouse` is the SOURCE, taken from the Employee
        Function's store warehouse, and `set_warehouse` is the TARGET, taken
        from its lab warehouse - the same pair the Stock Entry builder this
        replaces used. Both sides are filled rather than left blank because
        validate_stock_item_warehouse in erpnext/buying/utils.py throws
        "Warehouse is mandatory for stock Item" on any row for a stock item
        with no target. Either can still be changed on the form before saving -
        which matters, because an Employee Function commonly lists several labs.
        """
        if self.docstatus != 1:
            frappe.throw("Submit the Material Allocation first.")

        existing_mr = self.get_linked_material_request()
        if existing_mr:
            frappe.throw(
                f"Material Request <b>{existing_mr.name}</b> already exists. Only one is allowed."
            )

        ef = self.get_employee_function_defaults()
        missing = [
            label
            for label, value in (
                ("store warehouse", ef.from_warehouse),
                ("lab warehouse", ef.to_warehouse),
                ("segment", ef.segment),
                ("cost center", ef.cost_center),
            )
            if not value
        ]
        if missing:
            frappe.throw(
                f"Employee Function {self.employee_function} has no "
                + ", ".join(missing)
                + "."
            )

        schedule_date = today()

        mr = frappe.new_doc("Material Request")
        mr.material_request_type = "Material Transfer"
        mr.transaction_date = today()
        mr.schedule_date = schedule_date
        # Material Allocation carries no company of its own, so this falls back
        # to the session default and, failing that, to whatever default the
        # Material Request field itself declares.
        company = frappe.defaults.get_user_default("Company")
        if company:
            mr.company = company
        mr.set_from_warehouse = ef.from_warehouse
        mr.set_warehouse = ef.to_warehouse
        mr.custom_batch_planning_no = self.batch_planning
        # What stamp_material_allocation reads once the user saves. Left off
        # the item rows on purpose - the allocation is a property of the whole
        # request, and batch_planning_id already carries the per-row tagging.
        mr.custom_material_allocation = self.name

        # employee_function and project are not decoration. validate_tagging in
        # api/tagging_enforcement.py fires on Material Request validate, and
        # is_exempt() only ever exempts Stock Entry - a Material Request dated
        # after the stock cutover is rejected outright unless both are present
        # on the parent AND on every item row. Dropping either side in a later
        # refactor breaks saving, not just reporting.
        mr.custom_employee_function = self.employee_function
        mr.project = self.project_id

        # Field is custom_function_head_name on Material Request but plain
        # function_head_name on this doctype and on Employee Function - the
        # custom-field prefix applies only where the field was bolted onto a
        # standard ERPNext doctype.
        #
        # This document's own copy wins, so a request reflects the head recorded
        # when the allocation was made rather than whoever holds the post today.
        # The Employee Function is the fallback for allocations created before
        # the value was captured, which would otherwise leave the request blank
        # with no way for the user to know where it should have come from.
        mr.custom_function_head_name = (
            self.function_head_name or ef.function_head_name
        )

        for row in self.material_allocation:
            # See the note above: the lab-sourced half of an untagged row never
            # moves, so it must never reach a Material Request.
            if (row.source_pool or "Tagged") == "Untagged":
                qty = flt(row.main_allocated_qty)
            else:
                qty = flt(row.allocate_qty)
            if qty <= 0:
                continue
            mr.append("items", {
                "item_code": row.item_code,
                "item_name": row.item_name,
                "qty": qty,
                "uom": row.uom,
                "schedule_date": schedule_date,
                "from_warehouse": ef.from_warehouse,
                "warehouse": ef.to_warehouse,
                # segment and cost_center are reqd on Material Request Item, so
                # a row without them fails the insert on mandatory, not on
                # tagging.
                "segment": ef.segment,
                "cost_center": ef.cost_center,
                "batch_planning_id": self.batch_planning,
                # Required by validate_tagging - see the note above.
                "employee_function": self.employee_function,
                "project": self.project_id,
            })

        if not mr.items:
            frappe.throw(
                "No allocated quantities to transfer. An allocation covered "
                "entirely from untagged lab stock has nothing to move — those "
                "units are already standing in the lab."
            )

        # Returned, not inserted. json_handler serialises this through
        # as_dict(), which keeps the __islocal flag frappe.model.sync needs to
        # treat it as a new form rather than an existing record.
        return mr

    def save_allocation_log(self, status):
        """Logs the allocation/deallocation activity to 'Material Allocation Log'."""
        existing = frappe.db.get_value(
            "Material Allocation Log",
            {"batch_planning": self.batch_planning},
            "name",
        )

        if existing:
            log = frappe.get_doc("Material Allocation Log", existing)
        else:
            log = frappe.new_doc("Material Allocation Log")
            log.batch_planning = self.batch_planning
            log.employee_function = self.employee_function
            log.project_id = self.project_id
            log.project_name = self.project_name

        for item in self.material_allocation:
            log.append("table", {
                "allocated_by": frappe.session.user,
                "allocated_on": now(),
                "material_allocation_id": self.name,
                "status": status,
                "item_code": item.item_code,
                "qty_allocated": item.qty_allocated if status == "Allocated" else 0,
            })

        existing_items = {}
        for r in (log.ma_logs or []):
            existing_items[r.item_code] = r

        for item in self.material_allocation:
            if status == "Allocated":
                if item.item_code in existing_items:
                    existing_items[item.item_code].qty_allocated += flt(item.allocate_qty)
                    existing_items[item.item_code].allocate_qty += flt(item.allocate_qty)
                    existing_items[item.item_code].allocated_on = now()
                else:
                    log.append("ma_logs", {
                        "item_code": item.item_code,
                        "item_name": item.item_name,
                        "uom": item.uom,
                        "quantity_required": flt(item.quantity_required),
                        "stock_available": flt(item.stock_available),
                        "allocate_qty": flt(item.allocate_qty),
                        "qty_allocated": flt(item.allocate_qty),
                        "shortage": flt(item.shortage),
                        "open_pr": flt(item.open_pr),
                        "open_po": flt(item.open_po),
                        "grn_qty": flt(item.grn_qty),
                        "status": "Allocated",
                        "allocated_on": now(),
                    })
            elif status == "Deallocated":
                if item.item_code in existing_items:
                    existing_items[item.item_code].qty_allocated -= flt(item.allocate_qty)
                    existing_items[item.item_code].allocate_qty -= flt(item.allocate_qty)
                    if existing_items[item.item_code].qty_allocated < 0:
                        existing_items[item.item_code].qty_allocated = 0
                    if existing_items[item.item_code].allocate_qty < 0:
                        existing_items[item.item_code].allocate_qty = 0

        log.save(ignore_permissions=True)

    def autoname(self):
        if self.batch_planning:
            count = frappe.db.count("Material Allocation", filters={"batch_planning": self.batch_planning})
            counter = str(count + 1).zfill(2)
            self.name = f"MA-{self.batch_planning}-{counter}"
        else:
            frappe.throw("Batch Planning is required.")

    def get_warehouse(self):
        ef_doc = frappe.get_doc("Employee Function", self.employee_function)
        for row in ef_doc.get("table_bukm"):
            if row.store_warehouse:
                return row.store_warehouse
        return None

    def get_employee_function_defaults(self):
        """
        The Material Request values only the Employee Function knows: both
        warehouses, the segment, the cost centre and the function head's name.

        Read together off one document rather than a method per field, because
        an insert needs all four at once - segment and cost_center are reqd on
        Material Request Item, so one missing value fails the whole request
        rather than a single row.

        Where a child table holds several rows the one flagged `default` wins
        and the first row is the fallback. That fallback is what the Stock
        Entry builder this replaces did for the warehouses, and it is the
        reason the target warehouse is worth a second look on the draft.
        """
        ef_doc = frappe.get_doc("Employee Function", self.employee_function)

        def pick(table, fieldname):
            rows = [r for r in (ef_doc.get(table) or []) if r.get(fieldname)]
            for row in rows:
                if row.get("default"):
                    return row.get(fieldname)
            return rows[0].get(fieldname) if rows else None

        return frappe._dict({
            "from_warehouse": pick("table_bukm", "store_warehouse"),
            "to_warehouse": pick("table_szrn", "lab_warehouse"),
            "segment": pick("table_xlgh", "segment"),
            "cost_center": pick("cost_center", "cost_center"),
            # A plain field on the Employee Function, not a child table, so it
            # needs no pick(). Included here rather than read separately in
            # make_material_request so every value the request takes from the
            # function comes from one read of one document.
            "function_head_name": ef_doc.get("function_head_name"),
        })

    def get_batches(self, item_code, warehouses):
        """Fetch batches with FEFO logic and exclude existing allocations.

        `warehouses` is a LIST. It used to be a single warehouse, because the
        only pool that could be allocated from was the Employee Function's
        store. Untagged allocation draws on the lab warehouses as well, and a
        row's lab-sourced portion has to be filled from the labs that actually
        hold it — so the scope is now a set, and an empty one means there is
        nowhere to look rather than every warehouse in the company.

        FEFO still runs across the whole set at once rather than warehouse by
        warehouse: the nearest expiry should be consumed first wherever it is
        standing, which is the entire point of the ordering.
        """
        if not warehouses:
            return []
        return frappe.db.sql("""
            SELECT
                sle.batch_no,
                b.expiry_date,
                (SUM(sle.actual_qty) - IFNULL((
                    SELECT SUM(mbd.qty_allocated)
                    FROM `tabMA Batch Detail` mbd
                    INNER JOIN `tabMaterial Allocation` ma ON ma.name = mbd.parent
                    WHERE mbd.batch_no = sle.batch_no
                      AND ma.name != %s
                      AND ma.allocation_status = 'Allocated'
                      AND ma.docstatus != 2
                ), 0)) AS actual_qty
            FROM `tabStock Ledger Entry` sle
            INNER JOIN `tabBatch` b ON b.name = sle.batch_no
            WHERE sle.item_code = %s
              AND sle.warehouse IN %s
              AND sle.is_cancelled = 0
              AND sle.batch_no IS NOT NULL
              AND sle.batch_no != ''
              AND b.disabled = 0
              AND (b.expiry_date IS NULL OR b.expiry_date >= CURDATE())
            GROUP BY sle.batch_no, b.expiry_date
            HAVING (SUM(sle.actual_qty) - IFNULL((
                SELECT SUM(mbd.qty_allocated)
                FROM `tabMA Batch Detail` mbd
                INNER JOIN `tabMaterial Allocation` ma ON ma.name = mbd.parent
                WHERE mbd.batch_no = sle.batch_no
                  AND ma.name != %s
                  AND ma.allocation_status = 'Allocated'
                  AND ma.docstatus != 2
            ), 0)) > 0
            ORDER BY b.expiry_date ASC
        """, (self.name, item_code, tuple(warehouses), self.name), as_dict=True)

@frappe.whitelist()
def ma_get_allocated_qty(item_code, employee_function, batch_planning, project, exclude_parent=None, row_name=None):
    warehouse = None
    ef_doc = frappe.get_doc("Employee Function", employee_function)
    for row in ef_doc.get("table_bukm"):
        if row.store_warehouse:
            warehouse = row.store_warehouse
            break

    if not warehouse:
        return {"free_stock": 0, "allocated_qty": 0}

    from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
        free_stock_figures,
    )
    from custom_batch_planning.custom_batch_planning.doctype.batch_planning_settings.batch_planning_settings import (
        get_stock_cutover_datetime,
    )

    figures = free_stock_figures(
        item_code,
        warehouse,
        employee_function,
        project,
        batch_planning,
        get_stock_cutover_datetime(),
        exclude_parent=exclude_parent,
    )

    local_free = max(flt(figures["bp_free_stock"]), 0.0)
    global_free = max(flt(figures["other_free_stock"]), 0.0)

    return {
        "local_free": local_free,
        "global_free": global_free,
        "free_stock": local_free + global_free,
        "allocated_qty": flt(figures["bp_allocated"]),
    }

@frappe.whitelist()
def ma_get_untagged_free(item_code, employee_function, batch_planning, exclude_parent=None):
    """Untagged pool figures for one row, for the form's live refresh.

    The untagged twin of ma_get_allocated_qty. It exists because that function
    answers a different question: it routes through free_stock_figures, which
    measures the batch-TAGGED pools. Asking it about a row drawn from the
    untagged pile returns the wrong pool's numbers, and the form was overwriting
    the builder's figures with them the moment the draft opened - an item with
    51,300 free untagged units displayed as 9,850, and items with thousands free
    displayed as 0.

    Returns the two halves separately as well as their sum, because the form
    needs the lab half to reproduce the lab-first split when the user edits Qty
    Requested. Both are clamped at zero here rather than in the client, so the
    two ends cannot disagree about what a negative half means.
    """
    from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
        _ef_lab_warehouses,
        untagged_free_figures,
    )

    ef_doc = frappe.get_doc("Employee Function", employee_function)
    warehouse = next(
        (r.store_warehouse for r in (ef_doc.table_bukm or []) if r.store_warehouse),
        None,
    )
    if not warehouse:
        return {"lab_free": 0.0, "main_free": 0.0, "free_stock": 0.0}

    figures = untagged_free_figures(
        item_code,
        warehouse,
        _ef_lab_warehouses(ef_doc),
        employee_function,
        batch_planning,
        exclude_parent=exclude_parent,
    )

    lab_free = max(flt(figures["lab_free"]), 0.0)
    main_free = max(flt(figures["main_free"]), 0.0)
    return {
        "lab_free": lab_free,
        "main_free": main_free,
        "free_stock": lab_free + main_free,
    }


@frappe.whitelist()
def get_open_pr_po(item_codes):
    if isinstance(item_codes, str):
        item_codes = json.loads(item_codes)

    result = {}
    for item_code in item_codes:
        open_pr = flt(frappe.db.sql("""
            SELECT SUM(mri.qty - mri.ordered_qty)
            FROM `tabMaterial Request Item` mri
            JOIN `tabMaterial Request` mr ON mr.name = mri.parent
            WHERE mri.item_code = %s AND mr.docstatus = 1 AND mri.ordered_qty < mri.qty
        """, (item_code,))[0][0])

        open_po = flt(frappe.db.sql("""
            SELECT SUM(poi.qty - poi.received_qty)
            FROM `tabPurchase Order Item` poi
            JOIN `tabPurchase Order` po ON po.name = poi.parent
            WHERE poi.item_code = %s AND poi.docstatus = 1 AND poi.received_qty < poi.qty
        """, (item_code,))[0][0])

        result[item_code] = {
            "open_pr": round(open_pr, 2),
            "open_po": round(open_po, 2),
        }

    return result

@frappe.whitelist(allow_guest=True)
def get_item_batch_expiry(item_codes):
    if isinstance(item_codes, str):
        item_codes = json.loads(item_codes)
    if not item_codes:
        return {}

    today_date = getdate(today())
    fmt = ",".join(["%s"] * len(item_codes))

    batches = frappe.db.sql(f"""
        SELECT b.item, b.name as batch_no, b.expiry_date, COALESCE(SUM(sle.actual_qty), 0) as qty
        FROM `tabBatch` b
        LEFT JOIN `tabStock Ledger Entry` sle ON sle.batch_no = b.name AND sle.is_cancelled = 0
        WHERE b.item IN ({fmt}) AND b.expiry_date IS NOT NULL AND b.disabled = 0
        GROUP BY b.item, b.name, b.expiry_date
    """, item_codes, as_dict=True)

    result = {}
    for b in batches:
        expiry = getdate(b.expiry_date)
        days_left = (expiry - today_date).days

        if days_left < 0:
            status, label = "expired", f"Expired ({abs(days_left)}d ago)"
        elif days_left <= 30:
            status, label = "expiring_soon", f"Expiring in {days_left}d"
        else:
            status, label = "ok", f"OK ({days_left}d left)"

        if b.item not in result:
            result[b.item] = {
                "status": status,
                "label": label,
                "days_left": days_left,
                "earliest_expiry": str(expiry),
                "batch_no": b.batch_no,
            }

    return result

def stock_entry_on_submit(doc, method=None):
    """doc_events hook for Stock Entry on_submit."""
    on_stock_entry_submit(doc.name)


def _allocation_for_stock_entry(stock_entry_name):
    """
    The Material Allocation a submitted Stock Entry belongs to, or None.

    Two routes, tried in that order:

    1. `stock_entry` on the allocation. This is only ever set by route 2 now,
       but it is also how every transfer made before the Material Request step
       existed is linked, so it stays the first thing checked.

    2. Through the Material Request. Stock Entries are no longer built here -
       the user raises a request and presses ERPNext's Create > Stock Entry,
       and make_stock_entry maps the request onto `material_request` on each
       Stock Entry Detail row (the parent has no such field, only the child
       does). That row points back at the request the allocation owns.

    Without route 2 nothing would connect the two documents and the allocation
    would never leave "Allocated". That is not a cosmetic problem: every free
    stock figure in this app, in batches_planned and in batch_planning, treats
    "Allocated" as the status that holds stock — see _HOLDS_STOCK in
    batch_planning.py — so a completed transfer would keep its quantities
    reserved forever and the over-allocation guard would refuse work that is
    actually free.

    The BOM ceiling is the one exception and reads wider on purpose:
    check_batch_planning_allocation_limit and _bp_allocated_by_item still use
    `allocation_status NOT IN ('Deallocated', 'Stock Entry Done')`, so that
    unallocated drafts still count against Qty Required.
    """
    ma_name = frappe.db.get_value(
        "Material Allocation", {"stock_entry": stock_entry_name}, "name"
    )
    if ma_name:
        return ma_name

    material_requests = frappe.db.get_all(
        "Stock Entry Detail",
        filters={"parent": stock_entry_name, "material_request": ["is", "set"]},
        pluck="material_request",
        distinct=True,
    )
    for mr_name in material_requests:
        ma_name = frappe.db.get_value(
            "Material Allocation", {"material_request": mr_name}, "name"
        )
        if ma_name:
            return ma_name

    return None


@frappe.whitelist()
def on_stock_entry_submit(stock_entry_name):
    """
    Called when a Stock Entry belonging to a Material Allocation is submitted.
    Updates allocation_status to 'Stock Entry Done' and back-fills the
    `stock_entry` link so the allocation can still offer "Open Stock Entry".
    """
    ma_name = _allocation_for_stock_entry(stock_entry_name)
    if not ma_name:
        return

    ma_doc = frappe.get_doc("Material Allocation", ma_name)

    if ma_doc.allocation_status != "Allocated":
        return

    # Resolved through the Material Request on a first transfer, so record the
    # Stock Entry here. That is what lets the allocation link straight to it
    # instead of routing every visit back through the request.
    if not ma_doc.stock_entry:
        ma_doc.db_set("stock_entry", stock_entry_name, update_modified=False)

    ma_doc.allocation_status = "Stock Entry Done"
    ma_doc.flags.ignore_validate_update_after_submit = True
    ma_doc.save(ignore_permissions=True)
    frappe.db.commit()

@frappe.whitelist()
def get_allocated_items(batch_planning, employee_function):
    """Every item allocated against this Batch Planning, for View Allocations.

    READ FROM THE ALLOCATIONS THEMSELVES, not from Material Allocation Log.

    The log's ma_logs rows are a hand-maintained running tally:
    save_allocation_log adds a row on allocate, subtracts on deallocate, clamps
    at zero, and this used to filter for qty_allocated > 0. Every one of those
    steps is a chance to drift, and it did — the dialog announced "4 Material
    Allocation(s) have been done" while listing one item, because rows for the
    other items had been decremented away or never written. A derived tally that
    can disagree with the documents it describes is worse than no tally, since
    the number still looks authoritative.

    Material Allocation Item is the record of what was allocated. Summing it
    cannot drift, and the count and the list are now built from the same query,
    so the header can no longer contradict the table beneath it.

    WHAT COUNTS: approved allocations that actually allocated something.

        workflow_state  = 'Approved'                     — no drafts
        allocation_status IN ('Allocated', 'Stock Entry Done')
        docstatus      <> 2                              — no cancellations

    Deallocated is excluded by that IN list: it was released deliberately and
    holds nothing.

    Stock-Entry-Done is INCLUDED, which is the important difference from the
    free-stock queries (_HOLDS_STOCK). Those ask "what is still reserved"; this
    asks "what has this plan been given". Material already transferred into the
    lab is the clearest case of an allocation that was done, and hiding it is
    what made completed items look unallocated on this plan.

    NOT gated on docstatus for the draft test. Allocations in this app reach
    workflow_state "Approved" and allocation_status "Allocated" while still at
    docstatus 0 — auto_allocate requires Approved but does not submit — so
    excluding docstatus 0 would drop live allocations. workflow_state is the
    field that actually distinguishes a draft here.
    """
    LIVE = """
          AND ma.batch_planning = %(bp)s
          AND ma.employee_function = %(ef)s
          AND ma.docstatus <> 2
          AND ma.workflow_state = 'Approved'
          AND IFNULL(ma.allocation_status, '') IN ('Allocated', 'Stock Entry Done')
          AND mai.allocate_qty > 0
    """

    rows = frappe.db.sql(f"""
        SELECT
            mai.item_code                   AS item_code,
            MAX(mai.item_name)              AS item_name,
            MAX(mai.uom)                    AS uom,
            MAX(mai.quantity_required)      AS quantity_required,
            SUM(mai.allocate_qty)           AS qty_allocated
        FROM `tabMaterial Allocation Item` mai
        INNER JOIN `tabMaterial Allocation` ma ON ma.name = mai.parent
        WHERE 1 = 1
          {LIVE}
        GROUP BY mai.item_code
        ORDER BY mai.item_code
    """, {"bp": batch_planning, "ef": employee_function}, as_dict=True)

    # The same set the rows came from, so the headline count and the table can
    # never disagree. Counting Material Allocation separately is what let the
    # dialog claim four allocations while showing one item.
    ma_count = flt(frappe.db.sql(f"""
        SELECT COUNT(DISTINCT ma.name)
        FROM `tabMaterial Allocation Item` mai
        INNER JOIN `tabMaterial Allocation` ma ON ma.name = mai.parent
        WHERE 1 = 1
          {LIVE}
    """, {"bp": batch_planning, "ef": employee_function})[0][0] or 0)

    for r in rows:
        r["qty_allocated"] = flt(r["qty_allocated"])
        r["quantity_required"] = flt(r["quantity_required"])

    return {"items": rows, "ma_count": int(ma_count)}
