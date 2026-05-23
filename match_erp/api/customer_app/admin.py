"""Admin endpoints — used by Waseem (or support) to link Frappe Users to
Customer records without opening the ERPNext desk.

These run with the **caller's** permissions: only users with write
access on the Customer doctype can succeed. We deliberately don't add a
custom permission gate — Frappe's existing role system is the right
place to control who can do this.
"""

from __future__ import annotations

import frappe

from match_erp.api.mobile.envelope import fail, mobile_endpoint, ok, parse_body


@frappe.whitelist()
@mobile_endpoint
def link_user_to_customer(**kwargs):
	"""Append `user` to the given customer's Portal Users table.

	Payload:
	    { "user": "customer1@example.com", "customer": "CUST-001" }

	Idempotent — running twice with the same args is a no-op. Returns the
	customer's Portal Users list after the change so callers can verify.
	"""
	body = parse_body()
	user = body.get("user")
	customer = body.get("customer")
	if not user or not customer:
		return fail(
			"user and customer are required",
			"المستخدم والعميل مطلوبان",
		)

	if not frappe.db.exists("User", user):
		return fail(f"User '{user}' not found", f"المستخدم '{user}' غير موجود")
	if not frappe.db.exists("Customer", customer):
		return fail(
			f"Customer '{customer}' not found",
			f"العميل '{customer}' غير موجود",
		)

	doc = frappe.get_doc("Customer", customer)
	existing = {p.user for p in (doc.get("portal_users") or [])}
	added = False
	if user not in existing:
		doc.append("portal_users", {"user": user})
		doc.save()  # uses caller permissions — Frappe enforces Write access
		frappe.db.commit()
		added = True

	return ok(
		{
			"customer": customer,
			"user": user,
			"added": added,
			"portal_users": [p.user for p in (doc.get("portal_users") or [])],
		},
		en="User linked to customer" if added else "User already linked",
		ar="تم ربط المستخدم بالعميل" if added else "المستخدم مرتبط مسبقاً",
	)


@frappe.whitelist()
@mobile_endpoint
def lookup_customer_by_email(**kwargs):
	"""Find candidate customers for a given email. Lets the admin caller
	double-check which Customer record an unlinked user "should" map to.

	Payload: { "email": "customer1@example.com" }
	"""
	body = parse_body()
	email = body.get("email")
	if not email:
		return fail("email is required", "البريد مطلوب")

	rows = frappe.get_list(
		"Customer",
		filters=[["email_id", "=", email]],
		fields=["name", "customer_name", "customer_group", "territory"],
		ignore_permissions=False,
		limit_page_length=20,
	)
	return ok(
		{"items": rows},
		en=f"{len(rows)} candidate(s) found",
		ar=f"تم العثور على {len(rows)} نتيجة",
	)
