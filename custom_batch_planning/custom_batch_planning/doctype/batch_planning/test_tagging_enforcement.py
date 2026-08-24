"""Cutover tagging enforcement across Material Request, PO, PR and Stock Entry.

The matrix each doctype must satisfy:

    pre-cutover, blanks        -> saves (grandfathered, forward-only rule)
    post-cutover, blanks       -> throws
    post-cutover, fully tagged -> saves

These run against the validator directly with a stub document rather than by
inserting real ERPNext documents. Inserting a Purchase Receipt for real drags
in supplier, tax template, valuation and stock-availability validation that has
nothing to do with tagging, and a failure there would look like a tagging
failure. The stub carries exactly the fields the validator reads, so a failure
here can only mean the tagging rule itself moved.

Run:  bench --site <site> run-tests --app custom_batch_planning \
          --module custom_batch_planning.custom_batch_planning.doctype.batch_planning.test_tagging_enforcement
"""

import unittest

import frappe

from custom_batch_planning.api.tagging_enforcement import (
    DOC_DATE_FIELDS,
    TAGGING_FIELDS,
    enforce_ef_project_tagging,
    is_exempt,
    is_post_cutover,
)
from custom_batch_planning.custom_batch_planning.doctype.batch_planning_settings.batch_planning_settings import (
    get_exempt_stock_entry_purposes,
    get_stock_cutover_datetime,
)

CUTOVER = "2026-06-01 09:00:00"

PARENT_EF_FIELD = {
    "Material Request": "custom_employee_function",
    "Purchase Order": "employee_function",
    "Purchase Receipt": "employee_function",
    "Stock Entry": "employee_function",
}


class _Row(dict):
    """Child row stub: attribute access plus .get(), like a frappe Document."""

    def __init__(self, idx, **kwargs):
        super().__init__(**kwargs)
        self.idx = idx

    def get(self, key, default=None):
        return super().get(key, default)


class _Doc(dict):
    def __init__(self, doctype, **kwargs):
        super().__init__(**kwargs)
        self.doctype = doctype

    def get(self, key, default=None):
        return super().get(key, default)


def _make_doc(doctype, dated, tagged, rows=1, purpose=None):
    """Build a stub document of `doctype` dated `dated`, tagged or not."""
    date_field, time_field = DOC_DATE_FIELDS[doctype]
    doc = _Doc(doctype)
    doc[date_field] = dated
    if time_field:
        doc[time_field] = "12:00:00"
    if purpose:
        doc["stock_entry_type"] = purpose

    if tagged:
        doc[PARENT_EF_FIELD[doctype]] = "VP-LTP-PRE-001"
        doc["project"] = "PLTP-2025-0001"

    doc["items"] = [_Row(i + 1, item_code=f"ITEM-{i}") for i in range(rows)]
    return doc


