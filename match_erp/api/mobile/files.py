"""File endpoints for Match ERP Mobile.

Two groups:

1. ITEM IMAGES — an item can now carry a gallery of images in addition to its
   primary picture. The primary image is always `Item.image`; the extra
   images live in the `custom_item_images` child table (doctype "Item Image").

       item_images.list    → primary + gallery
       item_images.add     → append an image to the gallery
       item_images.remove  → remove one gallery image
       item_images.set_primary → promote a gallery image to Item.image

2. VOUCHER ATTACHMENTS — attach files to ANY voucher (Sales Invoice, Sales
   Order, Payment Entry, …) using Frappe's standard File doctype, so
   attachments show up in the ERPNext UI sidebar too.

       attachments.upload  → attach a base64 file to a voucher
       attachments.list    → list a voucher's attachments
       attachments.delete  → remove one attachment

Files are returned with ABSOLUTE URLs so the mobile client can fetch them
directly.
"""

from __future__ import annotations

import base64

import frappe

from match_erp.api.mobile.envelope import fail, mobile_endpoint, ok, parse_body
from match_erp.api.mobile.sync import _absolutize_file

# Doctypes a mobile client may attach files to. Keeps the endpoint from being
# used as a generic file-writer for arbitrary doctypes.
ATTACHABLE_DOCTYPES = {
	"Sales Invoice",
	"Sales Order",
	"Payment Entry",
	"Purchase Invoice",
	"Purchase Order",
	"Item",
	"Customer",
	"Supplier",
}

# Guard against oversized uploads (base64 inflates ~33%). 10 MB decoded.
MAX_FILE_BYTES = 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _file_row(f: dict) -> dict:
	"""Normalize a File row for the mobile client."""
	return {
		"name": f.get("name"),
		"file_name": f.get("file_name"),
		"file_url": _absolutize_file(f.get("file_url")),
		"file_size": f.get("file_size") or 0,
		"is_private": f.get("is_private") or 0,
		"creation": str(f.get("creation") or ""),
		"owner": f.get("owner"),
	}


def _save_file(
	*,
	file_name: str,
	content_b64: str,
	attached_to_doctype: str | None = None,
	attached_to_name: str | None = None,
	is_private: int = 0,
) -> dict:
	"""Decode a base64 payload and store it as a Frappe File."""
	try:
		content = base64.b64decode(content_b64, validate=True)
	except Exception:
		raise ValueError("content must be valid base64")

	if len(content) > MAX_FILE_BYTES:
		raise ValueError(
			f"File is too large ({len(content)} bytes). Max is {MAX_FILE_BYTES}."
		)

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name,
			"content": content,
			"is_private": 1 if is_private else 0,
			"attached_to_doctype": attached_to_doctype,
			"attached_to_name": attached_to_name,
		}
	)
	file_doc.save(ignore_permissions=False)
	return file_doc


# ---------------------------------------------------------------------------
# 1. ITEM IMAGES
# ---------------------------------------------------------------------------
@frappe.whitelist()
@mobile_endpoint
def list_item_images(**kwargs):
	"""Return an item's primary image plus its gallery images.

	Body: { "item_code": "ITEM-001" }
	"""
	body = parse_body()
	item_code = body.get("item_code") or body.get("name")
	if not item_code:
		return fail("item_code is required", "رمز الصنف مطلوب")
	if not frappe.db.exists("Item", item_code):
		return fail("Item not found", "الصنف غير موجود")

	doc = frappe.get_doc("Item", item_code)
	gallery = []
	for row in doc.get("custom_item_images") or []:
		gallery.append(
			{
				"row_id": row.name,
				"image": _absolutize_file(row.get("image")),
				"title": row.get("title"),
				"is_primary": row.get("is_primary") or 0,
			}
		)

	return ok(
		{
			"item_code": doc.name,
			"item_name": doc.item_name,
			# The Item's own image field is ALWAYS the primary image.
			"primary_image": _absolutize_file(doc.image),
			"images": gallery,
			"total": len(gallery),
		},
		en="Item images fetched",
		ar="تم جلب صور الصنف",
	)


