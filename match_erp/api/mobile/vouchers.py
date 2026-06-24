"""Voucher fetch endpoints for Match ERP Mobile.

Read-only, paginated listing + detail for the transactional vouchers a
distributor cares about: Sales Order, Sales Invoice, Payment Entry.

MULTI-TENANT ISOLATION
----------------------
Every list endpoint REQUIRES `dist_pos_profile` and filters strictly at the
database level by it (`WHERE custom_dist_pos_profile = <input>`). A request
without `dist_pos_profile` is rejected. This guarantees each POS device only
ever sees the vouchers created under its own profile. Vouchers created
directly in ERPNext (no profile tag) are NOT returned by these endpoints.

> Field-name note: the physical column is `custom_dist_pos_profile` because
> Frappe forces the `custom_` prefix on custom fields added to standard
> doctypes. The mobile API contract uses `dist_pos_profile` everywhere — both
> as the request parameter and as the key in every response row — so the
> client never sees the `custom_` prefix.

PAGINATION (spec contract)
--------------------------
Request:  `page` (1-based, default 1), `page_size` (default 20, max 200).
Response: `page`, `page_size`, `total_records`, `total_pages`, `data`.

OPTIONAL FILTERS
----------------
`name` (voucher id, exact), `customer`/`party`, `status`, `from_date`,
`to_date` (on the doctype's posting/transaction date), and `modified_after`.
Anything outside the per-doctype whitelist is ignored.

Detail endpoints return the header fields plus child rows:
  - Sales Order   → items (item_code, item_name, uom, qty, rate, amount, …)
  - Sales Invoice → items
  - Payment Entry → references (reference_doctype, reference_name,
                    allocated_amount, …)
"""

from __future__ import annotations

import math

import frappe
from frappe.utils import flt, get_datetime

from match_erp.api.mobile.envelope import fail, mobile_endpoint, ok, parse_body

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200

# Physical column (Frappe-mandated custom_ prefix) and the API-facing alias.
PROFILE_DB_FIELD = "custom_dist_pos_profile"
PROFILE_API_FIELD = "dist_pos_profile"


