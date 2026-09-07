import time
import frappe
from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
    get_untagged_material_data, get_material_planning_data,
    get_consolidated_bom_components, _ef_lab_warehouses,
    _stock_qty, _open_mr, _open_po, _open_pr_grn,
)


def run(bp="BP-26-10-001"):
    ef = frappe.db.get_value("Batch Planning", bp, "custom_employee_function")
    ef_doc = frappe.get_doc("Employee Function", ef)
    wh = next(r.store_warehouse for r in ef_doc.table_bukm if r.store_warehouse)
    labs = _ef_lab_warehouses(ef_doc)

    t = time.time(); items = get_consolidated_bom_components(bp, scale="stock")
    print("consolidated BOM (%d items): %.2fs" % (len(items), time.time() - t))

    sample = [i["item_code"] for i in items[:5]]
    totals = {"main": 0.0, "lab": 0.0, "mr": 0.0, "po": 0.0, "grn": 0.0}
    for code in sample:
        t = time.time(); _stock_qty(code, wh, None, None, "UNTAGGED", in_main=True); totals["main"] += time.time() - t
        t = time.time(); _stock_qty(code, wh, None, None, "UNTAGGED", warehouses=labs); totals["lab"] += time.time() - t
        t = time.time(); _open_mr(code, ef, None, None, "UNTAGGED"); totals["mr"] += time.time() - t
        t = time.time(); _open_po(code, ef, None, None, "UNTAGGED"); totals["po"] += time.time() - t
        t = time.time(); _open_pr_grn(code, ef, None, None, "UNTAGGED"); totals["grn"] += time.time() - t

    n = len(sample)
    print("\nper-item average over %d items:" % n)
    for k, v in sorted(totals.items(), key=lambda x: -x[1]):
        print("   %-6s %7.3fs   -> x%d items = %6.1fs" % (k, v / n, len(items), v / n * len(items)))

    t = time.time(); out = get_untagged_material_data(bp)
    untagged_secs = time.time() - t
    print("\nget_untagged_material_data TOTAL: %.1fs  (%d rows)" % (untagged_secs, len(out["results"])))

    t = time.time(); mp = get_material_planning_data(bp)
    print("get_material_planning_data TOTAL: %.1fs  (%d rows)  <- has project filter"
          % (time.time() - t, len(mp["results"])))
