"""Sales analytics endpoints for Match ERP Mobile.

Two online reports the field team actually uses:

    sales_by_item     → what sells, ranked by revenue
    sales_by_customer → who buys, ranked by revenue

Both aggregate SUBMITTED Sales Invoices (docstatus = 1) and exclude returns
by default, so the numbers match ERPNext's own sales figures. Filters and
pagination follow the same contract as the rest of the mobile API.

All SQL is parameterised — no user input is ever interpolated into a query.
"""

from __future__ import annotations

import math

import frappe
from frappe.utils import flt

from match_erp.api.mobile.envelope import mobile_endpoint, ok, parse_body

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200

# Columns a client may sort by, mapped to the SQL expression. Whitelisted so
# `order_by` can never be used for injection.
_SORT_ITEM = {
	"revenue": "revenue",
	"qty": "total_qty",
	"item_code": "sii.item_code",
	"invoices": "invoices",
}
_SORT_CUSTOMER = {
	"revenue": "revenue",
	"invoices": "invoices",
	"customer": "si.customer",
	"last_sale": "last_sale",
}


def _pagination(body: dict) -> tuple[int, int]:
	try:
		page_size = int(body.get("page_size") or DEFAULT_PAGE_SIZE)
	except (TypeError, ValueError):
		page_size = DEFAULT_PAGE_SIZE
	page_size = max(1, min(page_size, MAX_PAGE_SIZE))
	try:
		page = int(body.get("page") or 1)
	except (TypeError, ValueError):
		page = 1
	return max(1, page), page_size


def _common_where(body: dict) -> tuple[list[str], list]:
	"""WHERE fragments + params shared by both reports (on alias `si`)."""
	where = ["si.docstatus = 1"]
	params: list = []

	# Returns/credit notes are excluded unless explicitly asked for.
	if not body.get("include_returns"):
		where.append("IFNULL(si.is_return, 0) = 0")

	if body.get("company"):
		where.append("si.company = %s")
		params.append(body["company"])
	if body.get("from_date"):
		where.append("si.posting_date >= %s")
		params.append(body["from_date"])
	if body.get("to_date"):
		where.append("si.posting_date <= %s")
		params.append(body["to_date"])
	if body.get("customer"):
		where.append("si.customer = %s")
		params.append(body["customer"])
	# Scope to a POS profile when the column exists and one was supplied.
	if body.get("dist_pos_profile") and frappe.db.has_column(
		"Sales Invoice", "custom_dist_pos_profile"
	):
		where.append("si.custom_dist_pos_profile = %s")
		params.append(body["dist_pos_profile"])
	return where, params


def _paged(page: int, page_size: int, total: int, rows: list, extra: dict) -> dict:
	out = {
		"page": page,
		"page_size": page_size,
		"total_records": total,
		"total_pages": math.ceil(total / page_size) if total else 0,
		"data": rows,
	}
	out.update(extra)
	return out


@frappe.whitelist()
@mobile_endpoint
def sales_by_item(**kwargs):
	"""Revenue and quantity sold per item.

	Filters: company, from_date, to_date, customer, item_group, item_code,
	search, dist_pos_profile, include_returns.
	Sort: sort_by = revenue (default) | qty | item_code | invoices, and
	sort_dir = desc (default) | asc.
	"""
	body = parse_body()
	page, page_size = _pagination(body)

	where, params = _common_where(body)
	where.append("sii.docstatus = 1")

	if body.get("item_code"):
		where.append("sii.item_code = %s")
		params.append(body["item_code"])
	if body.get("item_group"):
		where.append("sii.item_group = %s")
		params.append(body["item_group"])
	if body.get("search"):
		where.append("(sii.item_code LIKE %s OR sii.item_name LIKE %s)")
		term = f"%{body['search']}%"
		params.extend([term, term])

	where_sql = " AND ".join(where)

	sort_by = _SORT_ITEM.get(str(body.get("sort_by") or "revenue"), "revenue")
	sort_dir = "ASC" if str(body.get("sort_dir") or "desc").lower() == "asc" else "DESC"

	# Total distinct items matching the filters (for the pager).
	total = frappe.db.sql(
		f"""
		SELECT COUNT(*) FROM (
			SELECT sii.item_code
			FROM `tabSales Invoice Item` sii
			INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
			WHERE {where_sql}
			GROUP BY sii.item_code
		) t
		""",
		tuple(params),
	)[0][0]

	rows = frappe.db.sql(
		f"""
		SELECT
			sii.item_code                         AS item_code,
			MAX(sii.item_name)                    AS item_name,
			MAX(sii.item_group)                   AS item_group,
			MAX(sii.stock_uom)                    AS stock_uom,
			SUM(sii.stock_qty)                    AS total_qty,
			SUM(sii.base_net_amount)              AS revenue,
			COUNT(DISTINCT si.name)               AS invoices,
			AVG(sii.base_net_rate)                AS avg_rate,
			MAX(si.posting_date)                  AS last_sale
		FROM `tabSales Invoice Item` sii
		INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
		WHERE {where_sql}
		GROUP BY sii.item_code
		ORDER BY {sort_by} {sort_dir}
		LIMIT %s OFFSET %s
		""",
		tuple(params) + (page_size, (page - 1) * page_size),
		as_dict=True,
	)

	# Grand totals across the WHOLE filtered set, not just this page.
	grand = frappe.db.sql(
		f"""
		SELECT SUM(sii.base_net_amount) AS revenue, SUM(sii.stock_qty) AS qty
		FROM `tabSales Invoice Item` sii
		INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
		WHERE {where_sql}
		""",
		tuple(params),
		as_dict=True,
	)

	for r in rows:
		r["total_qty"] = flt(r.get("total_qty"))
		r["revenue"] = flt(r.get("revenue"))
		r["avg_rate"] = flt(r.get("avg_rate"))
		r["last_sale"] = str(r.get("last_sale") or "")

	return ok(
		_paged(
			page,
			page_size,
			total,
			rows,
			{
				"totals": {
					"revenue": flt(grand[0].get("revenue") if grand else 0),
					"qty": flt(grand[0].get("qty") if grand else 0),
				}
			},
		),
		en="Sales by item fetched",
		ar="تم جلب المبيعات حسب الصنف",
	)


