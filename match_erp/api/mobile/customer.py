"""Customer CRUD endpoints for Match ERP Mobile.

Client-side toggles in Settings decide whether to expose these, but
permissions are still enforced server-side via frappe.get_doc().
"""

from __future__ import annotations

import frappe

from match_erp.api.mobile.envelope import fail, mobile_endpoint, ok, parse_body


@frappe.whitelist()
@mobile_endpoint
def create():
	body = parse_body()
	if not body.get("customer_name"):
		return fail("customer_name is required", "اسم العميل مطلوب")
	body["doctype"] = "Customer"
	doc = frappe.get_doc(body)
	doc.insert(ignore_permissions=False)
	frappe.db.commit()
	return ok(doc.as_dict(), en="Customer created", ar="تم إنشاء العميل")


@frappe.whitelist()
@mobile_endpoint
def update():
	body = parse_body()
	name = body.get("name")
	data = body.get("data") or {}
	if not name:
		return fail("name is required", "اسم العميل مطلوب")
	if not isinstance(data, dict) or not data:
		return fail("data is required", "البيانات مطلوبة")

	doc = frappe.get_doc("Customer", name)
	doc.update(data)
	doc.save(ignore_permissions=False)
	frappe.db.commit()
	return ok(doc.as_dict(), en="Customer updated", ar="تم تحديث العميل")