@frappe.whitelist()
@mobile_endpoint
def add_item_image(**kwargs):
	"""Append an image to an item's gallery.

	Body (one of `file_url` OR `content`):
	    {
	      "item_code": "ITEM-001",
	      "file_name": "side.jpg",       # required with `content`
	      "content":   "<base64>",       # upload a new file
	      "file_url":  "/files/x.jpg",   # or reference an existing file
	      "title":     "Side view",      # optional
	      "set_as_primary": false        # optional — also set Item.image
	    }
	"""
	body = parse_body()
	item_code = body.get("item_code") or body.get("name")
	if not item_code:
		return fail("item_code is required", "رمز الصنف مطلوب")
	if not frappe.db.exists("Item", item_code):
		return fail("Item not found", "الصنف غير موجود")

	file_url = body.get("file_url")
	content = body.get("content")
	if not file_url and not content:
		return fail(
			"Either file_url or content (base64) is required",
			"مطلوب إما رابط الملف أو محتواه",
		)

	# Upload path — store the base64 payload as a File attached to the Item.
	if content:
		file_name = body.get("file_name")
		if not file_name:
			return fail("file_name is required with content", "اسم الملف مطلوب")
		try:
			file_doc = _save_file(
				file_name=file_name,
				content_b64=content,
				attached_to_doctype="Item",
				attached_to_name=item_code,
				is_private=int(body.get("is_private") or 0),
			)
		except ValueError as e:
			return fail(str(e), "تعذّر حفظ الملف")
		file_url = file_doc.file_url

	doc = frappe.get_doc("Item", item_code)
	row = doc.append(
		"custom_item_images",
		{
			"image": file_url,
			"title": body.get("title"),
			"is_primary": 1 if body.get("set_as_primary") else 0,
		},
	)
	# Promoting to primary also updates the Item's own image field.
	if body.get("set_as_primary"):
		doc.image = file_url
	doc.save(ignore_permissions=False)
	frappe.db.commit()

	return ok(
		{
			"item_code": doc.name,
			"row_id": row.name,
			"image": _absolutize_file(file_url),
			"primary_image": _absolutize_file(doc.image),
			"total": len(doc.get("custom_item_images") or []),
		},
		en="Image added",
		ar="تمت إضافة الصورة",
	)


@frappe.whitelist()
@mobile_endpoint
def remove_item_image(**kwargs):
	"""Remove one gallery image.

	Body: { "item_code": "ITEM-001", "row_id": "<row name>" }
	   or: { "item_code": "ITEM-001", "file_url": "/files/x.jpg" }
	"""
	body = parse_body()
	item_code = body.get("item_code") or body.get("name")
	row_id = body.get("row_id")
	file_url = body.get("file_url")
	if not item_code:
		return fail("item_code is required", "رمز الصنف مطلوب")
	if not row_id and not file_url:
		return fail("row_id or file_url is required", "معرّف الصف أو رابط الملف مطلوب")
	if not frappe.db.exists("Item", item_code):
		return fail("Item not found", "الصنف غير موجود")

	doc = frappe.get_doc("Item", item_code)
	rows = doc.get("custom_item_images") or []
	keep = []
	removed = 0
	for row in rows:
		match = (row_id and row.name == row_id) or (
			file_url and row.get("image") == file_url
		)
		if match:
			removed += 1
			continue
		keep.append(row)

	if not removed:
		return fail("Image not found on this item", "الصورة غير موجودة لهذا الصنف")

	doc.set("custom_item_images", keep)
	doc.save(ignore_permissions=False)
	frappe.db.commit()

	return ok(
		{"item_code": doc.name, "removed": removed, "total": len(keep)},
		en="Image removed",
		ar="تم حذف الصورة",
	)


@frappe.whitelist()
@mobile_endpoint
def set_primary_item_image(**kwargs):
	"""Promote an image to be the item's primary image (`Item.image`).

	Body: { "item_code": "ITEM-001", "file_url": "/files/x.jpg" }
	"""
	body = parse_body()
	item_code = body.get("item_code") or body.get("name")
	file_url = body.get("file_url")
	if not item_code or not file_url:
		return fail(
			"item_code and file_url are required", "رمز الصنف ورابط الصورة مطلوبان"
		)
	if not frappe.db.exists("Item", item_code):
		return fail("Item not found", "الصنف غير موجود")

	doc = frappe.get_doc("Item", item_code)
	doc.image = file_url
	for row in doc.get("custom_item_images") or []:
		row.is_primary = 1 if row.get("image") == file_url else 0
	doc.save(ignore_permissions=False)
	frappe.db.commit()

	return ok(
		{"item_code": doc.name, "primary_image": _absolutize_file(doc.image)},
		en="Primary image updated",
		ar="تم تحديث الصورة الرئيسية",
	)


@frappe.whitelist()
@mobile_endpoint
def get_item_images(**kwargs):
	"""Bulk gallery fetch for the catalog sync.

	Body: { "item_codes": ["ITEM-001", "ITEM-002"] }  (max 500)
	Returns a map keyed by item_code → {primary_image, images[]}.
	Items with no gallery rows are omitted.
	"""
	body = parse_body()
	codes = body.get("item_codes") or []
	if isinstance(codes, str):
		import json as _json

		try:
			codes = _json.loads(codes)
		except (ValueError, TypeError):
			codes = []
	codes = [c for c in codes if c][:500]
	if not codes:
		return fail("item_codes is required", "قائمة أصناف مطلوبة")

	rows = frappe.get_all(
		"Item Image",
		filters={"parent": ["in", codes], "parenttype": "Item"},
		fields=["parent", "name", "image", "title", "is_primary", "idx"],
		order_by="parent asc, idx asc",
		limit_page_length=0,
	)

	primaries = {
		r["name"]: r.get("image")
		for r in frappe.get_all(
			"Item", filters={"name": ["in", codes]}, fields=["name", "image"]
		)
	}

	out: dict[str, dict] = {}
	for r in rows:
		entry = out.setdefault(
			r["parent"],
			{
				"primary_image": _absolutize_file(primaries.get(r["parent"])),
				"images": [],
			},
		)
		entry["images"].append(
			{
				"row_id": r["name"],
				"image": _absolutize_file(r.get("image")),
				"title": r.get("title"),
				"is_primary": r.get("is_primary") or 0,
			}
		)

	return ok({"items": out, "total": len(out)}, en="Item images fetched", ar="تم جلب الصور")


