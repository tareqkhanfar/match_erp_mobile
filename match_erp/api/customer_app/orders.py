"""Sales orders / invoices / payments — scoped to the session's Customer.

Every endpoint here looks up `frappe.session.user` → Customer and filters
the response to that single customer. Cross-customer access is impossible
through this surface; an unlinked user gets an explicit "not linked"
error rather than an empty list.
"""

from __future__ import annotations

from typing import Any

import frappe

from match_erp.api.customer_app._session import require_session_customer
from match_erp.api.mobile.envelope import fail, mobile_endpoint, ok, parse_body
from match_erp.api.mobile._voucher import create_voucher


# ── List endpoints ───────────────────────────────────────────────────────────

def _list_for_customer(
	doctype: str,
	customer: str,
	body: dict,
	party_field: str = "customer",
) -> dict:
	"""Generic paginated list of `doctype` rows filtered by customer."""
	try:
		limit = int(body.get("limit") or 50)
	except (TypeError, ValueError):
		limit = 50
	limit = max(1, min(limit, 200))

	try:
		offset = int(body.get("offset") or 0)
	except (TypeError, ValueError):
		offset = 0
	offset = max(0, offset)

	status = body.get("status")
	filters: list = [[party_field, "=", customer]]
	if status:
		filters.append(["status", "=", status])

	fields = [
		"name",
		"posting_date" if doctype != "Sales Order" else "transaction_date",
		"status",
		"grand_total",
		"currency",
	]
	# Sales Order has delivery_date; Payment Entry has paid_amount instead
	# of grand_total. Branch the column list accordingly.
	if doctype == "Sales Order":
		fields.append("delivery_date")
	if doctype == "Payment Entry":
		fields = [
			"name", "posting_date", "status", "paid_amount", "received_amount",
			"paid_from_account_currency", "paid_to_account_currency",
			"mode_of_payment",
		]
	if doctype == "Sales Invoice":
		fields.append("outstanding_amount")
		fields.append("due_date")

	rows = frappe.get_list(
		doctype,
		filters=filters,
		fields=fields,
		order_by="modified desc, name desc",
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
	return {
		"items": rows,
		"has_more": has_more,
		"next_offset": offset + len(rows) if has_more else None,
	}


@frappe.whitelist()
@mobile_endpoint
def list_sales_orders(**kwargs):
	customer, err = require_session_customer()
	if err:
		return err
	body = parse_body()
	return ok(
		_list_for_customer("Sales Order", customer, body),
		en="Sales orders loaded",
		ar="تم تحميل أوامر البيع",
	)


@frappe.whitelist()
@mobile_endpoint
def list_sales_invoices(**kwargs):
	customer, err = require_session_customer()
	if err:
		return err
	body = parse_body()
	return ok(
		_list_for_customer("Sales Invoice", customer, body),
		en="Sales invoices loaded",
		ar="تم تحميل فواتير البيع",
	)


@frappe.whitelist()
@mobile_endpoint
def list_payments(**kwargs):
	customer, err = require_session_customer()
	if err:
		return err
	body = parse_body()
	# Payment Entry stores the party in `party` with `party_type=Customer`.
	body_with_filter = dict(body)
	try:
		limit = int(body.get("limit") or 50)
	except (TypeError, ValueError):
		limit = 50
	limit = max(1, min(limit, 200))
	try:
		offset = int(body.get("offset") or 0)
	except (TypeError, ValueError):
		offset = 0
	offset = max(0, offset)

	filters: list = [
		["party_type", "=", "Customer"],
		["party", "=", customer],
	]
	rows = frappe.get_list(
		"Payment Entry",
		filters=filters,
		fields=[
			"name", "posting_date", "status", "paid_amount", "received_amount",
			"mode_of_payment", "reference_no", "reference_date",
		],
		order_by="modified desc, name desc",
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
	_ = body_with_filter  # quiet unused
	return ok(
		{
			"items": rows,
			"has_more": has_more,
			"next_offset": offset + len(rows) if has_more else None,
		},
		en="Payments loaded",
		ar="تم تحميل الدفعات",
	)


# ── Detail endpoint (line items for one Sales Order) ─────────────────────────

@frappe.whitelist()
@mobile_endpoint
def get_sales_order(**kwargs):
	"""Full detail for one Sales Order — header + line items.

	Only returns when the SO belongs to the logged-in customer. Any other
	`name` value yields the same "not found" error so a malicious client
	can't enumerate other customers' order names.
	"""
	customer, err = require_session_customer()
	if err:
		return err
	body = parse_body()
	name = body.get("name")
	if not name:
		return fail("name is required", "اسم المستند مطلوب")

	row = frappe.db.get_value(
		"Sales Order",
		{"name": name, "customer": customer},
		[
			"name", "customer", "customer_name", "transaction_date",
			"delivery_date", "status", "currency", "grand_total",
			"net_total", "total_taxes_and_charges", "remarks",
		],
		as_dict=True,
	)
	if not row:
		return fail("Sales Order not found", "أمر البيع غير موجود")

	# Line items.
	items = frappe.get_list(
		"Sales Order Item",
		filters={"parent": name},
		fields=[
			"item_code", "item_name", "qty", "uom", "rate", "amount",
			"discount_percentage", "discount_amount", "image",
		],
		order_by="idx asc",
		ignore_permissions=True,  # parent already checked above
	)
	row["items"] = items
	for k, v in list(row.items()):
		if hasattr(v, "isoformat"):
			row[k] = v.isoformat()
	return ok(row, en="Sales order loaded", ar="تم تحميل أمر البيع")


# ── Create Sales Order (the cart-checkout path) ──────────────────────────────

@frappe.whitelist()
@mobile_endpoint
def create_sales_order(**kwargs):
	"""Customer-app cart checkout. Forces `customer` to the session's
	linked Customer so a malicious payload can't post on someone else's
	behalf."""
	customer, err = require_session_customer()
	if err:
		return err
	payload = parse_body()
	payload["party"] = customer  # ignore any client-supplied party
	payload["customer"] = customer
	# Default the company when the client doesn't know it. Customers
	# shouldn't have to think about which company to bill against.
	if not payload.get("company"):
		payload["company"] = frappe.db.get_single_value(
			"Global Defaults", "default_company"
		) or frappe.db.get_value("Company", {}, "name", order_by="name asc")
	return create_voucher("Sales Order", payload)


# ── General Ledger (party-scoped) ────────────────────────────────────────────

@frappe.whitelist()
@mobile_endpoint
def get_ledger(**kwargs):
	"""Customer general ledger — always scoped to the session customer.

	Same shape as `customer.get_ledger` in the mobile API but the customer
	is implicit (taken from the session), so the client can't request
	someone else's ledger.
	"""
	customer, err = require_session_customer()
	if err:
		return err
	body = parse_body()
	from_date = body.get("from_date") or None
	to_date   = body.get("to_date")   or None
	try:
		limit = int(body.get("limit") or 200)
	except (TypeError, ValueError):
		limit = 200
	limit = max(1, min(limit, 1000))

	opening = 0.0
	if from_date:
		row = frappe.db.sql(
			"""
			SELECT COALESCE(SUM(debit - credit), 0)
			FROM `tabGL Entry`
			WHERE party_type = %s AND party = %s
			  AND posting_date < %s
			  AND is_cancelled = 0
			""",
			("Customer", customer, from_date),
		)
		opening = float(row[0][0]) if row and row[0] else 0.0

	where_parts = ["party_type = %s", "party = %s", "is_cancelled = 0"]
	args: list[Any] = ["Customer", customer]
	if from_date:
		where_parts.append("posting_date >= %s")
		args.append(from_date)
	if to_date:
		where_parts.append("posting_date <= %s")
		args.append(to_date)
	args.append(limit)

	rows = frappe.db.sql(
		f"""
		SELECT
		    posting_date,
		    voucher_type,
		    voucher_no,
		    account,
		    debit,
		    credit,
		    remarks
		FROM `tabGL Entry`
		WHERE {" AND ".join(where_parts)}
		ORDER BY posting_date ASC, creation ASC
		LIMIT %s
		""",
		tuple(args),
		as_dict=True,
	)

	balance = opening
	total_debit = 0.0
	total_credit = 0.0
	out_rows: list[dict] = []
	for r in rows:
		debit  = float(r.get("debit")  or 0)
		credit = float(r.get("credit") or 0)
		balance += (debit - credit)
		total_debit  += debit
		total_credit += credit
		pd = r.get("posting_date")
		out_rows.append({
			"posting_date":  pd.isoformat() if hasattr(pd, "isoformat") else str(pd or ""),
			"voucher_type":  r.get("voucher_type") or "",
			"voucher_no":    r.get("voucher_no")   or "",
			"account":       r.get("account")      or "",
			"debit":         debit,
			"credit":        credit,
			"balance":       balance,
			"remarks":       r.get("remarks") or "",
		})

	currency = frappe.db.get_value(
		"Customer", customer, "default_currency"
	)

	return ok(
		{
			"rows":            out_rows,
			"opening_balance": opening,
			"closing_balance": balance,
			"total_debit":     total_debit,
			"total_credit":    total_credit,
			"currency":        currency,
		},
		en="Ledger loaded",
		ar="تم تحميل دفتر الحساب",
	)
