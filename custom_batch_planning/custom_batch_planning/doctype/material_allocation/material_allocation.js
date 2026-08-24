frappe.ui.form.on("Material Allocation", {
    setup: function (frm) {
        frm.set_query("batch_planning", function () {
            return { filters: { workflow_state: "Approved" } };
        });
    },

    onload: function (frm) {
        if (frm.is_new() && !frm.doc.workflow_state) {
            frm.doc.workflow_state = "Draft";
        }
    },

    refresh: function (frm) {
        console.log("🔄 Material Allocation refreshed for:", frm.doc.name);

        frm.clear_custom_buttons();

        let is_empty = !frm.doc.material_allocation || frm.doc.material_allocation.length === 0;
        if (
            (frm.is_new() || frm.doc.workflow_state === "Draft") &&
            frm.doc.batch_planning &&
            is_empty
        ) {
            setTimeout(() => {
                let current_empty =
                    !frm.doc.material_allocation || frm.doc.material_allocation.length === 0;
                if (current_empty) {
                    window.upload_bom_items(frm);
                }
            }, 500);
        }

        if (!frm.is_new() && frm.doc.workflow_state !== "Draft") {
            frm.set_df_property("employee_function", "read_only", 1);
            frm.set_df_property("batch_planning", "read_only", 1);
        }

        frm.set_df_property("material_allocation", "cannot_add_rows", false);
        frm.set_df_property("material_allocation", "cannot_delete_rows", false);
        if (frm.fields_dict["material_allocation"] && frm.fields_dict["material_allocation"].grid) {
            frm.fields_dict["material_allocation"].grid.cannot_delete_rows = false;
            frm.fields_dict["material_allocation"].grid.df.cannot_delete_rows = false;
        }

        if (frm.doc.allocation_status) {

            frm.set_df_property("material_allocation", "read_only", 1);
            frm.refresh_field("material_allocation");
        } else {
            let read_only_cols = [
                "item_code", "item_name", "uom", "quantity_required",
                "stock_available", "open_pr", "open_po", "grn_qty",
                "qty_allocated", "shortage", "batch_details"
            ];
            read_only_cols.forEach(function (fieldname) {
                try {
                    frm.fields_dict["material_allocation"].grid.update_docfield_property(
                        fieldname, "read_only", 1
                    );
                } catch (e) {

                }
            });
            try {
                frm.fields_dict["material_allocation"].grid.update_docfield_property(
                    "allocate_qty", "read_only", 0
                );
                frm.fields_dict["material_allocation"].grid.update_docfield_property(
                    "reason", "read_only", 0
                );
            } catch (e) {}

            frm.refresh_field("material_allocation");
        }

        if (
            !frm.is_new() &&
            frm.doc.employee_function &&
            frm.doc.material_allocation &&
            frm.doc.material_allocation.length
        ) {
            if (!frm.doc.allocation_status) {
                setTimeout(function () {
                    window.refresh_stock_available(frm);
                }, 1000);
            }
        }

        if (frm.doc.batch_planning) {
            setTimeout(function () {
                frm.add_custom_button(__("View Allocations"), function () {
                    frappe.call({
                        method: "custom_batch_planning.custom_batch_planning.doctype.material_allocation.material_allocation.get_allocated_items",
                        args: {
                            batch_planning: frm.doc.batch_planning,
                            employee_function: frm.doc.employee_function
                        },
                        callback: function(r) {
                            let data = r.message || {};
                            let items = data.items || [];
                            items = items.filter(d => d.qty_allocated > 0);
                            let ma_count = data.ma_count || 0;

                            if (!items.length) {
                                frappe.msgprint({
                                    title: "No Allocations",
                                    message: __("No allocated items for this batch planning."),
                                    indicator: "orange"
                                });
                                return;
                            }

                            let rows = items.map(d => {
                                let row_style = "";
                                if (d.qty_allocated > d.quantity_required) {
                                    row_style = "background-color: #ffebee; color: #c62828;";
                                }
                                return `
                                <tr style="${row_style}">
                                    <td style="padding:8px 12px; font-weight:bold;">${d.item_code}</td>
                                    <td style="padding:8px 12px;">${d.item_name}</td>
                                    <td style="padding:8px 12px;">${d.uom}</td>
                                    <td style="padding:8px 12px;">${d.quantity_required}</td>
                                    <td style="padding:8px 12px; font-weight:bold;">${d.qty_allocated}</td>
                                </tr>
                                `;
                            }).join("");

                            let d_dialog = new frappe.ui.Dialog({
                                title: "Allocated Items",
                                size: "large",
                            });
                            d_dialog.body.innerHTML = `
                                <p style="margin-bottom: 15px; font-size: 14px;">
                                    <b>${ma_count} Material Allocation(s) have been done against this Batch Planning.</b>
                                </p>
                                <table class="table table-bordered" style="width:100%;font-size:13px;">
                                    <thead style="background:#f1f5f9; color:#333;">
                                        <tr>
                                            <th style="padding:8px 12px;">Item Code</th>
                                            <th style="padding:8px 12px;">Item Name</th>
                                            <th style="padding:8px 12px;">UOM</th>
                                            <th style="padding:8px 12px;">Qty Required</th>
                                            <th style="padding:8px 12px;">Qty Allocated</th>
                                        </tr>
                                    </thead>
                                    <tbody>${rows}</tbody>
                                </table>
                            `;
                            d_dialog.show();
                        }
                    });
                });
            }, 100);
        }

        setTimeout(function () {
            if (!frm.is_new() && frm.doc.workflow_state === "Approved" && frm.doc.docstatus !== 2) {
                if (!frm.doc.allocation_status) {
                    frm.add_custom_button(
                        __("Allocate"),
                        function () { window.auto_allocate_all(frm); }
                    ).addClass("btn-primary");

                } else if (frm.doc.allocation_status === "Allocated") {

                    frappe.call({
                        method: "frappe.client.get_list",
                        args: {
                            doctype: "Stock Entry",
                            filters: {
                                name: frm.doc.stock_entry || "__none__",
                                docstatus: ["!=", 2],
                            },
                            fields: ["name", "docstatus"],
                            limit: 1,
                        },
                        callback: function (r) {
                            if (r.message && r.message.length > 0) {
                                let se = r.message[0];
                                frm.add_custom_button(
                                    __("📦 Open Stock Entry"),
                                    function () {
                                        frappe.set_route("Form", "Stock Entry", se.name);
                                    }
                                ).addClass("btn-success");

                                if (se.docstatus === 1) {
                                    frm.dashboard.add_comment(
                                        __("✅ Stock Entry <b>" + se.name + "</b> has been submitted. Deallocation is blocked."),
                                        "green", true
                                    );
                                } else {
                                    frm.dashboard.add_comment(
                                        __("⚠️ Stock Entry <b>" + se.name + "</b> is in Draft. Submit it to complete the process."),
                                        "orange", true
                                    );
                                }
                            } else {
                                frm.add_custom_button(
                                    __("📦 Create Stock Entry"),
                                    function () { window.create_stock_entry(frm); }
                                ).addClass("btn-primary");

                                frm.add_custom_button(
                                    __("Deallocate"),
                                    function () { window.deallocate_all(frm); }
                                ).addClass("btn-danger");
                            }
                        },
                    });
                }
            } else if (frm.doc.allocation_status === "Deallocated") {
                frm.dashboard.add_comment(
                    __("⚠️ This document has been Deallocated. Create a new Material Allocation for the same Planned Batch to allocate again."),
                    "orange", true
                );
            }
        }, 100);

        window.load_expiry_status(frm);

        setTimeout(function () {
            (frm.doc.material_allocation || []).forEach(function (row) {
                if (row.reason && row.allocate_qty != row.quantity_required) {
                    let grid_row =
                        frm.fields_dict["material_allocation"].grid.grid_rows_by_docname[row.name];
                    if (grid_row && grid_row.row) {
                        grid_row.row.css("background-color", "#f3e5f5");
                    }
                }
            });
        }, 1000);
    },

    after_save: function (frm) {
        if (frm._allocating) return;
        setTimeout(function () {
            if (frm.doc.allocation_status) return;
            if (
                frm.doc.employee_function &&
                frm.doc.material_allocation &&
                frm.doc.material_allocation.length
            ) {
                window.refresh_stock_available(frm);
            }
            window.load_expiry_status(frm);
        }, 1500);
    },

    employee_function: function (frm) {
        if (
            frm.doc.employee_function &&
            frm.doc.material_allocation &&
            frm.doc.material_allocation.length
        ) {
            window.refresh_stock_available(frm);
        }
    },

});

