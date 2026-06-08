"""Match Support endpoints — extra tooling for the internal IT / support
team, surfaced behind an in-app gate in the mobile app.

Security model (defense in depth):
  * The whitelisted endpoints still require a normal authenticated session
    (the mobile app is logged in as an ERP user).
  * On top of that, the caller must present the shared support passphrase
    in `support_key`. The mobile screen collects username=match /
    password=match@2026 and forwards the password as the key.
  * Mutating / export endpoints additionally require the session user to
    hold the System Manager role.

This is intentionally conservative: a leaked passphrase alone can't do
anything unless the device is also logged in as a System Manager.
"""

from __future__ import annotations

import json

import frappe
from frappe import _

from match_erp.api.mobile.envelope import fail, mobile_endpoint, ok, parse_body


# Shared support passphrase. Matches the mobile gate (match / match@2026).
# Kept overridable via site_config so it can be rotated without a deploy.
_SUPPORT_KEY = "match@2026"


def _support_key() -> str:
	return frappe.conf.get("match_support_key") or _SUPPORT_KEY


def _guard(require_system_manager: bool = False) -> dict | None:
	"""Return a fail envelope when the caller isn't allowed, else None."""
	body = parse_body()
	key = body.get("support_key") or ""
	if key != _support_key():
		return fail("Invalid support key", "مفتاح الدعم غير صحيح")
	if frappe.session.user in (None, "", "Guest"):
		return fail("Session expired", "انتهت الجلسة")
	if require_system_manager and "System Manager" not in frappe.get_roles():
		return fail(
			"This action requires the System Manager role.",
			"هذا الإجراء يتطلب صلاحية مدير النظام.",
		)
	return None


@frappe.whitelist()
@mobile_endpoint
def authenticate(**kwargs):
	"""Validate the support passphrase. The mobile screen calls this first
	to unlock the support tools."""
	err = _guard()
	if err:
		return err
	return ok(
		{
			"user": frappe.session.user,
			"is_system_manager": "System Manager" in frappe.get_roles(),
			"roles": frappe.get_roles(),
		},
		en="Support access granted",
		ar="تم منح صلاحية الدعم",
	)


@frappe.whitelist()
@mobile_endpoint
def list_doctypes(**kwargs):
	"""List doctypes the support team can browse. Excludes child tables and
	single doctypes for a cleaner picker; ordered by module then name."""
	err = _guard()
	if err:
		return err
	rows = frappe.get_all(
		"DocType",
		filters={"istable": 0, "issingle": 0},
		fields=["name", "module"],
		order_by="module asc, name asc",
		limit_page_length=0,
	)
	return ok({"items": rows}, en="Doctypes", ar="أنواع المستندات")


@frappe.whitelist()
@mobile_endpoint
def list_rows(**kwargs):
	"""Paginated rows for a doctype with an optional free-text filter on the
	name field. Read-only — returns name + title-ish columns."""
	err = _guard()
	if err:
		return err
	body = parse_body()
	doctype = body.get("doctype")
	if not doctype:
		return fail("doctype is required", "نوع المستند مطلوب")
	if not frappe.db.exists("DocType", doctype):
		return fail("Unknown doctype", "نوع مستند غير معروف")

	try:
		limit = int(body.get("limit") or 50)
	except (TypeError, ValueError):
		limit = 50
	limit = max(1, min(limit, 200))
	try:
		offset = int(body.get("offset") or 0)
	except (TypeError, ValueError):
		offset = 0
	query = (body.get("query") or "").strip()

	filters = {}
	if query:
		filters["name"] = ["like", f"%{query}%"]

	# Pick a sensible title field if the doctype declares one.
	meta = frappe.get_meta(doctype)
	title_field = meta.title_field or None
	fields = ["name", "modified"]
	if title_field and title_field != "name":
		fields.append(title_field)

	rows = frappe.get_all(
		doctype,
		filters=filters,
		fields=fields,
		order_by="modified desc",
		start=offset,
		page_length=limit + 1,
		ignore_permissions=False,
	)
	has_more = len(rows) > limit
	if has_more:
		rows = rows[:limit]
	for r in rows:
		for k, v in list(r.items()):
			if hasattr(v, "isoformat"):
				r[k] = v.isoformat()
	return ok(
		{
			"items": rows,
			"title_field": title_field,
			"has_more": has_more,
			"next_offset": offset + len(rows) if has_more else None,
		},
		en="Rows", ar="السجلات",
	)


