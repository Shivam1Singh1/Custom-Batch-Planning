"""The deallocation guard: what counts as stock already committed.

Deallocation releases quantities other allocations may then take. The guard
decides when that is no longer safe. Before the Material Request step existed
there was one answer - a live Stock Entry - and the check read the `stock_entry`
link alone.

Inserting Material Request between allocation and transfer opened a window that
check cannot see: the request is raised and submitted, the stock is spoken for,
but nobody has pressed Create > Stock Entry yet, so `stock_entry` is still
empty. Deallocating there hands away quantities an approved request is counting
on. The case below named "approved MR, no Stock Entry yet" is that window, and
it is the one this suite exists for.

These run against a site, like the rest of the app's suites:

    bench --site <site> run-tests --app custom_batch_planning \\
        --module custom_batch_planning.custom_batch_planning.doctype.material_allocation.test_material_allocation

Each test stubs only the two lookups the guard performs, so nothing here
inserts documents or touches the ledger. What is asserted is the guard's
decision, not the surrounding allocation logic, which this change does not
alter.
"""

import unittest
from unittest.mock import patch

import frappe

from custom_batch_planning.custom_batch_planning.doctype.material_allocation import (
	material_allocation as ma_module,
)


class GuardHarness(ma_module.MaterialAllocation):
	"""A Material Allocation stripped to what the guard reads.

	Built with object.__new__ rather than frappe.new_doc so no site document is
	created: deallocate() only reaches the two link lookups before it throws,
	and every test here expects it to throw or to get past them.
	"""

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
		"""Run deallocate() with both link lookups stubbed."""

		def get_value(doctype, name, fields, as_dict=False):
			if doctype == "Stock Entry":
				return se_row
			if doctype == "Material Request":
				return mr_row
			raise AssertionError(f"unexpected lookup on {doctype}")

		with patch.object(frappe.db, "get_value", side_effect=get_value):
			return doc.deallocate()

	def test_a_submitted_stock_entry_still_blocks(self):
		"""The original rule, unchanged by the new step."""
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
		"""The window this suite exists for.

		docstatus 1 is the gate. Where a Material Request workflow is installed
		the document only reaches it on the approve transition; where none is
		installed it is the only signal available. Reading workflow_state
		instead would let everything through on a site without a workflow.
		"""
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
		"""A draft request is not committed stock, but it is still a document
		someone must deal with - deleting the allocation's quantities out from
		under it leaves a request nobody can fulfil. Same treatment a draft
		Stock Entry has always had."""
		doc = _harness(material_request="MR-0002")
		with self.assertRaises(Exception) as caught:
			self._run(
				doc, mr_row=frappe._dict(name="MR-0002", docstatus=0, workflow_state="Draft")
			)
		self.assertIn("Draft", str(caught.exception))

	def test_a_cancelled_mr_is_no_link_at_all(self):
		"""Cancelled leaves a stale pointer. It is cleared and does not block,
		which is what frees the allocation to raise a fresh request."""
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
	"""_allocation_for_stock_entry: the chain that survives the new step.

	The Stock Entry is now made by ERPNext's mapper from the Material Request,
	so nothing sets `stock_entry` on the allocation up front. If this lookup
	fails the allocation never reaches "Stock Entry Done", and six queries
	across this app read that status to mean "no longer holding stock".
	"""

	def test_the_direct_link_is_tried_first(self):
		with patch.object(frappe.db, "get_value", return_value="MA-0001") as gv:
			self.assertEqual(ma_module._allocation_for_stock_entry("SE-0001"), "MA-0001")
		gv.assert_called_once()

	def test_it_falls_back_through_the_material_request(self):
		"""No direct link, but the Stock Entry's rows name the request."""

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
