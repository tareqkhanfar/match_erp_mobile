// Desk client script for Mobile Request Log.
//
// Adds a "Replay Request" button that re-sends the (possibly edited)
// request body to the original endpoint server-side. Editing the value
// column in the "Request Fields" grid and saving rewrites the JSON body,
// so the typical repair flow is: open a failed log → fix a value →
// Save → Replay.

frappe.ui.form.on("Mobile Request Log", {
	refresh(frm) {
		if (!frm.is_new()) {
			frm.add_custom_button(__("Replay Request"), () => {
				frappe.confirm(
					__(
						"Re-send this request to {0} as user {1}?",
						[frm.doc.endpoint, frm.doc.request_user || frappe.session.user]
					),
					() => {
						frappe.dom.freeze(__("Replaying…"));
						frappe
							.call({
								method:
									"match_erp.match_erp.doctype.mobile_request_log.mobile_request_log.replay_request",
								args: { name: frm.doc.name },
							})
							.then((r) => {
								frappe.dom.unfreeze();
								const res = r.message || {};
								if (res.ok) {
									frappe.show_alert({
										message: __("Replay succeeded"),
										indicator: "green",
									});
								} else {
									frappe.msgprint({
										title: __("Replay failed"),
										message:
											res.error ||
											__("The endpoint returned an error. See Response Body."),
										indicator: "red",
									});
								}
								frm.reload_doc();
							})
							.catch(() => frappe.dom.unfreeze());
					}
				);
			}).addClass("btn-primary");
		}

		// Colour the status indicator on the form header.
		if (frm.doc.status === "Failed") {
			frm.dashboard.set_headline_alert(
				__("This request failed: {0}", [frm.doc.error_message || ""]),
				"red"
			);
		}
	},
});
