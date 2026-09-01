frappe.ui.form.on("Slot Opening", {
	onload: function (frm) {
		// A new doc already carrying employee_function got it from the
		// frappe.new_doc() handoff on Slot Master List. form.js replays every
		// populated Link field through trigger_link_fields() once the form is up,
		// which fires the handler below and clears the rest of that handoff. Arm
		// the guard so the replay is skipped exactly once.
		//
		// Only when the value arrived with the doc: on a blank Slot Opening the
		// replay never fires, so an unconditional flag would instead swallow the
		// user's own first pick -- and set_project_filter() would never re-run,
		// leaving Project stuck behind the empty filter onload installed.
		if (frm.is_new() && frm.doc.employee_function) {
			frm.__skip_ef_clear_once = true;
		}

		if (frm.is_new() && frm.doc.slot_master && frm.doc.employee_function) {
			frm.trigger('slot_master');
		}
		set_slot_master_filter(frm);
		set_project_filter(frm);
	},

	refresh: function (frm) {
		frm.clear_custom_buttons();

		frm.set_df_property("total_batch_remained", "hidden", 0);
		frm.refresh_field("total_batch_remained");

		frm.add_custom_button("📊 Capacity Remained", function () {
			if (!frm.doc.slot_master) {
				frappe.msgprint("Please select a Slot Master first!");
				return;
			}
			frappe.call({
				method: "custom_batch_planning.custom_batch_planning.doctype.slot_opening.slot_opening.get_sct_details",
				args: { slot_master: frm.doc.slot_master },
				callback: function (r) {
					if (!r.message || !r.message.length) {
						frappe.msgprint("No Slot Capacity Tracker found!");
						return;
					}
					let rows = r.message
						.sort((a, b) => (a.date > b.date ? 1 : -1))
						.map(
							(d) => `
    <tr>
        <td>${d.date.split("-").reverse().join("-")}</td>
        <td>${d.total_capacity}</td>
        <td>${d.capacity_booked}</td>
        <td style="color:${d.capacity_available > 0 ? "green" : "red"}; font-weight:bold;">
            ${d.capacity_available}
        </td>
        <td style="color:${d.batches_planned > 0 ? "#e67e22" : "#27ae60"}; font-weight:bold;">
            ${d.batches_planned || 0}
        </td>
    </tr>
`,
						)
						.join("");

					let d = new frappe.ui.Dialog({
						title: __("Capacity Remained"),
						size: "extra-large",
						fields: [
							{
								fieldtype: "HTML",
								fieldname: "capacity_table",
								options: `
									<div style="overflow-x: auto; margin-top: -15px;">
										<table class="table table-bordered" style="width: 100%; min-width: 600px; margin-bottom: 0;">
											<thead style="background: #f0f0f0;">
												<tr>
													<th>Date</th>
													<th>Total Capacity</th>
													<th>Booked</th>
													<th>Available</th>
													<th>Batches Planned</th>
												</tr>
											</thead>
											<tbody>${rows}</tbody>
										</table>
									</div>
								`
							}
						]
					});
					d.show();
				},
			});
		});

		if (frm.doc.docstatus === 1 && frm.doc.slot_master) {
			frappe.call({
				method: 'custom_batch_planning.custom_batch_planning.doctype.slot_opening.slot_opening.get_slot_opening_usage',
				args: { slot_opening: frm.doc.name },
				callback: function (sct_r) {
					let has_remaining = (sct_r.message || []).some(function (d) {
						return (parseInt(d.remaining) || 0) > 0;
					});

					let today = frappe.datetime.nowdate();
					let end_date_valid = !frm.doc.batch_end_date || frm.doc.batch_end_date >= today;

					if (!has_remaining || !end_date_valid) return;

					frm.add_custom_button(__("➕ Create Batch"), function () {
						if (frm.is_dirty()) {
							frappe.msgprint(__("Please save the document before creating a Batch."));
							return;
						}

						frappe.new_doc("Batch Planning", {
							slot_opening: frm.doc.name,
							custom_employee_function: frm.doc.employee_function,
							custom_function_head_name: frm.doc.function_head_name,
							custom_slot_master: frm.doc.slot_master,
							project: frm.doc.project,
							month: frm.doc.batch_start_date
								? new Date(frm.doc.batch_start_date).toLocaleString("en-US", {
									month: "long",
								})
								: "",
							custom_total_batches_planned: frm.doc.total_batches_planned,
						});
					});
				}
			});
		}

		if (frm.doc.slot_master) {
			frappe.db.get_value(
				"Slot Master List",
				frm.doc.slot_master,
				["batch_end_date", "batch_start_date", "batch_capacity"],
				function (data) {
					if (data) {
						frm.doc.__batch_end_date = data.batch_end_date;
						frm.doc.__batch_start_date = data.batch_start_date;
						frm.doc.__batch_capacity = data.batch_capacity;
					}
					fetch_sct_remaining(frm);
					loadSlotOpeningCalendar(frm);
				},
			);
		} else {
			loadSlotOpeningCalendar(frm);
		}
		set_slot_master_filter(frm);
	},

	after_save: function (frm) {
		if (frm.doc.name.startsWith("SO-")) {
			if (frm.doc.__islocal || frm._was_new) {
				frappe.set_route("Form", "Slot Opening", frm.doc.name);
			}
		}
	},

	employee_function: function (frm) {
		if (frm.__skip_ef_clear_once) {
			frm.__skip_ef_clear_once = false;
			return;
		}

		frm.set_value("slot_master", "");
		frm.set_value("project", "");
		frm.set_value("total_batch_capacity", "");
		frm.set_value("total_batch_remained", "");
		frm.set_value("batch_start_date", "");
		frm.set_value("batch_end_date", "");
		frm.doc.__batch_end_date = null;
		frm.doc.__batch_start_date = null;
		frm.doc.__batch_capacity = null;
		set_slot_master_filter(frm);
		set_project_filter(frm);
		loadSlotOpeningCalendar(frm);
	},

	project: function (frm) {
		loadSlotOpeningCalendar(frm);
	},

	slot_master: function (frm) {

		if (frm.doc.slot_master && !frm.doc.employee_function) {
			frappe.msgprint({
				title: __("Missing Employee Function"),
				message: __(
					"Please select an Employee Function first before selecting a Slot Master.",
				),
				indicator: "orange",
			});
			frm.set_value("slot_master", "");
			return;
		}

		if (!frm.doc.slot_master) {
			frm.set_value("total_batch_capacity", "");
			frm.set_value("total_batch_remained", "");
			frm.set_value("batch_start_date", "");
			frm.set_value("batch_end_date", "");
			frm.doc.__batch_end_date = null;
			frm.doc.__batch_start_date = null;
			frm.doc.__batch_capacity = null;
			loadSlotOpeningCalendar(frm);
			return;
		}

		frappe.db.get_value(
			"Slot Master List",
			frm.doc.slot_master,
			["batch_capacity", "batch_end_date", "batch_start_date", "project"],
			function (data) {
				if (!data) return;

				frm.doc.__batch_end_date = data.batch_end_date;
				frm.doc.__batch_start_date = data.batch_start_date;
				frm.doc.__batch_capacity = data.batch_capacity;

				let today = frappe.datetime.nowdate();
				if (data.batch_end_date && data.batch_end_date < today) {
					frappe.msgprint({
						title: "⚠️ Slot Master Expired",
						message: `The selected Slot Master <b>${frm.doc.slot_master}</b> has expired.`,
						indicator: "red",
					});
					frm.set_value("slot_master", "");
					return;
				}

				frm.set_value("batch_start_date", data.batch_start_date);
				frm.set_value("batch_end_date", data.batch_end_date);
				frm.set_value("project", data.project);

				loadSlotOpeningCalendar(frm);

				let total_capacity = calculate_total_capacity(
					data.batch_start_date,
					data.batch_end_date,
					data.batch_capacity,
				);
				frm.set_value("total_batch_capacity", total_capacity);
				calculate_totals(frm);
				fetch_sct_remaining(frm);

				frappe.call({
					method: "custom_batch_planning.custom_batch_planning.doctype.slot_opening.slot_opening.get_sct_details",
					args: { slot_master: frm.doc.slot_master },
					callback: function (r) {
						if (!r.message || !r.message.length) return;

						let available_dates = r.message.sort((a, b) => (a.date > b.date ? 1 : -1));
						if (!available_dates.length) return;

						let total_available = available_dates.reduce((sum, d) => sum + (parseInt(d.capacity_available) || 0), 0);
						if (total_available <= 0) {
							frappe.msgprint({
								title: __("Capacity Full"),
								message: __("The selected Slot Master has no remaining capacity."),
								indicator: "red"
							});
							frm.set_value("slot_master", "");
							return;
						}

						frm.doc.slot_booking = [];
						frm.refresh_field("slot_booking");

						available_dates.forEach(function (d) {
							if ((parseInt(d.capacity_available) || 0) <= 0) return;
							let row = frm.add_child("slot_booking");
							row.slot_booking_date = d.date;
							row.batch_capacity = data.batch_capacity;
							row.total_slots = parseInt(data.batch_capacity) || 0;
							row.availabe_capacity = parseInt(d.capacity_available) || 0;
							row.__sct_available = d.capacity_available;
							if (parseInt(d.capacity_available) === 0) {
								row.reason = "";
							}
						});

						frm.refresh_field("slot_booking");
						calculate_totals(frm);

					},
				});
			},
		);
	},

	validate: function (frm) {
		let total_planned = parseInt(frm.doc.total_batches_planned) || 0;
		let capacity = parseInt(frm.doc.total_batch_capacity) || 0;
		if (capacity > 0 && total_planned > capacity) {
			frappe.throw(
				`⚠️ Total Batches Planned (${total_planned}) exceeds Total Batch Capacity (${capacity})!`,
			);
		}

		(frm.doc.slot_booking || []).forEach(function (row) {
			let val = row.planning_capacity;
			if (val === undefined || val === null || val === "" || !Number.isInteger(Number(val)) || parseInt(val, 10) <= 0) {
				frappe.throw(`⚠️ Row #${row.idx}: Planning Capacity must be a positive integer greater than zero.`);
			}

			let intVal = parseInt(val, 10);
			if (intVal != val) {
				row.planning_capacity = intVal;
			}
		});
	},
});

