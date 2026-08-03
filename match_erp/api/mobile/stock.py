"""Stock endpoints for Match ERP Mobile.

Two groups:

1. REPORTS (read-only, filtered + paginated) — the three that actually matter
   on a phone:
       stock_balance          → current qty/value per item+warehouse (from Bin)
       stock_ledger           → movement history (from Stock Ledger Entry)
       item_stock_by_warehouse→ "where is this item?" across warehouses

2. STOCK ENTRY — same contract as the other vouchers:
       get_stock_entry_types  → the picker
       create_stock_entry     → create + submit, idempotent on local_id
       get_stock_entries      → paginated list
       get_stock_entry        → one entry with its items

All list endpoints share the pagination contract used across the mobile API:
request `page` + `page_size`, response `page`, `page_size`, `total_records`,
`total_pages`, `data`.
"""

from __future__ import annotations

import math

import frappe
from frappe.utils import flt, get_datetime

from match_erp.api.mobile.envelope import fail, mobile_endpoint, ok, parse_body

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200

PROFILE_DB_FIELD = "custom_dist_pos_profile"
PROFILE_API_FIELD = "dist_pos_profile"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
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


def _paged(page: int, page_size: int, total: int, rows: list) -> dict:
	return {
		"page": page,
		"page_size": page_size,
		"total_records": total,
		"total_pages": math.ceil(total / page_size) if total else 0,
		"data": rows,
	}


def _existing_fields(doctype: str, fields: list[str]) -> list[str]:
	meta = frappe.get_meta(doctype)
	cols = {df.fieldname for df in meta.fields}
	cols.update({"name", "owner", "creation", "modified", "docstatus", "idx"})
	return [f for f in fields if f in cols or f == "name"]


def _alias_profile(row: dict) -> dict:
	if PROFILE_DB_FIELD in row:
		row[PROFILE_API_FIELD] = row.pop(PROFILE_DB_FIELD)
	return row


# ===========================================================================
# 1. REPORTS
# ===========================================================================
@frappe.whitelist()
@mobile_endpoint
def stock_balance(**kwargs):
	"""Current stock balance per item + warehouse, read from `Bin`.

	Filters (all optional): item_code, item_group, warehouse, company,
	search (item code/name), hide_zero (default 1 → skip empty bins).
	"""
	body = parse_body()
	page, page_size = _pagination(body)

	bin_filters: dict = {}
	if body.get("item_code"):
		bin_filters["item_code"] = body["item_code"]
	if body.get("warehouse"):
		bin_filters["warehouse"] = body["warehouse"]

	# Zero-qty bins are noise on a phone — hidden unless asked for.
	hide_zero = body.get("hide_zero")
	if hide_zero is None or str(hide_zero) in ("1", "true", "True"):
		bin_filters["actual_qty"] = ["!=", 0]

	empty = ok(
		_paged(page, page_size, 0, []),
		en="Stock balance fetched",
		ar="تم جلب رصيد المخزون",
	)

	# `company` isn't a Bin column — narrow to that company's warehouses.
	# An explicit `warehouse` filter must also belong to the company.
	if body.get("company"):
		warehouses = frappe.get_all(
			"Warehouse", filters={"company": body["company"]}, pluck="name"
		)
		if not warehouses:
			return empty
		requested = body.get("warehouse")
		if requested:
			if requested not in warehouses:
				return empty
		else:
			bin_filters["warehouse"] = ["in", warehouses]

	# `item_group` / `search` aren't Bin columns either — resolve to codes.
	item_filters: dict = {}
	if body.get("item_group"):
		item_filters["item_group"] = body["item_group"]
	if body.get("search"):
		item_filters["item_name"] = ["like", f"%{body['search']}%"]
	if item_filters:
		codes = frappe.get_all("Item", filters=item_filters, pluck="name")
		if not codes:
			return empty
		requested = body.get("item_code")
		if requested:
			if requested not in codes:
				return empty
		else:
			bin_filters["item_code"] = ["in", codes]

	total = frappe.db.count("Bin", filters=bin_filters)
	rows = frappe.get_all(
		"Bin",
		filters=bin_filters,
		fields=[
			"item_code",
			"warehouse",
			"actual_qty",
			"reserved_qty",
			"ordered_qty",
			"indented_qty",
			"planned_qty",
			"projected_qty",
			"stock_uom",
			"valuation_rate",
			"stock_value",
		],
		order_by="item_code asc, warehouse asc",
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)

	# Decorate with the item name so the app doesn't need a second lookup.
	codes = list({r["item_code"] for r in rows})
	names = {
		i["name"]: i["item_name"]
		for i in frappe.get_all(
			"Item", filters={"name": ["in", codes]}, fields=["name", "item_name"]
		)
	} if codes else {}
	for r in rows:
		r["item_name"] = names.get(r["item_code"])
		for f in ("actual_qty", "reserved_qty", "projected_qty", "valuation_rate", "stock_value"):
			r[f] = flt(r.get(f))

	return ok(
		_paged(page, page_size, total, rows),
		en="Stock balance fetched",
		ar="تم جلب رصيد المخزون",
	)


