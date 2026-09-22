import unittest
from unittest.mock import patch

import frappe

from custom_batch_planning.custom_batch_planning.doctype.material_allocation import (
	material_allocation as ma_module,
)


class GuardHarness(ma_module.MaterialAllocation):

	def __init__(self, stock_entry=None, material_request=None):
		self.docstatus = 1
		self.stock_entry = stock_entry
		self.material_request = material_request
		self.material_allocation = []
		self.allocation_status = "Allocated"
		self.cleared = []

	def db_set(self, field, value, update_modified=True):
		self.cleared.append((field, value))
		setattr(self, field, value)

	def save(self):
		self.saved = True

	def save_allocation_log(self, status):
		self.logged = status


def _harness(stock_entry=None, material_request=None):
	doc = object.__new__(GuardHarness)
	GuardHarness.__init__(doc, stock_entry, material_request)
	return doc


class TestDeallocationGuard(unittest.TestCase):
	def _run(self, doc, se_row=None, mr_row=None):

		def get_value(doctype, name, fields, as_dict=False):
			if doctype == "Stock Entry":
				return se_row
			if doctype == "Material Request":
				return mr_row
			raise AssertionError(f"unexpected lookup on {doctype}")

		with patch.object(frappe.db, "get_value", side_effect=get_value):
			return doc.deallocate()

	def test_a_submitted_stock_entry_still_blocks(self):
		doc = _harness(stock_entry="SE-0001")
		with self.assertRaises(Exception) as caught:
			self._run(doc, se_row=frappe._dict(name="SE-0001", docstatus=1))
		self.assertIn("SE-0001", str(caught.exception))

	def test_a_draft_stock_entry_still_blocks(self):
		doc = _harness(stock_entry="SE-0002")
		with self.assertRaises(Exception) as caught:
			self._run(doc, se_row=frappe._dict(name="SE-0002", docstatus=0))
		self.assertIn("SE-0002", str(caught.exception))

	def test_an_approved_mr_with_no_stock_entry_blocks(self):
		doc = _harness(material_request="MR-0001")
		with self.assertRaises(Exception) as caught:
			self._run(
				doc,
				mr_row=frappe._dict(
					name="MR-0001", docstatus=1, workflow_state="Approved"
				),
			)
		self.assertIn("MR-0001", str(caught.exception))

	def test_a_draft_mr_blocks_and_says_so_differently(self):
		doc = _harness(material_request="MR-0002")
		with self.assertRaises(Exception) as caught:
			self._run(
				doc, mr_row=frappe._dict(name="MR-0002", docstatus=0, workflow_state="Draft")
			)
		self.assertIn("Draft", str(caught.exception))

	def test_a_cancelled_mr_is_no_link_at_all(self):
		doc = _harness(material_request="MR-0003")
		self._run(
			doc, mr_row=frappe._dict(name="MR-0003", docstatus=2, workflow_state="Cancelled")
		)
		self.assertIn(("material_request", None), doc.cleared)
		self.assertEqual(doc.allocation_status, "Deallocated")

	def test_no_links_at_all_deallocates(self):
		doc = _harness()
		self._run(doc)
		self.assertEqual(doc.allocation_status, "Deallocated")


class TestStockEntryResolution(unittest.TestCase):

	def test_the_direct_link_is_tried_first(self):
		with patch.object(frappe.db, "get_value", return_value="MA-0001") as gv:
			self.assertEqual(ma_module._allocation_for_stock_entry("SE-0001"), "MA-0001")
		gv.assert_called_once()

	def test_it_falls_back_through_the_material_request(self):

		def get_value(doctype, filters, fieldname):
			if doctype == "Material Allocation" and "stock_entry" in filters:
				return None
			if doctype == "Material Allocation" and filters.get("material_request") == "MR-0001":
				return "MA-0002"
			return None

		with patch.object(frappe.db, "get_value", side_effect=get_value):
			with patch.object(frappe.db, "get_all", return_value=["MR-0001"]):
				self.assertEqual(
					ma_module._allocation_for_stock_entry("SE-0002"), "MA-0002"
				)

	def test_an_unrelated_stock_entry_resolves_to_nothing(self):
		with patch.object(frappe.db, "get_value", return_value=None):
			with patch.object(frappe.db, "get_all", return_value=[]):
				self.assertIsNone(ma_module._allocation_for_stock_entry("SE-0003"))
