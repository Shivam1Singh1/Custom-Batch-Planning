import unittest

from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
    free_pools,
    settle_cross_batch_draw,
    split_local_first,
)


def _view(bp_main, other_main, bp_loc, bp_glob, oth_loc=0, oth_glob=0):
    bp_main, other_main = settle_cross_batch_draw(bp_main, other_main)
    pools = free_pools(bp_main, bp_loc, bp_glob, other_main, oth_loc, oth_glob)
    return {
        "global_main": other_main,
        "global_allocated": oth_loc + bp_glob,
        "global_free": pools["other_free"],
        "current_main": bp_main,
        "current_allocated": bp_loc + bp_glob,
        "current_free": pools["bp_free"],
        "bp_local": bp_loc,
        "other_global": oth_glob,
    }


def _global_closes(v):
    return abs(v["global_main"] - (v["global_allocated"] + v["global_free"])) < 1e-9


def _pool_invariant_holds(v):
    return abs(
        v["current_main"] - (v["bp_local"] + v["other_global"] + v["current_free"])
    ) < 1e-9


def net_req(qty_required, total_stock, bp_global_allocated, open_mr=0.0, open_po=0.0):
    return max(
        qty_required - total_stock - bp_global_allocated - open_mr - open_po, 0.0
    )


class TestScenario1LocalDraw(unittest.TestCase):

    def test_split_is_entirely_local(self):
        row = split_local_first([40], 100, 90)["rows"][0]
        self.assertEqual((row["from_local"], row["from_global"]), (40, 0))

    def test_allocation_moves_only_the_current_line(self):
        before = _view(100, 90, 0, 0)
        after = _view(100, 90, 40, 0)

        self.assertEqual(after["global_main"], 90)
        self.assertEqual(after["global_allocated"], 0)
        self.assertEqual(after["global_free"], 90, "a local draw touched the global pool")

        self.assertEqual(after["current_allocated"], 40)
        self.assertEqual(after["current_free"], 60)
        self.assertEqual(before["current_free"] - after["current_free"], 40)

    def test_transfer_moves_main_and_clears_the_reservation(self):
        after_alloc = _view(100, 90, 40, 0)
        after_xfer = _view(60, 90, 0, 0)

        self.assertEqual(after_xfer["current_main"], 60, "Main Wh did not fall")
        self.assertEqual(after_xfer["current_allocated"], 0, "phantom hold survived")
        self.assertEqual(
            after_xfer["current_free"], after_alloc["current_free"],
            "Free Qty moved at transfer; it should already have excluded the reservation",
        )

    def test_every_state_closes_on_both_lines(self):
        for v in (_view(100, 90, 0, 0), _view(100, 90, 40, 0), _view(60, 90, 0, 0)):
            self.assertTrue(_global_closes(v))
            self.assertTrue(_pool_invariant_holds(v))
            self.assertAlmostEqual(
                v["current_main"], v["current_allocated"] + v["current_free"]
            )


class TestScenario2GlobalDraw(unittest.TestCase):

    def test_split_is_entirely_global(self):
        row = split_local_first([10], 0, 90)["rows"][0]
        self.assertEqual((row["from_local"], row["from_global"]), (0, 10))

    def test_borrowing_reduces_the_global_free_line(self):
        before = _view(0, 90, 0, 0)
        after = _view(0, 90, 0, 10)

        self.assertEqual(before["global_free"], 90)
        self.assertEqual(after["global_free"], 80, "global pool still offers the borrowed units")
        self.assertEqual(after["global_allocated"], 10, "borrowing missing from Global Allocated")

    def test_both_lines_show_the_borrowed_qty(self):
        after = _view(0, 90, 0, 10)
        self.assertEqual(after["global_allocated"], 10)
        self.assertEqual(after["current_allocated"], 10)

    def test_global_line_closes_and_current_line_does_not(self):
        after = _view(0, 90, 0, 10)
        self.assertTrue(_global_closes(after))

        gap = after["current_allocated"] + after["current_free"] - after["current_main"]
        self.assertEqual(gap, 10, "the current-line gap must equal the borrowed qty")
        self.assertTrue(_pool_invariant_holds(after), "the pool-side invariant must still hold")

    def test_transfer_of_borrowed_stock_drains_the_lender(self):
        after_alloc = _view(0, 90, 0, 10)
        after_xfer = _view(-10, 90, 0, 0)

        self.assertEqual(after_xfer["global_main"], 80, "Main Wh did not fall for the lender")
        self.assertEqual(after_xfer["global_allocated"], 0, "phantom hold survived")
        self.assertEqual(
            after_xfer["global_free"], after_alloc["global_free"],
            "Free Qty moved at transfer",
        )
        self.assertTrue(_global_closes(after_xfer))
        self.assertTrue(_pool_invariant_holds(after_xfer))


class TestScenario3LegacyRowWithNoSplit(unittest.TestCase):

    def test_missing_split_hides_the_draw_from_the_global_pool(self):
        correct = _view(0, 90, 0, 10)
        legacy = _view(0, 90, 10, 0)

        self.assertEqual(correct["global_free"], 80)
        self.assertEqual(legacy["global_free"], 90, "precondition: the pool never moved")
        self.assertEqual(legacy["current_free"], -10)

    def test_the_negative_is_what_the_ui_clamps_away(self):
        legacy = _view(0, 90, 10, 0)
        self.assertLess(legacy["current_free"], 0)
        self.assertEqual(max(legacy["current_free"], 0.0), 0.0)


class TestNetRequirement(unittest.TestCase):

    def test_local_allocation_is_not_credited_twice(self):
        self.assertEqual(net_req(100, 40 + 0, 10), 50)

    def test_old_formula_understated_by_the_local_reservation(self):
        old = max(100 - 40 - 0 - 10 - 40, 0.0)
        self.assertEqual(old, 10)
        self.assertEqual(net_req(100, 40, 10) - old, 40)

    def test_grouping_lab_into_total_stock_is_not_the_fix(self):
        proposal = max(100 - (40 + 0) - (40 + 10), 0.0)
        self.assertEqual(proposal, max(100 - 40 - 0 - 10 - 40, 0.0))

    def test_lab_stock_is_counted_once_through_total_stock(self):
        self.assertEqual(net_req(100, 40 + 25, 0), 35)

    def test_borrowed_stock_is_credited(self):
        self.assertEqual(net_req(100, 0, 10), 90)

    def test_open_pipeline_is_subtracted(self):
        self.assertEqual(net_req(100, 40, 10, open_mr=20, open_po=5), 25)

    def test_never_negative(self):
        self.assertEqual(net_req(10, 500, 0), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
