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
from frappe.utils import flt

from match_erp.api.mobile.envelope import fail, mobile_endpoint, ok, parse_body


def _get_exchange_rate(from_currency: str, to_currency: str, posting_date) -> float:
	"""Best-effort exchange-rate lookup for Payment Entry.

	Uses ERPNext's own `get_exchange_rate` helper when available, falling
	back to 1.0 — this is safe because the caller only invokes us when
	source and target currencies actually differ; in same-currency setups
	the rate must be 1.0 anyway.
	"""
	if not from_currency or not to_currency or from_currency == to_currency:
		return 1.0
	try:
		from erpnext.setup.utils import get_exchange_rate

		rate = get_exchange_rate(from_currency, to_currency, posting_date)
		return float(rate) if rate else 1.0
	except Exception:
		return 1.0


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

	# References to invoices/orders this payment settles. Two shapes are
	# accepted:
	#   1. A `references` list: [{reference_doctype, reference_name,
	#      allocated_amount}, ...] — the multi-invoice case.
	#   2. Legacy single ref: reference_doctype + reference_name (the whole
	#      paid_amount is allocated to it).
	# References to docs that don't exist (e.g. an invoice that failed to
	# submit upstream) are silently skipped so the payment still posts.
	references = payload.get("references")
	ref_rows: list[dict] = []
	if isinstance(references, list) and references:
		for ref in references:
			if not isinstance(ref, dict):
				continue
			r_dt = ref.get("reference_doctype") or "Sales Invoice"
			r_name = ref.get("reference_name")
			if not r_name or not frappe.db.exists(r_dt, r_name):
				continue
			alloc = flt(ref.get("allocated_amount")) or paid_amount
			ref_rows.append(
				{
					"reference_doctype": r_dt,
					"reference_name": r_name,
					"allocated_amount": alloc,
				}
			)
	else:
		ref_doctype = payload.get("reference_doctype")
		ref_name = payload.get("reference_name")
		if ref_doctype and ref_name and frappe.db.exists(ref_doctype, ref_name):
			ref_rows.append(
				{
					"reference_doctype": ref_doctype,
					"reference_name": ref_name,
					"allocated_amount": paid_amount,
				}
			)

	if ref_rows:
		doc_data["references"] = ref_rows

	# Resolve paid_from / paid_to from the Mode of Payment Account child
	# table when the client didn't send them explicitly. Without these,
	# ERPNext refuses to compute exchange rates or set account currencies.
	mop = payload["mode_of_payment"]
	company = payload["company"]
	mop_bank_account = frappe.db.get_value(
		"Mode of Payment Account",
		{"parent": mop, "company": company},
		"default_account",
	)
	if payment_type == "Receive":
		# Money comes IN — paid_to is the bank/cash account, paid_from is
		# the party receivable account (resolved by set_missing_values).
		doc_data.setdefault("paid_to", mop_bank_account)
	else:
		# Money goes OUT — paid_from is the bank/cash account.
		doc_data.setdefault("paid_from", mop_bank_account)

	doc = frappe.get_doc(doc_data)

	# Populate party account, currencies, and exchange rates from the
	# company defaults so the document validates cleanly. We have to call
	# these BEFORE insert because Payment Entry's own validate() reads
	# account_currency and exchange-rate fields.
	try:
		doc.setup_party_account_field()
	except Exception:
		pass
	try:
		doc.set_missing_values()
	except Exception:
		pass

	# Backstop: when set_missing_values can't auto-compute the exchange
	# rates (happens on some v15/v16 builds when accounts share the company
	# currency), force them to 1.0 explicitly. ERPNext rejects the doc
	# with "Target Exchange Rate required" otherwise.
	company_currency = frappe.db.get_value(
		"Company", company, "default_currency"
	)
	if not doc.get("paid_from_account_currency") and doc.get("paid_from"):
		doc.paid_from_account_currency = frappe.db.get_value(
			"Account", doc.paid_from, "account_currency"
		) or company_currency
	if not doc.get("paid_to_account_currency") and doc.get("paid_to"):
		doc.paid_to_account_currency = frappe.db.get_value(
			"Account", doc.paid_to, "account_currency"
		) or company_currency
	if not doc.get("source_exchange_rate"):
		# When source and company currency match → 1.0. Otherwise let
		# ERPNext's currency_exchange helper resolve a rate (best-effort).
		doc.source_exchange_rate = (
			1.0
			if doc.paid_from_account_currency == company_currency
			else _get_exchange_rate(
				doc.paid_from_account_currency, company_currency, doc.posting_date
			)
		)
	if not doc.get("target_exchange_rate"):
		doc.target_exchange_rate = (
			1.0
			if doc.paid_to_account_currency == company_currency
			else _get_exchange_rate(
				doc.paid_to_account_currency, company_currency, doc.posting_date
			)
		)
	# `base_paid_amount` / `base_received_amount` are derived from the
	# exchange rates — let ERPNext recompute them on validate.

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
