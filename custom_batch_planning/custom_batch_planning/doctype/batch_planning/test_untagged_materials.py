"""Untagged Materials tab — predicate, project scoping, and BOM scale.

The three things that decide whether this tab shows anything at all:

  * UNTAGGED selects rows with no batch tag, and is disjoint from GEN and BP
  * the Project predicate DISAPPEARS when no project is passed, and is
    unchanged for every existing caller that passes one
  * Qty Req is summed on the stock_qty scale, not qty_consumed_per_unit

The SQL-building helpers are pure string functions, so most of this runs
without a site. The live class at the end exercises the whole endpoint and
skips when there is no data.

Run:  bench --site <site> run-tests --app custom_batch_planning \
          --module custom_batch_planning.custom_batch_planning.doctype.batch_planning.test_untagged_materials
"""

import unittest

import frappe
from frappe.utils import flt

from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
    MR_PROJECT,
    _bp_predicate,
    _project_clause,
)


class TestUntaggedPredicate(unittest.TestCase):
    def test_untagged_matches_null_and_blank(self):
        """NULLIF folds '' into NULL, so one IS NULL covers both."""
        sql = _bp_predicate("sle", "UNTAGGED")
        self.assertIn("NULLIF(sle.batch_planning_id, '')", sql)
        self.assertIn("IS NULL", sql)

    def test_untagged_honours_every_place_a_tag_can_live(self):
        """A row whose PARENT names a Batch Planning is not untagged.

        PR-2026-2027-00002 is the case that exposed this: header reads
        BP-26-11-001, item row has no batch_planning_id at all, and the first
        version of this predicate counted it as untagged stock.
        """
        from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
            MR_BP, PO_BP, PR_BP,
        )

        for alias, expr, parent in (
            ("mri", MR_BP, "mr"), ("poi", PO_BP, "po"), ("pri", PR_BP, "pr"),
        ):
            sql = _bp_predicate(alias, "UNTAGGED", expr)
            self.assertIn(f"{alias}.batch_planning_id", sql)
            self.assertIn(f"{parent}.custom_batch_planning_no", sql)
            # NOT the bare custom_batch_planning. It is defined on no doctype
            # and exists only as an orphan column on databases old enough to
            # predate its rename, so naming it made every untagged query a 1054
            # everywhere else. This assertion used to require it.
            self.assertNotIn(f"{parent}.custom_batch_planning,", sql)
            self.assertTrue(sql.endswith("IS NULL"), sql)

    def test_item_level_custom_field_is_covered_where_it_exists(self):
        """MR and PO items carry custom_batch_planning_no; PR items do not."""
        from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
            MR_BP, PO_BP, PR_BP,
        )
        self.assertIn("mri.custom_batch_planning_no", MR_BP)
        self.assertIn("poi.custom_batch_planning_no", PO_BP)
        self.assertNotIn("pri.custom_batch_planning_no", PR_BP)

    def test_untagged_takes_no_bp_parameter(self):
        """No batch to compare against — binding one would be meaningless."""
        self.assertNotIn("%(bp)s", _bp_predicate("sle", "UNTAGGED"))

    def test_the_three_modes_are_disjoint(self):
        gen = _bp_predicate("sle", "GEN")
        bp = _bp_predicate("sle", "BP")
        untagged = _bp_predicate("sle", "UNTAGGED")

        # GEN and BP both require a non-null tag; UNTAGGED requires its absence.
        self.assertIn("IS NOT NULL", gen)
        self.assertIn("IS NULL", untagged)
        self.assertNotIn("IS NULL", gen)
        self.assertEqual(bp, "sle.batch_planning_id = %(bp)s")
        self.assertNotEqual(untagged, gen)
        self.assertNotEqual(untagged, bp)

    def test_existing_modes_are_untouched(self):
        """The UNTAGGED branch must not have altered GEN or BP."""
        self.assertEqual(
            _bp_predicate("mri", "GEN"),
            "(mri.batch_planning_id IS NOT NULL "
            "AND mri.batch_planning_id <> '' "
            "AND mri.batch_planning_id <> %(bp)s)",
        )
        self.assertEqual(_bp_predicate("mri", "BP"), "mri.batch_planning_id = %(bp)s")


