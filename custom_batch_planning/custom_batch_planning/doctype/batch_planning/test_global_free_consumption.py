"""Global Free Qty must fall when an allocation draws on the global pool.

The bug this pins: a batch with no stock of its own allocated 10 out of the
shared pool, and Global Free Qty stayed where it was. The reservation was
charged to the borrower's own tagged stock — where the units had never been —
so the global pool still offered them and the same 10 could be taken again.

Two layers are covered:

  * free_pools()        the rule itself, as pure arithmetic
  * a real allocation   inserted, checked, deallocated and rolled back, which
                        proves the rule survives the whole save path

Run:  bench --site <site> run-tests --app custom_batch_planning \
          --module custom_batch_planning.custom_batch_planning.doctype.batch_planning.test_global_free_consumption
"""

import unittest

import frappe
from frappe.utils import flt

from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
    free_pools,
    free_stock_figures,
    settle_cross_batch_draw,
    split_local_first,
)


def _pools(
    bp_main,
    other_main,
    local_alloc,
    global_alloc,
    other_alloc=0,
    other_global_alloc=0,
):
    """free_pools in the shape the acceptance tests are written in.

    other_alloc is other batches' LOCAL draws (against their own stock);
    other_global_alloc is their borrowings, which come out of this batch's.
    """
    return free_pools(
        bp_main, local_alloc, global_alloc, other_main, other_alloc, other_global_alloc
    )


class TestGlobalFreeConsumption(unittest.TestCase):
    """Sections 5-8 and 18 of the spec, one test per numbered scenario."""

    def test_1_local_zero_global_allocation_reduces_global_free(self):
        split = split_local_first([10], 0, 90)["rows"][0]
        self.assertEqual((split["from_local"], split["from_global"]), (0, 10))

        after = _pools(
            bp_main=0, other_main=90,
            local_alloc=split["from_local"], global_alloc=split["from_global"],
        )
        self.assertEqual(after["bp_free"], 0)
        self.assertEqual(after["other_free"], 80)

    def test_2_partial_local_only_the_global_part_hits_the_global_pool(self):
        split = split_local_first([30], 20, 90)["rows"][0]
        self.assertEqual((split["from_local"], split["from_global"]), (20, 10))

        after = _pools(
            bp_main=20, other_main=90,
            local_alloc=split["from_local"], global_alloc=split["from_global"],
        )
        self.assertEqual(after["bp_free"], 0)
        self.assertEqual(after["other_free"], 80, "global fell by 30 instead of by 10")

    def test_3_sufficient_local_leaves_the_global_pool_untouched(self):
        split = split_local_first([20], 50, 90)["rows"][0]
        self.assertEqual((split["from_local"], split["from_global"]), (20, 0))

        after = _pools(
            bp_main=50, other_main=90,
            local_alloc=split["from_local"], global_alloc=split["from_global"],
        )
        self.assertEqual(after["bp_free"], 30)
        self.assertEqual(after["other_free"], 90, "global moved with no global draw")

    def test_4_global_pool_can_be_drained_to_exactly_zero(self):
        split = split_local_first([100], 0, 100)["rows"][0]
        self.assertEqual((split["from_local"], split["from_global"]), (0, 100))

        after = _pools(
            bp_main=0, other_main=100,
            local_alloc=split["from_local"], global_alloc=split["from_global"],
        )
        self.assertEqual(after["bp_free"], 0)
        self.assertEqual(after["other_free"], 0)

    def test_5_request_beyond_both_pools_is_a_shortfall(self):
        result = split_local_first([91], 0, 90)
        self.assertEqual(result["capacity"], 90)
        self.assertEqual(result["shortfall"], 1)

    def test_6_deallocation_returns_the_units_to_the_global_pool(self):
        allocated = _pools(bp_main=0, other_main=90, local_alloc=0, global_alloc=10)
        self.assertEqual(allocated["other_free"], 80)

        deallocated = _pools(bp_main=0, other_main=90, local_alloc=0, global_alloc=0)
        self.assertEqual(deallocated["other_free"], 90)

    def test_borrowed_units_never_become_local_stock(self):
        after = _pools(bp_main=0, other_main=90, local_alloc=0, global_alloc=10)
        self.assertEqual(after["bp_free"], 0, "borrowed stock leaked into the local pool")

    def test_mixed_allocation_conserves_the_total(self):
        before = _pools(bp_main=20, other_main=90, local_alloc=0, global_alloc=0)
        after = _pools(bp_main=20, other_main=90, local_alloc=20, global_alloc=10)
        self.assertEqual(
            (before["bp_free"] + before["other_free"])
            - (after["bp_free"] + after["other_free"]),
            30,
        )

    def test_other_batches_reservations_still_reduce_the_global_pool(self):
        after = _pools(
            bp_main=0, other_main=90, local_alloc=0, global_alloc=10, other_alloc=5
        )
        self.assertEqual(after["other_free"], 75)

    def test_a_loan_reduces_the_lender_own_free_stock(self):
        lender = _pools(
            bp_main=9950, other_main=0,
            local_alloc=0, global_alloc=0,
            other_global_alloc=50,
        )
        self.assertEqual(lender["bp_free"], 9900, "lender still offers the lent units")
        self.assertEqual(lender["other_free"], 0)

    def test_lender_and_borrower_report_the_same_free_stock(self):
        lender = _pools(
            bp_main=9950, other_main=0,
            local_alloc=0, global_alloc=0, other_global_alloc=50,
        )
        borrower = _pools(
            bp_main=0, other_main=9950,
            local_alloc=0, global_alloc=50, other_alloc=0,
        )
        self.assertEqual(lender["bp_free"], borrower["other_free"])
        self.assertEqual(borrower["bp_free"], lender["other_free"])
        self.assertEqual(
            lender["bp_free"] + lender["other_free"],
            borrower["bp_free"] + borrower["other_free"],
            "the two batches disagree on how much of the item is free",
        )


