"""Customer CRUD endpoints for Match ERP Mobile.

The Dist POS Profile decides whether create/edit is permitted. The mobile
UI hides the actions per profile, and we re-check here server-side so an
old build or tampered request can't bypass the configuration. Frappe's
own DocType permissions still apply on top via frappe.get_doc().
"""

from __future__ import annotations

import frappe

from match_erp.api.mobile.envelope import fail, mobile_endpoint, ok, parse_body
from match_erp.match_erp.doctype.dist_pos_profile.dist_pos_profile import is_allowed


@frappe.whitelist()
@mobile_endpoint
def create(**kwargs):
	if not is_allowed("allow_customer_create"):
		return fail(
			"Creating customers is not permitted by your profile.",
			"إنشاء العملاء غير مسموح به وفق ملفك.",
		)
	body = parse_body()
	if not body.get("customer_name"):
		return fail("customer_name is required", "اسم العميل مطلوب")
	body["doctype"] = "Customer"
	doc = frappe.get_doc(body)
	doc.insert(ignore_permissions=False)
	frappe.db.commit()
	return ok(doc.as_dict(), en="Customer created", ar="تم إنشاء العميل")


@frappe.whitelist()
@mobile_endpoint
def get(**kwargs):
	"""Fetch a single customer with a freshly-computed outstanding amount.

	The bulk sync endpoint also includes outstanding, but it's expensive to
	recompute for every row on every full sync. This single-row endpoint is
	called after the client pushes an invoice for a customer, so the local
	cache reflects the new balance immediately.
	"""
	body = parse_body()
	name = body.get("name")
	if not name:
		return fail("name is required", "اسم العميل مطلوب")

	row = frappe.db.get_value(
		"Customer",
		name,
		[
			"name",
			"customer_name",
			"customer_group",
			"territory",
			"mobile_no" if frappe.db.has_column("Customer", "mobile_no") else "name",
			"email_id" if frappe.db.has_column("Customer", "email_id") else "name",
			"disabled",
			"default_price_list",
			"default_currency",
			"modified",
		],
		as_dict=True,
	)
	if not row:
		return fail("Customer not found", "العميل غير موجود")

	# Strip the placeholder when mobile_no/email_id columns don't exist.
	if not frappe.db.has_column("Customer", "mobile_no"):
		row["mobile_no"] = None
	if not frappe.db.has_column("Customer", "email_id"):
		row["email_id"] = None

	# credit_limit is conditionally present.
	if frappe.db.has_column("Customer", "credit_limit"):
		row["credit_limit"] = frappe.db.get_value("Customer", name, "credit_limit") or 0
	else:
		row["credit_limit"] = 0

	outstanding = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(outstanding_amount), 0)
		FROM `tabSales Invoice`
		WHERE customer = %s AND docstatus = 1 AND outstanding_amount > 0
		""",
		(name,),
	)
	row["outstanding_amount"] = (
		float(outstanding[0][0]) if outstanding and outstanding[0] else 0.0
	)

	m = row.get("modified")
	if m is not None and hasattr(m, "isoformat"):
		row["modified"] = m.isoformat(sep=" ")

	return ok(row, en="Customer fetched", ar="تم جلب العميل")


@frappe.whitelist()
@mobile_endpoint
def get_balance(**kwargs):
	"""Live receivable balance for a customer — used when a voucher print
	format is configured to show the customer's balance.

	Returns the true ledger balance (invoices minus payments / credits)
	from GL Entry against the party, plus the company's default currency.
	A positive `balance` means the customer owes us.
	"""
	body = parse_body()
	name = body.get("name")
	if not name:
		return fail("name is required", "اسم العميل مطلوب")
	if not frappe.db.exists("Customer", name):
		return fail("Customer not found", "العميل غير موجود")

	company = body.get("company") or frappe.db.get_single_value(
		"Global Defaults", "default_company"
	)

	# True party balance from the GL: SUM(debit - credit) over all
	# non-cancelled entries for this customer. This nets invoices against
	# payments/credit notes — unlike the Sales-Invoice-only `outstanding`.
	filters = ["party_type = %s", "party = %s", "is_cancelled = 0"]
	args: list = ["Customer", name]
	if company:
		filters.append("company = %s")
		args.append(company)
	row = frappe.db.sql(
		f"""
		SELECT COALESCE(SUM(debit - credit), 0)
		FROM `tabGL Entry`
		WHERE {" AND ".join(filters)}
		""",
		tuple(args),
	)
	balance = float(row[0][0]) if row and row[0] and row[0][0] is not None else 0.0

	currency = frappe.db.get_value("Customer", name, "default_currency")
	if not currency and company:
		currency = frappe.db.get_value("Company", company, "default_currency")

	return ok(
		{
			"customer": name,
			"balance": balance,
			"currency": currency or "",
		},
		en="Balance fetched",
		ar="تم جلب الرصيد",
	)


@frappe.whitelist()
@mobile_endpoint
def get_ledger(**kwargs):
	"""Customer general ledger — online-only, pulled live from `tabGL Entry`.

	Payload:
	    {
	        "customer":   "<customer name>",     # required
	        "from_date":  "YYYY-MM-DD" | null,   # optional
	        "to_date":    "YYYY-MM-DD" | null,   # optional
	        "company":    "<company>" | null,    # optional (scopes by company)
	        "limit":      200                    # optional (max 1000)
	    }

	Returns rows in chronological order with a running balance computed
	server-side, so the mobile client doesn't need to recompute on every
	scroll:

	    {
	        "rows": [
	            {"posting_date", "voucher_type", "voucher_no",
	             "debit", "credit", "balance", "remarks", "account"},
	            ...
	        ],
	        "opening_balance": float,
	        "closing_balance": float,
	        "total_debit":     float,
	        "total_credit":    float,
	        "currency":        str | None
	    }

	GL Entry is the source of truth across ERPNext v15 + v16 — the schema
	hasn't changed between versions, so this query works on both without
	branching.
	"""
	body = parse_body()
	customer = body.get("customer")
	if not customer:
		return fail("customer is required", "العميل مطلوب")

	from_date = body.get("from_date") or None
	to_date   = body.get("to_date")   or None
	company   = body.get("company")   or None
	try:
		limit = int(body.get("limit") or 200)
	except (TypeError, ValueError):
		limit = 200
	if limit < 1:
		limit = 200
	if limit > 1000:
		limit = 1000

	# Confirm the customer exists — surfacing a clear error here is nicer
	# than returning an empty ledger silently.
	if not frappe.db.exists("Customer", customer):
		return fail("Customer not found", "العميل غير موجود")

	# --- Opening balance: sum debit-credit BEFORE from_date --------------
	opening = 0.0
	if from_date:
		opening_args: list = ["Customer", customer, from_date]
		opening_sql = """
			SELECT COALESCE(SUM(debit - credit), 0)
			FROM `tabGL Entry`
			WHERE party_type = %s AND party = %s
			  AND posting_date < %s
			  AND is_cancelled = 0
		"""
		if company:
			opening_sql += " AND company = %s"
			opening_args.append(company)
		row = frappe.db.sql(opening_sql, tuple(opening_args))
		opening = float(row[0][0]) if row and row[0] else 0.0

	# --- Period rows ------------------------------------------------------
	where_parts = ["party_type = %s", "party = %s", "is_cancelled = 0"]
	args: list = ["Customer", customer]
	if from_date:
		where_parts.append("posting_date >= %s")
		args.append(from_date)
	if to_date:
		where_parts.append("posting_date <= %s")
		args.append(to_date)
	if company:
		where_parts.append("company = %s")
		args.append(company)
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

	# --- Running balance + totals ----------------------------------------
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

	# Default currency from the customer's preferred currency, else company.
	currency = frappe.db.get_value("Customer", customer, "default_currency")
	if not currency and company:
		currency = frappe.db.get_value("Company", company, "default_currency")

	return ok(
		{
			"rows":            out_rows,
			"opening_balance": opening,
			"closing_balance": balance,
			"total_debit":     total_debit,
			"total_credit":    total_credit,
			"currency":        currency,
		},
		en="Customer ledger fetched",
		ar="تم جلب دفتر حساب العميل",
	)


@frappe.whitelist()
@mobile_endpoint
def update(**kwargs):
	if not is_allowed("allow_customer_edit"):
		return fail(
			"Editing customers is not permitted by your profile.",
			"تعديل العملاء غير مسموح به وفق ملفك.",
		)
	body = parse_body()
	name = body.get("name")
	data = body.get("data") or {}
	if not name:
		return fail("name is required", "اسم العميل مطلوب")
	if not isinstance(data, dict) or not data:
		return fail("data is required", "البيانات مطلوبة")

	doc = frappe.get_doc("Customer", name)
	doc.update(data)
	doc.save(ignore_permissions=False)
	frappe.db.commit()
	return ok(doc.as_dict(), en="Customer updated", ar="تم تحديث العميل")
