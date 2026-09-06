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


def _site_base_url() -> str:
	"""Best-effort absolute base URL for the current site.

	Order of preference:
	  1. `frappe.utils.get_url()` — works for v15+v16 when accessed via HTTP.
	  2. The site's `host_name` System Setting.
	  3. Empty string — caller falls back to relative paths.
	"""
	try:
		from frappe.utils import get_url

		url = get_url()
		if url:
			return url.rstrip("/")
	except Exception:
		pass
	try:
		host = frappe.db.get_single_value("System Settings", "host_name")
		if host:
			return host.rstrip("/")
	except Exception:
		pass
	return ""


def _absolutize_file(file_url: str | None) -> str | None:
	"""Convert a relative `/files/...` or `/private/files/...` URL into an
	absolute one so mobile clients can fetch it directly."""
	if not file_url:
		return None
	if file_url.startswith(("http://", "https://")):
		return file_url
	base = _site_base_url()
	if not base:
		return file_url  # leave relative; client may rewrite
	if not file_url.startswith("/"):
		file_url = "/" + file_url
	return f"{base}{file_url}"


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
def _profile_item_group_filter() -> list | None:
	"""Build a get_list filter restricting items to the item groups
	configured on the caller's Dist POS Profile.

	Returns None when the profile has no item-group restriction (→ all
	items). Descendant groups are included so picking a parent group
	("Beverages") also pulls its children ("Hot Drinks", …).
	"""
	try:
		from match_erp.match_erp.doctype.dist_pos_profile.dist_pos_profile import (
			resolve_for_user,
		)

		profile = resolve_for_user(frappe.session.user)
	except Exception:
		profile = None
	if profile is None:
		return None

	groups = [r.item_group for r in (profile.get("item_groups") or []) if r.get("item_group")]
	if not groups:
		return None

	# Expand each selected group to include its descendants via the nested
	# set columns (lft/rgt) on Item Group.
	all_groups: set[str] = set(groups)
	for g in groups:
		bounds = frappe.db.get_value("Item Group", g, ["lft", "rgt"])
		if not bounds:
			continue
		lft, rgt = bounds
		children = frappe.get_all(
			"Item Group",
			filters=[["lft", ">=", lft], ["rgt", "<=", rgt]],
			pluck="name",
		)
		all_groups.update(children)
	return [["item_group", "in", list(all_groups)]]


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
		# Per-transaction default units. ERPNext lets an item be sold in one
		# unit (e.g. Carton) and bought in another (Pallet) while stock is
		# kept in a third. The client picks the right one per voucher type
		# and falls back to stock_uom when unset.
		"sales_uom",
		"purchase_uom",
		"description",
		"image",
		"standard_rate",
		"has_batch_no",
		"has_serial_no",
		"disabled",
		"modified",
	]

	# Honor the profile's item-group restriction (empty = all).
	extra = _profile_item_group_filter()
	rows, has_more, next_cursor = _fetch(
		"Item", fields, modified_after, limit, extra_filters=extra
	)

	if not rows:
		return ok(
			{"items": rows, "has_more": has_more, "next_cursor": next_cursor},
			en="Items synced",
			ar="تمت مزامنة الأصناف",
		)

	item_codes = [r["item_code"] for r in rows]

	# --- price_list_rate ------------------------------------------------------
	# Per-UOM prices are returned separately via `get_item_uom_conversions`.
	# Here we only want the stock-UOM price, which in ERPNext is the row
	# where `Item Price.uom` is either NULL or equal to the item's stock UOM.
	# Without that filter we'd accidentally pick up an alternate-UOM row
	# (e.g. Carton) and use it as the per-piece price.
	price_map: dict[str, float] = {}
	if price_list:
		# Build {item_code: stock_uom} so we can match per row.
		stock_uoms = {r["item_code"]: r.get("stock_uom") for r in rows}
		distinct_stock_uoms = {u for u in stock_uoms.values() if u}
		uom_filter_sql = ""
		uom_params: list[Any] = []
		if distinct_stock_uoms:
			uom_filter_sql = "AND (uom IS NULL OR uom = '' OR uom IN %s)"
			uom_params.append(tuple(distinct_stock_uoms))

		price_rows = frappe.db.sql(
			f"""
			SELECT item_code, uom, price_list_rate
			FROM `tabItem Price`
			WHERE price_list = %s
			  AND item_code IN %s
			  {uom_filter_sql}
			  AND (valid_from IS NULL OR valid_from <= CURDATE())
			  AND (valid_upto IS NULL OR valid_upto >= CURDATE())
			ORDER BY valid_from DESC
			""",
			(price_list, tuple(item_codes), *uom_params),
			as_dict=True,
		)
		# Prefer a row whose UOM matches the item's stock UOM exactly;
		# fall back to a NULL/blank-UOM row when nothing better is found.
		for p in price_rows:
			code = p["item_code"]
			rate = float(p["price_list_rate"] or 0)
			row_uom = p.get("uom") or ""
			stock_uom = stock_uoms.get(code) or ""
			if code in price_map:
				continue
			# Always accept the exact stock-UOM match.
			if row_uom and stock_uom and row_uom == stock_uom:
				price_map[code] = rate
				continue
			# Fall back to NULL/blank UOM only if no exact match seen yet.
			if not row_uom:
				price_map.setdefault(code, rate)

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
		# `image` arrives as a relative `/files/...` path; rewrite to absolute
		# so the mobile client can render it without rebuilding the URL.
		if r.get("image"):
			r["image"] = _absolutize_file(r["image"])

	return ok(
		{"items": rows, "has_more": has_more, "next_cursor": next_cursor},
		en="Items synced",
		ar="تمت مزامنة الأصناف",
	)


