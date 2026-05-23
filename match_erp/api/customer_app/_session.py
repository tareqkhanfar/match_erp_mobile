"""Session helpers for the customer-facing Flutter app.

Every endpoint under `customer_app.*` must resolve the logged-in Frappe
user to a single Customer record and scope its responses to that record.
A user without a linked Customer is treated as unauthorized — we never
fall back to "show everything" because that would leak data across
customer accounts.

Resolution order:
  1. `Customer.portal_users.user` — the v15/v16 idiomatic link.
  2. `Contact.user` → `Dynamic Link` → Customer — older installs.
  3. Auto-link by matching `User.email = Customer.email_id` when exactly
     one customer matches. Created lazily so Waseem doesn't have to
     touch the Portal Users table for every customer he onboards.

Steps 1+2 are cheap reads; step 3 mutates the Customer doc so we only
run it when the prior lookups fail.
"""

from __future__ import annotations

import frappe

from match_erp.api.mobile.envelope import fail


def _by_portal_user(user: str) -> str | None:
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


def _auto_link_by_email(user: str) -> str | None:
	"""Find a unique Customer by `email_id == user`. When exactly one
	matches, append the user to its Portal Users so future requests
	resolve via the fast path. Multi-match → bail; we'd rather show the
	error than guess wrong.
	"""
	candidates = frappe.db.sql(
		"SELECT name FROM `tabCustomer` WHERE email_id = %s LIMIT 2",
		(user,),
		as_dict=True,
	)
	if len(candidates) != 1:
		return None
	customer = candidates[0]["name"]
	try:
		doc = frappe.get_doc("Customer", customer)
		# `portal_users` is a child table on Customer in v15+v16. Only
		# append when the row isn't already there — Frappe will happily
		# create duplicates otherwise.
		existing = [p.user for p in (doc.get("portal_users") or [])]
		if user not in existing:
			doc.append("portal_users", {"user": user})
			doc.save(ignore_permissions=True)
			frappe.db.commit()
	except Exception:
		# Best-effort: even if the save fails (e.g. portal_users field
		# unavailable on this build) we still return the resolved customer
		# so the request can proceed.
		pass
	return customer


def session_customer() -> str | None:
	"""Return the Customer name linked to the logged-in user, or None."""
	user = frappe.session.user
	if not user or user in ("Guest", "Administrator"):
		# Administrator gets explicit None — we don't want admins
		# accidentally fetching "their" Customer (there isn't one) and
		# getting an empty response that hides the misconfiguration.
		return None

	for finder in (_by_portal_user, _by_contact, _auto_link_by_email):
		match = finder(user)
		if match:
			return match
	return None


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
		"Please ask your sales rep to add this email to the customer's "
		"Portal Users in ERPNext.",
		f"هذا الحساب ({user}) غير مرتبط بسجل عميل. "
		"يرجى مطالبة مندوب المبيعات بإضافة هذا البريد إلى "
		"مستخدمي بوابة العميل في ERPNext.",
	)
