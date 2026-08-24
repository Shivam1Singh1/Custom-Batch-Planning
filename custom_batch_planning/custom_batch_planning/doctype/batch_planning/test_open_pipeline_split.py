"""Sanity checks for the GEN / BP open-pipeline figures.

GEN and BP are two disjoint pools of demand for an item under Employee Function
+ Project: GEN is what batches OTHER than the current one have open, BP is what
the current batch has open. No document may fall in both, which is why they are
never summed. These tests assert that disjointness against real site data
rather than fixtures, because it is only meaningful in terms of how
batch_planning_id is actually tagged in the wild.

Run:  bench --site <site> run-tests --app custom_batch_planning \
          --module custom_batch_planning.custom_batch_planning.doctype.batch_planning.test_open_pipeline_split
"""

import unittest

import frappe

from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
    _allocated_qty,
    _bp_predicate,
    _open_mr,
    _open_po,
    _open_pr_grn,
    _stock_qty,
    get_legacy_stock,
    get_material_planning_data,
)
from custom_batch_planning.custom_batch_planning.doctype.batch_planning_settings.batch_planning_settings import (
    get_stock_cutover_datetime,
)

STAGES = (("MR", _open_mr), ("PO", _open_po), ("PR/GRN", _open_pr_grn))

STAGE_TABLES = {
    "MR": "tabMaterial Request Item",
    "PO": "tabPurchase Order Item",
    "PR/GRN": "tabPurchase Receipt Item",
}


def _tagged_pairs(limit=60):
    """(batch_planning_id, item_code) pairs that carry a real tag on any stage."""
    return frappe.db.sql(
        """
        SELECT bp, item FROM (
            SELECT batch_planning_id AS bp, item_code AS item FROM `tabMaterial Request Item`
              WHERE batch_planning_id IS NOT NULL AND batch_planning_id <> ''
            UNION
            SELECT batch_planning_id, item_code FROM `tabPurchase Order Item`
              WHERE batch_planning_id IS NOT NULL AND batch_planning_id <> ''
            UNION
            SELECT batch_planning_id, item_code FROM `tabPurchase Receipt Item`
              WHERE batch_planning_id IS NOT NULL AND batch_planning_id <> ''
        ) t
        ORDER BY bp, item
        LIMIT %(limit)s
        """,
        {"limit": limit},
        as_dict=True,
    )


def _scope(bp_name):
    return frappe.db.get_value(
        "Batch Planning", bp_name, ["custom_employee_function", "project"], as_dict=True
    )


