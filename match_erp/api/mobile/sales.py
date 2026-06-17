"""Sales document endpoints for Match ERP Mobile.

All three endpoints share the generic voucher payload (see `_voucher.py`)
and are idempotent on `custom_mobile_local_id`.

- create_sales_order    → Sales Order
- create_sales_invoice  → Sales Invoice (supports is_paid + mode_of_payment)
- create_sales_return   → Sales Invoice with is_return=1 + return_against
"""

from __future__ import annotations

import frappe
from frappe.utils import flt

from match_erp.api.mobile._voucher import create_voucher
from match_erp.api.mobile.envelope import fail, mobile_endpoint, ok, parse_body


@frappe.whitelist()
@mobile_endpoint
def create_sales_order(**kwargs):
	return create_voucher("Sales Order", parse_body())


@frappe.whitelist()
@mobile_endpoint
def create_sales_invoice(**kwargs):
	return create_voucher("Sales Invoice", parse_body())


@frappe.whitelist()
@mobile_endpoint
def create_sales_return(**kwargs):
	# A sales return is a Sales Invoice with is_return=1 and negative qty,
	# submitted against the original invoice via return_against.
	return create_voucher("Sales Invoice", parse_body(), is_return=True)


# ---------------------------------------------------------------------------
# Read-only invoice browsing — used by the mobile Payment Entry editor to
# pick which invoices a payment settles. Returns only submitted (docstatus=1)
# Sales Invoices so a payment can never reference a draft.
# ---------------------------------------------------------------------------
@frappe.whitelist()
@mobile_endpoint
def list_customer_invoices(**kwargs):
	"""List submitted Sales Invoices for a customer.

	Body: customer (required), company (optional), only_outstanding (optional,
	default 1 → only invoices with a remaining balance), limit (default 100).
	"""
	body = parse_body()
	customer = body.get("customer")
	if not customer:
		return fail("customer is required", "العميل مطلوب")

	filters = {"customer": customer, "docstatus": 1}
	if body.get("company"):
		filters["company"] = body["company"]
	# By default only show invoices that still owe money — that's what a
	# payment would settle. Pass only_outstanding=0 to list everything.
	only_outstanding = body.get("only_outstanding")
	if only_outstanding is None or str(only_outstanding) in ("1", "true", "True"):
		filters["outstanding_amount"] = [">", 0]

	try:
		limit = int(body.get("limit") or 100)
	except (TypeError, ValueError):
		limit = 100

	rows = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=[
			"name",
			"posting_date",
			"customer",
			"customer_name",
			"currency",
			"grand_total",
			"outstanding_amount",
			"status",
			"company",
			"modified",
		],
		order_by="posting_date desc, modified desc",
		limit_page_length=limit,
	)
	return ok(
		{"invoices": rows},
		en="Customer invoices listed",
		ar="تم سرد فواتير العميل",
	)


@frappe.whitelist()
@mobile_endpoint
def get_invoice(**kwargs):
	"""Return a single Sales Invoice with its line items for read-only display.

	Body: name (required).
	"""
	body = parse_body()
	name = body.get("name")
	if not name:
		return fail("name is required", "اسم الفاتورة مطلوب")

	if not frappe.db.exists("Sales Invoice", name):
		return fail("Invoice not found", "الفاتورة غير موجودة")

	doc = frappe.get_doc("Sales Invoice", name)
	items = [
		{
			"item_code": it.item_code,
			"item_name": it.item_name,
			"uom": it.uom,
			"qty": flt(it.qty),
			"rate": flt(it.rate),
			"amount": flt(it.amount),
			"discount_percentage": flt(it.discount_percentage),
		}
		for it in doc.items
	]
	data = {
		"name": doc.name,
		"posting_date": str(doc.posting_date or ""),
		"customer": doc.customer,
		"customer_name": doc.customer_name,
		"currency": doc.currency,
		"company": doc.company,
		"status": doc.status,
		"docstatus": doc.docstatus,
		"net_total": flt(doc.net_total),
		"total_taxes_and_charges": flt(doc.total_taxes_and_charges),
		"discount_amount": flt(doc.discount_amount),
		"grand_total": flt(doc.grand_total),
		"paid_amount": flt(doc.grand_total) - flt(doc.outstanding_amount),
		"outstanding_amount": flt(doc.outstanding_amount),
		"items": items,
	}
	return ok(data, en="Invoice fetched", ar="تم جلب الفاتورة")