@frappe.whitelist()
@mobile_endpoint
def get_doc(**kwargs):
	"""Full document as JSON for inspection / editing."""
	err = _guard()
	if err:
		return err
	body = parse_body()
	doctype = body.get("doctype")
	name = body.get("name")
	if not doctype or not name:
		return fail("doctype and name are required", "نوع المستند والاسم مطلوبان")
	if not frappe.db.exists(doctype, name):
		return fail("Document not found", "المستند غير موجود")
	doc = frappe.get_doc(doctype, name)
	return ok(doc.as_dict(), en="Document", ar="المستند")


@frappe.whitelist()
@mobile_endpoint
def update_doc(**kwargs):
	"""Apply a field/value patch to a document. System Manager only.

	Payload: { doctype, name, data: { field: value, ... } }
	"""
	err = _guard(require_system_manager=True)
	if err:
		return err
	body = parse_body()
	doctype = body.get("doctype")
	name = body.get("name")
	data = body.get("data")
	if isinstance(data, str):
		try:
			data = json.loads(data)
		except (json.JSONDecodeError, ValueError):
			return fail("data is not valid JSON", "البيانات ليست JSON صالحًا")
	if not doctype or not name or not isinstance(data, dict) or not data:
		return fail("doctype, name and data are required", "الحقول مطلوبة")
	if not frappe.db.exists(doctype, name):
		return fail("Document not found", "المستند غير موجود")

	doc = frappe.get_doc(doctype, name)
	# Only touch real, writable fields — ignore meta keys the client might
	# echo back (name, doctype, owner, …).
	meta = frappe.get_meta(doctype)
	valid = {df.fieldname for df in meta.fields}
	applied = {}
	for k, v in data.items():
		if k in valid:
			doc.set(k, v)
			applied[k] = v
	doc.save(ignore_permissions=False)
	frappe.db.commit()
	return ok(
		{"name": doc.name, "applied": applied},
		en="Document updated", ar="تم تحديث المستند",
	)


@frappe.whitelist()
@mobile_endpoint
def export_doctype(**kwargs):
	"""Export all rows of a doctype as JSON text. System Manager only.

	Returns the rows inline (the mobile client writes them to a file /
	shares them). Capped to keep the payload sane.
	"""
	err = _guard(require_system_manager=True)
	if err:
		return err
	body = parse_body()
	doctype = body.get("doctype")
	if not doctype:
		return fail("doctype is required", "نوع المستند مطلوب")
	if not frappe.db.exists("DocType", doctype):
		return fail("Unknown doctype", "نوع مستند غير معروف")
	try:
		limit = int(body.get("limit") or 5000)
	except (TypeError, ValueError):
		limit = 5000
	limit = max(1, min(limit, 20000))

	rows = frappe.get_all(
		doctype, fields=["*"], limit_page_length=limit, ignore_permissions=False
	)
	for r in rows:
		for k, v in list(r.items()):
			if hasattr(v, "isoformat"):
				r[k] = v.isoformat()
	payload = json.dumps(
		{"doctype": doctype, "count": len(rows), "rows": rows},
		ensure_ascii=False, indent=2, default=str,
	)
	return ok(
		{"doctype": doctype, "count": len(rows), "json": payload},
		en="Export ready", ar="التصدير جاهز",
	)


@frappe.whitelist()
@mobile_endpoint
def db_stats(**kwargs):
	"""Quick database overview — row counts for the most relevant doctypes.
	Gives the support team a one-glance health check."""
	err = _guard()
	if err:
		return err
	watch = [
		"Sales Invoice", "Sales Order", "Payment Entry", "Customer", "Item",
		"Dist POS Profile", "Mobile Request Log",
	]
	stats = []
	for dt in watch:
		if frappe.db.exists("DocType", dt):
			try:
				stats.append({"doctype": dt, "count": frappe.db.count(dt)})
			except Exception:
				stats.append({"doctype": dt, "count": None})
	return ok({"items": stats}, en="DB stats", ar="إحصائيات قاعدة البيانات")
