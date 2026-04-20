"""Shared voucher-document helper for sales / purchase / return endpoints.

The mobile client sends a generic payload for every transactional voucher:

    {
        "local_id":        "uuid-ish idempotency key",
        "voucher_type":    "sales_invoice",        # registry id, informational
        "doc_type":        "Sales Invoice",         # ERPNext DocType name
        "party_type":      "Customer" | "Supplier",
        "party":           "<customer or supplier name>",
        "posting_date":    "YYYY-MM-DD",
        "company":         "<company name>",
        "price_list":      "<price list name>",
        "currency":        "ILS",
        "is_paid":         true | false,            # Sales/Purchase Invoice
        "mode_of_payment": "Cash",                  # required if is_paid
        "return_against":  "SI-001",                # returns only
        "total_discount_pct": 0,
        "total_discount_amt": 0,
        "notes":           "free text",
        "items": [
            {
                "item_code":          "ITEM-001",
                "uom":                "Carton",
                "conversion_factor":  12,
                "qty":                2,
                "rate":               120.0,
                "discount_percentage": 0,
                "discount_amount":    0
            }
        ]
    }

Responsibilities:
- Idempotency on `custom_mobile_local_id`.
- Honor `conversion_factor` — ERPNext computes stock_qty/stock_uom_rate
  automatically when both uom and conversion_factor are set on the line.
- Map `party` → `customer` or `supplier` depending on doctype.
- Handle Sales Order's `transaction_date` vs `posting_date`.
- Handle `is_paid` + `mode_of_payment` on Sales/Purchase Invoice.
- For returns: set `is_return = 1` and `return_against`; the client must
  send negative qty.
"""

from __future__ import annotations

import frappe

from match_erp.api.mobile.envelope import fail, ok


# Doctypes we support through this helper.
SALES_DOCTYPES = {"Sales Order", "Sales Invoice"}
PURCHASE_DOCTYPES = {"Purchase Order", "Purchase Invoice"}
ORDER_DOCTYPES = {"Sales Order", "Purchase Order"}  # use transaction_date
INVOICE_DOCTYPES = {"Sales Invoice", "Purchase Invoice"}  # support is_paid


def _idempotency_lookup(doctype: str, local_id: str) -> str | None:
	if not local_id:
		return None
	if not frappe.db.has_column(doctype, "custom_mobile_local_id"):
		return None
	existing = frappe.db.get_value(doctype, {"custom_mobile_local_id": local_id}, "name")
	return existing or None


def _validate_payload(payload: dict, doctype: str, is_return: bool) -> tuple[bool, str, str]:
	if not payload.get("local_id"):
		return False, "local_id is required for idempotency", "معرّف محلي مطلوب لمنع التكرار"

	# Accept either new `party` or legacy `customer`/`supplier`.
	party = payload.get("party") or payload.get("customer") or payload.get("supplier")
	if not party:
		return False, "party is required", "الطرف (عميل/مورد) مطلوب"

	if not payload.get("company"):
		return False, "company is required", "الشركة مطلوبة"

	if is_return and not payload.get("return_against"):
		return (
			False,
			"return_against is required for return documents",
			"يجب تحديد المستند الأصلي للمرتجع",
		)

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
		# For returns, qty must be negative; for everything else, positive.
		if is_return:
			if qty >= 0:
				return (
					False,
					f"qty must be negative on return line {i}",
					f"يجب أن تكون الكمية سالبة في سطر المرتجع {i}",
				)
		else:
			if qty <= 0:
				return (
					False,
					f"qty must be greater than 0 on line {i}",
					f"يجب أن تكون الكمية أكبر من صفر في السطر {i}",
				)

	if payload.get("is_paid") and not payload.get("mode_of_payment"):
		return (
			False,
			"mode_of_payment is required when is_paid = true",
			"وسيلة الدفع مطلوبة عند تفعيل خيار مدفوع",
		)

	return True, "", ""


def _build_items(items_payload: list[dict], schedule_date: str | None = None) -> list[dict]:
	rows = []
	for line in items_payload:
		row = {
			"item_code": line["item_code"],
			"qty": float(line.get("qty") or 0),
			"uom": line.get("uom") or None,
			"rate": float(line.get("rate") or 0),
			"discount_percentage": float(line.get("discount_percentage") or 0),
			"discount_amount": float(line.get("discount_amount") or 0),
		}
		cf = line.get("conversion_factor")
		if cf is not None:
			try:
				row["conversion_factor"] = float(cf) or 1.0
			except (TypeError, ValueError):
				row["conversion_factor"] = 1.0
		# Sales Order lines need `delivery_date`; Purchase Order lines need
		# `schedule_date`. Fall back to the header-level value if the line
		# doesn't supply its own.
		line_date = line.get("delivery_date") or line.get("schedule_date") or schedule_date
		if line_date:
			row["delivery_date"] = line_date
			row["schedule_date"] = line_date
		rows.append(row)
	return rows


