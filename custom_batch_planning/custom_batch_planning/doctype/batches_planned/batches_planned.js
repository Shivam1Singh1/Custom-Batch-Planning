frappe.ui.form.on("Batches Planned", {
	refresh: function (frm) {
		frm.page.clear_custom_actions();
		render_bom_items(frm);
	},
});

function render_bom_items(frm) {
	let $field = frm.fields_dict["bom_items"];
	if (!$field) return;

	if (!frm.doc.batch_planning) {
		$field.$wrapper.html(empty_state("📦", "No Batch Planning linked to this record."));
		return;
	}

	frappe.call({
		method: "frappe.client.get",
		args: { doctype: "Batch Planning", name: frm.doc.batch_planning },
		callback: function (r) {
			if (!r.message) {
				$field.$wrapper.html(empty_state("❌", "Could not load Batch Planning."));
				return;
			}

			let rows = r.message.custom_batch_details || [];
			let matched = find_matched_row(rows, frm.doc.batch_planning_id, frm.doc.amended_from);

			if (!matched || !matched.bom_list) {
				$field.$wrapper.html(empty_state("📋", "No BOM linked to this batch."));
				return;
			}

			let batch_key = `${frm.doc.batch_planning}-${matched.idx}`;
			// Server side so the newest store row wins; the table can carry more than
			// one row per key from the days the dialog inserted client side.
			frappe.call({
				method: "custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning.get_batch_bom_store",
				args: { batch_key: batch_key },
				callback: function (store) {
					if (store.message && (store.message.bom_components || []).length) {
						render_bom_table_html(
							frm,
							store.message.bom_components,
							matched.bom_list,
							frm.doc.finished_item,
							true,
						);
					} else {
						frappe.call({
							method: "frappe.client.get",
							args: { doctype: "BOM", name: matched.bom_list },
							callback: function (b) {
								if (!b.message) {
									$field.$wrapper.html(
										empty_state("❌", "BOM not found: " + matched.bom_list),
									);
									return;
								}
								let items = b.message.exploded_items || b.message.items || [];
								render_bom_table_html(
									frm,
									items,
									matched.bom_list,
									frm.doc.finished_item,
									false,
								);
							},
						});
					}
				},
			});
		},
	});
}

function render_bom_table_html(frm, items, bom_name, finished_item, is_edited) {
	let $field = frm.fields_dict["bom_items"];
	if (!$field) return;

	if (!items.length) {
		$field.$wrapper.html(empty_state("📦", "No items found in this BOM."));
		return;
	}

	let rows_html = items
		.map(function (item, i) {
			let bg = i % 2 === 0 ? "#f0faf4" : "#ffffff";
			return `
        <tr style="background:${bg};">
            <td style="padding:10px 12px; text-align:center; color:#6b7280; font-weight:600; font-size:13px;">${i + 1}</td>
            <td style="padding:10px 12px;">
                <span style="background:#16a34a; color:#fff; padding:3px 10px; border-radius:5px;
                    font-size:11px; font-weight:700; letter-spacing:0.3px; white-space:nowrap;">
                    ${item.item_code || ""}
                </span>
            </td>
            <td style="padding:10px 12px; color:#1f2937; font-size:13px;">${item.item_name || ""}</td>
            <td style="padding:10px 12px; text-align:center; color:#4b5563; font-size:12px;
                font-family:monospace;">${item.stock_uom || item.uom || ""}</td>
            <td style="padding:10px 12px; text-align:center; font-weight:700; color:#15803d;
                font-size:13px;">${parseFloat(item.qty_consumed_per_unit || item.qty || 0)}</td>
        </tr>`;
		})
		.join("");

	let edited_badge = is_edited
		? `<span style="background:#fef3c7; color:#92400e; padding:3px 10px; border-radius:20px; font-size:11px; font-weight:700;">✏️ Edited BOM</span>`
		: "";

	let html = `
    <div style="width:100%; box-sizing:border-box; font-family:inherit;">
        <div style="background:linear-gradient(135deg, #16a34a 0%, #14532d 100%);
            color:#fff; padding:16px 20px; border-radius:10px 10px 0 0;
            display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:10px;">
            <div>
                <div style="font-size:15px; font-weight:700; margin-bottom:3px;">🧪 BOM Components ${edited_badge}</div>
                <div style="font-size:12px; opacity:0.85;">
                    BOM: <b>${bom_name}</b> &nbsp;|&nbsp; Finished Item: <b>${finished_item || "N/A"}</b>
                </div>
            </div>
            <div style="background:rgba(255,255,255,0.15); padding:8px 18px;
                border-radius:20px; font-weight:700; font-size:13px; white-space:nowrap;">
                📋 ${items.length} Items
            </div>
        </div>
        <div style="overflow-x:auto; border:1px solid #d1fae5; border-top:none; border-radius:0 0 10px 10px;">
            <table style="width:100%; border-collapse:collapse; font-family:inherit; font-size:13px;">
                <thead>
                    <tr style="background:#dcfce7;">
                        <th style="padding:11px 12px; text-align:center; color:#166534; font-weight:700; font-size:11px; text-transform:uppercase; letter-spacing:0.8px; border-bottom:2px solid #bbf7d0;">#</th>
                        <th style="padding:11px 12px; text-align:left; color:#166534; font-weight:700; font-size:11px; text-transform:uppercase; letter-spacing:0.8px; border-bottom:2px solid #bbf7d0;">Item Code</th>
                        <th style="padding:11px 12px; text-align:left; color:#166534; font-weight:700; font-size:11px; text-transform:uppercase; letter-spacing:0.8px; border-bottom:2px solid #bbf7d0;">Item Name</th>
                        <th style="padding:11px 12px; text-align:center; color:#166534; font-weight:700; font-size:11px; text-transform:uppercase; letter-spacing:0.8px; border-bottom:2px solid #bbf7d0;">UOM</th>
                        <th style="padding:11px 12px; text-align:center; color:#166534; font-weight:700; font-size:11px; text-transform:uppercase; letter-spacing:0.8px; border-bottom:2px solid #bbf7d0;">Qty</th>
                    </tr>
                </thead>
                <tbody>${rows_html}</tbody>
            </table>
        </div>
        <div style="padding:8px 16px; background:#f0fdf4; border:1px solid #d1fae5; border-top:none; border-radius:0 0 10px 10px; font-size:12px; color:#166534;">
            ✅ <b>${items.length}</b> component(s) in this BOM
        </div>
    </div>`;
	$field.$wrapper.html(html);
}

function find_matched_row(rows, batch_planning_id, amended_from) {

	let matched = rows.find((r) => r.batch_planning_id === batch_planning_id);
	if (matched) return matched;

	if (amended_from) {
		matched = rows.find((r) => r.batch_planning_id === amended_from);
		if (matched) return matched;
	}

	let base_id = batch_planning_id.replace(/-\d+$/, "");
	matched = rows.find((r) => r.batch_planning_id === base_id);
	if (matched) return matched;

	return null;
}

function empty_state(icon, msg) {
	return `<div style="padding:48px; text-align:center; color:#6b7280; border:2px dashed #d1fae5; border-radius:12px; background:#f0fdf4;"><div style="font-size:36px;">${icon}</div><div style="font-size:13px;">${msg}</div></div>`;
}