frappe.ui.form.on("Slot Booking CT", {

	slot_booking_date: function (frm, cdt, cdn) {
		let row = locals[cdt][cdn];
		if (!row.slot_booking_date) return;

		if (row.slot_booking_date < frappe.datetime.nowdate()) {
			frappe.model.set_value(cdt, cdn, "slot_booking_date", "");
			frappe.msgprint("⚠️ Slot Booking Date cannot be before today!");
			return;
		}

		let start = frm.doc.__batch_start_date || frm.doc.batch_start_date;
		let end = frm.doc.__batch_end_date || frm.doc.batch_end_date;

		if (start && row.slot_booking_date < start) {
			frappe.model.set_value(cdt, cdn, "slot_booking_date", "");
			frappe.msgprint(`⚠️ Slot Booking Date cannot be before Batch Start Date: ${start}`);
			return;
		}
		if (end && row.slot_booking_date > end) {
			frappe.model.set_value(cdt, cdn, "slot_booking_date", "");
			frappe.msgprint(`⚠️ Slot Booking Date cannot be after Batch End Date: ${end}`);
			return;
		}

		let duplicate = (frm.doc.slot_booking || []).filter(
			(r) => r.name !== row.name && r.slot_booking_date === row.slot_booking_date,
		);
		if (duplicate.length > 0) {
			frappe.model.set_value(cdt, cdn, "slot_booking_date", "");
			frappe.msgprint(`⚠️ Date ${row.slot_booking_date} already exists in another row!`);
			return;
		}

		if (!frm.doc.slot_master) return;

		frappe.call({
			method: "custom_batch_planning.custom_batch_planning.doctype.slot_opening.slot_opening.get_sct_details",
			args: { slot_master: frm.doc.slot_master, date: row.slot_booking_date },
			callback: function (r) {
				if (!r.message || r.message.length === 0) {
					frappe.msgprint(
						`⚠️ Date <b>${row.slot_booking_date}</b> not found in Slot Capacity Tracker!`,
					);
					frappe.model.set_value(cdt, cdn, "slot_booking_date", "");
					return;
				}

				let detail = r.message[0];
				let available = parseInt(detail.capacity_available) || 0;

				if (available <= 0) {
					frappe.msgprint({
						title: "⚠️ No Capacity Available",
						message: `Date <b>${row.slot_booking_date}</b> has <b>0</b> capacity available.<br>
                                  Total: ${detail.total_capacity} |
                                  Booked: ${detail.capacity_booked} |
                                  Available: ${detail.capacity_available}`,
						indicator: "red",
					});
					frappe.model.set_value(cdt, cdn, "slot_booking_date", "");
					return;
				}

				row.__sct_available = available;
				row.total_slots = parseInt(frm.doc.__batch_capacity) || parseInt(detail.total_capacity) || 0;
				row.availabe_capacity = available;
				row.__sct_detail_name = detail.name;
				frm.refresh_field("slot_booking");

				frappe.msgprint({
					title: "✅ Capacity Available",
					message: `Date <b>${row.slot_booking_date}</b>: <b>${available}</b> slot(s) available.`,
					indicator: "green",
				});
			},
		});
	},

	planning_capacity: function (frm, cdt, cdn) {
		let row = locals[cdt][cdn];
		let available = parseInt(row.availabe_capacity) || 0;
		let booked = parseInt(row.planning_capacity) || 0;
		if (booked > available) {
			frappe.msgprint(__("⚠️ Planning Capacity cannot exceed Available Capacity ({0})!", [available]));
			frappe.model.set_value(cdt, cdn, "planning_capacity", 0);
			return;
		}
		if (row.total_slots && row.planning_capacity && parseInt(row.total_slots) === parseInt(row.planning_capacity)) {
			frappe.model.set_value(cdt, cdn, "reason", "N/A");
		} else if (row.reason === "N/A") {
			frappe.model.set_value(cdt, cdn, "reason", "");
		}
		calculate_totals(frm);
	},

	slot_booking_remove: function (frm) {
		calculate_totals(frm);
		fetch_sct_remaining(frm);
	},
});

