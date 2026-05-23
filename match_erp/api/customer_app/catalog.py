"""Read-only catalog for the customer app.

We reuse the distributor app's catalog endpoints conceptually but expose
slim variants here that:
  - skip warehouse-scoped quantities (customers don't need stock visibility),
  - apply the customer's own price list when no override is sent,
  - resolve `frappe.session.user` → Customer to enforce the auth boundary.

Compatible with ERPNext v15 + v16.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import get_url

from match_erp.api.customer_app._session import require_session_customer
from match_erp.api.mobile.envelope import mobile_endpoint, ok, parse_body


DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


def _parse_args() -> tuple[str | None, int, dict]:
	body = parse_body()
	modified_after = body.get("modified_after")
	if modified_after in ("", "null", None):
		modified_after = None
	limit = int(body.get("limit") or DEFAULT_LIMIT)
	if limit < 1:
		limit = DEFAULT_LIMIT
	if limit > MAX_LIMIT:
		limit = MAX_LIMIT
	return modified_after, limit, body


def _absolutize(file_url: str | None) -> str | None:
	if not file_url:
		return None
	if file_url.startswith(("http://", "https://")):
		return file_url
	try:
		base = get_url().rstrip("/")
	except Exception:
		return file_url
	if not file_url.startswith("/"):
		file_url = "/" + file_url
	return f"{base}{file_url}"


def _resolve_price_list(customer: str, override: str | None) -> str | None:
	if override:
		return override
	return frappe.db.get_value("Customer", customer, "default_price_list")


@frappe.whitelist()
@mobile_endpoint
def get_items(**kwargs):
	customer, err = require_session_customer()
	if err:
		return err

	modified_after, limit, body = _parse_args()
	price_list = _resolve_price_list(customer, body.get("price_list"))

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
	filters: list = [["disabled", "=", 0]]
	if modified_after:
		filters.append(["modified", ">", modified_after])

	rows = frappe.get_list(
		"Item",
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
		last = rows[-1].get("modified")
		if last is not None:
			next_cursor = (
				last.isoformat(sep=" ") if hasattr(last, "isoformat") else str(last)
			)
		for r in rows:
			m = r.get("modified")
			if m is not None and hasattr(m, "isoformat"):
				r["modified"] = m.isoformat(sep=" ")

	# Stock-UOM price for the chosen price list.
	price_map: dict[str, float] = {}
	if price_list and rows:
		codes = [r["item_code"] for r in rows]
		stock_uoms = {r["item_code"]: r.get("stock_uom") for r in rows}
		distinct_uoms = {u for u in stock_uoms.values() if u}
		uom_filter = ""
		uom_args: list[Any] = []
		if distinct_uoms:
			uom_filter = "AND (uom IS NULL OR uom = '' OR uom IN %s)"
			uom_args.append(tuple(distinct_uoms))
		price_rows = frappe.db.sql(
			f"""
			SELECT item_code, uom, price_list_rate
			FROM `tabItem Price`
			WHERE price_list = %s
			  AND item_code IN %s
			  {uom_filter}
			  AND (valid_from IS NULL OR valid_from <= CURDATE())
			  AND (valid_upto IS NULL OR valid_upto >= CURDATE())
			ORDER BY valid_from DESC
			""",
			(price_list, tuple(codes), *uom_args),
			as_dict=True,
		)
		for p in price_rows:
			code = p["item_code"]
			rate = float(p["price_list_rate"] or 0)
			row_uom = p.get("uom") or ""
			stock_uom = stock_uoms.get(code) or ""
			if code in price_map:
				continue
			if row_uom and stock_uom and row_uom == stock_uom:
				price_map[code] = rate
			elif not row_uom:
				price_map.setdefault(code, rate)

	for r in rows:
		r["price_list_rate"] = price_map.get(r["item_code"], 0.0)
		r["actual_qty"] = 0.0  # not surfaced in the customer app
		if r.get("image"):
			r["image"] = _absolutize(r["image"])

	return ok(
		{
			"items": rows,
			"has_more": has_more,
			"next_cursor": next_cursor,
			"price_list": price_list,
		},
		en="Items synced",
		ar="تمت مزامنة الأصناف",
	)


@frappe.whitelist()
@mobile_endpoint
def get_item_groups(**kwargs):
	_customer, err = require_session_customer()
	if err:
		return err

	modified_after, limit, _body = _parse_args()
	fields = [
		"name",
		"item_group_name",
		"parent_item_group",
		"is_group",
		"image",
		"modified",
	]
	filters: list = []
	if modified_after:
		filters.append(["modified", ">", modified_after])

	rows = frappe.get_list(
		"Item Group",
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
		last = rows[-1].get("modified")
		if last is not None:
			next_cursor = (
				last.isoformat(sep=" ") if hasattr(last, "isoformat") else str(last)
			)
		for r in rows:
			m = r.get("modified")
			if m is not None and hasattr(m, "isoformat"):
				r["modified"] = m.isoformat(sep=" ")
			if r.get("image"):
				r["image"] = _absolutize(r["image"])

	return ok(
		{"items": rows, "has_more": has_more, "next_cursor": next_cursor},
		en="Item groups synced",
		ar="تمت مزامنة مجموعات الأصناف",
	)


@frappe.whitelist()
@mobile_endpoint
def get_item_barcodes(**kwargs):
	_customer, err = require_session_customer()
	if err:
		return err

	modified_after, limit, _body = _parse_args()
	where = ""
	params: list[Any] = []
	if modified_after:
		where = "WHERE modified > %s"
		params.append(modified_after)
	params.append(limit + 1)
	rows = frappe.db.sql(
		f"""
		SELECT name, parent AS item_code, barcode, uom, modified
		FROM `tabItem Barcode`
		{where}
		ORDER BY modified ASC, name ASC
		LIMIT %s
		""",
		tuple(params),
		as_dict=True,
	)
	has_more = len(rows) > limit
	if has_more:
		rows = rows[:limit]
	next_cursor = None
	if rows:
		last = rows[-1].get("modified")
		if last is not None:
			next_cursor = (
				last.isoformat(sep=" ") if hasattr(last, "isoformat") else str(last)
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


@frappe.whitelist()
@mobile_endpoint
def get_item_uom_conversions(**kwargs):
	customer, err = require_session_customer()
	if err:
		return err

	modified_after, limit, body = _parse_args()
	price_list = _resolve_price_list(customer, body.get("price_list"))

	where = ""
	params: list[Any] = []
	if modified_after:
		where = "WHERE modified > %s"
		params.append(modified_after)
	params.append(limit + 1)
	rows = frappe.db.sql(
		f"""
		SELECT name, parent AS item_code, uom, conversion_factor, modified
		FROM `tabUOM Conversion Detail`
		{where}
		ORDER BY modified ASC, name ASC
		LIMIT %s
		""",
		tuple(params),
		as_dict=True,
	)
	has_more = len(rows) > limit
	if has_more:
		rows = rows[:limit]
	next_cursor = None
	if rows:
		last = rows[-1].get("modified")
		if last is not None:
			next_cursor = (
				last.isoformat(sep=" ") if hasattr(last, "isoformat") else str(last)
			)
		for r in rows:
			m = r.get("modified")
			if m is not None and hasattr(m, "isoformat"):
				r["modified"] = m.isoformat(sep=" ")
			if r.get("conversion_factor") is not None:
				r["conversion_factor"] = float(r["conversion_factor"])

	if price_list and rows:
		item_codes = list({r["item_code"] for r in rows if r.get("item_code")})
		uoms = list({r["uom"] for r in rows if r.get("uom")})
		price_map: dict[tuple[str, str], float] = {}
		if item_codes and uoms:
			price_rows = frappe.db.sql(
				"""
				SELECT item_code, uom, price_list_rate
				FROM `tabItem Price`
				WHERE price_list = %s
				  AND item_code IN %s
				  AND uom IN %s
				  AND (valid_from IS NULL OR valid_from <= CURDATE())
				  AND (valid_upto IS NULL OR valid_upto >= CURDATE())
				ORDER BY valid_from DESC
				""",
				(price_list, tuple(item_codes), tuple(uoms)),
				as_dict=True,
			)
			for p in price_rows:
				price_map.setdefault(
					(p["item_code"], p["uom"]),
					float(p["price_list_rate"] or 0),
				)
		for r in rows:
			r["price_list_rate"] = price_map.get((r["item_code"], r["uom"]), 0.0)
	else:
		for r in rows:
			r["price_list_rate"] = 0.0

	return ok(
		{"items": rows, "has_more": has_more, "next_cursor": next_cursor},
		en="UOM conversions synced",
		ar="تمت مزامنة تحويلات الوحدات",
	)