class TestProjectClause(unittest.TestCase):
    def test_a_project_produces_the_full_clause(self):
        self.assertEqual(
            _project_clause("sle.project", "PLTP-2025-0001"),
            "AND sle.project = %(project)s",
        )

    def test_no_project_produces_nothing(self):
        """Not `= NULL`, which never matches — the clause must vanish entirely."""
        self.assertEqual(_project_clause("sle.project", None), "")
        self.assertEqual(_project_clause("sle.project", ""), "")

    def test_it_works_for_the_coalesced_pipeline_expressions(self):
        self.assertEqual(
            _project_clause(MR_PROJECT, "P"), f"AND {MR_PROJECT} = %(project)s"
        )
        self.assertEqual(_project_clause(MR_PROJECT, None), "")

    def test_dropping_the_clause_is_what_makes_untagged_rows_visible(self):
        """A NULL project can only be reached by removing the predicate."""
        with_project = _project_clause("sle.project", "P")
        without = _project_clause("sle.project", None)
        self.assertIn("=", with_project)
        self.assertNotIn("=", without)


class TestUntaggedMaterialDataLive(unittest.TestCase):
    """The whole endpoint against real data. Skips when there is none."""

    def _candidate(self):
        for bp in frappe.get_all(
            "Batch Planning",
            filters={"docstatus": 1},
            fields=["name", "custom_employee_function"],
            order_by="modified desc",
            limit=25,
        ):
            if not bp.custom_employee_function:
                continue
            ef_doc = frappe.get_doc("Employee Function", bp.custom_employee_function)
            if next((r for r in (ef_doc.table_bukm or []) if r.store_warehouse), None):
                return bp.name
        return None

    def test_endpoint_returns_the_expected_shape(self):
        from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
            get_untagged_material_data,
        )

        name = self._candidate()
        if not name:
            self.skipTest("no submitted Batch Planning with a store warehouse")

        out = get_untagged_material_data(name)
        self.assertIn("results", out)
        self.assertTrue(out["warehouse"])
        # Untagged stock became reservable on 2026-09-07; the flag exists to say
        # "nothing CAN be reserved", which is no longer true.
        self.assertFalse(out["allocation_pending"])

        for row in out["results"]:
            for key in (
                "qty_required", "total_stock", "main_stock", "global_allocated",
                "current_allocated", "current_main_allocated",
                "free_qty", "lab_stock", "lab_available", "labware_qty",
                "global_main_allocated", "lab_after_alloc",
                "mr_qty", "po_qty", "pr_qty", "net_requirement",
            ):
                self.assertIn(key, row, f"{key} missing from {row['item_code']}")

    def test_the_locked_formulas_hold_on_every_row(self):
        from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
            get_untagged_material_data,
        )

        name = self._candidate()
        if not name:
            self.skipTest("no submitted Batch Planning with a store warehouse")

        rows = get_untagged_material_data(name)["results"]
        if not rows:
            self.skipTest("no BOM items on this plan")

        for r in rows:
            self.assertAlmostEqual(
                r["total_stock"], r["main_stock"] + r["lab_stock"], places=1,
                msg=f"Total Stock != Main + Lab on {r['item_code']}",
            )
            # Lab is credited from 2026-09-07: every untagged unit is available
            # wherever it sits until something reserves it.
            self.assertAlmostEqual(
                r["free_qty"], r["total_stock"] - r["global_allocated"], places=1,
                msg=f"Free Qty != Total Stock - Allocated(Global) on {r['item_code']}",
            )
            # Only the MAIN-sourced share is added. Lab-sourced allocation moves
            # nothing, and those units are already inside lab_stock, so adding
            # the whole reservation would count them twice.
            self.assertAlmostEqual(
                r["lab_after_alloc"],
                r["lab_stock"] + r["current_main_allocated"], places=1,
                msg=f"Lab After Alloc != Existing + Main-sourced on {r['item_code']}",
            )
            self.assertLessEqual(
                r["current_main_allocated"], r["current_allocated"] + 1e-6,
                msg=f"Main-sourced share exceeds the whole draw on {r['item_code']}",
            )
            # The symbolic lab_item -> Labware move: what Lab Item gives up,
            # Labware takes, and the gross figure Total Stock reconciles
            # against is untouched by it.
            self.assertAlmostEqual(
                r["lab_available"] + r["labware_qty"], r["lab_stock"], places=1,
                msg=f"Lab Item + Labware != gross lab stock on {r['item_code']}",
            )
            # The Allocated column dropped its lab half to Labware. Nothing was
            # lost in the move: the two still account for the whole reservation.
            self.assertAlmostEqual(
                r["global_main_allocated"] + r["labware_qty"],
                r["global_allocated"], places=1,
                msg=f"Allocated + Labware != total reservation on {r['item_code']}",
            )
            # And the row still reads straight across.
            self.assertAlmostEqual(
                r["main_stock"] + r["lab_available"] - r["global_main_allocated"],
                r["free_qty"], places=1,
                msg=f"Main Wh + Lab Item - Allocated != Free Qty on {r['item_code']}",
            )
            expected_net = max(
                r["qty_required"] - r["free_qty"] - r["mr_qty"] - r["po_qty"], 0.0
            )
            self.assertAlmostEqual(
                r["net_requirement"], round(expected_net, 2), places=1,
                msg=f"Net Req off on {r['item_code']}",
            )

    def test_unapproved_grn_is_never_subtracted(self):
        """Display only — those units are already credited through Open PO."""
        from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
            get_untagged_material_data,
        )

        name = self._candidate()
        if not name:
            self.skipTest("no submitted Batch Planning with a store warehouse")

        for r in get_untagged_material_data(name)["results"]:
            if r["pr_qty"] > 0:
                without_grn = max(
                    r["qty_required"] - r["free_qty"] - r["mr_qty"] - r["po_qty"], 0.0
                )
                self.assertAlmostEqual(
                    r["net_requirement"], round(without_grn, 2), places=1,
                    msg=f"Unapproved GRN leaked into Net Req on {r['item_code']}",
                )
                return
        self.skipTest("no rows with unapproved GRN to check")