def create_voucher(doctype: str, payload: dict, is_return: bool = False) -> dict:
	"""Create a sales/purchase document (or return) from the generic mobile payload."""

	valid, en, ar = _validate_payload(payload, doctype, is_return)
	if not valid:
		return fail(en, ar)

	local_id = payload["local_id"]

	existing = _idempotency_lookup(doctype, local_id)
	if existing:
		status = frappe.db.get_value(doctype, existing, "status") or ""
		return ok(
			{
				"name": existing,
				"doc_type": doctype,
				"status": status,
				"duplicate": True,
			},
			en="Document already exists — returning prior result",
			ar="المستند موجود مسبقاً — إرجاع النتيجة السابقة",
		)

	# --- Party mapping ------------------------------------------------------
	party = payload.get("party") or payload.get("customer") or payload.get("supplier")

	# Sales Order needs `delivery_date`, Purchase Order needs `schedule_date`.
	# Fall back to posting_date if the client didn't send a specific value.
	header_schedule_date = (
		payload.get("delivery_date")
		or payload.get("schedule_date")
		or payload.get("posting_date")
	)

	doc_data: dict = {
		"doctype": doctype,
		"company": payload["company"],
		"currency": payload.get("currency"),
		"items": _build_items(payload.get("items") or [], schedule_date=header_schedule_date),
		"custom_mobile_local_id": local_id,
	}

	# Discount — accept both new names (total_discount_*) and old
	# (additional_discount_percentage / discount_amount).
	pct = payload.get("total_discount_pct")
	if pct is None:
		pct = payload.get("additional_discount_percentage")
	amt = payload.get("total_discount_amt")
	if amt is None:
		amt = payload.get("discount_amount")
	if pct is not None:
		doc_data["additional_discount_percentage"] = float(pct or 0)
	if amt is not None:
		doc_data["discount_amount"] = float(amt or 0)

	if payload.get("notes"):
		# Frappe Sales/Purchase doctypes use `remarks` for free-text notes.
		doc_data["remarks"] = payload["notes"]

	# --- Customer vs Supplier ----------------------------------------------
	if doctype in SALES_DOCTYPES:
		doc_data["customer"] = party
		if payload.get("price_list"):
			doc_data["selling_price_list"] = payload["price_list"]
	elif doctype in PURCHASE_DOCTYPES:
		doc_data["supplier"] = party
		if payload.get("price_list"):
			doc_data["buying_price_list"] = payload["price_list"]
	else:
		return fail(f"Unsupported doctype: {doctype}", f"نوع المستند غير مدعوم: {doctype}")

	# --- Date field: Order vs Invoice --------------------------------------
	posting_date = payload.get("posting_date")
	if posting_date:
		if doctype in ORDER_DOCTYPES:
			doc_data["transaction_date"] = posting_date
		else:
			doc_data["posting_date"] = posting_date

	# Sales Order wants `delivery_date` on the header; Purchase Order wants
	# `schedule_date`. Both default to posting_date/delivery_date/schedule_date
	# as sent in the payload.
	if doctype == "Sales Order":
		doc_data["delivery_date"] = header_schedule_date
	elif doctype == "Purchase Order":
		doc_data["schedule_date"] = header_schedule_date

	# --- is_paid on invoices -----------------------------------------------
	# ERPNext Sales/Purchase Invoice marks payment via the `payments` child
	# table — setting is_paid=1 alone has no effect. We add one payment row
	# for the full outstanding amount (0 at insert time; ERPNext fills it in
	# during submit/save via its payment logic).
	if doctype in INVOICE_DOCTYPES and payload.get("is_paid"):
		doc_data["is_paid"] = 1
		mop = payload["mode_of_payment"]
		doc_data["mode_of_payment"] = mop
		# Populate the payments table so ERPNext knows which MOP was used.
		# `amount` is intentionally left at 0 here — ERPNext sets it to the
		# outstanding amount automatically during save/submit.
		doc_data["payments"] = [
			{
				"mode_of_payment": mop,
				"amount": 0,
			}
		]

	# --- Return handling ----------------------------------------------------
	if is_return:
		doc_data["is_return"] = 1
		doc_data["return_against"] = payload["return_against"]

	# --- Create -------------------------------------------------------------
	doc = frappe.get_doc(doc_data)
	doc.insert(ignore_permissions=False)

	if frappe.has_permission(doctype, "submit", doc=doc):
		try:
			doc.submit()
		except frappe.PermissionError:
			pass

	frappe.db.commit()

	return ok(
		{
			"name": doc.name,
			"doc_type": doctype,
			"status": doc.status or "Draft",
			"duplicate": False,
		},
		en=f"{doctype} created",
		ar=f"تم إنشاء {doctype}",
	)