@frappe.whitelist()
@mobile_endpoint
def stock_ledger(**kwargs):
	"""Stock movement history from `Stock Ledger Entry`.

	Filters (all optional): item_code, warehouse, company, voucher_type,
	voucher_no, from_date, to_date. Cancelled entries are excluded.
	"""
	body = parse_body()
	page, page_size = _pagination(body)

	filters: dict = {"is_cancelled": 0}
	for key in ("item_code", "warehouse", "company", "voucher_type", "voucher_no"):
		if body.get(key):
			filters[key] = body[key]

	from_date = body.get("from_date")
	to_date = body.get("to_date")
	if from_date and to_date:
		filters["posting_date"] = ["between", [from_date, to_date]]
	elif from_date:
		filters["posting_date"] = [">=", from_date]
	elif to_date:
		filters["posting_date"] = ["<=", to_date]

	total = frappe.db.count("Stock Ledger Entry", filters=filters)
	rows = frappe.get_all(
		"Stock Ledger Entry",
		filters=filters,
		fields=[
			"name",
			"posting_date",
			"posting_time",
			"item_code",
			"warehouse",
			"actual_qty",
			"qty_after_transaction",
			"incoming_rate",
			"outgoing_rate",
			"valuation_rate",
			"stock_value",
			"stock_value_difference",
			"stock_uom",
			"voucher_type",
			"voucher_no",
			"batch_no",
			"company",
		],
		order_by="posting_date desc, posting_time desc, creation desc",
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	for r in rows:
		r["posting_date"] = str(r.get("posting_date") or "")
		r["posting_time"] = str(r.get("posting_time") or "")
		for f in (
			"actual_qty",
			"qty_after_transaction",
			"incoming_rate",
			"outgoing_rate",
			"valuation_rate",
			"stock_value",
			"stock_value_difference",
		):
			r[f] = flt(r.get(f))

	return ok(
		_paged(page, page_size, total, rows),
		en="Stock ledger fetched",
		ar="تم جلب حركة المخزون",
	)


@frappe.whitelist()
@mobile_endpoint
def item_stock_by_warehouse(**kwargs):
	"""Where is this item? Its quantity across every warehouse.

	Body: { "item_code": "ITEM-001", "company": "..."(optional),
	        "hide_zero": 1 (default) }
	Not paginated on purpose — one item never spans many warehouses, and the
	app shows it as a single sheet. Includes a `totals` summary.
	"""
	body = parse_body()
	item_code = body.get("item_code")
	if not item_code:
		return fail("item_code is required", "رمز الصنف مطلوب")
	if not frappe.db.exists("Item", item_code):
		return fail("Item not found", "الصنف غير موجود")

	filters: dict = {"item_code": item_code}
	hide_zero = body.get("hide_zero")
	if hide_zero is None or str(hide_zero) in ("1", "true", "True"):
		filters["actual_qty"] = ["!=", 0]

	if body.get("company"):
		warehouses = frappe.get_all(
			"Warehouse", filters={"company": body["company"]}, pluck="name"
		)
		filters["warehouse"] = ["in", warehouses or [""]]

	rows = frappe.get_all(
		"Bin",
		filters=filters,
		fields=[
			"warehouse",
			"actual_qty",
			"reserved_qty",
			"projected_qty",
			"stock_uom",
			"valuation_rate",
			"stock_value",
		],
		order_by="warehouse asc",
		limit_page_length=0,
	)
	for r in rows:
		for f in ("actual_qty", "reserved_qty", "projected_qty", "valuation_rate", "stock_value"):
			r[f] = flt(r.get(f))

	item = frappe.db.get_value(
		"Item", item_code, ["item_name", "stock_uom", "item_group"], as_dict=True
	)

	return ok(
		{
			"item_code": item_code,
			"item_name": item.item_name if item else None,
			"item_group": item.item_group if item else None,
			"stock_uom": item.stock_uom if item else None,
			"warehouses": rows,
			"totals": {
				"actual_qty": flt(sum(r["actual_qty"] for r in rows)),
				"reserved_qty": flt(sum(r["reserved_qty"] for r in rows)),
				"projected_qty": flt(sum(r["projected_qty"] for r in rows)),
				"stock_value": flt(sum(r["stock_value"] for r in rows)),
			},
			"total_warehouses": len(rows),
		},
		en="Item stock fetched",
		ar="تم جلب رصيد الصنف",
	)


# ===========================================================================
# 2. STOCK ENTRY
# ===========================================================================
STOCK_ENTRY_LIST_FIELDS = [
	"name",
	"posting_date",
	"posting_time",
	"stock_entry_type",
	"purpose",
	"company",
	"from_warehouse",
	"to_warehouse",
	"total_outgoing_value",
	"total_incoming_value",
	"value_difference",
	"remarks",
	"docstatus",
	"custom_mobile_local_id",
	PROFILE_DB_FIELD,
	"owner",
	"creation",
	"modified",
]

STOCK_ENTRY_ITEM_FIELDS = [
	"idx",
	"item_code",
	"item_name",
	"s_warehouse",
	"t_warehouse",
	"qty",
	"transfer_qty",
	"uom",
	"stock_uom",
	"conversion_factor",
	"basic_rate",
	"basic_amount",
	"batch_no",
	"serial_no",
	"cost_center",
]

STOCK_ENTRY_FILTER_FIELDS = {
	"name",
	"stock_entry_type",
	"purpose",
	"company",
	"from_warehouse",
	"to_warehouse",
	"docstatus",
}


@frappe.whitelist()
@mobile_endpoint
def get_stock_entry_types(**kwargs):
	"""Stock Entry Types for the mobile picker (with their purpose)."""
	fields = ["name", "purpose"]
	if frappe.db.has_column("Stock Entry Type", "is_standard"):
		fields.append("is_standard")
	rows = frappe.get_all(
		"Stock Entry Type",
		fields=fields,
		order_by="name asc",
		limit_page_length=0,
	)
	return ok(
		{"items": rows, "total": len(rows)},
		en="Stock entry types fetched",
		ar="تم جلب أنواع حركة المخزون",
	)


@frappe.whitelist()
@mobile_endpoint
def create_stock_entry(**kwargs):
	"""Create (and submit) a Stock Entry.

	Required: local_id, stock_entry_type, company, items[] (item_code + qty).
	Warehouses: send `from_warehouse` / `to_warehouse` on the header and/or
	`s_warehouse` / `t_warehouse` per line. Which one is required depends on
	the type's purpose:
	    Material Issue    → source (from) warehouse
	    Material Receipt  → target (to) warehouse
	    Material Transfer → both
	When omitted, the Dist POS Profile's default warehouse is used as a
	fallback for whichever side the purpose needs.
	"""
	payload = parse_body()

	local_id = payload.get("local_id")
	if not local_id:
		return fail(
			"local_id is required for idempotency", "معرّف محلي مطلوب لمنع التكرار"
		)
	if not payload.get("stock_entry_type"):
		return fail("stock_entry_type is required", "نوع حركة المخزون مطلوب")
	if not payload.get("company"):
		return fail("company is required", "الشركة مطلوبة")

	items = payload.get("items") or []
	if not isinstance(items, list) or not items:
		return fail(
			"At least one item is required", "يجب إضافة صنف واحد على الأقل"
		)
	for i, line in enumerate(items, start=1):
		if not isinstance(line, dict) or not line.get("item_code"):
			return fail(
				f"item_code is required on line {i}", f"رمز الصنف مطلوب في السطر {i}"
			)
		try:
			qty = float(line.get("qty") or 0)
		except (TypeError, ValueError):
			return fail(f"Invalid qty on line {i}", f"كمية غير صالحة في السطر {i}")
		if qty <= 0:
			return fail(
				f"qty must be greater than 0 on line {i}",
				f"يجب أن تكون الكمية أكبر من صفر في السطر {i}",
			)

	if not frappe.db.exists("Stock Entry Type", payload["stock_entry_type"]):
		return fail(
			f"Stock Entry Type '{payload['stock_entry_type']}' not found",
			"نوع حركة المخزون غير موجود",
		)

	# --- Idempotency --------------------------------------------------------
	if frappe.db.has_column("Stock Entry", "custom_mobile_local_id"):
		existing = frappe.db.get_value(
			"Stock Entry", {"custom_mobile_local_id": local_id}, "name"
		)
		if existing:
			docstatus = frappe.db.get_value("Stock Entry", existing, "docstatus")
			return ok(
				{
					"name": existing,
					"doc_type": "Stock Entry",
					"docstatus": docstatus,
					"duplicate": True,
				},
				en="Stock Entry already exists — returning prior result",
				ar="حركة المخزون موجودة مسبقاً — إرجاع النتيجة السابقة",
			)

	# --- Profile defaults ---------------------------------------------------
	from match_erp.api.mobile._voucher import resolve_profile_name

	profile_name = resolve_profile_name(payload)
	profile_warehouse = None
	if profile_name:
		profile_warehouse = (
			frappe.db.get_value("Dist POS Profile", profile_name, "default_warehouse")
			or None
		)

	purpose = frappe.db.get_value(
		"Stock Entry Type", payload["stock_entry_type"], "purpose"
	)
	from_wh = payload.get("from_warehouse")
	to_wh = payload.get("to_warehouse")
	# Fall back to the profile warehouse only for the side this purpose needs.
	if profile_warehouse:
		if purpose in ("Material Issue", "Material Transfer") and not from_wh:
			from_wh = profile_warehouse
		if purpose in ("Material Receipt", "Material Transfer") and not to_wh:
			to_wh = profile_warehouse

	doc_data: dict = {
		"doctype": "Stock Entry",
		"stock_entry_type": payload["stock_entry_type"],
		"company": payload["company"],
		"items": [],
	}
	if purpose:
		doc_data["purpose"] = purpose
	if from_wh:
		doc_data["from_warehouse"] = from_wh
	if to_wh:
		doc_data["to_warehouse"] = to_wh
	if payload.get("remarks"):
		doc_data["remarks"] = payload["remarks"]
	# Honour a client-supplied posting date/time.
	if payload.get("posting_date"):
		doc_data["set_posting_time"] = 1
		doc_data["posting_date"] = payload["posting_date"]
		if payload.get("posting_time"):
			doc_data["posting_time"] = payload["posting_time"]

	if frappe.db.has_column("Stock Entry", "custom_mobile_local_id"):
		doc_data["custom_mobile_local_id"] = local_id
	if profile_name and frappe.db.has_column("Stock Entry", PROFILE_DB_FIELD):
		doc_data[PROFILE_DB_FIELD] = profile_name

	for line in items:
		row: dict = {
			"item_code": line["item_code"],
			"qty": float(line.get("qty") or 0),
		}
		# Per-line warehouses win; else fall back to the header sides.
		s_wh = line.get("s_warehouse") or from_wh
		t_wh = line.get("t_warehouse") or to_wh
		if s_wh:
			row["s_warehouse"] = s_wh
		if t_wh:
			row["t_warehouse"] = t_wh
		for f in ("uom", "batch_no", "serial_no", "cost_center"):
			if line.get(f):
				row[f] = line[f]
		if line.get("conversion_factor"):
			row["conversion_factor"] = float(line["conversion_factor"])
		if line.get("basic_rate") is not None:
			row["basic_rate"] = float(line.get("basic_rate") or 0)
		doc_data["items"].append(row)

	doc = frappe.get_doc(doc_data)
	doc.insert(ignore_permissions=False)

	if frappe.has_permission("Stock Entry", "submit", doc=doc):
		try:
			doc.submit()
		except frappe.PermissionError:
			pass

	frappe.db.commit()

	return ok(
		{
			"name": doc.name,
			"doc_type": "Stock Entry",
			"docstatus": doc.docstatus,
			"stock_entry_type": doc.stock_entry_type,
			"purpose": doc.purpose,
			"total_incoming_value": flt(doc.total_incoming_value),
			"total_outgoing_value": flt(doc.total_outgoing_value),
			"duplicate": False,
		},
		en="Stock Entry created",
		ar="تم إنشاء حركة المخزون",
	)


def _attach_stock_entry_items(rows: list[dict]) -> None:
	"""Attach item rows to every stock entry on the page in one query."""
	if not rows:
		return
	parents = [r["name"] for r in rows]
	fields = _existing_fields("Stock Entry Detail", STOCK_ENTRY_ITEM_FIELDS)
	children = frappe.get_all(
		"Stock Entry Detail",
		filters={"parent": ["in", parents], "parenttype": "Stock Entry"},
		fields=["parent", *fields],
		order_by="parent asc, idx asc",
		limit_page_length=0,
	)
	grouped: dict[str, list[dict]] = {}
	for c in children:
		parent = c.pop("parent")
		grouped.setdefault(parent, []).append(c)
	for r in rows:
		r["items"] = grouped.get(r["name"], [])


@frappe.whitelist()
@mobile_endpoint
def get_stock_entries(**kwargs):
	"""Paginated Stock Entry list (with their items).

	Optional filters: dist_pos_profile, stock_entry_type, purpose, company,
	from_warehouse, to_warehouse, name, from_date, to_date, modified_after,
	search, docstatus.
	"""
	body = parse_body()
	page, page_size = _pagination(body)

	filters: dict = {}
	profile = body.get(PROFILE_API_FIELD)
	if profile:
		filters[PROFILE_DB_FIELD] = profile

	client_filters = body.get("filters") or {}
	if isinstance(client_filters, dict):
		for key, value in client_filters.items():
			if key in STOCK_ENTRY_FILTER_FIELDS and value not in (None, ""):
				filters[key] = value
	for key in ("name", "stock_entry_type", "purpose", "company", "from_warehouse", "to_warehouse"):
		if body.get(key):
			filters[key] = body[key]

	from_date = body.get("from_date")
	to_date = body.get("to_date")
	if from_date and to_date:
		filters["posting_date"] = ["between", [from_date, to_date]]
	elif from_date:
		filters["posting_date"] = [">=", from_date]
	elif to_date:
		filters["posting_date"] = ["<=", to_date]

	modified_after = body.get("modified_after")
	if modified_after:
		try:
			get_datetime(modified_after)
			filters["modified"] = [">", modified_after]
		except Exception:
			pass

	search = body.get("search")
	if search and "name" not in filters:
		filters["name"] = ["like", f"%{search}%"]

	total = frappe.db.count("Stock Entry", filters=filters)
	rows = frappe.get_all(
		"Stock Entry",
		filters=filters,
		fields=_existing_fields("Stock Entry", STOCK_ENTRY_LIST_FIELDS),
		order_by="posting_date desc, modified desc",
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	rows = [_alias_profile(r) for r in rows]
	_attach_stock_entry_items(rows)

	return ok(
		_paged(page, page_size, total, rows),
		en="Stock Entry list fetched",
		ar="تم جلب قائمة حركات المخزون",
	)


@frappe.whitelist()
@mobile_endpoint
def get_stock_entry(**kwargs):
	"""One Stock Entry with its items — same shape as a list row.

	Body: { "name": "MAT-STE-2026-00001" }
	"""
	body = parse_body()
	name = body.get("name")
	if not name:
		return fail("name is required", "اسم المستند مطلوب")
	if not frappe.db.exists("Stock Entry", name):
		return fail("Stock Entry not found", "حركة المخزون غير موجودة")

	req_profile = body.get(PROFILE_API_FIELD)
	if req_profile:
		doc_profile = frappe.db.get_value("Stock Entry", name, PROFILE_DB_FIELD)
		if (doc_profile or None) != req_profile:
			return fail("Stock Entry not found", "حركة المخزون غير موجودة")

	if not frappe.has_permission("Stock Entry", "read", doc=name):
		return fail("Permission denied", "ليس لديك صلاحية")

	rows = frappe.get_all(
		"Stock Entry",
		filters={"name": name},
		fields=_existing_fields("Stock Entry", STOCK_ENTRY_LIST_FIELDS),
		limit_page_length=1,
	)
	if not rows:
		return fail("Stock Entry not found", "حركة المخزون غير موجودة")

	row = _alias_profile(rows[0])
	_attach_stock_entry_items([row])

	return ok(row, en="Stock Entry fetched", ar="تم جلب حركة المخزون")
