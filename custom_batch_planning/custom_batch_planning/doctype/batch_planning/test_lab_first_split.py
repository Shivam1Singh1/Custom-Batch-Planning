import unittest

from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
    split_lab_first,
)


class TestLabFirstSplit(unittest.TestCase):
    def test_the_worked_example_from_the_specification(self):
        row = split_lab_first([200], 100, 100)["rows"][0]
        self.assertEqual(row["from_lab"], 100)
        self.assertEqual(row["from_main"], 100)

    def test_lab_is_exhausted_before_the_store_is_touched(self):
        row = split_lab_first([50], 100, 100)["rows"][0]
        self.assertEqual(row["from_lab"], 50)
        self.assertEqual(
            row["from_main"], 0,
            "reserved store stock while lab stock was still free — that "
            "manufactures a transfer nobody needed",
        )

    def test_with_no_lab_stock_everything_falls_to_the_store(self):
        row = split_lab_first([50], 0, 100)["rows"][0]
        self.assertEqual(row["from_lab"], 0)
        self.assertEqual(row["from_main"], 50)

    def test_rows_of_one_item_drain_a_single_shared_lab_pool(self):
        rows = split_lab_first([60, 60], 100, 100)["rows"]
        self.assertEqual(rows[0]["from_lab"], 60)
        self.assertEqual(rows[1]["from_lab"], 40)
        self.assertEqual(rows[1]["from_main"], 20)
        self.assertEqual(
            sum(r["from_lab"] for r in rows), 100,
            "the two rows together took more lab stock than the lab holds",
        )

    def test_shortfall_is_reported_and_rows_still_sum_to_the_request(self):
        split = split_lab_first([300], 100, 100)
        self.assertEqual(split["capacity"], 200)
        self.assertEqual(split["shortfall"], 100)
        row = split["rows"][0]
        self.assertEqual(
            row["from_lab"] + row["from_main"], 300,
            "rows are the REQUEST, not the issue — callers must check shortfall",
        )

    def test_a_negative_half_contributes_nothing(self):
        split = split_lab_first([50], -30, 100)
        self.assertEqual(split["lab_free"], 0)
        self.assertEqual(split["rows"][0]["from_main"], 50)

    def test_capacity_is_the_sum_of_both_halves(self):
        self.assertEqual(split_lab_first([0], 40, 25)["capacity"], 65)


if __name__ == "__main__":
    unittest.main(verbosity=2)
