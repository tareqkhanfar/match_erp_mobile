"""Sales document endpoints for Match ERP Mobile.

Both `create_sales_order` and `create_sales_invoice` accept the same payload
and are idempotent on `custom_mobile_local_id` — the mobile sync queue will
retry on network failure and we must never create duplicate documents.

Payload:
    {
        "local_id": "UUID-ish stable id",
        "doc_type": "Sales Order" | "Sales Invoice",
        "customer": "CUST-0001",
        "posting_date": "YYYY-MM-DD",
        "company": "My Company",
        "price_list": "Standard Selling",
        "currency": "USD",
        "additional_discount_percentage": 0,
        "discount_amount": 0,
        "items": [
            { "item_code": "ITEM-0001", "uom": "Nos", "qty": 3, "rate": 10.5,
              "discount_percentage": 0, "discount_amount": 0 }
        ]
    }
"""

from __future__ import annotations

import frappe

from match_erp.api.mobile.envelope import fail, mobile_endpoint, ok, parse_body


def _idempotency_lookup(doctype: str, local_id: str) -> str | None:
	"""Return the existing doc `name` if a doc with `custom_mobile_local_id == local_id` exists."""
	if not local_id:
		return None
	# Guard: the custom field may not be installed yet on first run.
	if not frappe.db.has_column(doctype, "custom_mobile_local_id"):
		return None
	existing = frappe.db.get_value(doctype, {"custom_mobile_local_id": local_id}, "name")
	return existing or None


def _validate_payload(payload: dict) -> tuple[bool, str, str]:
	if not payload.get("local_id"):
		return False, "local_id is required for idempotency", "معرّف محلي مطلوب لمنع التكرار"
	if not payload.get("customer"):
		return False, "customer is required", "العميل مطلوب"
	if not payload.get("company"):
		return False, "company is required", "الشركة مطلوبة"
	items = payload.get("items") or []
	if not isinstance(items, list) or not items:
		return False, "At least one item is required", "يجب إضافة صنف واحد على الأقل"
	for i, line in enumerate(items, start=1):
		if not line.get("item_code"):
			return (
				False,
				f"item_code is required on line {i}",
				f"رمز الصنف مطلوب في السطر {i}",
			)
		try:
			qty = float(line.get("qty") or 0)
		except (TypeError, ValueError):
			return False, f"Invalid qty on line {i}", f"كمية غير صالحة في السطر {i}"
		if qty <= 0:
			return (
				False,
				f"qty must be greater than 0 on line {i}",
				f"يجب أن تكون الكمية أكبر من صفر في السطر {i}",
			)
	return True, "", ""


def _build_items(items_payload: list[dict]) -> list[dict]:
	"""Translate mobile line payload to ERPNext child-table rows."""
	rows = []
	for line in items_payload:
		rows.append(
			{
				"item_code": line["item_code"],
				"qty": float(line.get("qty") or 0),
				"uom": line.get("uom") or None,
				"rate": float(line.get("rate") or 0),
				"discount_percentage": float(line.get("discount_percentage") or 0),
				"discount_amount": float(line.get("discount_amount") or 0),
			}
		)
	return rows


def _create_sales_doc(doctype: str) -> dict:
	payload = parse_body()

	ok_valid, en, ar = _validate_payload(payload)
	if not ok_valid:
		return fail(en, ar)

	local_id = payload["local_id"]

	# Idempotency — short-circuit if we already processed this local_id.
	existing = _idempotency_lookup(doctype, local_id)
	if existing:
		status = frappe.db.get_value(doctype, existing, "status") or ""
		return ok(
			{"name": existing, "status": status, "duplicate": True},
			en="Document already exists — returning prior result",
			ar="المستند موجود مسبقاً — إرجاع النتيجة السابقة",
		)

	doc_data: dict = {
		"doctype": doctype,
		"customer": payload["customer"],
		"company": payload["company"],
		"posting_date": payload.get("posting_date"),
		"selling_price_list": payload.get("price_list"),
		"currency": payload.get("currency"),
		"additional_discount_percentage": float(
			payload.get("additional_discount_percentage") or 0
		),
		"discount_amount": float(payload.get("discount_amount") or 0),
		"items": _build_items(payload.get("items") or []),
	}

	# Sales Order uses `transaction_date` rather than `posting_date` on the
	# header — Frappe translates it automatically in recent versions, but set
	# both to be safe when the caller only sent `posting_date`.
	if doctype == "Sales Order" and payload.get("posting_date"):
		doc_data["transaction_date"] = payload["posting_date"]
		doc_data.pop("posting_date", None)

	# Stash the idempotency key. This assumes the Custom Field has been
	# installed via fixtures; _idempotency_lookup above is our guard.
	doc_data["custom_mobile_local_id"] = local_id

	doc = frappe.get_doc(doc_data)
	doc.insert(ignore_permissions=False)

	# Submit only if the user has Submit permission; otherwise leave as draft.
	if frappe.has_permission(doctype, "submit", doc=doc):
		try:
			doc.submit()
		except frappe.PermissionError:
			# Fall through — keep as draft.
			pass

	frappe.db.commit()

	return ok(
		{"name": doc.name, "status": doc.status or "Draft", "duplicate": False},
		en=f"{doctype} created",
		ar=f"تم إنشاء {doctype}",
	)


@frappe.whitelist()
@mobile_endpoint
def create_sales_order(**kwargs):
	return _create_sales_doc("Sales Order")


@frappe.whitelist()
@mobile_endpoint
def create_sales_invoice(**kwargs):
	return _create_sales_doc("Sales Invoice")
