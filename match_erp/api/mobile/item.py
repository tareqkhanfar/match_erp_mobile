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
from match_erp.match_erp.doctype.dist_pos_profile.dist_pos_profile import is_allowed


# Top-level keys that aren't real Item fields — extracted before insert.
_CHILD_KEYS = ("barcodes", "uom_conversions", "images", "item_prices")


def _extract_children(body: dict) -> tuple[list[dict], list[dict]]:
	barcodes = body.pop("barcodes", None) or []
	uoms     = body.pop("uom_conversions", None) or []
	if not isinstance(barcodes, list):
		barcodes = []
	if not isinstance(uoms, list):
		uoms = []
	return barcodes, uoms


def _extract_images(body: dict) -> list[dict]:
	"""Pull the `images` list out of the payload.

	Each entry is either an already-uploaded file reference or a base64 blob:
	    {"file_url": "/files/x.jpg", "title": "...", "is_primary": 0}
	    {"file_name": "x.jpg", "content": "<base64>", "title": "...",
	     "is_primary": 1}
	"""
	images = body.pop("images", None) or []
	if not isinstance(images, list):
		return []
	return [i for i in images if isinstance(i, dict)]


def _apply_images(doc, images: list[dict]) -> None:
	"""Upload/attach images and rebuild the Item's gallery.

	The Item's own `image` field stays the PRIMARY image: it's set from the
	entry flagged `is_primary`, or from the first image when the Item has no
	primary yet. Extra images land in the `custom_item_images` child table.
	Caller must `save()` afterwards.
	"""
	if not images:
		return

	# Imported lazily so item.py doesn't hard-depend on files.py at import time.
	from match_erp.api.mobile.files import _save_file

	rows: list[dict] = []
	primary_url: str | None = None

	for img in images:
		file_url = img.get("file_url")
		# Upload path — store the base64 payload as a File on this Item.
		if not file_url and img.get("content"):
			file_name = img.get("file_name") or f"{doc.item_code}.jpg"
			file_doc = _save_file(
				file_name=file_name,
				content_b64=img["content"],
				attached_to_doctype="Item",
				attached_to_name=doc.name,
				is_private=int(img.get("is_private") or 0),
			)
			file_url = file_doc.file_url
		if not file_url:
			continue

		if img.get("is_primary") and not primary_url:
			primary_url = file_url
		rows.append(
			{
				"image": file_url,
				"title": img.get("title"),
				"is_primary": 1 if img.get("is_primary") else 0,
			}
		)

	if not rows:
		return

	# No explicit primary → use the first image, but never clobber an
	# `image` the caller set directly on the Item.
	if not primary_url and not doc.get("image"):
		primary_url = rows[0]["image"]
	if primary_url:
		doc.image = primary_url

	# Replace the gallery so re-sending the full list stays idempotent.
	doc.set("custom_item_images", [])
	for r in rows:
		doc.append("custom_item_images", r)


def _extract_item_prices(body: dict) -> list[dict]:
	"""Pull the `item_prices` list out of the payload.

	Each entry: {"price_list": "Standard Selling", "rate": 12.5,
	             "uom": "Nos"(optional), "currency": "ILS"(optional),
	             "valid_from": "YYYY-MM-DD"(optional)}
	"""
	prices = body.pop("item_prices", None) or []
	if not isinstance(prices, list):
		return []
	return [p for p in prices if isinstance(p, dict) and p.get("price_list")]


def _apply_item_prices(item_code: str, prices: list[dict]) -> list[dict]:
	"""Create/update `Item Price` rows for the item.

	`selling` / `buying` are taken from the Price List itself so the price
	lands on the right side of the ledger. Re-sending the same price list +
	uom updates the existing row instead of creating a duplicate.
	Returns a summary of what was written.
	"""
	written: list[dict] = []
	for p in prices:
		price_list = p.get("price_list")
		if not frappe.db.exists("Price List", price_list):
			continue
		try:
			rate = float(p.get("rate") if p.get("rate") is not None else p.get("price_list_rate") or 0)
		except (TypeError, ValueError):
			continue
		if rate < 0:
			continue

		pl = frappe.db.get_value(
			"Price List", price_list, ["selling", "buying", "currency"], as_dict=True
		)
		uom = p.get("uom") or frappe.db.get_value("Item", item_code, "stock_uom")

		# One Item Price per (item, price list, uom) — update if it exists.
		existing = frappe.db.get_value(
			"Item Price",
			{"item_code": item_code, "price_list": price_list, "uom": uom},
			"name",
		)
		if existing:
			doc = frappe.get_doc("Item Price", existing)
			doc.price_list_rate = rate
			if p.get("valid_from"):
				doc.valid_from = p["valid_from"]
			doc.save(ignore_permissions=False)
		else:
			doc = frappe.get_doc(
				{
					"doctype": "Item Price",
					"item_code": item_code,
					"price_list": price_list,
					"price_list_rate": rate,
					"uom": uom,
					"currency": p.get("currency") or (pl.currency if pl else None),
					"selling": 1 if (pl and pl.selling) else 0,
					"buying": 1 if (pl and pl.buying) else 0,
					**({"valid_from": p["valid_from"]} if p.get("valid_from") else {}),
				}
			)
			doc.insert(ignore_permissions=False)
		written.append(
			{
				"name": doc.name,
				"price_list": price_list,
				"uom": uom,
				"rate": rate,
				"selling": doc.selling,
				"buying": doc.buying,
			}
		)
	return written


