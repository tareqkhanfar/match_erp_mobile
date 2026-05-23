"""Session helpers for the customer-facing Flutter app.

Every endpoint under `customer_app.*` must resolve the logged-in Frappe
user to a single Customer record and scope its responses to that record.
A user without a linked Customer is treated as unauthorized — we never
fall back to "show everything" because that would leak data across
customer accounts.
"""

from __future__ import annotations

import frappe

from match_erp.api.mobile.envelope import fail


def session_customer() -> str | None:
	"""Return the Customer name linked to the logged-in user, or None.

	Resolution order (mirrors ERPNext's standard Portal behavior):
	  1. `Customer.portal_users.user` — the v15/v16 idiomatic link.
	  2. `Contact.user` → `Dynamic Link` → Customer — older installs.

	None means "no Customer link found" and the caller MUST refuse to
	return data, not fall through to an unscoped query.
	"""
	user = frappe.session.user
	if not user or user in ("Guest", "Administrator"):
		# Administrator deserves explicit handling — we don't want admins
		# accidentally fetching "their" Customer (there isn't one) and
		# getting an empty response that hides the misconfiguration.
		return None

	# Path 1: Customer.portal_users
	row = frappe.db.sql(
		"""
		SELECT parent
		FROM `tabPortal User`
		WHERE user = %s AND parenttype = 'Customer'
		LIMIT 1
		""",
		(user,),
	)
	if row and row[0] and row[0][0]:
		return row[0][0]

	# Path 2: Contact → Dynamic Link → Customer
	contact = frappe.db.get_value("Contact", {"user": user}, "name")
	if contact:
		link = frappe.db.get_value(
			"Dynamic Link",
			{
				"parent": contact,
				"parenttype": "Contact",
				"link_doctype": "Customer",
			},
			"link_name",
		)
		if link:
			return link

	return None


def require_session_customer() -> tuple[str | None, dict | None]:
	"""Return (customer_name, None) when the user is linked to a Customer.

	When unlinked, returns (None, fail_envelope) so the caller can
	`return err`. Saves every endpoint from duplicating the same check.
	"""
	customer = session_customer()
	if not customer:
		return None, fail(
			"This account is not linked to a customer record. "
			"Please contact your sales rep.",
			"هذا الحساب غير مرتبط بسجل عميل. يرجى التواصل مع مندوب المبيعات.",
		)
	return customer, None
