"""Session helpers for the customer-facing Flutter app.

Every endpoint under `customer_app.*` must resolve the logged-in Frappe
user to a single Customer record and scope its responses to that record.
A user without a linked Customer is treated as unauthorized — we never
fall back to "show everything" because that would leak data across
customer accounts.

Linking model (strict):
  The administrator goes to Customer → Portal Users → adds the User's
  email. At runtime we look the user up in `tabPortal User` and return
  the parent Customer. That's the only canonical link.

  As a secondary path (for older installs that wired things through
  Contact dynamic-links instead) we also check the Contact route. We
  intentionally do NOT auto-create links, do NOT match by email, and do
  NOT fall back to "any Customer" — the admin must opt-in explicitly by
  populating Portal Users.
"""

from __future__ import annotations

import frappe

from match_erp.api.mobile.envelope import fail


def _by_portal_user(user: str) -> str | None:
	"""Canonical lookup: find the Customer whose `portal_users` child
	table contains this user. Direct SQL because `frappe.db.get_value`
	on child tables varies between ERPNext versions.
	"""
	row = frappe.db.sql(
		"""
		SELECT parent
		FROM `tabPortal User`
		WHERE user = %s AND parenttype = 'Customer'
		LIMIT 1
		""",
		(user,),
	)
	return row[0][0] if row and row[0] else None


def _by_contact(user: str) -> str | None:
	"""Secondary path: legacy installs link via Contact instead of
	Portal Users. Returns the Customer linked to the Contact whose
	`user` field matches. Skipped silently if no such Contact exists.
	"""
	contact = frappe.db.get_value("Contact", {"user": user}, "name")
	if not contact:
		return None
	return frappe.db.get_value(
		"Dynamic Link",
		{
			"parent": contact,
			"parenttype": "Contact",
			"link_doctype": "Customer",
		},
		"link_name",
	)


def session_customer() -> str | None:
	"""Return the Customer name explicitly linked to the logged-in user,
	or None when no link exists. Never guesses.
	"""
	user = frappe.session.user
	if not user or user in ("Guest", "Administrator"):
		# Administrator gets explicit None — we don't want admins
		# accidentally fetching "their" Customer (there isn't one) and
		# getting an empty response that hides the misconfiguration.
		return None

	return _by_portal_user(user) or _by_contact(user)


def require_session_customer() -> tuple[str | None, dict | None]:
	"""Return (customer_name, None) when the user is linked to a Customer.

	When unlinked, returns (None, fail_envelope) with the user's email in
	the message so support knows exactly which account to fix.
	"""
	customer = session_customer()
	if customer:
		return customer, None

	user = frappe.session.user or "(none)"
	return None, fail(
		f"This account ({user}) is not linked to a customer record. "
		"Open the customer in ERPNext and add this email to the "
		"Portal Users table.",
		f"هذا الحساب ({user}) غير مرتبط بسجل عميل. "
		"افتح العميل في ERPNext وأضف هذا البريد إلى جدول "
		"مستخدمي البوابة.",
	)