class TestTransferOutOfBorrowedStock(unittest.TestCase):
    """Main Wh must fall when borrowed stock is moved to Lab.

    The Stock Entry tags its issue with the batch that CONSUMED the material,
    while the receipt carries the batch that BOUGHT it — so a borrowed transfer
    leaves a negative on one line and the full quantity on the other, and the
    negative is clamped away on screen. Main Wh read 9,950 after 50 units had
    left the store.
    """

    def test_borrower_deficit_is_charged_to_the_pile_it_came_from(self):
        bp_main, other_main = settle_cross_batch_draw(-50, 9950)
        self.assertEqual(bp_main, 0)
        self.assertEqual(other_main, 9900, "Main Wh did not fall by the transferred qty")

    def test_lender_sees_the_same_reduction(self):
        bp_main, other_main = settle_cross_batch_draw(9950, -50)
        self.assertEqual(bp_main, 9900)
        self.assertEqual(other_main, 0)

    def test_both_sides_agree_on_what_is_left_in_the_store(self):
        borrower = settle_cross_batch_draw(-50, 9950)
        lender = settle_cross_batch_draw(9950, -50)
        self.assertEqual(sum(borrower), sum(lender))
        self.assertEqual(sum(borrower), 9900)
        self.assertEqual(lender[0], borrower[1])

    def test_positive_figures_are_left_alone(self):
        self.assertEqual(settle_cross_batch_draw(100, 50), (100, 50))
        self.assertEqual(settle_cross_batch_draw(0, 0), (0, 0))

    def test_both_negative_is_left_alone(self):
        self.assertEqual(settle_cross_batch_draw(-10, -20), (-10, -20))

    def test_deficit_larger_than_the_other_pile_stays_visible(self):
        self.assertEqual(settle_cross_batch_draw(-80, 30), (0, -50))

    def test_net_req_no_longer_inflated_by_the_deficit(self):
        qty_required, lab = 50.0, 50.0

        raw_bp_main = -50.0
        before = max(qty_required - raw_bp_main - lab, 0.0)
        self.assertEqual(before, 50.0, "precondition: the old figure over-asked")

        settled_bp_main, _ = settle_cross_batch_draw(raw_bp_main, 9950)
        after = max(qty_required - settled_bp_main - lab, 0.0)
        self.assertEqual(after, 0.0)


