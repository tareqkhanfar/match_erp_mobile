"""Customer-facing "who am I?" endpoint.

Returns the User identity + linked Customer record + outstanding balance
+ default currency for the logged-in user. The Flutter app calls this
right after login so it can render the home screen without making three
extra requests, and mirrors the response to local storage so the
identity remains visible offline.
"""

from __future__ import annotations

import frappe

from match_erp.api.customer_app._session import require_session_customer
from match_erp.api.mobile.envelope import mobile_endpoint, ok


def _user_payload(user_id: str) -> dict:
	"""Build the User block — fields are nullable on User in Frappe so we
	always provide string defaults to keep the client decoding simple.
	"""
	row = frappe.db.get_value(
		"User",
		user_id,
		[
			"name",
			"email",
			"full_name",
			"first_name",
			"last_name",
			"username",
			"user_image",
			"language",
		],
		as_dict=True,
	) or {}
	# Resolve role list separately — it's a child table on User.
	roles = frappe.db.sql(
		"SELECT role FROM `tabHas Role` WHERE parent = %s AND parenttype = 'User'",
		(user_id,),
	)
	row["roles"] = [r[0] for r in roles] if roles else []
	# Standard payload shape — always include email even when the user
	# doc had nothing else, so the client can label the session.
	row.setdefault("email", user_id)
	row.setdefault("name", user_id)
	return row


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

	# Default company — every Sales Order we create needs one and the
	# customer can't be expected to know which company to bill against.
	doc["default_company"] = frappe.db.get_single_value(
		"Global Defaults", "default_company"
	) or frappe.db.get_value("Company", {}, "name", order_by="name asc")

	# Identity block — lets the client greet the user by name and cache
	# the email for offline sessions.
	doc["user"] = _user_payload(frappe.session.user)

	return ok(doc, en="Profile loaded", ar="تم تحميل الملف الشخصي")
