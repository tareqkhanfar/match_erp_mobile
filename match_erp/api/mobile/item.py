"""Item CRUD endpoints for Match ERP Mobile.

Accepts the standard Item fields plus two convenience child-table inputs
the mobile client uses:

- `barcodes`: list of {barcode, uom?} rows → written to `Item Barcode`.
- `uom_conversions`: list of {uom, conversion_factor} rows → written to
  `UOM Conversion Detail`.

Both children are append-style so callers can re-send the full set on each
edit without worrying about partial rebuilds. Compatible with ERPNext
v15 and v16 (child doctypes are unchanged across both).
"""

from __future__ import annotations

import frappe

from match_erp.api.mobile.envelope import fail, mobile_endpoint, ok, parse_body


# Top-level keys that aren't real Item fields — extracted before insert.
_CHILD_KEYS = ("barcodes", "uom_conversions")


def _extract_children(body: dict) -> tuple[list[dict], list[dict]]:
	barcodes = body.pop("barcodes", None) or []
	uoms     = body.pop("uom_conversions", None) or []
	if not isinstance(barcodes, list):
		barcodes = []
	if not isinstance(uoms, list):
		uoms = []
	return barcodes, uoms


def _apply_children(doc, barcodes: list[dict], uoms: list[dict]) -> None:
	"""Replace child rows on the Item doc. Caller must `save()` afterwards."""
	if barcodes:
		# Wipe and rewrite — keeps the API call idempotent.
		doc.set("barcodes", [])
		for b in barcodes:
			code = b.get("barcode") if isinstance(b, dict) else None
			if not code:
				continue
			doc.append("barcodes", {
				"barcode": code,
				"uom": (b.get("uom") if isinstance(b, dict) else None) or doc.stock_uom,
			})
	if uoms:
		# Always include the stock UOM with factor 1 — ERPNext requires it.
		doc.set("uoms", [])
		stock_seen = False
		for u in uoms:
			if not isinstance(u, dict):
				continue
			uom = u.get("uom")
			factor = float(u.get("conversion_factor") or 1.0)
			if not uom:
				continue
			if uom == doc.stock_uom and abs(factor - 1.0) < 1e-9:
				stock_seen = True
			doc.append("uoms", {"uom": uom, "conversion_factor": factor})
		if not stock_seen:
			doc.append("uoms", {"uom": doc.stock_uom, "conversion_factor": 1.0})


@frappe.whitelist()
@mobile_endpoint
def create(**kwargs):
	body = parse_body()
	if not body.get("item_code"):
		return fail("item_code is required", "رمز الصنف مطلوب")
	barcodes, uoms = _extract_children(body)
	body["doctype"] = "Item"
	doc = frappe.get_doc(body)
	_apply_children(doc, barcodes, uoms)
	doc.insert(ignore_permissions=False)
	frappe.db.commit()
	return ok(doc.as_dict(), en="Item created", ar="تم إنشاء الصنف")


@frappe.whitelist()
@mobile_endpoint
def update(**kwargs):
	body = parse_body()
	name = body.get("name")
	data = body.get("data") or {}
	if not name:
		return fail("name is required", "اسم الصنف مطلوب")
	if not isinstance(data, dict) or not data:
		return fail("data is required", "البيانات مطلوبة")

	barcodes, uoms = _extract_children(data)
	doc = frappe.get_doc("Item", name)
	doc.update(data)
	_apply_children(doc, barcodes, uoms)
	doc.save(ignore_permissions=False)
	frappe.db.commit()
	return ok(doc.as_dict(), en="Item updated", ar="تم تحديث الصنف")