class TestGlobalFreeConsumptionLive(unittest.TestCase):
    """The same rule through a real Material Allocation, then rolled back."""

    def tearDown(self):
        frappe.db.rollback()

    def _candidate(self):
        """An item on a submitted Batch Planning with global free stock to borrow."""
        from custom_batch_planning.custom_batch_planning.doctype.batch_planning_settings.batch_planning_settings import (
            get_stock_cutover_datetime,
        )

        cutover = get_stock_cutover_datetime()
        for bp in frappe.get_all(
            "Batch Planning",
            filters={"docstatus": 1},
            fields=["name", "project", "custom_employee_function"],
            order_by="modified desc",
            limit=25,
        ):
            ef = bp.custom_employee_function
            if not ef or not bp.project:
                continue
            ef_doc = frappe.get_doc("Employee Function", ef)
            warehouse = next(
                (r.store_warehouse for r in (ef_doc.table_bukm or []) if r.store_warehouse),
                None,
            )
            if not warehouse:
                continue
            for row in frappe.get_all(
                "Material Allocation Item",
                filters={"parenttype": "Material Allocation"},
                fields=["item_code"],
                limit=60,
            ):
                figures = free_stock_figures(
                    row.item_code, warehouse, ef, bp.project, bp.name, cutover
                )
                if flt(figures["other_free_stock"]) >= 2:
                    return bp, ef, warehouse, row.item_code, figures
        return None, None, None, None, None

    def test_a_real_global_allocation_moves_the_figure(self):
        bp, ef, warehouse, item_code, before = self._candidate()
        if not bp:
            self.skipTest("no item on this site currently has global free stock to borrow")

        from custom_batch_planning.custom_batch_planning.doctype.batch_planning_settings.batch_planning_settings import (
            get_stock_cutover_datetime,
        )

        cutover = get_stock_cutover_datetime()
        global_before = flt(before["other_free_stock"])
        local_before = flt(before["bp_free_stock"])
        take = 2.0

        ma = frappe.new_doc("Material Allocation")
        ma.batch_planning = bp.name
        ma.employee_function = ef
        ma.project_id = bp.project
        ma.append("material_allocation", {
            "item_code": item_code,
            "quantity_required": take,
            "allocate_qty": take,
        })
        ma.insert(ignore_permissions=True)

        row = ma.material_allocation[0]
        expected_local = min(take, max(local_before, 0.0))
        self.assertAlmostEqual(flt(row.local_allocated_qty), expected_local, places=2)
        self.assertAlmostEqual(
            flt(row.global_allocated_qty), take - expected_local, places=2
        )

        after = free_stock_figures(item_code, warehouse, ef, bp.project, bp.name, cutover)
        self.assertAlmostEqual(
            flt(after["other_free_stock"]),
            global_before - flt(row.global_allocated_qty),
            places=2,
            msg="Global Free Qty did not fall by the borrowed quantity",
        )
        self.assertAlmostEqual(
            flt(after["bp_free_stock"]),
            local_before - flt(row.local_allocated_qty),
            places=2,
            msg="local pool moved by something other than its own share",
        )

        ma.allocation_status = "Deallocated"
        ma.save(ignore_permissions=True)
        restored = free_stock_figures(
            item_code, warehouse, ef, bp.project, bp.name, cutover
        )
        self.assertAlmostEqual(
            flt(restored["other_free_stock"]), global_before, places=2,
            msg="deallocation did not return the borrowed units to the global pool",
        )
        self.assertAlmostEqual(
            flt(restored["bp_free_stock"]), local_before, places=2
        )

    def test_both_pools_balance_against_real_data(self):
        """Each pile equals what is free in it plus what was reserved out of it.

        The invariant that ties every column together, and the one the old
        arithmetic broke: a borrowed unit used to be missing from one side and
        double-counted on the other.

            other_main = other_free + their local draws + my borrowings
            bp_main    = bp_free    + my local draws    + their borrowings
        """
        from custom_batch_planning.custom_batch_planning.doctype.batch_planning_settings.batch_planning_settings import (
            get_stock_cutover_datetime,
        )

        cutover = get_stock_cutover_datetime()
        checked = 0

        for bp in frappe.get_all(
            "Batch Planning",
            filters={"docstatus": 1},
            fields=["name", "project", "custom_employee_function"],
            order_by="modified desc",
            limit=6,
        ):
            ef = bp.custom_employee_function
            if not ef or not bp.project:
                continue
            ef_doc = frappe.get_doc("Employee Function", ef)
            wh = next(
                (x.store_warehouse for x in (ef_doc.table_bukm or []) if x.store_warehouse),
                None,
            )
            if not wh:
                continue

            items = {
                i.item_code
                for i in frappe.get_all("Material Allocation Item", fields=["item_code"], limit=30)
            }
            for item_code in items:
                f = free_stock_figures(item_code, wh, ef, bp.project, bp.name, cutover)

                self.assertAlmostEqual(
                    flt(f["other_main_stock"]),
                    flt(f["other_free_stock"]) + flt(f["other_allocated_total"]),
                    places=2,
                    msg=f"other pool does not balance for {item_code} on {bp.name}",
                )
                self.assertAlmostEqual(
                    flt(f["bp_main_stock"]),
                    flt(f["bp_free_stock"])
                    + flt(f["bp_local_allocated"])
                    + flt(f["other_global_allocated"]),
                    places=2,
                    msg=f"own pool does not balance for {item_code} on {bp.name}",
                )
                checked += 1

        self.assertGreater(checked, 0, "no planning rows available to check")

    def test_a_request_beyond_the_live_pools_is_rejected_and_changes_nothing(self):
        """Section 15 / TEST 5: the server re-reads the pools and refuses."""
        bp, ef, warehouse, item_code, before = self._candidate()
        if not bp:
            self.skipTest("no item on this site currently has global free stock to borrow")

        from custom_batch_planning.custom_batch_planning.doctype.batch_planning_settings.batch_planning_settings import (
            get_stock_cutover_datetime,
        )

        cutover = get_stock_cutover_datetime()
        capacity = max(flt(before["bp_free_stock"]), 0.0) + max(
            flt(before["other_free_stock"]), 0.0
        )

        ma = frappe.new_doc("Material Allocation")
        ma.batch_planning = bp.name
        ma.employee_function = ef
        ma.project_id = bp.project
        ma.append("material_allocation", {
            "item_code": item_code,
            "quantity_required": capacity + 1,
            "allocate_qty": capacity + 1,
        })
        with self.assertRaises(frappe.ValidationError):
            ma.insert(ignore_permissions=True)

        after = free_stock_figures(item_code, warehouse, ef, bp.project, bp.name, cutover)
        self.assertAlmostEqual(
            flt(after["other_free_stock"]), flt(before["other_free_stock"]), places=2,
            msg="a rejected allocation still moved the global pool",
        )
        self.assertAlmostEqual(
            flt(after["bp_free_stock"]), flt(before["bp_free_stock"]), places=2
        )
