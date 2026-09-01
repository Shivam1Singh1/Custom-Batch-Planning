frappe.ui.form.on('Slot Master List', {

    onload: function (frm) {
        set_project_filter(frm);
    },

    employee_function: function (frm) {
        frm.set_value('project', '');
        set_project_filter(frm);
    },

    batch_start_date: function (frm) {
        calculate_totals(frm);
    },

    batch_end_date: function (frm) {
        if (!frm.doc.batch_start_date) {
            frappe.msgprint({
                title: __('Required'),
                message: __('Kindly select the Planning Start Date first.'),
                indicator: 'orange'
            });
            frm.set_value('batch_end_date', '');
            return;
        }
        calculate_totals(frm);
    },

    batch_capacity: function (frm) {
        let val = frm.doc.batch_capacity;
        if (val !== undefined && val !== null && val !== "") {
            if (!Number.isInteger(Number(val)) || parseInt(val, 10) < 1) {
                frappe.msgprint({
                    title: __('Invalid Capacity'),
                    message: __('Daily Batch Capacity must be a positive integer.'),
                    indicator: 'orange'
                });
                frappe.model.set_value(frm.doctype, frm.docname, 'batch_capacity', '');
                return;
            }
        }
        calculate_totals(frm);
    },

    refresh: function (frm) {
        let today = frappe.datetime.nowdate();

        if (
            !frm.is_new()
            && frm.doc.docstatus === 1
            && frm.doc.workflow_state === 'Approved'
            && frm.doc.batch_end_date
            && frm.doc.batch_end_date >= today
        ) {
            frappe.call({
                method: "custom_batch_planning.custom_batch_planning.doctype.slot_opening.slot_opening.get_sct_details",
                args: { slot_master: frm.doc.name },
                callback: function (r) {
                    let details = r.message || [];
                    if (!details.length) return;

                    let total_available = details.reduce(
                        (sum, d) => sum + (parseInt(d.capacity_available) || 0), 0
                    );
                    if (total_available <= 0) return;

                    frm.add_custom_button('📋 Create Slot Opening', function () {
                        frappe.new_doc('Slot Opening', {
                            employee_function: frm.doc.employee_function,
                            slot_master: frm.doc.name,
                            project: frm.doc.project
                        });
                    });
                }
            });
        }
    },

    before_save: function (frm) {
        calculate_totals(frm);
    },

    validate: function (frm) {
        let val = frm.doc.batch_capacity;
        if (val === undefined || val === null || val === "" || !Number.isInteger(Number(val)) || parseInt(val, 10) < 1) {
            frappe.msgprint({
                title: __('Invalid Capacity'),
                message: __('Daily Batch Capacity must be a valid whole number of at least 1.'),
                indicator: 'red'
            });
            frappe.validated = false;
        }
    }

});

function calculate_totals(frm) {
    const start = frm.doc.batch_start_date;
    const end = frm.doc.batch_end_date;
    const capacity_per_day = frm.doc.batch_capacity;

    if (start && end) {
        const start_date = frappe.datetime.str_to_obj(start);
        const end_date = frappe.datetime.str_to_obj(end);

        if (
            start_date.getMonth() !== end_date.getMonth() ||
            start_date.getFullYear() !== end_date.getFullYear()
        ) {
            frappe.msgprint({
                title: __('Invalid Date Range'),
                message: __('Both dates must fall within the same month.'),
                indicator: 'red'
            });
            frm.set_value('batch_end_date', '');
            frm.set_value('total_days', 0);
            frm.set_value('total_capacity', 0);
            return;
        }

        const total_days = frappe.datetime.get_day_diff(end_date, start_date) + 1;

        if (total_days < 1) {
            frappe.msgprint({
                title: __('Invalid Date Range'),
                message: __('The End Date cannot be earlier than the Start Date.'),
                indicator: 'red'
            });
            frm.set_value('total_days', 0);
            frm.set_value('total_capacity', 0);
            return;
        }

        frm.set_value('total_days', total_days);
        frm.set_value('total_capacity', capacity_per_day ? total_days * capacity_per_day : 0);
    }
}

function set_project_filter(frm) {
    if (!frm.doc.employee_function) {
        frm.set_query('project', function () {
            return {
                filters: [['name', '=', '']]
            };
        });
        return;
    }

    frappe.call({
        method: 'custom_batch_planning.custom_batch_planning.doctype.slot_master_list.slot_master_list.get_employee_function_projects',
        args: {
            employee_function: frm.doc.employee_function
        },
        callback: function (r) {
            let projects = r.message || [];
            frm.set_query('project', function () {
                return {
                    filters: [['name', 'in', projects.length ? projects : ['']]]
                };
            });
        }
    });
}