window.apply_local_first_split = function (row) {
    let requested = Math.max(parseFloat(row.allocate_qty) || 0, 0);
    let local_free = Math.max(parseFloat(row.local_free_qty) || 0, 0);

    row.local_allocated_qty = Math.min(requested, local_free);
    row.global_allocated_qty = requested - row.local_allocated_qty;
};

frappe.ui.form.on("Material Allocation Item", {
    allocate_qty: function (frm, cdt, cdn) {
        let row = locals[cdt][cdn];
        window.apply_local_first_split(row);
        let grid = (frm.fields_dict["material_allocation"] || {}).grid;
        if (grid) grid.refresh_row(cdn);
        if (row.allocate_qty != row.quantity_required && !row.reason) {
            frappe.show_alert({
                message: "Row " + row.idx + ": Reason is required.",
                indicator: "orange",
            });
        }
    },

    before_material_allocation_remove: function (frm, cdt, cdn) {

        if ((frm.doc.material_allocation || []).length <= 1) {
            frappe.msgprint({
                title: __("Cannot Delete"),
                message: __("At least one item must remain."),
                indicator: "red",
            });
            frappe.validated = false;
        }
    },
});

window._show_item_history = function (item_code) {
    let filtered = (window._ma_history_data || [])
        .filter((d) => d.item_code === item_code)
        .sort((a, b) => (a.allocated_on > b.allocated_on ? 1 : -1));

    if (!filtered.length) {
        frappe.msgprint({
            title: "No Events",
            message: `No events found for ${item_code}`,
            indicator: "orange",
        });
        return;
    }

    let rows = filtered.map((d) => `
        <tr>
            <td style="padding:8px 12px;">${d.allocated_by}</td>
            <td style="padding:8px 12px;">${d.allocated_on}</td>
            <td style="padding:8px 12px;">${d.qty_allocated ?? "-"}</td>
            <td style="padding:8px 12px;">${d.material_allocation_id}</td>
        </tr>
    `).join("");

    let ed = new frappe.ui.Dialog({
        title: `📦 Events for: ${item_code}`,
        size: "extra-large",
    });
    ed.body.innerHTML = `
        <table class="table table-bordered" style="width:100%;font-size:13px;">
            <thead style="background:#f1f5f9;">
                <tr>
                    <th style="padding:8px 12px;">Allocated By</th>
                    <th style="padding:8px 12px;">Date & Time</th>
                    <th style="padding:8px 12px;">Qty Allocated</th>
                    <th style="padding:8px 12px;">MA ID</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>
    `;
    ed.show();
};

