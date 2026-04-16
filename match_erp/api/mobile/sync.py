"""Catalog sync endpoints (cursor-paginated) for Match ERP Mobile.

All sync endpoints share the same contract:

    Input:  { "modified_after": "ISO-8601" | null, "limit": int,
              "price_list": str | null, "warehouse": str | null }
    Output (data):
            { "items": [...], "has_more": bool,
              "next_cursor": "ISO-8601" | null }

Rows are ordered by (modified ASC, name ASC) for a stable tie-break. Disabled
rows are included so the client can honor the flag locally after a deletion
or disable on the server.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import get_datetime

from match_erp.api.mobile.envelope import mobile_endpoint, ok, parse_body


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


def _parse_sync_args() -> tuple[str | None, int, dict]:
	body = parse_body()
	modified_after = body.get("modified_after")
	if modified_after in ("", "null", None):
		modified_after = None
	else:
		# Validate early — frappe.utils.get_datetime raises on garbage.
		try:
			get_datetime(modified_after)
		except Exception:
			modified_after = None

	limit = int(body.get("limit") or DEFAULT_LIMIT)
	if limit < 1:
		limit = DEFAULT_LIMIT
	if limit > MAX_LIMIT:
		limit = MAX_LIMIT

	return modified_after, limit, body


def _fetch(
	doctype: str,
	fields: list[str],
	modified_after: str | None,
	limit: int,
	extra_filters: list | None = None,
) -> tuple[list[dict], bool, str | None]:
	"""Cursor-paginated list fetch.

	We pull `limit + 1` rows to cheaply detect whether more exist, then trim.
	Uses frappe.get_list so DocType permissions apply.
	"""
	filters: list = list(extra_filters or [])
	if modified_after:
		filters.append(["modified", ">", modified_after])

	rows = frappe.get_list(
		doctype,
		filters=filters,
		fields=fields,
		order_by="modified asc, name asc",
		limit_page_length=limit + 1,
		ignore_permissions=False,
	)

	has_more = len(rows) > limit
	if has_more:
		rows = rows[:limit]

	next_cursor = None
	if rows:
		# Serialize the `modified` timestamp as ISO-8601 string.
		last_modified = rows[-1].get("modified")
		if last_modified is not None:
			next_cursor = (
				last_modified.isoformat(sep=" ") if hasattr(last_modified, "isoformat") else str(last_modified)
			)

	# Convert datetime fields to strings for JSON serialization safety.
	for r in rows:
		m = r.get("modified")
		if m is not None and hasattr(m, "isoformat"):
			r["modified"] = m.isoformat(sep=" ")

	return rows, has_more, next_cursor


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------
@frappe.whitelist()
@mobile_endpoint
def get_customers(**kwargs):
	modified_after, limit, _body = _parse_sync_args()

	fields = [
		"name",
		"customer_name",
		"customer_group",
		"territory",
		"mobile_no",
		"email_id",
		"credit_limit",  # not always present on v15 Customer doc itself — guarded below
		"disabled",
		"default_price_list",
		"default_currency",
		"modified",
	]

	# Some ERPNext versions removed `credit_limit` from Customer in favor of
	# the `Customer Credit Limit` child table. Guard the field.
	has_credit_limit_col = frappe.db.has_column("Customer", "credit_limit")
	if not has_credit_limit_col:
		fields.remove("credit_limit")
	has_email_col = frappe.db.has_column("Customer", "email_id")
	if not has_email_col:
		fields.remove("email_id")
	has_mobile_col = frappe.db.has_column("Customer", "mobile_no")
	if not has_mobile_col:
		fields.remove("mobile_no")

	rows, has_more, next_cursor = _fetch("Customer", fields, modified_after, limit)

	# Fill missing / computed fields.
	for r in rows:
		if "credit_limit" not in r:
			r["credit_limit"] = 0
		if "email_id" not in r:
			r["email_id"] = None
		if "mobile_no" not in r:
			r["mobile_no"] = None
		# outstanding_amount: sum unpaid Sales Invoice outstanding for this customer.
		r["outstanding_amount"] = _customer_outstanding(r["name"])

	return ok(
		{"items": rows, "has_more": has_more, "next_cursor": next_cursor},
		en="Customers synced",
		ar="تمت مزامنة العملاء",
	)


def _customer_outstanding(customer: str) -> float:
	val = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(outstanding_amount), 0)
		FROM `tabSales Invoice`
		WHERE customer = %s AND docstatus = 1 AND outstanding_amount > 0
		""",
		(customer,),
	)
	return float(val[0][0]) if val and val[0] and val[0][0] is not None else 0.0


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------
@frappe.whitelist()
@mobile_endpoint
def get_items(**kwargs):
	modified_after, limit, body = _parse_sync_args()
	price_list: str | None = body.get("price_list") or None
	warehouse: str | None = body.get("warehouse") or None

	fields = [
		"name",
		"item_code",
		"item_name",
		"item_group",
		"stock_uom",
		"description",
		"image",
		"standard_rate",
		"has_batch_no",
		"has_serial_no",
		"disabled",
		"modified",
	]

	rows, has_more, next_cursor = _fetch("Item", fields, modified_after, limit)

	if not rows:
		return ok(
			{"items": rows, "has_more": has_more, "next_cursor": next_cursor},
			en="Items synced",
			ar="تمت مزامنة الأصناف",
		)

	item_codes = [r["item_code"] for r in rows]

	# --- price_list_rate ------------------------------------------------------
	price_map: dict[str, float] = {}
	if price_list:
		price_rows = frappe.db.sql(
			"""
			SELECT item_code, price_list_rate
			FROM `tabItem Price`
			WHERE price_list = %s
			  AND item_code IN %s
			  AND (valid_from IS NULL OR valid_from <= CURDATE())
			  AND (valid_upto IS NULL OR valid_upto >= CURDATE())
			ORDER BY valid_from DESC
			""",
			(price_list, tuple(item_codes)),
			as_dict=True,
		)
		# If multiple valid rows, first wins (ORDER BY valid_from DESC).
		for p in price_rows:
			price_map.setdefault(p["item_code"], float(p["price_list_rate"] or 0))

	# --- actual_qty -----------------------------------------------------------
	qty_map: dict[str, float] = {}
	if warehouse:
		qty_rows = frappe.db.sql(
			"""
			SELECT item_code, COALESCE(SUM(actual_qty), 0) AS qty
			FROM `tabBin`
			WHERE warehouse = %s AND item_code IN %s
			GROUP BY item_code
			""",
			(warehouse, tuple(item_codes)),
			as_dict=True,
		)
	else:
		qty_rows = frappe.db.sql(
			"""
			SELECT item_code, COALESCE(SUM(actual_qty), 0) AS qty
			FROM `tabBin`
			WHERE item_code IN %s
			GROUP BY item_code
			""",
			(tuple(item_codes),),
			as_dict=True,
		)
	for q in qty_rows:
		qty_map[q["item_code"]] = float(q["qty"] or 0)

	for r in rows:
		code = r["item_code"]
		r["price_list_rate"] = price_map.get(code, 0.0)
		r["actual_qty"] = qty_map.get(code, 0.0)

	return ok(
		{"items": rows, "has_more": has_more, "next_cursor": next_cursor},
		en="Items synced",
		ar="تمت مزامنة الأصناف",
	)


