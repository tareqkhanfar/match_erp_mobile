"""Item CRUD endpoints for Match ERP Mobile.

Same shape as customer.py — thin wrappers around frappe.get_doc with
server-side permission enforcement.
"""

from __future__ import annotations

import frappe

from match_erp.api.mobile.envelope import fail, mobile_endpoint, ok, parse_body


@frappe.whitelist()
@mobile_endpoint
def create():
	body = parse_body()
	if not body.get("item_code"):
		return fail("item_code is required", "رمز الصنف مطلوب")
	body["doctype"] = "Item"
	doc = frappe.get_doc(body)
	doc.insert(ignore_permissions=False)
	frappe.db.commit()
	return ok(doc.as_dict(), en="Item created", ar="تم إنشاء الصنف")


@frappe.whitelist()
@mobile_endpoint
def update():
	body = parse_body()
	name = body.get("name")
	data = body.get("data") or {}
	if not name:
		return fail("name is required", "اسم الصنف مطلوب")
	if not isinstance(data, dict) or not data:
		return fail("data is required", "البيانات مطلوبة")

	doc = frappe.get_doc("Item", name)
	doc.update(data)
	doc.save(ignore_permissions=False)
	frappe.db.commit()
	return ok(doc.as_dict(), en="Item updated", ar="تم تحديث الصنف")