def _validate_item_payload(body: dict) -> tuple[bool, str, str]:
	"""ERPNext requires item_code + item_group + stock_uom. Check them here
	so the client gets a clear bilingual message instead of a raw throw."""
	if not body.get("item_code"):
		return False, "item_code is required", "رمز الصنف مطلوب"
	if not body.get("item_group"):
		return False, "item_group is required", "مجموعة الصنف مطلوبة"
	if not body.get("stock_uom"):
		return False, "stock_uom is required", "وحدة القياس مطلوبة"
	if not frappe.db.exists("Item Group", body["item_group"]):
		return (
			False,
			f"Item Group '{body['item_group']}' not found",
			"مجموعة الصنف غير موجودة",
		)
	if not frappe.db.exists("UOM", body["stock_uom"]):
		return False, f"UOM '{body['stock_uom']}' not found", "وحدة القياس غير موجودة"
	if frappe.db.exists("Item", body["item_code"]):
		return (
			False,
			f"Item '{body['item_code']}' already exists",
			"الصنف موجود مسبقاً",
		)
	return True, "", ""


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
	if not is_allowed("allow_item_create", default=False):
		return fail(
			"Creating items is not permitted by your profile.",
			"إنشاء الأصناف غير مسموح به وفق ملفك.",
		)
	body = parse_body()
	barcodes, uoms = _extract_children(body)
	images = _extract_images(body)
	prices = _extract_item_prices(body)

	# IDEMPOTENCY: Item is named by item_code, so an existing item means the
	# offline queue is retrying a push that already succeeded. Return it as a
	# duplicate instead of failing — otherwise the queue would jam forever.
	item_code = body.get("item_code")
	if item_code and frappe.db.exists("Item", item_code):
		return ok(
			{"name": item_code, "doc_type": "Item", "duplicate": True},
			en="Item already exists — returning prior result",
			ar="الصنف موجود مسبقاً — إرجاع النتيجة السابقة",
		)

	valid, en, ar = _validate_item_payload(body)
	if not valid:
		return fail(en, ar)

	body["doctype"] = "Item"
	doc = frappe.get_doc(body)
	_apply_children(doc, barcodes, uoms)
	doc.insert(ignore_permissions=False)

	# Images are applied AFTER insert — uploaded files need a saved docname
	# to attach to. One extra save() writes the gallery + primary image.
	warnings: list[str] = []
	if images:
		try:
			_apply_images(doc, images)
			doc.save(ignore_permissions=False)
		except Exception as e:
			# A bad image must not discard the created item.
			warnings.append(f"images: {e}")

	# Selling / buying prices become Item Price records.
	written_prices = []
	if prices:
		try:
			written_prices = _apply_item_prices(doc.name, prices)
		except Exception as e:
			warnings.append(f"prices: {e}")

	frappe.db.commit()

	data = doc.as_dict()
	data["item_prices"] = written_prices
	data["duplicate"] = False
	if warnings:
		data["warnings"] = warnings
		return ok(
			data,
			en="Item created with warnings: " + "; ".join(warnings),
			ar="تم إنشاء الصنف مع تحذيرات: " + "; ".join(warnings),
		)
	return ok(data, en="Item created", ar="تم إنشاء الصنف")


@frappe.whitelist()
@mobile_endpoint
def update(**kwargs):
	if not is_allowed("allow_item_edit", default=False):
		return fail(
			"Editing items is not permitted by your profile.",
			"تعديل الأصناف غير مسموح به وفق ملفك.",
		)
	body = parse_body()
	name = body.get("name")
	data = body.get("data") or {}
	if not name:
		return fail("name is required", "اسم الصنف مطلوب")
	if not isinstance(data, dict) or not data:
		return fail("data is required", "البيانات مطلوبة")

	if not frappe.db.exists("Item", name):
		return fail(f"Item '{name}' not found", "الصنف غير موجود")

	barcodes, uoms = _extract_children(data)
	images = _extract_images(data)
	prices = _extract_item_prices(data)
	doc = frappe.get_doc("Item", name)
	doc.update(data)
	_apply_children(doc, barcodes, uoms)
	# The item already exists, so uploads can attach immediately.
	try:
		_apply_images(doc, images)
	except ValueError as e:
		return fail(str(e), f"تعذّر حفظ الصور: {e}")
	doc.save(ignore_permissions=False)

	written_prices = _apply_item_prices(doc.name, prices) if prices else []
	frappe.db.commit()

	out = doc.as_dict()
	out["item_prices"] = written_prices
	return ok(out, en="Item updated", ar="تم تحديث الصنف")