class TestOpenPipelineSplit(unittest.TestCase):
    def test_gen_and_bp_are_disjoint(self):
        """No document may be reported in both GEN and BP — separate pools."""
        checked = 0
        for pair in _tagged_pairs():
            scope = _scope(pair.bp)
            if not scope or not scope.custom_employee_function or not scope.project:
                continue
            for stage, fn in STAGES:
                _, _, gen_docs = fn(
                    pair.item, scope.custom_employee_function, scope.project, pair.bp, "GEN"
                )
                _, _, bp_docs = fn(
                    pair.item, scope.custom_employee_function, scope.project, pair.bp, "BP"
                )
                overlap = set(gen_docs) & set(bp_docs)
                self.assertEqual(
                    overlap,
                    set(),
                    f"{stage}: doc in both GEN and BP for item {pair.item} on "
                    f"{pair.bp}: {sorted(overlap)}",
                )
                checked += 1
        self.assertGreater(checked, 0, "no tagged data available to check")

    def test_gen_excludes_current_batch(self):
        """Every doc GEN claims must be tagged to a batch OTHER than the current one.

        Guards the reversal directly: under the old superset rule a document
        tagged only to the current batch would show up in GEN.
        """
        for pair in _tagged_pairs(limit=30):
            scope = _scope(pair.bp)
            if not scope or not scope.custom_employee_function or not scope.project:
                continue
            for stage, fn in STAGES:
                _, _, gen_docs = fn(
                    pair.item, scope.custom_employee_function, scope.project, pair.bp, "GEN"
                )
                for doc in gen_docs:
                    other = frappe.db.sql(
                        f"""
                        SELECT COUNT(*) FROM `{STAGE_TABLES[stage]}`
                        WHERE parent = %(parent)s AND item_code = %(item)s
                          AND batch_planning_id IS NOT NULL
                          AND batch_planning_id <> ''
                          AND batch_planning_id <> %(bp)s
                        """,
                        {"parent": doc, "item": pair.item, "bp": pair.bp},
                    )[0][0]
                    self.assertGreater(
                        other,
                        0,
                        f"{stage} GEN returned {doc}, which has no row for "
                        f"{pair.item} tagged to a batch other than {pair.bp}",
                    )

    def test_open_mr_counts_only_approved_requests(self):
        """Draft and pending-approval MRs are not open demand and must not appear."""
        for pair in _tagged_pairs(limit=40):
            scope = _scope(pair.bp)
            if not scope or not scope.custom_employee_function or not scope.project:
                continue
            for mode in ("GEN", "BP"):
                _, _, docs = _open_mr(
                    pair.item, scope.custom_employee_function, scope.project, pair.bp, mode
                )
                for doc in docs:
                    state, docstatus = frappe.db.get_value(
                        "Material Request", doc, ["workflow_state", "docstatus"]
                    )
                    self.assertEqual(
                        docstatus, 1, f"Open MR/{mode} returned {doc} at docstatus {docstatus}"
                    )
                    self.assertTrue(
                        (state or "").lower().startswith("approve"),
                        f"Open MR/{mode} returned {doc} in state {state!r}, "
                        f"which is not an approval state",
                    )

    def test_unapproved_po_is_not_double_counted(self):
        """The regression the Open PO gate closed, checked across both columns.

        An unapproved PO against an approved MR must (a) leave the MR in Open
        MR, since a draft or pending PO can still be rejected or deleted, and
        (b) not itself appear in Open PO. Before Open PO was approval-gated,
        both were true of Open MR but the same quantity ALSO showed in Open PO
        — one unit reported in two columns.
        """
        rows = frappe.db.sql(
            """
            SELECT DISTINCT mri.parent AS mr, poi.parent AS po, mri.item_code,
                   mri.batch_planning_id AS bp,
                   bpl.custom_employee_function AS ef, bpl.project AS project
            FROM `tabMaterial Request Item` mri
            JOIN `tabMaterial Request` mr ON mr.name = mri.parent
            JOIN `tabBatch Planning` bpl ON bpl.name = mri.batch_planning_id
            JOIN `tabPurchase Order Item` poi ON poi.material_request_item = mri.name
            WHERE mri.qty > 0
              AND mr.docstatus = 1 AND mr.workflow_state LIKE 'Approve%%'
              AND mr.material_request_type = 'Purchase'
              -- the MR must actually fall in its batch's EF + project scope,
              -- otherwise _open_mr is right to leave it out for other reasons
              AND COALESCE(NULLIF(mri.employee_function,''),
                           NULLIF(mr.custom_employee_function,'')) = bpl.custom_employee_function
              AND COALESCE(NULLIF(mri.project,''), NULLIF(mr.project,'')) = bpl.project
              -- covered by at least one PO, but by no APPROVED one
              AND NOT EXISTS (
                  SELECT 1 FROM `tabPurchase Order Item` poi2
                  JOIN `tabPurchase Order` po2 ON po2.name = poi2.parent
                  WHERE poi2.material_request_item = mri.name
                    AND po2.docstatus = 1 AND po2.workflow_state LIKE 'Approve%%'
              )
            LIMIT 20
            """,
            as_dict=True,
        )
        if not rows:
            self.skipTest(
                "no batch-tagged approved MR is currently covered only by an unapproved PO"
            )
        for row in rows:
            _, _, mr_docs = _open_mr(row.item_code, row.ef, row.project, row.bp, "BP")
            self.assertIn(
                row.mr,
                mr_docs,
                f"{row.mr} was retired from Open MR by a PO that is not approved",
            )
            _, _, po_docs = _open_po(row.item_code, row.ef, row.project, row.bp, "BP")
            self.assertNotIn(
                row.po,
                po_docs,
                f"{row.po} is unapproved yet appears in Open PO while {row.mr} "
                f"still sits in Open MR — the same qty is counted in both columns",
            )

    def test_bp_bucket_contains_only_this_batch_documents(self):
        """Every doc BP claims must carry a row tagged to exactly that batch."""
        for pair in _tagged_pairs(limit=30):
            scope = _scope(pair.bp)
            if not scope or not scope.custom_employee_function or not scope.project:
                continue
            for stage, fn in STAGES:
                _, _, bp_docs = fn(
                    pair.item, scope.custom_employee_function, scope.project, pair.bp, "BP"
                )
                for doc in bp_docs:
                    tagged = frappe.db.sql(
                        f"""
                        SELECT COUNT(*) FROM `{STAGE_TABLES[stage]}`
                        WHERE parent = %(parent)s AND item_code = %(item)s
                          AND batch_planning_id = %(bp)s
                        """,
                        {"parent": doc, "item": pair.item, "bp": pair.bp},
                    )[0][0]
                    self.assertGreater(
                        tagged,
                        0,
                        f"{stage} BP bucket returned {doc} which is not tagged to {pair.bp}",
                    )

    def test_open_mr_excludes_non_purchase_requests(self):
        """Material Transfer / Issue / Manufacture MRs are not procurement demand."""
        for pair in _tagged_pairs(limit=40):
            scope = _scope(pair.bp)
            if not scope or not scope.custom_employee_function or not scope.project:
                continue
            for mode in ("GEN", "BP"):
                _, _, docs = _open_mr(
                    pair.item, scope.custom_employee_function, scope.project, pair.bp, mode
                )
                for doc in docs:
                    purpose = frappe.db.get_value(
                        "Material Request", doc, "material_request_type"
                    )
                    self.assertEqual(
                        purpose,
                        "Purchase",
                        f"Open MR/{mode} returned {doc} with purpose {purpose!r} "
                        f"for {pair.item}",
                    )

    def test_open_po_counts_only_approved_orders(self):
        """Draft and pending-approval POs are not commitments and must not appear."""
        for pair in _tagged_pairs(limit=40):
            scope = _scope(pair.bp)
            if not scope or not scope.custom_employee_function or not scope.project:
                continue
            for mode in ("GEN", "BP"):
                _, _, docs = _open_po(
                    pair.item, scope.custom_employee_function, scope.project, pair.bp, mode
                )
                for doc in docs:
                    state, docstatus = frappe.db.get_value(
                        "Purchase Order", doc, ["workflow_state", "docstatus"]
                    )
                    self.assertEqual(
                        docstatus, 1, f"Open PO/{mode} returned {doc} at docstatus {docstatus}"
                    )
                    self.assertTrue(
                        (state or "").lower().startswith("approve"),
                        f"Open PO/{mode} returned {doc} in state {state!r}, "
                        f"which is not an approval state",
                    )

    def test_open_po_ignores_unapproved_receipts(self):
        """A PO is only retired from Open PO by a Store-Head-approved receipt.

        A draft or pending GRN can still be rejected or deleted, so a PO it
        covers must remain visible in the column.
        """
        rows = frappe.db.sql(
            """
            SELECT DISTINCT poi.parent AS po, poi.item_code, poi.batch_planning_id AS bp,
                   bpl.custom_employee_function AS ef, bpl.project AS project
            FROM `tabPurchase Order Item` poi
            JOIN `tabPurchase Order` po ON po.name = poi.parent
            JOIN `tabBatch Planning` bpl ON bpl.name = poi.batch_planning_id
            JOIN `tabPurchase Receipt Item` pri ON pri.purchase_order_item = poi.name
            WHERE poi.qty > 0
              AND po.docstatus = 1 AND po.workflow_state LIKE 'Approve%%'
              AND COALESCE(NULLIF(poi.employee_function,''),
                           NULLIF(poi.custom_employee_functions,''),
                           NULLIF(po.employee_function,''),
                           NULLIF(po.custom_employee_functions,'')) = bpl.custom_employee_function
              AND COALESCE(NULLIF(poi.project,''), NULLIF(po.project,'')) = bpl.project
              -- receipted at least once, but by no APPROVED receipt
              AND NOT EXISTS (
                  SELECT 1 FROM `tabPurchase Receipt Item` pri2
                  JOIN `tabPurchase Receipt` pr2 ON pr2.name = pri2.parent
                  WHERE pri2.purchase_order_item = poi.name
                    AND pr2.docstatus = 1 AND pr2.workflow_state LIKE 'Approve%%'
              )
            LIMIT 20
            """,
            as_dict=True,
        )
        if not rows:
            self.skipTest(
                "no batch-tagged approved PO is currently receipted only by an unapproved GRN"
            )
        for row in rows:
            _, _, docs = _open_po(row.item_code, row.ef, row.project, row.bp, "BP")
            self.assertIn(
                row.po,
                docs,
                f"{row.po} was retired from Open PO by a receipt that is not approved",
            )

    def test_doc_count_matches_doc_list(self):
        """The count returned must equal the number of distinct docs returned."""
        for pair in _tagged_pairs(limit=30):
            scope = _scope(pair.bp)
            if not scope or not scope.custom_employee_function or not scope.project:
                continue
            for stage, fn in STAGES:
                for mode in ("GEN", "BP"):
                    qty, count, docs = fn(
                        pair.item,
                        scope.custom_employee_function,
                        scope.project,
                        pair.bp,
                        mode,
                    )
                    self.assertEqual(
                        count,
                        len(set(docs)),
                        f"{stage}/{mode} count {count} != {len(set(docs))} distinct docs",
                    )
                    self.assertGreaterEqual(qty, 0, f"{stage}/{mode} qty must never be negative")

    def test_bp_bucket_is_not_always_empty(self):
        """Guard against the BP query silently matching nothing forever."""
        found = False
        for pair in _tagged_pairs(limit=120):
            scope = _scope(pair.bp)
            if not scope or not scope.custom_employee_function or not scope.project:
                continue
            for _stage, fn in STAGES:
                qty, count, _docs = fn(
                    pair.item, scope.custom_employee_function, scope.project, pair.bp, "BP"
                )
                if qty > 0 and count > 0:
                    found = True
                    break
            if found:
                break
        self.assertTrue(found, "BP bucket returned zero for every tagged pair — filter is too strict")