window.refresh_stock_available = function (frm) {
    let items = frm.doc.material_allocation || [];
    if (!items.length) return;

    let item_codes = items.map((r) => r.item_code);
    fetch(
        "/api/method/custom_batch_planning.custom_batch_planning.doctype.material_allocation.material_allocation.get_open_pr_po",
        {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Frappe-CSRF-Token": frappe.csrf_token,
            },
            body: JSON.stringify({ item_codes: item_codes }),
        },
    )
        .then((r) => r.json())
        .then((data) => {
            let pr_po_map = data.message || {};
            items.forEach(function (row) {
                frappe.call({
                    method: "custom_batch_planning.custom_batch_planning.doctype.material_allocation.material_allocation.ma_get_allocated_qty",
                    args: {
                        item_code: row.item_code,
                        employee_function: frm.doc.employee_function,
                        batch_planning: frm.doc.batch_planning,
                        project: frm.doc.project_id,
                        exclude_parent: frm.doc.name,
                        row_name: row.name,
                    },
                    callback: function (res) {
                        if (res.message) {
                            let grid_row =
                                frm.fields_dict["material_allocation"].grid.grid_rows_by_docname[row.name];
                            let local_free = res.message.local_free || 0;
                            let global_free = res.message.global_free || 0;
                            let available = res.message.free_stock || 0;
                            let qty_req = row.quantity_required || 0;
                            let pr_po = pr_po_map[row.item_code] || {};

                            if (grid_row) {
                                grid_row.doc.local_free_qty = local_free;
                                grid_row.doc.global_free_qty = global_free;
                                grid_row.doc.stock_available = available;
                                grid_row.doc.shortage = Math.max(qty_req - available, 0);
                                grid_row.doc.open_pr = pr_po.open_pr || 0;
                                grid_row.doc.open_po = pr_po.open_po || 0;

                                window.apply_local_first_split(grid_row.doc);

                                grid_row.refresh_field("local_free_qty");
                                grid_row.refresh_field("global_free_qty");
                                grid_row.refresh_field("local_allocated_qty");
                                grid_row.refresh_field("global_allocated_qty");
                                grid_row.refresh_field("stock_available");
                                grid_row.refresh_field("shortage");
                                grid_row.refresh_field("open_pr");
                                grid_row.refresh_field("open_po");
                            }
                        }
                    },
                });
            });
        });
};