function calculate_total_capacity(start_date, end_date, batch_capacity) {
	if (!start_date || !end_date || !batch_capacity) return 0;
	let start = frappe.datetime.str_to_obj(start_date);
	let end = frappe.datetime.str_to_obj(end_date);
	let num_days = Math.floor((end - start) / (1000 * 60 * 60 * 24)) + 1;
	return num_days * parseInt(batch_capacity);
}

function set_slot_master_filter(frm) {
	console.log("set_slot_master_filter called, ef:", frm.doc.employee_function);
	frm.set_query("slot_master", function () {
		console.log("get_query fired, ef:", frm.doc.employee_function);
		return {
			query: "custom_batch_planning.custom_batch_planning.doctype.slot_opening.slot_opening.get_active_slot_masters",
			filters: {
				employee_function: frm.doc.employee_function || ""
			}
		};
	});
}

function calculate_totals(frm) {
	let total_batches = 0;
	(frm.doc.slot_booking || []).forEach(function (row) {
		total_batches += parseInt(row.planning_capacity) || 0;
	});
	frm.set_value("total_batches_planned", total_batches);
	fetch_sct_remaining(frm);
}

function fetch_sct_remaining(frm) {
	if (!frm.doc.slot_master) return;
	if (frm.doc.docstatus === 1) return;
	frappe.call({
		method: "custom_batch_planning.custom_batch_planning.doctype.slot_opening.slot_opening.get_sct_details",
		args: { slot_master: frm.doc.slot_master },
		callback: function (r) {
			if (!r.message) return;

			let db_total_available = r.message.reduce(
				(sum, d) => sum + (parseInt(d.capacity_available) || 0),
				0,
			);

			let current_planned = parseInt(frm.doc.total_batches_planned) || 0;
			let remained = db_total_available - current_planned;

			if (frm.doc.total_batch_remained !== remained) {
				frm.set_value("total_batch_remained", remained);
			}
		},
	});
}