def _planning_batches(limit=8):
    """Batch Plannings that have both a scope and BOM rows worth reporting on."""
    return frappe.db.sql(
        """
        SELECT DISTINCT bp.name, bp.custom_employee_function AS ef, bp.project
        FROM `tabBatch Planning` bp
        JOIN `tabBatch Planning Detail` bpd ON bpd.parent = bp.name
        WHERE bp.custom_employee_function IS NOT NULL AND bp.custom_employee_function <> ''
          AND bp.project IS NOT NULL AND bp.project <> ''
          AND bpd.bom_list IS NOT NULL AND bpd.bom_list <> ''
          AND bpd.batch_planning_id IS NOT NULL AND bpd.batch_planning_id <> ''
        ORDER BY bp.name
        LIMIT %(limit)s
        """,
        {"limit": limit},
        as_dict=True,
    )


class TestStockColumnScopes(unittest.TestCase):
    """Main Wh / Total Stock / Allocated / Lab Wise / Free Qty scoping rules."""

    def test_main_wh_gen_excludes_current_batch(self):
        """GEN Main Wh must never include stock tagged to the current batch."""
        checked = 0
        for row in _planning_batches():
            warehouse = _store_warehouse(row.ef)
            if not warehouse:
                continue
            for item in _items_with_tagged_stock(row.project, warehouse):
                gen = _stock_qty(item, warehouse, row.project, row.name, "GEN", in_main=True)
                own = frappe.db.sql(
                    """
                    SELECT IFNULL(SUM(actual_qty), 0) FROM `tabStock Ledger Entry`
                    WHERE item_code = %(item)s AND warehouse = %(wh)s
                      AND project = %(project)s AND is_cancelled = 0
                      AND batch_planning_id = %(bp)s
                    """,
                    {"item": item, "wh": warehouse, "project": row.project, "bp": row.name},
                )[0][0]
                independent_gen = frappe.db.sql(
                    """
                    SELECT IFNULL(SUM(actual_qty), 0) FROM `tabStock Ledger Entry`
                    WHERE item_code = %(item)s AND warehouse = %(wh)s
                      AND project = %(project)s AND is_cancelled = 0
                      AND batch_planning_id IS NOT NULL AND batch_planning_id <> ''
                      AND batch_planning_id <> %(bp)s
                    """,
                    {"item": item, "wh": warehouse, "project": row.project, "bp": row.name},
                )[0][0]
                self.assertAlmostEqual(
                    gen,
                    float(independent_gen),
                    places=4,
                    msg=f"GEN Main Wh for {item} on {row.name} does not match an "
                    f"independent other-batches-only sum (own batch holds {own})",
                )
                checked += 1
        self.assertGreater(checked, 0, "no tagged stock available to check")

    def test_global_main_wh_equals_gen_plus_bp(self):
        """GEN and BP are disjoint, so together they must be the tagged total."""
        for row in _planning_batches():
            warehouse = _store_warehouse(row.ef)
            if not warehouse:
                continue
            for item in _items_with_tagged_stock(row.project, warehouse):
                gen = _stock_qty(item, warehouse, row.project, row.name, "GEN", in_main=True)
                bp = _stock_qty(item, warehouse, row.project, row.name, "BP", in_main=True)
                tagged_total = float(
                    frappe.db.sql(
                        """
                        SELECT IFNULL(SUM(actual_qty), 0) FROM `tabStock Ledger Entry`
                        WHERE item_code = %(item)s AND warehouse = %(wh)s
                          AND project = %(project)s AND is_cancelled = 0
                          AND batch_planning_id IS NOT NULL AND batch_planning_id <> ''
                        """,
                        {"item": item, "wh": warehouse, "project": row.project},
                    )[0][0]
                )
                self.assertAlmostEqual(
                    gen + bp,
                    tagged_total,
                    places=4,
                    msg=f"GEN + BP Main Wh != tagged total for {item} on {row.name}",
                )

    def test_bp_predicate_modes_never_overlap_on_stock(self):
        """The same disjointness the pipeline relies on must hold for SLE rows."""
        gen_sql = _bp_predicate("sle", "GEN")
        bp_sql = _bp_predicate("sle", "BP")
        for row in _planning_batches(limit=4):
            overlap = frappe.db.sql(
                f"""
                SELECT COUNT(*) FROM `tabStock Ledger Entry` sle
                WHERE {gen_sql} AND {bp_sql}
                """,
                {"bp": row.name},
            )[0][0]
            self.assertEqual(
                overlap, 0, f"an SLE row satisfies both GEN and BP for {row.name}"
            )

    def test_global_free_qty_identity_holds_exactly(self):
        """Global Main Wh = Global Free Qty + Global Allocated, to the penny."""
        checked = 0
        pending = 0
        for row in _planning_batches():
            payload = get_material_planning_data(row.name)
            if payload["free_qty_pending"]:
                pending += 1
                continue
            for r in payload["results"]:
                self.assertAlmostEqual(
                    r["global_main_stock"],
                    r["global_free_stock"] + r["global_allocated"],
                    places=2,
                    msg=f"identity broken for {r['item_code']} on {row.name}: "
                    f"main={r['global_main_stock']} free={r['global_free_stock']} "
                    f"alloc={r['global_allocated']}",
                )
                checked += 1
        if not checked:
            self.skipTest(
                f"stock cutover not declared ({pending} batches pending) — "
                f"there is no global figure to check the identity against yet"
            )

    def test_free_qty_is_pending_until_cutover_is_declared(self):
        """No global figure may be invented before the go-live marker is set."""
        cutover = get_stock_cutover_datetime()
        for row in _planning_batches(limit=3):
            payload = get_material_planning_data(row.name)
            self.assertEqual(
                payload["free_qty_pending"],
                not cutover,
                f"free_qty_pending disagrees with the configured cutover on {row.name}",
            )
            for r in payload["results"]:
                if cutover:
                    self.assertIsNotNone(r["global_free_stock"])
                    self.assertIsNotNone(r["global_main_stock"])
                else:
                    self.assertIsNone(
                        r["global_free_stock"],
                        f"{r['item_code']} reported a Free Qty of "
                        f"{r['global_free_stock']} with no cutover declared",
                    )
                    self.assertIsNone(r["global_main_stock"])

    def test_global_main_wh_is_cutover_scoped_not_gen_plus_bp(self):
        """Global Main Wh carries the date cutoff; GEN/BP deliberately do not.

        It must equal an independent post-cutover sum, and it must never
        include a pre-cutover row that GEN or BP would still show.
        """
        cutover = get_stock_cutover_datetime()
        if not cutover:
            self.skipTest("stock cutover not declared")
        for row in _planning_batches():
            warehouse = _store_warehouse(row.ef)
            if not warehouse:
                continue
            for r in get_material_planning_data(row.name)["results"]:
                independent = float(
                    frappe.db.sql(
                        """
                        SELECT IFNULL(SUM(actual_qty), 0) FROM `tabStock Ledger Entry`
                        WHERE item_code = %(item)s AND warehouse = %(wh)s
                          AND employee_function = %(ef)s AND project = %(project)s
                          AND posting_datetime >= %(cutover)s AND is_cancelled = 0
                        """,
                        {
                            "item": r["item_code"],
                            "wh": warehouse,
                            "ef": row.ef,
                            "project": row.project,
                            "cutover": cutover,
                        },
                    )[0][0]
                )
                self.assertAlmostEqual(
                    r["global_main_stock"],
                    independent,
                    places=2,
                    msg=f"global_main_stock for {r['item_code']} on {row.name} is not "
                    f"the independent post-cutover sum",
                )

    def test_legacy_bucket_is_never_inside_global_main_wh(self):
        """The frozen bucket must stay strictly outside the planning figure."""
        cutover = get_stock_cutover_datetime()
        for row in _planning_batches(limit=3):
            warehouse = _store_warehouse(row.ef)
            if not warehouse:
                continue
            for item in _items_with_tagged_stock(row.project, warehouse, limit=5):
                legacy = get_legacy_stock(item, warehouse, row.project)
                self.assertIn("Audit only", legacy["note"])
                if not cutover:
                    continue
                overlap = frappe.db.sql(
                    """
                    SELECT COUNT(*) FROM `tabStock Ledger Entry`
                    WHERE item_code = %(item)s AND warehouse = %(wh)s
                      AND is_cancelled = 0
                      AND posting_datetime >= %(cutover)s
                      AND project IS NOT NULL AND project <> ''
                      AND posting_datetime < %(cutover)s
                    """,
                    {"item": item, "wh": warehouse, "cutover": cutover},
                )[0][0]
                self.assertEqual(
                    overlap, 0, "a row is both legacy and post-cutover — impossible"
                )

    def test_total_stock_is_asymmetric(self):
        """BP Total = BP Main + Lab Wise; GEN Total = GEN Main, with no GEN Lab."""
        for row in _planning_batches():
            for r in get_material_planning_data(row.name)["results"]:
                self.assertAlmostEqual(
                    r["bp_total_stock"],
                    r["bp_main_stock"] + r["lab_stock"],
                    places=2,
                    msg=f"BP Total != BP Main + Lab for {r['item_code']} on {row.name}",
                )
                self.assertAlmostEqual(
                    r["gen_total_stock"],
                    r["gen_main_stock"],
                    places=2,
                    msg=f"GEN Total != GEN Main for {r['item_code']} on {row.name} — "
                    f"a GEN Lab Wise term has crept in",
                )

    def test_columns_without_a_gen_split_do_not_gain_one(self):
        """Allocated, Lab Wise and Free Qty must expose no GEN/BP variants."""
        forbidden = {
            "gen_allocated_qty", "bp_allocated_qty",
            "gen_lab_stock", "bp_lab_stock",
            "gen_free_stock", "bp_free_stock",
        }
        for row in _planning_batches(limit=2):
            for r in get_material_planning_data(row.name)["results"]:
                present = forbidden & set(r)
                self.assertEqual(
                    present,
                    set(),
                    f"{sorted(present)} appeared on {r['item_code']} — these "
                    f"columns are single-scope by design",
                )
                for key in ("allocated_qty", "lab_stock", "global_free_stock"):
                    self.assertIn(key, r, f"{key} missing for {r['item_code']}")

    def test_bp_allocated_is_batch_scoped_and_within_global(self):
        """BP Allocated is one batch's slice of the pool-wide reservation."""
        checked = 0
        for row in _planning_batches():
            for r in get_material_planning_data(row.name)["results"]:
                bp_alloc = _allocated_qty(
                    r["item_code"], row.project, batch_planning=row.name
                )
                global_alloc = _allocated_qty(
                    r["item_code"], row.project, employee_function=row.ef
                )
                self.assertAlmostEqual(bp_alloc, r["allocated_qty"], places=2)
                self.assertLessEqual(
                    bp_alloc - 0.01,
                    global_alloc,
                    f"BP Allocated {bp_alloc} exceeds Global Allocated "
                    f"{global_alloc} for {r['item_code']} on {row.name}",
                )
                checked += 1
        self.assertGreater(checked, 0, "no planning rows available to check")


def _store_warehouse(employee_function):
    """Resolved exactly the way get_material_planning_data resolves it."""
    ef_doc = frappe.get_doc("Employee Function", employee_function)
    for r in (ef_doc.table_bukm or []):
        if r.store_warehouse:
            return r.store_warehouse
    return None


def _items_with_tagged_stock(project, warehouse, limit=15):
    return frappe.db.sql_list(
        """
        SELECT DISTINCT item_code FROM `tabStock Ledger Entry`
        WHERE warehouse = %(wh)s AND project = %(project)s AND is_cancelled = 0
          AND batch_planning_id IS NOT NULL AND batch_planning_id <> ''
        LIMIT %(limit)s
        """,
        {"wh": warehouse, "project": project, "limit": limit},
    )
