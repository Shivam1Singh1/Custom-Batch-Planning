"""The allocation priority rule: local free stock first, global second.

split_local_first is the one place that rule lives. Both the proposal built by
create_bulk_material_allocations and the save-time enforcement in
Material Allocation.check_global_free_stock_limit run through it, so asserting
it here covers both paths.

Run:  bench --site <site> run-tests --app custom_batch_planning \
          --module custom_batch_planning.custom_batch_planning.doctype.batch_planning.test_local_first_allocation
"""

import unittest

from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
    split_local_first,
)


class TestLocalFirstAllocation(unittest.TestCase):

    def split(self, qty, local, glob):
        result = split_local_first([qty], local, glob)
        row = result["rows"][0]
        return row["from_local"], row["from_global"], result

    def test_local_is_exhausted_before_global_is_touched(self):
        from_local, from_global, _ = self.split(100, 40, 80)
        self.assertEqual(from_local, 40)
        self.assertEqual(from_global, 60)

    def test_no_global_used_while_local_covers_the_request(self):
        from_local, from_global, _ = self.split(30, 40, 80)
        self.assertEqual(from_local, 30)
        self.assertEqual(from_global, 0)

    def test_local_exactly_covers_the_request(self):
        from_local, from_global, _ = self.split(40, 40, 80)
        self.assertEqual(from_local, 40)
        self.assertEqual(from_global, 0)

    def test_no_local_stock_falls_entirely_to_global(self):
        from_local, from_global, _ = self.split(50, 0, 80)
        self.assertEqual(from_local, 0)
        self.assertEqual(from_global, 50)

    def test_negative_local_pool_contributes_nothing(self):
        from_local, from_global, result = self.split(50, -30, 80)
        self.assertEqual(from_local, 0)
        self.assertEqual(from_global, 50)
        self.assertEqual(result["capacity"], 80)
        self.assertEqual(result["shortfall"], 0)

    def test_parts_always_add_back_up_to_the_request(self):
        for qty, local, glob in ((100, 40, 80), (30, 40, 80), (7.5, 2.25, 10), (0, 40, 80)):
            from_local, from_global, _ = self.split(qty, local, glob)
            self.assertAlmostEqual(from_local + from_global, qty, places=6)

    def test_shortfall_reported_when_both_pools_are_exceeded(self):
        _, _, result = self.split(200, 40, 80)
        self.assertEqual(result["capacity"], 120)
        self.assertEqual(result["shortfall"], 80)

    def test_no_shortfall_when_the_request_exactly_drains_both_pools(self):
        from_local, from_global, result = self.split(120, 40, 80)
        self.assertEqual(result["shortfall"], 0)
        self.assertEqual((from_local, from_global), (40, 80))

    def test_rows_for_one_item_share_a_single_local_pool(self):
        result = split_local_first([30, 30, 30], 40, 80)
        self.assertEqual(
            [(r["from_local"], r["from_global"]) for r in result["rows"]],
            [(30, 0), (10, 20), (0, 30)],
        )
        self.assertEqual(
            sum(r["from_local"] for r in result["rows"]), result["local_free"]
        )
        self.assertEqual(result["requested"], 90)
        self.assertEqual(result["shortfall"], 0)

    def test_multi_row_shortfall_is_measured_across_all_rows(self):
        result = split_local_first([70, 70], 40, 80)
        self.assertEqual(result["requested"], 140)
        self.assertEqual(result["shortfall"], 20)

    def test_empty_quantities_still_reports_the_pools(self):
        result = split_local_first([], -5, 80)
        self.assertEqual(result["local_free"], 0)
        self.assertEqual(result["global_free"], 80)
        self.assertEqual(result["capacity"], 80)
        self.assertEqual(result["requested"], 0)
        self.assertEqual(result["rows"], [])