function loadSlotOpeningCalendar(frm) {
	if (!frm.fields_dict.calenders) return;

	const wrapper = frm.fields_dict.calenders.$wrapper;

	if (!frm.doc.employee_function) {
		wrapper.html(`
            <div class="so-cal-empty">
                <div class="so-cal-empty-icon">📅</div>
                <div class="so-cal-empty-title">Calendar Unavailable</div>
                <div class="so-cal-empty-sub">
                    Select an <strong>Employee Function</strong> to view the slot calendar.
                </div>
            </div>
        `);
		_injectCalendarBaseStyles();
		return;
	}

	_injectCalendarBaseStyles();

	wrapper.html(`
        <div class="so-cal-shell">
            <div class="so-cal-loading">
                <div class="so-cal-spinner"></div>
                <span>Loading slot calendar…</span>
            </div>
        </div>
    `);

	frappe.call({
		method: "custom_batch_planning.custom_batch_planning.doctype.slot_opening.slot_opening.get_calendar_data",
		args: {
			employee_function: frm.doc.employee_function,
			project: frm.doc.project || ""
		},
		callback: function (r) {
			const rows = r.message || [];

			if (!rows.length) {
				wrapper.html(`
                <div class="so-cal-empty">
                    <div class="so-cal-empty-icon">🔍</div>
                    <div class="so-cal-empty-title">No SCT Records Found</div>
                    <div class="so-cal-empty-sub">
                        No Slot Capacity Tracker found for<br>
                        <strong>${frm.doc.employee_function}</strong>
                    </div>
                </div>
            `);
				return;
			}

			const byDate = {};
			rows.forEach(function (row) {
				const d = String(row.date).split(" ")[0];
				if (!byDate[d]) {
					byDate[d] = { total: 0, booked: 0, available: 0, sources: [] };
				}
				byDate[d].total += parseInt(row.total_capacity) || 0;
				byDate[d].booked += parseInt(row.capacity_booked) || 0;
				byDate[d].available += parseInt(row.capacity_available) || 0;
				byDate[d].sources.push({
					sct_name: row.sct_name,
					slot_master: row.slot_master || "-",
					employee_headname: row.employee_headname || "-",
					project: row.project || "-",
					total: parseInt(row.total_capacity) || 0,
					booked: parseInt(row.capacity_booked) || 0,
					available: parseInt(row.capacity_available) || 0,
				});
			});

			_renderCalendar(frm, wrapper, byDate);
		},
	});
}