# Per-doctype configuration: the date field used for range filtering, the
# fields returned in LIST rows, the whitelist of fields a client may filter
# on, and the child-table projection for DETAIL.
_CONFIG = {
	"Sales Order": {
		"date_field": "transaction_date",
		"party_field": "customer",
		"list_fields": [
			"name",
			"transaction_date",
			"delivery_date",
			"customer",
			"customer_name",
			"customer_group",
			"territory",
			"company",
			"currency",
			"conversion_rate",
			"selling_price_list",
			"po_no",
			"po_date",
			"total_qty",
			"base_total",
			"total",
			"net_total",
			"total_taxes_and_charges",
			"apply_discount_on",
			"additional_discount_percentage",
			"discount_amount",
			"rounding_adjustment",
			"rounded_total",
			"grand_total",
			"advance_paid",
			"per_delivered",
			"per_billed",
			"status",
			"docstatus",
			"order_type",
			"contact_person",
			"contact_mobile",
			"contact_email",
			"customer_address",
			"shipping_address_name",
			"tc_name",
			"remarks",
			"custom_mobile_local_id",
			PROFILE_DB_FIELD,
			"owner",
			"creation",
			"modified",
		],
		"filter_fields": {"name", "customer", "status", "docstatus", "company", "currency"},
		"child_table": "items",
		"item_fields": [
			"idx",
			"item_code",
			"item_name",
			"description",
			"item_group",
			"uom",
			"conversion_factor",
			"stock_uom",
			"qty",
			"stock_qty",
			"price_list_rate",
			"base_price_list_rate",
			"rate",
			"base_rate",
			"discount_percentage",
			"discount_amount",
			"amount",
			"base_amount",
			"net_rate",
			"net_amount",
			"warehouse",
			"delivery_date",
			"delivered_qty",
			"billed_amt",
		],
	},
	"Sales Invoice": {
		"date_field": "posting_date",
		"party_field": "customer",
		"list_fields": [
			"name",
			"posting_date",
			"posting_time",
			"due_date",
			"customer",
			"customer_name",
			"customer_group",
			"territory",
			"company",
			"currency",
			"conversion_rate",
			"selling_price_list",
			"is_return",
			"return_against",
			"is_pos",
			"update_stock",
			"po_no",
			"po_date",
			"total_qty",
			"base_total",
			"total",
			"net_total",
			"total_taxes_and_charges",
			"apply_discount_on",
			"additional_discount_percentage",
			"discount_amount",
			"rounding_adjustment",
			"rounded_total",
			"grand_total",
			"total_advance",
			"outstanding_amount",
			"paid_amount",
			"status",
			"docstatus",
			"contact_person",
			"contact_mobile",
			"contact_email",
			"customer_address",
			"shipping_address_name",
			"tc_name",
			"remarks",
			"custom_mobile_local_id",
			PROFILE_DB_FIELD,
			"owner",
			"creation",
			"modified",
		],
		"filter_fields": {
			"name",
			"customer",
			"status",
			"docstatus",
			"company",
			"currency",
			"is_return",
		},
		"child_table": "items",
		"item_fields": [
			"idx",
			"item_code",
			"item_name",
			"description",
			"item_group",
			"uom",
			"conversion_factor",
			"stock_uom",
			"qty",
			"stock_qty",
			"price_list_rate",
			"base_price_list_rate",
			"rate",
			"base_rate",
			"discount_percentage",
			"discount_amount",
			"amount",
			"base_amount",
			"net_rate",
			"net_amount",
			"warehouse",
			"batch_no",
			"serial_no",
			"sales_order",
			"so_detail",
		],
	},
	"Payment Entry": {
		"date_field": "posting_date",
		"party_field": "party",
		"list_fields": [
			"name",
			"posting_date",
			"payment_type",
			"mode_of_payment",
			"party_type",
			"party",
			"party_name",
			"company",
			"paid_from",
			"paid_from_account_currency",
			"paid_to",
			"paid_to_account_currency",
			"paid_amount",
			"base_paid_amount",
			"received_amount",
			"base_received_amount",
			"source_exchange_rate",
			"target_exchange_rate",
			"total_allocated_amount",
			"unallocated_amount",
			"reference_no",
			"reference_date",
			"status",
			"docstatus",
			"remarks",
			"custom_mobile_local_id",
			PROFILE_DB_FIELD,
			"owner",
			"creation",
			"modified",
		],
		"filter_fields": {
			"name",
			"party_type",
			"party",
			"payment_type",
			"status",
			"docstatus",
			"company",
			"mode_of_payment",
		},
		"child_table": "references",
		"item_fields": [
			"idx",
			"reference_doctype",
			"reference_name",
			"due_date",
			"total_amount",
			"outstanding_amount",
			"allocated_amount",
		],
	},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _pagination(body: dict) -> tuple[int, int]:
	"""Resolve (page, page_size). 1-based page per the spec contract."""
	try:
		page_size = int(body.get("page_size") or DEFAULT_PAGE_SIZE)
	except (TypeError, ValueError):
		page_size = DEFAULT_PAGE_SIZE
	page_size = max(1, min(page_size, MAX_PAGE_SIZE))

	try:
		page = int(body.get("page") or 1)
	except (TypeError, ValueError):
		page = 1
	page = max(1, page)
	return page, page_size


def _build_filters(doctype: str, body: dict, profile: str) -> dict:
	"""Compose ERPNext filters: MANDATORY profile scope (DB-level isolation)
	+ whitelisted client filters + party + date range + modified_after."""
	cfg = _CONFIG[doctype]

	# Strict tenant isolation — always filter by the profile column.
	filters: dict = {PROFILE_DB_FIELD: profile}

	# Whitelisted equality filters.
	client_filters = body.get("filters") or {}
	if isinstance(client_filters, dict):
		for key, value in client_filters.items():
			if key in cfg["filter_fields"] and value not in (None, ""):
				filters[key] = value

	# Top-level convenience params (also accepted directly on the body).
	if body.get("name"):
		filters["name"] = body["name"]
	if body.get("status"):
		filters["status"] = body["status"]
	# `party` maps to the doctype's party field (customer for sales docs).
	party = body.get("party") or body.get("customer")
	if party:
		filters[cfg["party_field"]] = party

	# Date range on the doctype's posting/transaction date.
	date_field = cfg["date_field"]
	from_date = body.get("from_date")
	to_date = body.get("to_date")
	if from_date and to_date:
		filters[date_field] = ["between", [from_date, to_date]]
	elif from_date:
		filters[date_field] = [">=", from_date]
	elif to_date:
		filters[date_field] = ["<=", to_date]

	# Incremental sync cursor.
	modified_after = body.get("modified_after")
	if modified_after:
		try:
			get_datetime(modified_after)  # validate
			filters["modified"] = [">", modified_after]
		except Exception:
			pass

	# Free-text search on the document id.
	search = body.get("search")
	if search and "name" not in filters:
		filters["name"] = ["like", f"%{search}%"]

	return filters


def _alias_profile(row: dict) -> dict:
	"""Expose the profile column under the API field name `dist_pos_profile`."""
	if PROFILE_DB_FIELD in row:
		row[PROFILE_API_FIELD] = row.pop(PROFILE_DB_FIELD)
	return row


def _coerce(value):
	if isinstance(value, (int, float)):
		return flt(value)
	if value is None or isinstance(value, (str, bool)):
		return value
	return str(value)


# Cache of existing columns per doctype so we don't probe the schema on every
# request. Keeps the field lists robust against fields that don't exist on a
# given doctype/version (e.g. `remarks` exists on Sales Invoice but not Sales
# Order).
_COLUMN_CACHE: dict[str, set] = {}


def _existing_fields(doctype: str, fields: list[str]) -> list[str]:
	"""Return only the fields that are real columns on `doctype`, preserving
	order. `name` is always included."""
	cols = _COLUMN_CACHE.get(doctype)
	if cols is None:
		meta = frappe.get_meta(doctype)
		cols = {df.fieldname for df in meta.fields}
		# Standard columns present on every doctype.
		cols.update(
			{
				"name",
				"owner",
				"creation",
				"modified",
				"modified_by",
				"docstatus",
				"idx",
			}
		)
		_COLUMN_CACHE[doctype] = cols
	return [f for f in fields if f in cols or f == "name"]


# Child-table doctype per parent (for the batched list query).
_CHILD_DOCTYPE = {
	"Sales Order": "Sales Order Item",
	"Sales Invoice": "Sales Invoice Item",
	"Payment Entry": "Payment Entry Reference",
}


def _attach_children(doctype: str, rows: list[dict]) -> None:
	"""Populate `items` (and `references` for Payment Entry) on each list row
	using a single query against the child table for the whole page."""
	if not rows:
		return
	cfg = _CONFIG[doctype]
	child_dt = _CHILD_DOCTYPE[doctype]
	parents = [r["name"] for r in rows]

	# Only request columns that actually exist on the child doctype.
	item_fields = _existing_fields(child_dt, cfg["item_fields"])

	# `parent` + `idx` keep rows grouped per voucher and ordered.
	child_rows = frappe.get_all(
		child_dt,
		filters={"parent": ["in", parents], "parenttype": doctype},
		fields=["parent", "idx", *item_fields],
		order_by="parent asc, idx asc",
	)

	grouped: dict[str, list[dict]] = {}
	for cr in child_rows:
		parent = cr.pop("parent")
		cr.pop("idx", None)
		grouped.setdefault(parent, []).append(
			{f: _coerce(cr.get(f)) for f in item_fields}
		)

	for r in rows:
		items = grouped.get(r["name"], [])
		r["items"] = items
		if doctype == "Payment Entry":
			r["references"] = items


def _list(doctype: str) -> dict:
	body = parse_body()

	# --- Mandatory tenant key ------------------------------------------------
	profile = body.get(PROFILE_API_FIELD)
	if not profile:
		return fail(
			"dist_pos_profile is required",
			"ملف نقاط البيع (Dist POS Profile) مطلوب",
		)
	if not frappe.db.exists("Dist POS Profile", profile):
		return fail(
			f"Dist POS Profile '{profile}' not found",
			f"ملف نقاط البيع '{profile}' غير موجود",
		)

	cfg = _CONFIG[doctype]
	page, page_size = _pagination(body)
	filters = _build_filters(doctype, body, profile)

	total_records = frappe.db.count(doctype, filters=filters)
	total_pages = math.ceil(total_records / page_size) if total_records else 0

	rows = frappe.get_all(
		doctype,
		filters=filters,
		fields=_existing_fields(doctype, cfg["list_fields"]),
		order_by=f"{cfg['date_field']} desc, modified desc",
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	rows = [_alias_profile(r) for r in rows]

	# Attach child rows (items / references) to every row on the page in a
	# single batched query, so the list response is self-contained.
	_attach_children(doctype, rows)

	return ok(
		{
			"page": page,
			"page_size": page_size,
			"total_records": total_records,
			"total_pages": total_pages,
			"data": rows,
		},
		en=f"{doctype} list fetched",
		ar="تم جلب القائمة",
	)


def _detail(doctype: str) -> dict:
	body = parse_body()
	name = body.get("name")
	if not name:
		return fail("name is required", "اسم المستند مطلوب")
	if not frappe.db.exists(doctype, name):
		return fail(f"{doctype} not found", "المستند غير موجود")

	doc = frappe.get_doc(doctype, name)
	if not doc.has_permission("read"):
		return fail("Permission denied", "ليس لديك صلاحية")

	# Optional but recommended: enforce tenant isolation on detail too. When
	# the caller passes its profile, a document tagged with a different
	# profile is hidden.
	req_profile = body.get(PROFILE_API_FIELD)
	if req_profile and (doc.get(PROFILE_DB_FIELD) or None) != req_profile:
		return fail(f"{doctype} not found", "المستند غير موجود")

	# Detail returns the FULL document so the mobile app has every attribute
	# it might need (all header fields + all child tables: items, taxes,
	# payment schedule, references, …). `as_dict` serializes child tables too.
	data = doc.as_dict()

	# Drop noisy internal bits and JSON-serialize dates/decimals.
	for k in ("_user_tags", "_comments", "_assign", "_liked_by", "doctype_links"):
		data.pop(k, None)
	clean = _jsonable(data)

	# Expose the profile under the API field name.
	if PROFILE_DB_FIELD in clean:
		clean[PROFILE_API_FIELD] = clean.pop(PROFILE_DB_FIELD)

	# Convenience: Sales Invoice paid_amount derived from outstanding.
	if doctype == "Sales Invoice":
		clean.setdefault(
			"paid_amount",
			flt(data.get("grand_total")) - flt(data.get("outstanding_amount")),
		)

	return ok(clean, en=f"{doctype} fetched", ar="تم جلب المستند")


def _jsonable(value):
	"""Recursively coerce a Frappe doc dict into JSON-friendly primitives."""
	import datetime
	from decimal import Decimal

	if isinstance(value, dict):
		return {k: _jsonable(v) for k, v in value.items()}
	if isinstance(value, (list, tuple)):
		return [_jsonable(v) for v in value]
	if isinstance(value, Decimal):
		return flt(value)
	if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
		return str(value)
	return value


# ---------------------------------------------------------------------------
# Endpoints — spec names (get_*) + back-compat aliases (list_*).
# ---------------------------------------------------------------------------
@frappe.whitelist()
@mobile_endpoint
def get_sales_orders(**kwargs):
	return _list("Sales Order")


@frappe.whitelist()
@mobile_endpoint
def get_sales_invoices(**kwargs):
	return _list("Sales Invoice")


@frappe.whitelist()
@mobile_endpoint
def get_payment_entries(**kwargs):
	return _list("Payment Entry")


@frappe.whitelist()
@mobile_endpoint
def get_sales_order(**kwargs):
	return _detail("Sales Order")


@frappe.whitelist()
@mobile_endpoint
def get_sales_invoice(**kwargs):
	return _detail("Sales Invoice")


@frappe.whitelist()
@mobile_endpoint
def get_payment_entry(**kwargs):
	return _detail("Payment Entry")


# --- Back-compat aliases (earlier list_* names) ----------------------------
@frappe.whitelist()
@mobile_endpoint
def list_sales_orders(**kwargs):
	return _list("Sales Order")


@frappe.whitelist()
@mobile_endpoint
def list_sales_invoices(**kwargs):
	return _list("Sales Invoice")


@frappe.whitelist()
@mobile_endpoint
def list_payment_entries(**kwargs):
	return _list("Payment Entry")
