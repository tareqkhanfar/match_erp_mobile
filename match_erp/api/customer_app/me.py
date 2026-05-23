"""Customer-facing "who am I?" endpoint.

Returns the Customer record + outstanding balance + default currency for
the logged-in user. The Flutter app calls this right after login so it
can render the home screen without making three extra requests.
"""

from __future__ import annotations

import frappe

from match_erp.api.customer_app._session import require_session_customer
from match_erp.api.mobile.envelope import mobile_endpoint, ok


@frappe.whitelist()
@mobile_endpoint
def get(**kwargs):
	customer, err = require_session_customer()
	if err:
		return err

	doc = frappe.db.get_value(
		"Customer",
		customer,
		[
			"name",
			"customer_name",
			"customer_group",
			"territory",
			"default_currency",
			"default_price_list",
		],
		as_dict=True,
	)
	if not doc:
		return ok({}, en="Customer not found", ar="العميل غير موجود")

	outstanding = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(outstanding_amount), 0)
		FROM `tabSales Invoice`
		WHERE customer = %s AND docstatus = 1 AND outstanding_amount > 0
		""",
		(customer,),
	)
	doc["outstanding_amount"] = (
		float(outstanding[0][0]) if outstanding and outstanding[0] else 0.0
	)

	# Hand back the company too — every Sales Order we create needs one,
	# and there's no way for the customer to know which company they belong
	# to. We pick the default company from System Settings.
	doc["default_company"] = frappe.db.get_single_value(
		"Global Defaults", "default_company"
	) or frappe.db.get_value("Company", {}, "name", order_by="name asc")

	return ok(doc, en="Profile loaded", ar="تم تحميل الملف الشخصي")