function _renderCalendar(frm, wrapper, byDate) {
	const FC_CSS = "https://cdn.jsdelivr.net/npm/fullcalendar@6.1.9/index.global.min.css";
	const FC_JS = "https://cdn.jsdelivr.net/npm/fullcalendar@6.1.9/index.global.min.js";

	wrapper.html(`
        <div class="so-cal-shell">

            <!-- Header: title + legend -->
            <div class="so-cal-header">
                <div class="so-cal-header-left">
                    <span class="so-cal-icon">📅</span>
                    <div>
                        <div class="so-cal-title">Slot Availability Calendar</div>
                        <div class="so-cal-subtitle">
                            ${frm.doc.employee_function}
                        </div>
                    </div>
                </div>
                <div class="so-cal-legend">
                    <span><span class="so-cal-dot" style="background:#22c55e;"></span>Available</span>
                    <span><span class="so-cal-dot" style="background:#f59e0b;"></span>Partial</span>
                    <span><span class="so-cal-dot" style="background:#ef4444;"></span>Full</span>
                    <span><span class="so-cal-dot" style="background:#cbd5e1;"></span>No Slot</span>
                </div>
            </div>

            <!-- FullCalendar mounts here -->
            <div class="so-cal-body">
                <div id="so-fc-mount"></div>
            </div>

            <!-- Footer tip -->
            <div class="so-cal-footer">
                💡 <strong>Tip:</strong> Click any highlighted date to see a detailed breakdown.
            </div>

        </div>
    `);

	const _init = () => _mountFC(frm, byDate);

	if (!document.querySelector(`link[href="${FC_CSS}"]`)) {
		const link = document.createElement("link");
		link.rel = "stylesheet";
		link.href = FC_CSS;
		document.head.appendChild(link);
	}

	function _waitAndInit(retriesLeft) {
		if (typeof FullCalendar !== "undefined" && document.getElementById("so-fc-mount")) {
			setTimeout(_init, 50);
		} else if (retriesLeft > 0) {
			setTimeout(() => _waitAndInit(retriesLeft - 1), 120);
		}
	}

	if (typeof FullCalendar === "undefined") {
		const script = document.createElement("script");
		script.src = FC_JS;
		script.onload = () => _waitAndInit(10);
		document.head.appendChild(script);
	} else {
		_waitAndInit(10);
	}
}

