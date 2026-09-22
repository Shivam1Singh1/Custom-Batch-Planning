import frappe
from frappe.utils import flt


def _targets():
    return frappe.db.sql(
        """
        SELECT ma.name AS ma, ma.batch_planning, ma.employee_function,
               ma.project_id, mai.name AS row_name, mai.item_code,
               mai.allocate_qty
        FROM `tabMaterial Allocation Item` mai
        INNER JOIN `tabMaterial Allocation` ma ON ma.name = mai.parent
        WHERE mai.allocate_qty > 0
          AND IFNULL(mai.local_allocated_qty, 0) = 0
          AND IFNULL(mai.global_allocated_qty, 0) = 0
          AND ma.allocation_status NOT IN ('Deallocated', 'Stock Entry Done')
          AND ma.docstatus <> 2
        ORDER BY ma.creation, mai.idx
        """,
        as_dict=True,
    )


def run(apply=False):
    from custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning import (
        free_stock_figures,
        split_local_first,
    )
    from custom_batch_planning.custom_batch_planning.doctype.batch_planning_settings.batch_planning_settings import (
        get_stock_cutover_datetime,
    )

    cutover = get_stock_cutover_datetime()
    rows = _targets()
    if not rows:
        print("Nothing to backfill.")
        return

    grouped = {}
    for r in rows:
        grouped.setdefault((r.ma, r.item_code), []).append(r)

    planned, skipped = [], []

    for (ma_name, item_code), item_rows in grouped.items():
        head = item_rows[0]

        if not head.employee_function or not head.project_id:
            skipped.append((ma_name, item_code, "missing employee_function or project_id"))
            continue

        ef_doc = frappe.get_doc("Employee Function", head.employee_function)
        warehouse = next(
            (r.store_warehouse for r in (ef_doc.table_bukm or []) if r.store_warehouse),
            None,
        )
        if not warehouse:
            skipped.append((ma_name, item_code, "employee function has no store warehouse"))
            continue

        figures = free_stock_figures(
            item_code,
            warehouse,
            head.employee_function,
            head.project_id,
            head.batch_planning,
            cutover,
            exclude_parent=ma_name,
        )
        split = split_local_first(
            [flt(r.allocate_qty) for r in item_rows],
            figures["bp_free_stock"],
            figures["other_free_stock"],
        )

        if split["shortfall"] > 0:
            skipped.append(
                (ma_name, item_code,
                 f"shortfall {split['shortfall']} against today's pools")
            )
            continue

        for row, row_split in zip(item_rows, split["rows"]):
            planned.append({
                "row_name": row.row_name,
                "ma": ma_name,
                "item_code": item_code,
                "allocate_qty": flt(row.allocate_qty),
                "local": row_split["from_local"],
                "global": row_split["from_global"],
            })

    print(f"\n{'APPLY' if apply else 'DRY RUN'} — {len(planned)} row(s) to update, "
          f"{len(skipped)} skipped\n")
    for p in planned:
        print(f"  {p['ma']:<28} {p['item_code']:<14} "
              f"qty={p['allocate_qty']:<10} -> local={p['local']} global={p['global']}")
    for ma_name, item_code, why in skipped:
        print(f"  SKIP {ma_name:<28} {item_code:<14} {why}")

    if not apply:
        print("\nNothing written. Re-run with --kwargs \"{'apply': True}\" to commit.")
        return

    for p in planned:
        frappe.db.set_value(
            "Material Allocation Item",
            p["row_name"],
            {
                "local_allocated_qty": p["local"],
                "global_allocated_qty": p["global"],
            },
            update_modified=False,
        )
    frappe.db.commit()
    print(f"\nWritten: {len(planned)} row(s).")