window.auto_allocate_all = function (frm) {
    if (frm.is_dirty()) {
        frappe.msgprint(__("Save the document first."));
        return;
    }

    let shared = (frm.doc.material_allocation || []).filter(
        (r) => parseFloat(r.global_allocated_qty || 0) > 0
    );

    let run_fefo = function () {
        frappe.confirm(
            "Allocate batches by <b>FEFO</b> (earliest expiry first)?",
            function () {
                frm.call({
                    doc: frm.doc,
                    method: "auto_allocate",
                    freeze: true,
                    freeze_message: __("Allocating Batches..."),
                }).then((r) => {
                    if (!r.exc) {
                        frappe.show_alert({ message: __("✅ Allocated successfully!"), indicator: "green" });
                        frm.reload_doc();
                    }
                });
            }
        );
    };

    if (!shared.length) {
        run_fefo();
        return;
    }

    let shared_total = shared.reduce(
        (a, r) => a + parseFloat(r.global_allocated_qty || 0), 0
    );

    let rows = shared.map((r) => `
        <tr>
            <td style="padding:5px 8px; font-weight:600;">${r.item_code}</td>
            <td style="padding:5px 8px; text-align:right;">${r.allocate_qty || 0}</td>
            <td style="padding:5px 8px; text-align:right;">${r.local_free_qty || 0}</td>
            <td style="padding:5px 8px; text-align:right;">${r.global_free_qty || 0}</td>
            <td style="padding:5px 8px; text-align:right; font-weight:700; color:#b45309;">${r.global_allocated_qty || 0}</td>
        </tr>`).join("");

    let d = new frappe.ui.Dialog({
        title: __("⚠️ Shared Free Stock"),
        size: "small",
        primary_action_label: __("Continue"),
        primary_action: function () { d.hide(); run_fefo(); },
        secondary_action_label: __("Cancel"),
        secondary_action: function () { d.hide(); }
    });

    d.body.innerHTML = `
        <p style="font-size:13px; margin-bottom:10px;">
            <b>${shared_total}</b> unit(s) will be taken from other batches' free stock
            and reserved for <b>${frm.doc.batch_planning}</b>.
        </p>
        <table class="table table-bordered" style="width:100%; font-size:12px; margin-bottom:0;">
            <thead style="background:#f1f5f9;">
                <tr>
                    <th style="padding:5px 8px;">Item</th>
                    <th style="padding:5px 8px; text-align:right;">Req</th>
                    <th style="padding:5px 8px; text-align:right;">This Batch</th>
                    <th style="padding:5px 8px; text-align:right;">Global</th>
                    <th style="padding:5px 8px; text-align:right;">Shared</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>
    `;
    d.show();
};

window.deallocate_all = function (frm) {

    frappe.call({
        method: "frappe.client.get_list",
        args: {
            doctype: "Stock Entry",
            filters: {
                name: frm.doc.stock_entry || "__none__",
                docstatus: 1,
            },
            fields: ["name"],
            limit: 1,
        },
        callback: function (r) {
            if (r.message && r.message.length) {
                frappe.msgprint({
                    title: __("⛔ Deallocation Blocked"),
                    message: __(
                        "Stock Entry <b>" + r.message[0].name + "</b> is already submitted — items have been issued."
                    ),
                    indicator: "red",
                });
                return;
            }

            frappe.confirm(
                "Release all allocated quantities and clear batch details?",
                function () {
                    frm.call("deallocate").then((r) => {
                        if (!r.exc) {
                            frappe.show_alert({
                                message: __("✅ Deallocated successfully!"),
                                indicator: "blue",
                            });
                            frm.reload_doc();
                        }
                    });
                },
            );
        },
    });
};

