"""Company endpoints for Match ERP Mobile."""

from __future__ import annotations

import frappe

from match_erp.api.mobile.envelope import mobile_endpoint, ok, parse_body


@frappe.whitelist()
@mobile_endpoint
def get_companies():
	body = parse_body()
	limit = int(body.get("limit") or 20)
	if limit < 1:
		limit = 20
	if limit > 200:
		limit = 200

	rows = frappe.get_list(
		"Company",
		fields=[
			"name",
			"company_name",
			"default_currency",
			"default_letter_head",
			"country",
		],
		limit_page_length=limit,
		order_by="company_name asc",
	)

	# default_price_list lives on Selling Settings globally, not on Company,
	# but some deployments store it per-company via custom field. Fall back
	# to the global Selling Settings price list.
	global_price_list = frappe.db.get_single_value("Selling Settings", "selling_price_list")
	for r in rows:
		company_price_list = frappe.db.get_value(
			"Company", r["name"], "default_selling_price_list"
		) if frappe.db.has_column("Company", "default_selling_price_list") else None
		r["default_price_list"] = company_price_list or global_price_list
		r.pop("default_letter_head", None)

	return ok(rows, en="Companies loaded", ar="تم تحميل الشركات")