function _mountFC(frm, byDate) {
	const el = document.getElementById("so-fc-mount");
	if (!el) return;

	if (window.__soCalInstance) {
		try {
			window.__soCalInstance.destroy();
		} catch (e) { }
		window.__soCalInstance = null;
	}

	const allDates = Object.keys(byDate).sort();
	const initialDate = allDates.length ? allDates[0] : frappe.datetime.nowdate();

	const calendar = new FullCalendar.Calendar(el, {
		initialView: "dayGridMonth",
		initialDate: initialDate,
		headerToolbar: {
			left: "prev,next today",
			center: "title",
			right: "",
		},
		height: "auto",
		fixedWeekCount: false,
		showNonCurrentDates: true,
		dayMaxEvents: false,

		dayCellDidMount: function (info) {
			const dateStr = _toDateStr(info.date);
			const data = byDate[dateStr];
			const frame = info.el.querySelector(".fc-daygrid-day-frame");

			if (!frame || !data) return;

			const pill = document.createElement("div");
			pill.style.pointerEvents = "none";
			pill.className = "so-cal-pill";

			if (data.available === 0) {

				pill.classList.add("so-cal-pill--full");
				pill.innerHTML = '<span class="so-pill-label">FULL</span>';
				info.el.classList.add("so-day--full");
			} else if (data.booked > 0) {

				pill.classList.add("so-cal-pill--partial");
				pill.innerHTML = `
                    <span class="so-pill-num">${data.available}</span>
                    <span class="so-pill-sep">/</span>
                    <span class="so-pill-den">${data.total}</span>
                    <span class="so-pill-unit">left</span>
                `;
				info.el.classList.add("so-day--partial");
			} else {

				pill.classList.add("so-cal-pill--avail");
				pill.innerHTML = `
                    <span class="so-pill-num">${data.available}</span>
                    <span class="so-pill-sep">/</span>
                    <span class="so-pill-den">${data.total}</span>
                    <span class="so-pill-unit">free</span>
                `;
				info.el.classList.add("so-day--avail");
			}

			frame.appendChild(pill);
			info.el.style.cursor = "pointer";
		},

		dateClick: function (info) {
			const dateStr = _toDateStr(info.date);
			const data = byDate[dateStr];

			if (!data) {
				frappe.msgprint({
					title: `📅 ${_humanDate(dateStr)}`,
					message:
						'<p style="text-align:center;color:#64748b;padding:16px 0;">No slot configured for this date.</p>',
					indicator: "gray",
				});
				return;
			}

			const statusColor =
				data.available === 0 ? "#ef4444" : data.booked > 0 ? "#f59e0b" : "#22c55e";
			const statusLabel =
				data.available === 0 ? "FULL" : data.booked > 0 ? "PARTIAL" : "AVAILABLE";

			const rows = data.sources
				.map(
					(s) => `
                <tr>
                    <td style="font-size:12px;">${s.slot_master}</td>
                    <td style="font-size:12px;">${s.employee_headname}</td>
                    <td style="text-align:center;font-weight:700;">${s.total}</td>
                    <td style="text-align:center;font-weight:700;color:#ef4444;">${s.booked}</td>
                    <td style="text-align:center;font-weight:700;
                        color:${s.available > 0 ? "#22c55e" : "#ef4444"};">${s.available}</td>
                </tr>
            `,
				)
				.join("");

			let project_code = frm.doc.project;
			if (project_code) {
				frappe.db.get_value("Project", project_code, "project_name", function (r) {
					let project_full_name = (r && r.project_name) ? r.project_name : project_code;
					render_popup(project_full_name);
				});
			} else {
				render_popup("All Projects");
			}

			function render_popup(project_full_name) {
				frappe.msgprint({
					title: `📅 ${_humanDate(dateStr)}`,
					message: `
						<!-- Project Info -->
						<div style="font-size:14px;font-weight:600;color:#334155;margin-bottom:8px;">
							Project: <span style="color:#0f172a;font-weight:700;">${project_full_name}</span>
						</div>
						<!-- Summary bar -->
						<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px;">
							<span style="background:${statusColor};color:#fff;font-size:11px;
								font-weight:800;padding:3px 12px;border-radius:20px;letter-spacing:1px;">
								${statusLabel}
							</span>
							<span style="font-size:14px;color:#334155;">
								<strong>${data.total}</strong> total &nbsp;·&nbsp;
								<strong style="color:#ef4444;">${data.booked}</strong> booked &nbsp;·&nbsp;
								<strong style="color:#22c55e;">${data.available}</strong> available
							</span>
						</div>
						<!-- Per-SCT breakdown table -->
						<div style="overflow-x:auto;">
							<table class="table table-bordered" style="width:100%;font-size:13px;margin:0;">
								<thead style="background:#f1f5f9;">
									<tr>
										<th>Slot Master</th>
										<th>Head Name</th>
										<th style="text-align:center;">Total</th>
										<th style="text-align:center;">Booked</th>
										<th style="text-align:center;">Available</th>
									</tr>
								</thead>
								<tbody>${rows}</tbody>
							</table>
						</div>
					`,
					wide: true,
					indicator: data.available === 0 ? "red" : data.booked > 0 ? "orange" : "green",
				});
			}
		},
	});

	calendar.render();
	window.__soCalInstance = calendar;

	if (window.__soCalObserver) window.__soCalObserver.disconnect();
	window.__soCalObserver = new IntersectionObserver((entries) => {
		if (entries[0].isIntersecting) {
			setTimeout(() => calendar.updateSize(), 50);
		}
	});
	window.__soCalObserver.observe(el);

	if (window.__soCalResize) window.removeEventListener("resize", window.__soCalResize);
	window.__soCalResize = () => calendar.updateSize();
	window.addEventListener("resize", window.__soCalResize);
	setTimeout(() => calendar.updateSize(), 800);
}

function _toDateStr(date) {
	const y = date.getFullYear();
	const m = String(date.getMonth() + 1).padStart(2, "0");
	const d = String(date.getDate()).padStart(2, "0");
	return `${y}-${m}-${d}`;
}

function _humanDate(dateStr) {
	const [y, m, d] = dateStr.split("-");
	const mon = [
		"Jan",
		"Feb",
		"Mar",
		"Apr",
		"May",
		"Jun",
		"Jul",
		"Aug",
		"Sep",
		"Oct",
		"Nov",
		"Dec",
	];
	return `${d} ${mon[parseInt(m) - 1]} ${y}`;
}