class TestUntaggedLabStockIsEmployeeFunctionScoped(unittest.TestCase):
    """Lab Wise must count only THIS function's labs.

    The bug this pins: untagged Lab Wise was scoped with `warehouse <> main`,
    which under a batch tag means "this batch's labs" but with no tag means
    "every warehouse in the company". CN02010004 read 130,500 where only 6,300
    was in the function's own labs, and Total Stock came out identical for every
    Employee Function because it was simply all untagged stock everywhere.
    """

    def _bp_by_ef(self):
        """Submitted Batch Plannings grouped by Employee Function."""
        out = {}
        for bp in frappe.get_all(
            "Batch Planning",
            filters={"docstatus": 1},
            fields=["name", "custom_employee_function"],
            order_by="modified desc",
        ):
            ef = bp.custom_employee_function
            if ef and ef not in out:
                out[ef] = bp.name
        return out

    def test_lab_stock_only_counts_declared_lab_warehouses(self):
        """Recompute independently from table_szrn and demand an exact match."""
        from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
            _ef_lab_warehouses,
            get_untagged_material_data,
        )

        by_ef = self._bp_by_ef()
        if not by_ef:
            self.skipTest("no submitted Batch Planning")

        checked = 0
        for ef, bp_name in list(by_ef.items())[:3]:
            ef_doc = frappe.get_doc("Employee Function", ef)
            lab_warehouses = _ef_lab_warehouses(ef_doc)

            try:
                rows = get_untagged_material_data(bp_name)["results"]
            except frappe.ValidationError:
                continue

            for r in rows[:10]:
                expected = 0.0
                if lab_warehouses:
                    expected = flt(
                        frappe.db.sql(
                            """
                            SELECT IFNULL(SUM(actual_qty), 0)
                            FROM `tabStock Ledger Entry`
                            WHERE item_code = %(item)s
                              AND warehouse IN %(whs)s
                              AND is_cancelled = 0
                              AND (batch_planning_id IS NULL OR batch_planning_id = '')
                            """,
                            {"item": r["item_code"], "whs": tuple(lab_warehouses)},
                        )[0][0]
                        or 0.0
                    )
                self.assertAlmostEqual(
                    r["lab_stock"], round(expected, 2), places=1,
                    msg=(f"{r['item_code']} on {ef}: Lab Wise counts warehouses "
                         f"outside this function's declared labs"),
                )
                checked += 1

        if not checked:
            self.skipTest("no rows to check")

    def test_total_stock_differs_between_employee_functions(self):
        """The symptom check: an EF-scoped figure must move when the EF changes.

        Before the fix every function reported the same Total Stock for a shared
        item, because the figure was the company-wide untagged total.
        """
        from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
            get_untagged_material_data,
        )

        by_ef = self._bp_by_ef()
        if len(by_ef) < 2:
            self.skipTest("need two Employee Functions with submitted plans")

        totals = {}
        for ef, bp_name in by_ef.items():
            try:
                for r in get_untagged_material_data(bp_name)["results"]:
                    totals.setdefault(r["item_code"], {})[ef] = r["total_stock"]
            except frappe.ValidationError:
                continue

        shared = {
            item: per_ef for item, per_ef in totals.items()
            if len(per_ef) >= 2 and any(v for v in per_ef.values())
        }
        if not shared:
            self.skipTest("no item appears on two Employee Functions with stock")

        for item, per_ef in shared.items():
            values = set(round(v, 2) for v in per_ef.values())
            if len(values) > 1:
                return          # at least one item discriminates — fix holds

        self.fail(
            "Untagged Total Stock is identical across every Employee Function "
            f"for all {len(shared)} shared item(s) — the figure is not EF-scoped"
        )