# ---------------------------------------------------------------------------
# Item UOM Conversions (alternate units per item, e.g. Carton/Box/Piece)
# ---------------------------------------------------------------------------
@frappe.whitelist()
@mobile_endpoint
def get_item_uom_conversions(**kwargs):
	"""Stream the `UOM Conversion Detail` child table across all items.

	Each row tells the client: "for item X, UOM Y is worth Z stock UOMs".
	The client persists these alongside the item's stock UOM so the voucher
	editor can offer a UOM picker that auto-fills the conversion factor.

	When a `price_list` is supplied in the body, each row is augmented with
	the matching `Item Price` rate for that UOM (or 0 when ERPNext has no
	UOM-specific price — the client falls back to a factor-scaled rate
	from the stock-UOM price). This lets distributors price the same item
	differently for Carton vs Box vs Piece in the price list.

	Compatible with ERPNext v15 + v16 (`UOM Conversion Detail` and
	`Item Price.uom` exist on both).
	"""
	modified_after, limit, body = _parse_sync_args()
	price_list: str | None = body.get("price_list") or None

	where = ""
	params: list[Any] = []
	if modified_after:
		where = "WHERE modified > %s"
		params.append(modified_after)

	params.append(limit + 1)

	sql = f"""
		SELECT name, parent AS item_code, uom, conversion_factor, modified
		FROM `tabUOM Conversion Detail`
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
			# Normalize numeric type for JSON.
			if r.get("conversion_factor") is not None:
				r["conversion_factor"] = float(r["conversion_factor"])

	# Per-UOM price lookup. ERPNext's `Item Price.uom` is nullable: rows
	# without a UOM apply to the stock UOM only — we ignore those here
	# since `get_items` already returns the stock-UOM price. We only
	# want rows that explicitly target the alternate UOM.
	if price_list and rows:
		item_codes = list({r["item_code"] for r in rows if r.get("item_code")})
		uoms       = list({r["uom"]       for r in rows if r.get("uom")})
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
			price_map: dict[tuple[str, str], float] = {}
			for p in price_rows:
				key = (p["item_code"], p["uom"])
				# First row wins (most-recent valid_from), in line with
				# get_items' price selection.
				price_map.setdefault(key, float(p["price_list_rate"] or 0))
			for r in rows:
				r["price_list_rate"] = price_map.get(
					(r["item_code"], r["uom"]), 0.0
				)
	else:
		for r in rows:
			r["price_list_rate"] = 0.0

	return ok(
		{"items": rows, "has_more": has_more, "next_cursor": next_cursor},
		en="UOM conversions synced",
		ar="تمت مزامنة تحويلات الوحدات",
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


# ---------------------------------------------------------------------------
# Item Groups (category tree)
# ---------------------------------------------------------------------------
@frappe.whitelist()
@mobile_endpoint
def get_item_groups(**kwargs):
	modified_after, limit, _body = _parse_sync_args()
	fields = [
		"name",
		"item_group_name",
		"parent_item_group",
		"is_group",
		"image",
		"modified",
	]
	rows, has_more, next_cursor = _fetch("Item Group", fields, modified_after, limit)
	for r in rows:
		if r.get("image"):
			r["image"] = _absolutize_file(r["image"])
	return ok(
		{"items": rows, "has_more": has_more, "next_cursor": next_cursor},
		en="Item groups synced",
		ar="تمت مزامنة مجموعات الأصناف",
	)


# ---------------------------------------------------------------------------
# Modes of Payment (small table — still cursor-paginated for consistency)
# ---------------------------------------------------------------------------
@frappe.whitelist()
@mobile_endpoint
def get_modes_of_payment(**kwargs):
	modified_after, limit, _body = _parse_sync_args()
	fields = ["name", "mode_of_payment", "type", "enabled", "modified"]
	rows, has_more, next_cursor = _fetch(
		"Mode of Payment", fields, modified_after, limit
	)

	# Client expects `mode_name`; ERPNext field is `mode_of_payment`. Alias.
	for r in rows:
		r["mode_name"] = r.pop("mode_of_payment", None) or r.get("name")
		# default_account lives in the Mode of Payment Account child table,
		# keyed per company. Without a company hint we return the first one;
		# clients that need per-company defaults should filter locally.
		default_account = frappe.db.get_value(
			"Mode of Payment Account",
			{"parent": r["name"]},
			"default_account",
		)
		r["default_account"] = default_account

	return ok(
		{"items": rows, "has_more": has_more, "next_cursor": next_cursor},
		en="Modes of payment synced",
		ar="تمت مزامنة وسائل الدفع",
	)


# ---------------------------------------------------------------------------
# Customer Groups — small tree, cached by mobile pickers
# ---------------------------------------------------------------------------
@frappe.whitelist()
@mobile_endpoint
def get_customer_groups(**kwargs):
	"""Flat list of customer groups for the mobile create-customer form.

	The doctype exists unchanged across ERPNext v15 + v16. We return every
	row including is_group=1 nodes so the client can render a tree if it
	wants — current usage is a simple dropdown that filters out group rows.
	"""
	modified_after, limit, _body = _parse_sync_args()
	fields = [
		"name",
		"customer_group_name",
		"parent_customer_group",
		"is_group",
		"modified",
	]
	rows, has_more, next_cursor = _fetch(
		"Customer Group", fields, modified_after, limit
	)
	return ok(
		{"items": rows, "has_more": has_more, "next_cursor": next_cursor},
		en="Customer groups synced",
		ar="تمت مزامنة مجموعات العملاء",
	)


# ---------------------------------------------------------------------------
# Territories — also a small tree
# ---------------------------------------------------------------------------
@frappe.whitelist()
@mobile_endpoint
def get_territories(**kwargs):
	"""Flat list of territories for the mobile create-customer form."""
	modified_after, limit, _body = _parse_sync_args()
	fields = [
		"name",
		"territory_name",
		"parent_territory",
		"is_group",
		"modified",
	]
	rows, has_more, next_cursor = _fetch(
		"Territory", fields, modified_after, limit
	)
	return ok(
		{"items": rows, "has_more": has_more, "next_cursor": next_cursor},
		en="Territories synced",
		ar="تمت مزامنة المناطق",
	)


# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------
@frappe.whitelist()
@mobile_endpoint
def get_suppliers(**kwargs):
	modified_after, limit, _body = _parse_sync_args()

	fields = [
		"name",
		"supplier_name",
		"supplier_group",
		"country",
		"mobile_no",
		"email_id",
		"disabled",
		"default_price_list",
		"default_currency",
		"modified",
	]
	# Guard columns that may not exist on all ERPNext schemas.
	for col in ("mobile_no", "email_id"):
		if not frappe.db.has_column("Supplier", col):
			fields.remove(col)

	rows, has_more, next_cursor = _fetch("Supplier", fields, modified_after, limit)

	for r in rows:
		r.setdefault("mobile_no", None)
		r.setdefault("email_id", None)
		r["outstanding_amount"] = _supplier_outstanding(r["name"])

	return ok(
		{"items": rows, "has_more": has_more, "next_cursor": next_cursor},
		en="Suppliers synced",
		ar="تمت مزامنة الموردين",
	)


def _supplier_outstanding(supplier: str) -> float:
	val = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(outstanding_amount), 0)
		FROM `tabPurchase Invoice`
		WHERE supplier = %s AND docstatus = 1 AND outstanding_amount > 0
		""",
		(supplier,),
	)
	return float(val[0][0]) if val and val[0] and val[0][0] is not None else 0.0