window.create_stock_entry = function (frm) {
    if (frm.is_dirty()) {
        frappe.msgprint(__("Save the document first."));
        return;
    }

    if (frm.doc.stock_entry) {
        frappe.msgprint({
            title: __("Not Allowed"),
            message: __("Stock Entry <b>" + frm.doc.stock_entry + "</b> already exists. Only one is allowed."),
            indicator: "red"
        });
        return;
    }

    frappe.confirm(
        "Create a <b>Material Transfer</b> Stock Entry for all allocated items?",
        function () {
            frm.call({
                doc: frm.doc,
                method: "create_stock_entry",
                freeze: true,
                freeze_message: __("Creating Stock Entry..."),
            }).then((r) => {
                if (r.exc || !r.message) return;
                frappe.show_alert({
                    message: __("✅ " + r.message + " created — fill Cost Centre and Segment before submitting."),
                    indicator: "green",
                }, 7);
                frappe.set_route("Form", "Stock Entry", r.message);
            });
        }
    );
};

window.load_expiry_status = function (frm) {
    if (!frm.doc.material_allocation || !frm.doc.material_allocation.length) return;
    let item_codes = frm.doc.material_allocation.map((r) => r.item_code);
    fetch(
        "/api/method/custom_batch_planning.custom_batch_planning.doctype.material_allocation.material_allocation.get_item_batch_expiry",
        {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Frappe-CSRF-Token": frappe.csrf_token,
            },
            body: JSON.stringify({ item_codes: item_codes }),
        },
    )
        .then((r) => r.json())
        .then((data) => {
            if (data.message) {
                setTimeout(function () {
                    window.inject_expiry_badges(frm, data.message);
                }, 1500);
            }
        });
};

window.inject_expiry_badges = function (frm, expiry_map) {
    (frm.doc.material_allocation || []).forEach(function (row) {
        let expiry = expiry_map[row.item_code];
        if (!expiry) return;
        let b_color, b_bg;
        if (expiry.status === "expired") {
            b_color = "#c62828"; b_bg = "#fdecea";
        } else if (expiry.status === "expiring_soon") {
            b_color = "#e65100"; b_bg = "#fff3e0";
        } else {
            b_color = "#2e7d32"; b_bg = "#e8f5e9";
        }
        let badge = `<span class="expiry-badge" style="background:${b_bg};color:${b_color};padding:2px 8px;border-radius:20px;font-size:11px;font-weight:800;display:inline-block;margin-top:4px;">${expiry.label}</span>`;
        let row_el = frm.fields_dict["material_allocation"].grid.grid_rows_by_docname[row.name];
        if (row_el && row_el.row) {
            let shortage_col = row_el.row.find('[data-fieldname="shortage"] .static-area');
            if (shortage_col.find(".expiry-badge").length === 0) {
                shortage_col.append(badge);
            }
        }
    });
};

window.upload_bom_items = function (frm) {
    if (!frm.doc.batch_planning) return;

    frappe.call({
        method: "custom_batch_planning.custom_batch_planning.doctype.batch_planning.batch_planning.get_consolidated_bom_components",
        args: { doc_name: frm.doc.batch_planning },
        freeze: true,
        freeze_message: "Loading Consolidated BOM Items...",
        callback: function (r) {
            if (!r.message || !r.message.length) {
                frappe.msgprint({
                    title: "No Items",
                    message: "No BOM items found.",
                    indicator: "orange",
                });
                return;
            }

            frm.clear_table("material_allocation");
            r.message.forEach(function (item) {
                let row = frm.add_child("material_allocation");
                row.item_code = item.item_code;
                row.item_name = item.item_name;
                row.uom = item.uom;
                row.quantity_required = item.qty;
                row.allocate_qty = item.qty;
                row.stock_available = 0.0;
            });
            frm.refresh_field("material_allocation");

            setTimeout(function () {
                window.refresh_stock_available(frm);
            }, 1000);
        },
    });
};