# ---------------------------------------------------------------------------
# Item Barcodes
# ---------------------------------------------------------------------------
@frappe.whitelist()
@mobile_endpoint
def get_item_barcodes(**kwargs):
	modified_after, limit, _body = _parse_sync_args()

	where = ""
	params: list[Any] = []
	if modified_after:
		where = "WHERE modified > %s"
		params.append(modified_after)

	params.append(limit + 1)

	sql = f"""
		SELECT name, parent AS item_code, barcode, uom, modified
		FROM `tabItem Barcode`
		{where}
		ORDER BY modified ASC, name ASC
		LIMIT %s
	"""

	rows = frappe.db.sql(sql, tuple(params), as_dict=True)

	has_more = len(rows) > limit
	if has_more:
		rows = rows[:limit]

	next_cursor = None
	if rows:
		last_modified = rows[-1].get("modified")
		if last_modified is not None:
			next_cursor = (
				last_modified.isoformat(sep=" ")
				if hasattr(last_modified, "isoformat")
				else str(last_modified)
			)
		for r in rows:
			m = r.get("modified")
			if m is not None and hasattr(m, "isoformat"):
				r["modified"] = m.isoformat(sep=" ")

	return ok(
		{"items": rows, "has_more": has_more, "next_cursor": next_cursor},
		en="Barcodes synced",
		ar="تمت مزامنة الباركود",
	)


# ---------------------------------------------------------------------------
# UOMs
# ---------------------------------------------------------------------------
@frappe.whitelist()
@mobile_endpoint
def get_uoms(**kwargs):
	modified_after, limit, _body = _parse_sync_args()
	fields = ["name", "uom_name", "enabled", "modified"]
	rows, has_more, next_cursor = _fetch("UOM", fields, modified_after, limit)
	return ok(
		{"items": rows, "has_more": has_more, "next_cursor": next_cursor},
		en="UOMs synced",
		ar="تمت مزامنة وحدات القياس",
	)


# ---------------------------------------------------------------------------
# Price Lists
# ---------------------------------------------------------------------------
@frappe.whitelist()
@mobile_endpoint
def get_price_lists(**kwargs):
	modified_after, limit, _body = _parse_sync_args()
	fields = [
		"name",
		"price_list_name",
		"currency",
		"enabled",
		"buying",
		"selling",
		"modified",
	]
	rows, has_more, next_cursor = _fetch("Price List", fields, modified_after, limit)
	return ok(
		{"items": rows, "has_more": has_more, "next_cursor": next_cursor},
		en="Price lists synced",
		ar="تمت مزامنة قوائم الأسعار",
	)