# ---------------------------------------------------------------------------
# Warehouses
# ---------------------------------------------------------------------------
@frappe.whitelist()
@mobile_endpoint
def get_warehouses(**kwargs):
	modified_after, limit, _body = _parse_sync_args()
	fields = [
		"name",
		"warehouse_name",
		"parent_warehouse",
		"is_group",
		"company",
		"disabled",
		"modified",
	]
	rows, has_more, next_cursor = _fetch("Warehouse", fields, modified_after, limit)
	return ok(
		{"items": rows, "has_more": has_more, "next_cursor": next_cursor},
		en="Warehouses synced",
		ar="تمت مزامنة المستودعات",
	)


# ---------------------------------------------------------------------------
# Voucher status refresh — the mobile app stores submitted vouchers locally
# and periodically asks the server for their current status + paid/outstanding
# amounts so it can show paid/unpaid/cancelled badges and remaining balances
# without a full re-sync.
# ---------------------------------------------------------------------------

# Which fields carry the totals/outstanding for each supported doctype. The
# normalized output is always {status, docstatus, grand_total, paid_amount,
# outstanding_amount}.
_STATUS_DOCTYPES = {
	"Sales Invoice": True,
	"Purchase Invoice": True,
	"Sales Order": False,
	"Purchase Order": False,
	"Payment Entry": False,
}