@frappe.whitelist()
@mobile_endpoint
def sales_by_customer(**kwargs):
	"""Revenue, invoice count and outstanding per customer.

	Filters: company, from_date, to_date, customer, customer_group, search,
	dist_pos_profile, include_returns.
	Sort: sort_by = revenue (default) | invoices | customer | last_sale.
	"""
	body = parse_body()
	page, page_size = _pagination(body)

	where, params = _common_where(body)

	if body.get("customer_group"):
		where.append("si.customer_group = %s")
		params.append(body["customer_group"])
	if body.get("search"):
		where.append("(si.customer LIKE %s OR si.customer_name LIKE %s)")
		term = f"%{body['search']}%"
		params.extend([term, term])

	where_sql = " AND ".join(where)

	sort_by = _SORT_CUSTOMER.get(str(body.get("sort_by") or "revenue"), "revenue")
	sort_dir = "ASC" if str(body.get("sort_dir") or "desc").lower() == "asc" else "DESC"

	total = frappe.db.sql(
		f"""
		SELECT COUNT(*) FROM (
			SELECT si.customer FROM `tabSales Invoice` si
			WHERE {where_sql} GROUP BY si.customer
		) t
		""",
		tuple(params),
	)[0][0]

	rows = frappe.db.sql(
		f"""
		SELECT
			si.customer                       AS customer,
			MAX(si.customer_name)             AS customer_name,
			MAX(si.customer_group)            AS customer_group,
			MAX(si.territory)                 AS territory,
			SUM(si.base_net_total)            AS revenue,
			SUM(si.base_grand_total)          AS grand_total,
			SUM(si.outstanding_amount)        AS outstanding,
			COUNT(si.name)                    AS invoices,
			MAX(si.posting_date)              AS last_sale
		FROM `tabSales Invoice` si
		WHERE {where_sql}
		GROUP BY si.customer
		ORDER BY {sort_by} {sort_dir}
		LIMIT %s OFFSET %s
		""",
		tuple(params) + (page_size, (page - 1) * page_size),
		as_dict=True,
	)

	grand = frappe.db.sql(
		f"""
		SELECT SUM(si.base_net_total) AS revenue,
		       SUM(si.outstanding_amount) AS outstanding,
		       COUNT(si.name) AS invoices
		FROM `tabSales Invoice` si
		WHERE {where_sql}
		""",
		tuple(params),
		as_dict=True,
	)

	for r in rows:
		for f in ("revenue", "grand_total", "outstanding"):
			r[f] = flt(r.get(f))
		r["last_sale"] = str(r.get("last_sale") or "")

	return ok(
		_paged(
			page,
			page_size,
			total,
			rows,
			{
				"totals": {
					"revenue": flt(grand[0].get("revenue") if grand else 0),
					"outstanding": flt(grand[0].get("outstanding") if grand else 0),
					"invoices": int(grand[0].get("invoices") or 0) if grand else 0,
				}
			},
		),
		en="Sales by customer fetched",
		ar="تم جلب المبيعات حسب العميل",
	)
