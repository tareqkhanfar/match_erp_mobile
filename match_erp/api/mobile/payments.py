"""Payment Entry endpoints for Match ERP Mobile.

Payment Entry has no items — it's just party + amount + mode of payment,
optionally linked to a reference invoice.

- create_payment_entry   → payment_type="Pay"     (we pay a supplier)
- create_payment_receipt → payment_type="Receive" (we receive from customer)

Payload:
    {
        "local_id":         "idempotency key",
        "company":          "<company>",
        "posting_date":     "YYYY-MM-DD",
        "party_type":       "Customer" | "Supplier",
        "party":            "<name>",
        "paid_amount":      100.0,
        "received_amount":  100.0,                 // optional, defaults to paid_amount
        "mode_of_payment":  "Cash",
        "paid_from":        "<account>",           // optional — ERPNext will auto-pick
        "paid_to":          "<account>",           // optional
        "reference_doctype":"Sales Invoice",       // optional
        "reference_name":   "SI-001",              // optional
        "notes":            "free text"
    }
"""

from __future__ import annotations

import frappe

from match_erp.api.mobile.envelope import fail, mobile_endpoint, ok, parse_body


def _idempotency_lookup(local_id: str) -> str | None:
	if not local_id:
		return None
	if not frappe.db.has_column("Payment Entry", "custom_mobile_local_id"):
		return None
	existing = frappe.db.get_value(
		"Payment Entry", {"custom_mobile_local_id": local_id}, "name"
	)
	return existing or None


def _validate_payment(payload: dict) -> tuple[bool, str, str]:
	if not payload.get("local_id"):
		return False, "local_id is required for idempotency", "معرّف محلي مطلوب لمنع التكرار"
	if not payload.get("company"):
		return False, "company is required", "الشركة مطلوبة"
	if not payload.get("party_type"):
		return False, "party_type is required", "نوع الطرف مطلوب"
	if not payload.get("party"):
		return False, "party is required", "الطرف مطلوب"
	if not payload.get("mode_of_payment"):
		return False, "mode_of_payment is required", "وسيلة الدفع مطلوبة"
	try:
		amount = float(payload.get("paid_amount") or 0)
	except (TypeError, ValueError):
		return False, "Invalid paid_amount", "المبلغ غير صالح"
	if amount <= 0:
		return False, "paid_amount must be greater than 0", "يجب أن يكون المبلغ أكبر من صفر"
	return True, "", ""


def _create_payment(payment_type: str) -> dict:
	payload = parse_body()

	valid, en, ar = _validate_payment(payload)
	if not valid:
		return fail(en, ar)

	local_id = payload["local_id"]

	existing = _idempotency_lookup(local_id)
	if existing:
		status = frappe.db.get_value("Payment Entry", existing, "status") or ""
		return ok(
			{
				"name": existing,
				"doc_type": "Payment Entry",
				"status": status,
				"duplicate": True,
			},
			en="Payment already exists — returning prior result",
			ar="الدفعة موجودة مسبقاً — إرجاع النتيجة السابقة",
		)

	paid_amount = float(payload["paid_amount"])
	received_amount = float(payload.get("received_amount") or paid_amount)

	doc_data: dict = {
		"doctype": "Payment Entry",
		"payment_type": payment_type,  # "Pay" or "Receive"
		"company": payload["company"],
		"posting_date": payload.get("posting_date"),
		"party_type": payload["party_type"],
		"party": payload["party"],
		"paid_amount": paid_amount,
		"received_amount": received_amount,
		"mode_of_payment": payload["mode_of_payment"],
		"custom_mobile_local_id": local_id,
	}

	if payload.get("paid_from"):
		doc_data["paid_from"] = payload["paid_from"]
	if payload.get("paid_to"):
		doc_data["paid_to"] = payload["paid_to"]
	if payload.get("notes"):
		doc_data["remarks"] = payload["notes"]

	# Optional reference to an invoice/order.
	ref_doctype = payload.get("reference_doctype")
	ref_name = payload.get("reference_name")
	if ref_doctype and ref_name:
		doc_data["references"] = [
			{
				"reference_doctype": ref_doctype,
				"reference_name": ref_name,
				"allocated_amount": paid_amount,
			}
		]

	doc = frappe.get_doc(doc_data)

	# Payment Entry needs accounts set before insert. setup_party_account_field
	# + set_missing_values populates them from company defaults.
	try:
		doc.setup_party_account_field()
		doc.set_missing_values()
	except Exception:
		# If the helpers aren't available (older ERPNext), fall through — the
		# user can supply paid_from/paid_to explicitly.
		pass

	doc.insert(ignore_permissions=False)

	if frappe.has_permission("Payment Entry", "submit", doc=doc):
		try:
			doc.submit()
		except frappe.PermissionError:
			pass

	frappe.db.commit()

	return ok(
		{
			"name": doc.name,
			"doc_type": "Payment Entry",
			"status": doc.status or "Draft",
			"duplicate": False,
		},
		en="Payment Entry created",
		ar="تم إنشاء قيد الدفعة",
	)


@frappe.whitelist()
@mobile_endpoint
def create_payment_entry(**kwargs):
	# Outgoing — we pay a supplier.
	return _create_payment("Pay")


@frappe.whitelist()
@mobile_endpoint
def create_payment_receipt(**kwargs):
	# Incoming — we receive from a customer.
	return _create_payment("Receive")