# ---------------------------------------------------------------------------
# 2. VOUCHER ATTACHMENTS  (works for every voucher doctype)
# ---------------------------------------------------------------------------
def _validate_target(body: dict) -> tuple[str | None, str | None, str, str]:
	"""Resolve + validate (doctype, name) from the request body."""
	doctype = body.get("doctype") or body.get("voucher_type")
	name = body.get("name") or body.get("voucher_no")
	if not doctype or not name:
		return None, None, "doctype and name are required", "نوع المستند واسمه مطلوبان"
	if doctype not in ATTACHABLE_DOCTYPES:
		return (
			None,
			None,
			f"Attachments are not supported for '{doctype}'",
			f"المرفقات غير مدعومة لنوع المستند '{doctype}'",
		)
	if not frappe.db.exists(doctype, name):
		return None, None, f"{doctype} '{name}' not found", "المستند غير موجود"
	return doctype, name, "", ""


@frappe.whitelist()
@mobile_endpoint
def upload_attachment(**kwargs):
	"""Attach a file to any voucher.

	Body:
	    {
	      "doctype":  "Sales Invoice",
	      "name":     "ACC-SINV-2026-00001",
	      "file_name":"receipt.jpg",
	      "content":  "<base64>",
	      "is_private": 0            # optional
	    }
	"""
	body = parse_body()
	doctype, name, en, ar = _validate_target(body)
	if not doctype:
		return fail(en, ar)

	file_name = body.get("file_name")
	content = body.get("content")
	if not file_name or not content:
		return fail(
			"file_name and content (base64) are required",
			"اسم الملف ومحتواه مطلوبان",
		)

	# The caller must be allowed to write to the target document.
	if not frappe.has_permission(doctype, "write", doc=name):
		return fail("Permission denied", "ليس لديك صلاحية")

	try:
		file_doc = _save_file(
			file_name=file_name,
			content_b64=content,
			attached_to_doctype=doctype,
			attached_to_name=name,
			is_private=int(body.get("is_private") or 0),
		)
	except ValueError as e:
		return fail(str(e), "تعذّر حفظ الملف")

	frappe.db.commit()
	return ok(
		_file_row(file_doc.as_dict()),
		en="Attachment uploaded",
		ar="تم رفع المرفق",
	)


@frappe.whitelist()
@mobile_endpoint
def list_attachments(**kwargs):
	"""List a voucher's attachments.

	Body: { "doctype": "Sales Invoice", "name": "ACC-SINV-2026-00001" }
	"""
	body = parse_body()
	doctype, name, en, ar = _validate_target(body)
	if not doctype:
		return fail(en, ar)

	if not frappe.has_permission(doctype, "read", doc=name):
		return fail("Permission denied", "ليس لديك صلاحية")

	rows = frappe.get_all(
		"File",
		filters={"attached_to_doctype": doctype, "attached_to_name": name},
		fields=[
			"name",
			"file_name",
			"file_url",
			"file_size",
			"is_private",
			"creation",
			"owner",
		],
		order_by="creation desc",
		limit_page_length=0,
	)

	return ok(
		{
			"doctype": doctype,
			"name": name,
			"attachments": [_file_row(r) for r in rows],
			"total": len(rows),
		},
		en="Attachments fetched",
		ar="تم جلب المرفقات",
	)


@frappe.whitelist()
@mobile_endpoint
def delete_attachment(**kwargs):
	"""Delete one attachment by its File name.

	Body: { "file_name": "<File docname>" }
	"""
	body = parse_body()
	file_id = body.get("file_id") or body.get("file_name")
	if not file_id:
		return fail("file_id is required", "معرّف الملف مطلوب")
	if not frappe.db.exists("File", file_id):
		return fail("Attachment not found", "المرفق غير موجود")

	f = frappe.db.get_value(
		"File", file_id, ["attached_to_doctype", "attached_to_name"], as_dict=True
	)
	# Deleting an attachment requires write access on the document it belongs to.
	if f and f.attached_to_doctype and f.attached_to_name:
		if not frappe.has_permission(
			f.attached_to_doctype, "write", doc=f.attached_to_name
		):
			return fail("Permission denied", "ليس لديك صلاحية")

	frappe.delete_doc("File", file_id, ignore_permissions=False)
	frappe.db.commit()
	return ok({"deleted": file_id}, en="Attachment deleted", ar="تم حذف المرفق")