class TestLabItemBreakdown(unittest.TestCase):
    """The drill-down must reconcile with the figure it explains.

    Its only job is to let someone verify the Lab Item sum instead of trusting
    it, so a breakdown that does not add up to the row is worse than none.
    """

    def _candidate(self):
        for bp in frappe.get_all(
            "Batch Planning",
            filters={"docstatus": 1},
            fields=["name", "custom_employee_function"],
            order_by="modified desc",
            limit=25,
        ):
            if not bp.custom_employee_function:
                continue
            ef_doc = frappe.get_doc("Employee Function", bp.custom_employee_function)
            if next((r for r in (ef_doc.table_bukm or []) if r.store_warehouse), None):
                return bp.name
        return None

    def test_parts_sum_to_the_row_total(self):
        from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
            get_untagged_lab_breakdown,
            get_untagged_material_data,
        )

        name = self._candidate()
        if not name:
            self.skipTest("no submitted Batch Planning with a store warehouse")

        rows = get_untagged_material_data(name)["results"]
        if not rows:
            self.skipTest("no BOM items on this plan")

        checked = 0
        for r in rows[:15]:
            detail = get_untagged_lab_breakdown(name, r["item_code"])
            self.assertAlmostEqual(
                detail["total"], r["lab_stock"], places=1,
                msg=(f"{r['item_code']}: breakdown totals {detail['total']} but the "
                     f"row shows Lab Item {r['lab_stock']}"),
            )
            self.assertAlmostEqual(
                sum(x["qty"] for x in detail["rows"]), detail["total"], places=1,
                msg=f"{r['item_code']}: reported total is not the sum of its own rows",
            )
            checked += 1
        self.assertGreater(checked, 0)

    def test_every_declared_lab_is_listed_even_when_empty(self):
        """An empty lab that was checked is not the same as a lab not checked."""
        from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
            _ef_lab_warehouses,
            get_untagged_lab_breakdown,
            get_untagged_material_data,
        )

        name = self._candidate()
        if not name:
            self.skipTest("no submitted Batch Planning with a store warehouse")

        ef = frappe.db.get_value("Batch Planning", name, "custom_employee_function")
        expected = set(_ef_lab_warehouses(frappe.get_doc("Employee Function", ef)))

        rows = get_untagged_material_data(name)["results"]
        if not rows:
            self.skipTest("no BOM items on this plan")

        detail = get_untagged_lab_breakdown(name, rows[0]["item_code"])
        self.assertEqual(
            set(x["warehouse"] for x in detail["rows"]), expected,
            "breakdown must list every declared lab warehouse, zero or not",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
