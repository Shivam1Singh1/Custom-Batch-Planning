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
        sql = _bp_predicate("sle", "UNTAGGED")
        self.assertIn("NULLIF(sle.batch_planning_id, '')", sql)
        self.assertIn("IS NULL", sql)

    def test_untagged_honours_every_place_a_tag_can_live(self):
        from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
            MR_BP, PO_BP, PR_BP,
        )

        for alias, expr, parent in (
            ("mri", MR_BP, "mr"), ("poi", PO_BP, "po"), ("pri", PR_BP, "pr"),
        ):
            sql = _bp_predicate(alias, "UNTAGGED", expr)
            self.assertIn(f"{alias}.batch_planning_id", sql)
            self.assertIn(f"{parent}.custom_batch_planning_no", sql)
            self.assertNotIn(f"{parent}.custom_batch_planning,", sql)
            self.assertTrue(sql.endswith("IS NULL"), sql)

    def test_item_level_custom_field_is_covered_where_it_exists(self):
        from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
            MR_BP, PO_BP, PR_BP,
        )
        self.assertIn("mri.custom_batch_planning_no", MR_BP)
        self.assertIn("poi.custom_batch_planning_no", PO_BP)
        self.assertNotIn("pri.custom_batch_planning_no", PR_BP)

    def test_untagged_takes_no_bp_parameter(self):
        self.assertNotIn("%(bp)s", _bp_predicate("sle", "UNTAGGED"))

    def test_the_three_modes_are_disjoint(self):
        gen = _bp_predicate("sle", "GEN")
        bp = _bp_predicate("sle", "BP")
        untagged = _bp_predicate("sle", "UNTAGGED")

        self.assertIn("IS NOT NULL", gen)
        self.assertIn("IS NULL", untagged)
        self.assertNotIn("IS NULL", gen)
        self.assertEqual(bp, "sle.batch_planning_id = %(bp)s")
        self.assertNotEqual(untagged, gen)
        self.assertNotEqual(untagged, bp)

    def test_existing_modes_are_untouched(self):
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
        self.assertEqual(_project_clause("sle.project", None), "")
        self.assertEqual(_project_clause("sle.project", ""), "")

    def test_it_works_for_the_coalesced_pipeline_expressions(self):
        self.assertEqual(
            _project_clause(MR_PROJECT, "P"), f"AND {MR_PROJECT} = %(project)s"
        )
        self.assertEqual(_project_clause(MR_PROJECT, None), "")

    def test_dropping_the_clause_is_what_makes_untagged_rows_visible(self):
        with_project = _project_clause("sle.project", "P")
        without = _project_clause("sle.project", None)
        self.assertIn("=", with_project)
        self.assertNotIn("=", without)


class TestUntaggedMaterialDataLive(unittest.TestCase):

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
        self.assertFalse(out["allocation_pending"])

        for row in out["results"]:
            for key in (
                "qty_required", "total_stock", "main_stock", "global_allocated",
                "current_allocated", "current_main_allocated",
                "free_qty", "lab_stock", "lab_available", "labware_qty",
                "lab_allocated_global",
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
            self.assertAlmostEqual(
                r["free_qty"], r["total_stock"] - r["global_allocated"], places=1,
                msg=f"Free Qty != Total Stock - Allocated(Global) on {r['item_code']}",
            )
            self.assertAlmostEqual(
                r["lab_after_alloc"],
                r["lab_stock"] + r["current_main_allocated"], places=1,
                msg=f"Lab After Alloc != Existing + Main-sourced on {r['item_code']}",
            )
            self.assertLessEqual(
                r["current_main_allocated"], r["current_allocated"] + 1e-6,
                msg=f"Main-sourced share exceeds the whole draw on {r['item_code']}",
            )
            self.assertAlmostEqual(
                r["lab_available"] + r["lab_allocated_global"], r["lab_stock"],
                places=1,
                msg=f"Lab Item + pool lab claim != gross lab stock on {r['item_code']}",
            )
            self.assertAlmostEqual(
                r["global_main_allocated"] + r["lab_allocated_global"],
                r["global_allocated"], places=1,
                msg=f"Allocated + pool lab claim != total reservation on {r['item_code']}",
            )
            self.assertLessEqual(
                r["labware_qty"], r["lab_allocated_global"] + 1e-6,
                msg=f"Labware exceeds the pool-wide lab claim on {r['item_code']}",
            )
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

    def _bp_by_ef(self):
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
                return

        self.fail(
            "Untagged Total Stock is identical across every Employee Function "
            f"for all {len(shared)} shared item(s) — the figure is not EF-scoped"
        )


class TestLabItemBreakdown(unittest.TestCase):

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