def _normalize_status(doctype: str, row: dict) -> dict:
	"""Map a raw doc row to the mobile status envelope."""
	from frappe.utils import flt as _flt

	docstatus = int(row.get("docstatus") or 0)
	grand_total = _flt(row.get("grand_total") or row.get("paid_amount") or 0)
	outstanding = _flt(row.get("outstanding_amount") or 0)

	if docstatus == 2:
		simple = "Cancelled"
	elif doctype in ("Sales Invoice", "Purchase Invoice"):
		# Use ERPNext's own status string but also derive a simple paid flag.
		simple = row.get("status") or ("Paid" if outstanding <= 0 else "Unpaid")
	else:
		simple = row.get("status") or ("Submitted" if docstatus == 1 else "Draft")

	paid = grand_total - outstanding if grand_total else 0.0
	return {
		"status": simple,
		"docstatus": docstatus,
		"grand_total": grand_total,
		"paid_amount": _flt(paid),
		"outstanding_amount": outstanding,
	}


@frappe.whitelist()
@mobile_endpoint
def get_voucher_statuses(**kwargs):
	"""Batch status lookup.

	Body: refs = [{doc_type, name}, ...]  (or names grouped per doctype).
	Returns: { statuses: { <remote_name>: {status, docstatus, grand_total,
	          paid_amount, outstanding_amount} } } — keyed by remote name.
	Names not found (e.g. deleted upstream) are omitted.
	"""
	body = parse_body()
	refs = body.get("refs") or []
	if isinstance(refs, str):
		import json as _json

		try:
			refs = _json.loads(refs)
		except (ValueError, TypeError):
			refs = []

	# Group requested names by doctype so we issue one query per doctype.
	by_doctype: dict[str, list[str]] = {}
	for ref in refs:
		if not isinstance(ref, dict):
			continue
		dt = ref.get("doc_type") or ref.get("doctype")
		name = ref.get("name")
		if not dt or not name or dt not in _STATUS_DOCTYPES:
			continue
		by_doctype.setdefault(dt, []).append(name)

	statuses: dict[str, dict] = {}
	for dt, names in by_doctype.items():
		has_outstanding = _STATUS_DOCTYPES[dt]
		fields = ["name", "docstatus", "status"]
		if has_outstanding:
			fields += ["grand_total", "outstanding_amount"]
		elif dt == "Payment Entry":
			fields += ["paid_amount"]
		else:
			fields += ["grand_total"]

		rows = frappe.get_all(
			dt,
			filters={"name": ["in", list(set(names))]},
			fields=fields,
			limit_page_length=0,
		)
		for row in rows:
			statuses[row["name"]] = _normalize_status(dt, row)

	return ok(
		{"statuses": statuses},
		en="Voucher statuses fetched",
		ar="تم جلب حالات السندات",
	)
