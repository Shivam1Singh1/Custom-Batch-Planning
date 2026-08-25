"""The Unapproved GRN column: what it counts, and what it must never touch.

Three properties, asserted against real site data rather than fixtures, because
each is only meaningful in terms of how receipts are actually staged here:

  1. It shows unapproved receipts only, and approval removes them by itself.
  2. Nothing it counts has reached the Stock Ledger, so it cannot double-count
     against any stock column.
  3. It is display-only — no coverage arithmetic subtracts it.

Run:  bench --site <site> run-tests --app custom_batch_planning \
          --module custom_batch_planning.custom_batch_planning.doctype.batch_planning.test_unapproved_grn
"""

import unittest

import frappe

from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
    PR_APPROVED,
    PR_UNAPPROVED,
    _bp_predicate,
    get_material_planning_data,
)


class TestUnapprovedGRN(unittest.TestCase):

    def _count(self, extra_where):
        return frappe.db.sql(
            f"""
            SELECT COUNT(*) FROM `tabPurchase Receipt Item` pri
            JOIN `tabPurchase Receipt` pr ON pr.name = pri.parent
            WHERE {PR_UNAPPROVED} AND {extra_where}
            """
        )[0][0]

    def test_no_approved_receipt_is_counted_as_unapproved(self):
        self.assertEqual(self._count("pr.workflow_state LIKE 'Approve%%'"), 0)

    def test_no_rejected_or_cancelled_receipt_is_counted(self):
        self.assertEqual(
            self._count(
                "(pr.workflow_state LIKE 'Reject%%' OR pr.workflow_state LIKE 'Cancel%%' "
                "OR pr.docstatus = 2)"
            ),
            0,
        )

    def test_nothing_counted_has_reached_the_stock_ledger(self):
        self.assertEqual(
            self._count(
                "EXISTS (SELECT 1 FROM `tabStock Ledger Entry` sle "
                "WHERE sle.voucher_no = pr.name AND sle.voucher_type = 'Purchase Receipt' "
                "AND sle.is_cancelled = 0)"
            ),
            0,
        )

    def test_unapproved_and_approved_gates_never_overlap(self):
        self.assertEqual(self._count(PR_APPROVED.replace("%%", "%%")), 0)

    def test_global_and_local_classify_exactly_as_mr_and_po_do(self):
        gen = _bp_predicate("pri", "GEN")
        bp = _bp_predicate("pri", "BP")
        self.assertIn("IS NOT NULL", gen)
        self.assertNotIn("IS NULL", gen)
        self.assertEqual(bp, "pri.batch_planning_id = %(bp)s")
        self.assertIn("<> %(bp)s", gen)

    def test_net_requirement_does_not_subtract_the_grn_column(self):
        names = [
            d.name
            for d in frappe.get_all(
                "Batch Planning",
                filters={"docstatus": 1},
                fields=["name"],
                order_by="modified desc",
                limit=2,
            )
        ]
        if not names:
            self.skipTest("no submitted Batch Planning on this site")

        checked = 0
        for name in names:
            for row in get_material_planning_data(name)["results"]:
                expected = max(
                    row["qty_required"]
                    - row["bp_main_stock"]
                    - row["lab_stock"]
                    - row["global_allocated_qty"]
                    - row["local_allocated_qty"]
                    - row["bp_mr_qty"]
                    - row["bp_po_qty"],
                    0.0,
                )
                self.assertAlmostEqual(
                    row["net_requirement"],
                    round(expected, 2),
                    places=1,
                    msg=f"Net Req on {row['item_code']} no longer matches its own terms",
                )
                checked += 1
        self.assertGreater(checked, 0)