function _injectCalendarBaseStyles() {
	if (document.getElementById("so-cal-styles")) return;
	const s = document.createElement("style");
	s.id = "so-cal-styles";
	s.textContent = `

    /* Shell */
    .so-cal-shell {
        background: #fff;
        border: 1px solid #e2e8f0;
        border-radius: 16px;
        overflow: hidden;
        box-shadow: 0 8px 32px -8px rgba(15,23,42,0.10);
        font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
        margin: 8px 0 20px;
    }

    /* Header */
    .so-cal-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        flex-wrap: wrap;
        gap: 12px;
        padding: 18px 24px;
        background: linear-gradient(135deg, #0f172a 0%, #1e40af 100%);
    }
    .so-cal-header-left {
        display: flex;
        align-items: center;
        gap: 14px;
    }
    .so-cal-icon {
        font-size: 34px;
        line-height: 1;
        filter: drop-shadow(0 2px 6px rgba(0,0,0,0.35));
    }
    .so-cal-title {
        font-size: 17px;
        font-weight: 700;
        color: #fff;
        letter-spacing: -0.3px;
    }
    .so-cal-subtitle {
        font-size: 12px;
        color: #93c5fd;
        margin-top: 3px;
        max-width: 380px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .so-cal-legend {
        display: flex;
        align-items: center;
        gap: 14px;
        font-size: 12px;
        font-weight: 600;
        color: #cbd5e1;
        flex-wrap: wrap;
    }
    .so-cal-dot {
        display: inline-block;
        width: 9px; height: 9px;
        border-radius: 50%;
        margin-right: 4px;
        vertical-align: middle;
    }

    .so-cal-body {
    padding: 16px 16px 10px;
    background: #f8fafc;
    overflow-x: auto;          /* ← horizontal scroll */
    -webkit-overflow-scrolling: touch;
}

    /* Footer */
    .so-cal-footer {
        padding: 10px 24px;
        font-size: 12px;
        color: #64748b;
        background: #f1f5f9;
        border-top: 1px solid #e2e8f0;
    }

    /* Loading */
    .so-cal-loading {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 12px;
        padding: 70px 20px;
        color: #64748b;
        font-size: 14px;
        font-weight: 500;
    }
    .so-cal-spinner {
        width: 22px; height: 22px;
        border: 3px solid #e2e8f0;
        border-top-color: #3b82f6;
        border-radius: 50%;
        animation: so-spin 0.75s linear infinite;
        flex-shrink: 0;
    }
    @keyframes so-spin { to { transform: rotate(360deg); } }

    /* Empty state */
    .so-cal-empty {
        text-align: center;
        padding: 50px 24px;
        background: #f8fafc;
        border: 2px dashed #cbd5e1;
        border-radius: 16px;
        margin: 12px 0 20px;
    }
    .so-cal-empty-icon  { font-size: 46px; margin-bottom: 12px; }
    .so-cal-empty-title { font-size: 16px; font-weight: 700; color: #0f172a; margin-bottom: 6px; }
    .so-cal-empty-sub   { font-size: 13px; color: #64748b; line-height: 1.7; }

    /* FullCalendar overrides */
   #so-fc-mount {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif !important;
    min-width: 560px;
}
    #so-fc-mount .fc-toolbar {
        flex-wrap: wrap !important;
        gap: 10px !important;
        margin-bottom: 16px !important;
    }
    #so-fc-mount .fc-button-group {
        gap: 8px !important;
        display: flex !important;
    }
    #so-fc-mount .fc-button-group > .fc-button {
        border-radius: 8px !important;
        margin: 0 !important;
    }
    #so-fc-mount .fc-toolbar-chunk {
        display: flex !important;
        align-items: center !important;
        gap: 8px !important;
    }
    #so-fc-mount .fc-toolbar-title {
        font-size: clamp(16px, 2.5vw, 22px) !important;
        font-weight: 800 !important;
        color: #0f172a !important;
        letter-spacing: -0.5px;
    }
    #so-fc-mount .fc-col-header-cell {
        background: #f1f5f9 !important;
        padding: 10px 4px !important;
        font-size: 11px !important;
        font-weight: 700 !important;
        text-transform: uppercase;
        letter-spacing: 0.8px;
        color: #475569 !important;
    }
    #so-fc-mount .fc-daygrid-day {
    min-height: 72px !important;
    transition: background 0.15s ease;
}
    #so-fc-mount .fc-daygrid-day:hover { background: #f1f5f9 !important; }
    #so-fc-mount .fc-daygrid-day-frame {
    padding: 4px 5px 6px !important;
    min-height: 72px !important;   /* was 95px */
    display: flex !important;
    flex-direction: column !important;
    position: relative !important;
}
    #so-fc-mount .fc-daygrid-day-top { margin-bottom: 6px !important; }
    #so-fc-mount .fc-daygrid-day-number {
        font-size: 13px !important;
        font-weight: 600 !important;
        color: #475569 !important;
        padding: 4px 8px !important;
        border-radius: 50% !important;
        text-decoration: none !important;
        transition: background 0.15s, color 0.15s;
    }
    #so-fc-mount .fc-day-today {
        background: #eff6ff !important;
        box-shadow: inset 0 0 0 2px #3b82f6 !important;
    }
    #so-fc-mount .fc-day-today .fc-daygrid-day-number {
        background: #3b82f6 !important;
        color: #fff !important;
    }

    /* Day state tints */
    #so-fc-mount .so-day--avail   { background: #f0fdf4 !important; }
    #so-fc-mount .so-day--partial { background: #fffbeb !important; }
    #so-fc-mount .so-day--full    { background: #fef2f2 !important; }

    /* Pill */
    .so-cal-pill {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 2px;
        border-radius: 8px;
        padding: 4px 7px;
        margin-top: auto;
        white-space: nowrap;
        user-select: none;
    }
    .so-cal-pill--avail {
        background: linear-gradient(135deg, #22c55e, #15803d);
        box-shadow: 0 2px 6px rgba(34,197,94,0.28);
        color: #fff;
    }
    .so-cal-pill--partial {
        background: linear-gradient(135deg, #f59e0b, #b45309);
        box-shadow: 0 2px 6px rgba(245,158,11,0.28);
        color: #fff;
    }
    .so-cal-pill--full {
        background: linear-gradient(135deg, #ef4444, #b91c1c);
        box-shadow: 0 2px 6px rgba(239,68,68,0.28);
        color: #fff;
    }
    .so-pill-num   { font-size: 13px; font-weight: 800; line-height: 1; }
    .so-pill-sep   { font-size: 11px; opacity: 0.65; }
    .so-pill-den   { font-size: 11px; opacity: 0.85; font-weight: 700; }
    .so-pill-unit  { font-size: 9px; opacity: 0.8; margin-left: 2px; font-weight: 600; letter-spacing: 0.3px; }
    .so-pill-label { font-size: 10px; font-weight: 800; letter-spacing: 1px; }

    /* FC buttons */
    #so-fc-mount .fc-button-primary {
        background: #fff !important;
        border: 1.5px solid #cbd5e1 !important;
        color: #475569 !important;
        border-radius: 8px !important;
        font-weight: 600 !important;
        font-size: 13px !important;
        padding: 6px 16px !important;
        box-shadow: 0 1px 3px rgba(0,0,0,0.06) !important;
        transition: all 0.15s ease !important;
    }
    #so-fc-mount .fc-button-primary:hover {
        background: #f8fafc !important;
        border-color: #94a3b8 !important;
        color: #0f172a !important;
        transform: translateY(-1px) !important;
        box-shadow: 0 4px 10px rgba(0,0,0,0.08) !important;
    }
    #so-fc-mount .fc-button-primary:not(:disabled).fc-button-active,
    #so-fc-mount .fc-button-primary:not(:disabled):active {
        background: linear-gradient(135deg, #3b82f6, #1d4ed8) !important;
        border-color: #1d4ed8 !important;
        color: #fff !important;
        box-shadow: 0 4px 10px rgba(59,130,246,0.35) !important;
        transform: none !important;
    }
    #so-fc-mount .fc-scrollgrid { border-color: #e2e8f0 !important; border-radius: 10px !important; }
    #so-fc-mount td, #so-fc-mount th { border-color: #e2e8f0 !important; }
    #so-fc-mount .fc-view-harness { border-radius: 10px; overflow: hidden; }

    /* ── Responsive ── */
    @media (max-width: 768px) {
        .so-cal-header  { padding: 14px 16px; }
        .so-cal-legend  { gap: 10px; font-size: 11px; }
        .so-cal-body    { padding: 14px 10px 10px; }
    }
    @media (max-width: 560px) {
        .so-cal-legend  { display: none; }
        .so-cal-subtitle { max-width: 220px; }
        #so-fc-mount .fc-toolbar { flex-direction: column !important; align-items: stretch !important; }

       #so-fc-mount .fc-daygrid-day {
    min-height: 72px !important;   /* was 95px */
    transition: background 0.15s ease;
}
        #so-fc-mount .fc-daygrid-day-frame { min-height: 72px !important; padding: 4px 3px !important; }
        .so-cal-pill    { padding: 3px 4px; border-radius: 5px; }
        .so-pill-num    { font-size: 11px; }
        .so-pill-unit   { display: none; }
        #so-fc-mount .fc-col-header-cell { font-size: 9px !important; padding: 6px 2px !important; }
        #so-fc-mount .fc-daygrid-day-number { font-size: 11px !important; padding: 2px 5px !important; }
        #so-fc-mount .fc-button-primary { padding: 5px 10px !important; font-size: 12px !important; margin: 0 !important; }
        /* Fix arrow button spacing in FC toolbar */

     #so-fc-mount .fc-toolbar-title { font-size: 18px !important; }
    }
    @media (max-width: 380px) {
        .so-cal-title  { font-size: 14px; }
        .so-cal-icon   { font-size: 26px; }
        .so-pill-sep, .so-pill-den { display: none; }
        .so-cal-pill   { padding: 3px 5px; }
    }
    `;
	document.head.appendChild(s);
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