class TestTaggingEnforcement(unittest.TestCase):

    def test_pre_cutover_untagged_document_is_allowed(self):
        """Forward-only: documents dated before go-live keep their blanks."""
        for doctype in TAGGING_FIELDS:
            doc = _make_doc(doctype, "2026-05-31", tagged=False)
            try:
                enforce_ef_project_tagging(doc, cutover_dt=CUTOVER)
            except frappe.ValidationError as exc:
                self.fail(f"{doctype} dated before the cutover was rejected: {exc}")

    def test_post_cutover_untagged_document_is_rejected(self):
        """The whole point: no untagged document may exist after go-live."""
        for doctype in TAGGING_FIELDS:
            doc = _make_doc(doctype, "2026-06-02", tagged=False)
            with self.assertRaises(frappe.ValidationError, msg=f"{doctype} not enforced"):
                enforce_ef_project_tagging(doc, cutover_dt=CUTOVER)

    def test_post_cutover_tagged_document_is_allowed(self):
        """Enforcement must not block correctly tagged work."""
        for doctype in TAGGING_FIELDS:
            doc = _make_doc(doctype, "2026-06-02", tagged=True)
            try:
                enforce_ef_project_tagging(doc, cutover_dt=CUTOVER)
            except frappe.ValidationError as exc:
                self.fail(f"{doctype} was correctly tagged but rejected: {exc}")


    def test_either_field_missing_alone_is_rejected(self):
        """EF without Project, or Project without EF, is still untagged."""
        for doctype in TAGGING_FIELDS:
            ef_only = _make_doc(doctype, "2026-06-02", tagged=False)
            ef_only[PARENT_EF_FIELD[doctype]] = "VP-LTP-PRE-001"
            with self.assertRaises(frappe.ValidationError, msg=f"{doctype} EF-only"):
                enforce_ef_project_tagging(ef_only, cutover_dt=CUTOVER)

            project_only = _make_doc(doctype, "2026-06-02", tagged=False)
            project_only["project"] = "PLTP-2025-0001"
            with self.assertRaises(frappe.ValidationError, msg=f"{doctype} project-only"):
                enforce_ef_project_tagging(project_only, cutover_dt=CUTOVER)

    def test_item_level_tagging_satisfies_the_rule(self):
        """Values on the row count, because the reports read COALESCE(item, parent)."""
        for doctype, config in TAGGING_FIELDS.items():
            doc = _make_doc(doctype, "2026-06-02", tagged=False)
            for row in doc["items"]:
                row[config["item_ef"][0]] = "VP-LTP-PRE-001"
                row[config["item_project"][0]] = "PLTP-2025-0001"
            try:
                enforce_ef_project_tagging(doc, cutover_dt=CUTOVER)
            except frappe.ValidationError as exc:
                self.fail(f"{doctype} tagged at row level was rejected: {exc}")

    def test_one_untagged_row_among_many_is_rejected(self):
        """A single blank row is enough to break the pool, so it must throw."""
        for doctype, config in TAGGING_FIELDS.items():
            doc = _make_doc(doctype, "2026-06-02", tagged=False, rows=4)
            for row in doc["items"][:-1]:
                row[config["item_ef"][0]] = "VP-LTP-PRE-001"
                row[config["item_project"][0]] = "PLTP-2025-0001"
            with self.assertRaises(frappe.ValidationError, msg=f"{doctype} partial rows"):
                enforce_ef_project_tagging(doc, cutover_dt=CUTOVER)

    def test_whitespace_is_not_tagging(self):
        """A field holding spaces is blank, not filled in."""
        doc = _make_doc("Purchase Order", "2026-06-02", tagged=False)
        doc["employee_function"] = "   "
        doc["project"] = "  "
        with self.assertRaises(frappe.ValidationError):
            enforce_ef_project_tagging(doc, cutover_dt=CUTOVER)


    def test_date_only_doctypes_enforce_from_the_whole_go_live_day(self):
        """MR/PO have no time, so the whole go-live day is post-cutover.

        The cutover here is 09:00. A Material Request dated that day carries no
        hour at all; treating it as midnight would leave the morning of go-live
        as an unguarded window.
        """
        for doctype in ("Material Request", "Purchase Order"):
            doc = _make_doc(doctype, "2026-06-01", tagged=False)
            self.assertTrue(
                is_post_cutover(doc, CUTOVER),
                f"{doctype} dated on go-live day was treated as pre-cutover",
            )
            with self.assertRaises(frappe.ValidationError):
                enforce_ef_project_tagging(doc, cutover_dt=CUTOVER)

    def test_datetime_doctypes_respect_the_hour(self):
        """PR/SE carry posting_time, so the exact moment is honoured."""
        for doctype in ("Purchase Receipt", "Stock Entry"):
            before = _make_doc(doctype, "2026-06-01", tagged=False)
            before["posting_time"] = "08:00:00"
            self.assertFalse(is_post_cutover(before, CUTOVER))

            after = _make_doc(doctype, "2026-06-01", tagged=False)
            after["posting_time"] = "10:00:00"
            self.assertTrue(is_post_cutover(after, CUTOVER))

    def test_missing_date_is_treated_as_post_cutover(self):
        """'I don't know when this is' must resolve to enforce, not to allow."""
        doc = _make_doc("Stock Entry", None, tagged=False)
        self.assertTrue(is_post_cutover(doc, CUTOVER))


    def test_exempt_stock_entry_purposes_are_not_blocked(self):
        """General warehouse work must not stop on go-live day."""
        for purpose in get_exempt_stock_entry_purposes():
            doc = _make_doc("Stock Entry", "2026-06-02", tagged=False, purpose=purpose)
            self.assertTrue(is_exempt(doc), f"{purpose} should be exempt")
            try:
                enforce_ef_project_tagging(doc, cutover_dt=CUTOVER)
            except frappe.ValidationError as exc:
                self.fail(f"exempt purpose {purpose} was blocked: {exc}")

    def test_material_transfer_is_never_exempt(self):
        """It is the purpose the batch-planning flow uses, so it must be tagged."""
        doc = _make_doc("Stock Entry", "2026-06-02", tagged=False, purpose="Material Transfer")
        self.assertFalse(is_exempt(doc))
        with self.assertRaises(frappe.ValidationError):
            enforce_ef_project_tagging(doc, cutover_dt=CUTOVER)

    def test_exemption_applies_only_to_stock_entry(self):
        """An MR named after an exempt purpose must not inherit the exemption."""
        doc = _make_doc("Material Request", "2026-06-02", tagged=False)
        doc["stock_entry_type"] = "Repack"
        self.assertFalse(is_exempt(doc))
        with self.assertRaises(frappe.ValidationError):
            enforce_ef_project_tagging(doc, cutover_dt=CUTOVER)


    def test_nothing_is_enforced_before_the_cutover_is_declared(self):
        """Enforcement switches on only when the marker is set."""
        for doctype in TAGGING_FIELDS:
            doc = _make_doc(doctype, "2030-01-01", tagged=False)
            try:
                enforce_ef_project_tagging(doc, cutover_dt=None)
            except frappe.ValidationError as exc:
                if get_stock_cutover_datetime():
                    self.skipTest("a cutover is configured on this site")
                self.fail(f"{doctype} enforced with no cutover declared: {exc}")

    def test_unlisted_doctypes_are_untouched(self):
        """The validator is a no-op for anything not in the table."""
        doc = _Doc("Delivery Note")
        doc["posting_date"] = "2030-01-01"
        enforce_ef_project_tagging(doc, cutover_dt=CUTOVER)


    def test_all_four_doctypes_are_wired_on_validate(self):
        """A doctype in the table but not in hooks would be silently unguarded."""
        hooks = frappe.get_hooks("doc_events") or {}
        for doctype in TAGGING_FIELDS:
            events = hooks.get(doctype, {})
            handlers = events.get("validate") or []
            if isinstance(handlers, str):
                handlers = [handlers]
            self.assertIn(
                "custom_batch_planning.api.tagging_enforcement.validate_tagging",
                handlers,
                f"{doctype} is not wired to the tagging validator on validate",
            )

    def test_enforcement_is_not_wired_on_before_insert(self):
        """before_insert fires once; drafts edited later would keep their blanks."""
        hooks = frappe.get_hooks("doc_events") or {}
        for doctype in TAGGING_FIELDS:
            handlers = (hooks.get(doctype, {}) or {}).get("before_insert") or []
            if isinstance(handlers, str):
                handlers = [handlers]
            self.assertNotIn(
                "custom_batch_planning.api.tagging_enforcement.validate_tagging",
                handlers,
                f"{doctype} enforces on before_insert, which does not hold on re-save",
            )

    def test_existing_hooks_were_not_displaced(self):
        """Adding the validator must not drop the pre-existing validate hooks."""
        hooks = frappe.get_hooks("doc_events") or {}
        expected = {
            "Material Request": "custom_batch_planning.api.pr_integration.validate_material_request",
            "Purchase Order": "custom_batch_planning.api.po_integration.validate_purchase_order",
        }
        for doctype, handler in expected.items():
            handlers = (hooks.get(doctype, {}) or {}).get("validate") or []
            if isinstance(handlers, str):
                handlers = [handlers]
            self.assertIn(handler, handlers, f"{doctype} lost {handler}")
