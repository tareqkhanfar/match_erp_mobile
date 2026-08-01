"""Expense endpoints for Match ERP Mobile.

The `Expense` doctype ships with the **match_utils** app; these endpoints live
in match_erp so the mobile client has one integration surface and the same
sync contract as every other voucher:

- create_expense    → create (and submit) an Expense, idempotent on local_id
- get_expenses      → paginated list, optional dist_pos_profile scoping
- get_expense       → one expense by name (same shape as a list row)
- get_expense_types → the Expense Type picker (with per-company accounts)

Expense is submittable and posts a Journal Entry on submit — that logic lives
in the match_utils controller. We deliberately send only the user-entered
fields (`expense_type`, `company`, `posting_date`, `amount`, …) and let
`Expense.validate()` derive the expense/credit accounts, party type and
default cost center from the Expense Type's per-company account row.

Every endpoint returns the standard envelope; the module degrades gracefully
with a clear message when match_utils isn't installed.
"""

from __future__ import annotations

import math

import frappe
from frappe.utils import flt, get_datetime

from match_erp.api.mobile.envelope import fail, mobile_endpoint, ok, parse_body

DOCTYPE = "Expense"

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200

PROFILE_DB_FIELD = "custom_dist_pos_profile"
PROFILE_API_FIELD = "dist_pos_profile"

# Fields returned for each expense row.
LIST_FIELDS = [
	"name",
	"posting_date",
	"expense_type",
	"company",
	"amount",
	"mode_of_payment",
	"cost_center",
	"expense_account",
	"credit_account",
	"party_type",
	"party",
	"bill_no",
	"remarks",
	"attach_receipt",
	"journal_entry",
	"docstatus",
	"custom_mobile_local_id",
	PROFILE_DB_FIELD,
	"owner",
	"creation",
	"modified",
]

# Client-supplied filters we honour (anything else is ignored).
FILTER_FIELDS = {
	"name",
	"expense_type",
	"company",
	"mode_of_payment",
	"cost_center",
	"party_type",
	"party",
	"docstatus",
}

# Fields a client may set on create. Accounts + party_type are intentionally
# EXCLUDED — the Expense controller derives them from the Expense Type so the
# mobile app can never post to the wrong ledger.
CREATE_FIELDS = (
	"expense_type",
	"company",
	"posting_date",
	"amount",
	"mode_of_payment",
	"cost_center",
	"party",
	"bill_no",
	"remarks",
	"attach_receipt",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ensure_doctype() -> dict | None:
	"""Guard: match_utils (which owns Expense) must be installed."""
	if not frappe.db.exists("DocType", DOCTYPE):
		return fail(
			"Expenses are not available on this site (match_utils is not installed).",
			"المصاريف غير متاحة على هذا الموقع (تطبيق match_utils غير مثبّت).",
		)
	return None


def _existing_fields(doctype: str, fields: list[str]) -> list[str]:
	"""Drop fields that don't exist on this site so the query can't fail."""
	meta = frappe.get_meta(doctype)
	cols = {df.fieldname for df in meta.fields}
	cols.update({"name", "owner", "creation", "modified", "docstatus", "idx"})
	return [f for f in fields if f in cols or f == "name"]


def _alias_profile(row: dict) -> dict:
	"""Expose the profile column under the API name `dist_pos_profile`."""
	if PROFILE_DB_FIELD in row:
		row[PROFILE_API_FIELD] = row.pop(PROFILE_DB_FIELD)
	return row


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


def _idempotency_lookup(local_id: str) -> str | None:
	if not local_id or not frappe.db.has_column(DOCTYPE, "custom_mobile_local_id"):
		return None
	return frappe.db.get_value(DOCTYPE, {"custom_mobile_local_id": local_id}, "name")


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@frappe.whitelist()
@mobile_endpoint
def create_expense(**kwargs):
	"""Create (and submit) an Expense from the mobile app.

	Required: local_id, expense_type, company, posting_date, amount.
	Optional: mode_of_payment, cost_center, party, bill_no, remarks,
	          attach_receipt, dist_pos_profile.

	The expense/credit accounts, party_type and default cost center are
	derived server-side from the Expense Type's account row for the company.
	"""
	guard = _ensure_doctype()
	if guard:
		return guard

	payload = parse_body()

	# --- Validation ---------------------------------------------------------
	local_id = payload.get("local_id")
	if not local_id:
		return fail(
			"local_id is required for idempotency",
			"معرّف محلي مطلوب لمنع التكرار",
		)
	for field, en, ar in (
		("expense_type", "expense_type is required", "نوع المصروف مطلوب"),
		("company", "company is required", "الشركة مطلوبة"),
		("posting_date", "posting_date is required", "تاريخ القيد مطلوب"),
	):
		if not payload.get(field):
			return fail(en, ar)

	try:
		amount = float(payload.get("amount") or 0)
	except (TypeError, ValueError):
		return fail("Invalid amount", "المبلغ غير صالح")
	if amount <= 0:
		return fail(
			"amount must be greater than 0", "يجب أن يكون المبلغ أكبر من صفر"
		)

	if not frappe.db.exists("Expense Type", payload["expense_type"]):
		return fail(
			f"Expense Type '{payload['expense_type']}' not found",
			"نوع المصروف غير موجود",
		)

	# --- Idempotency --------------------------------------------------------
	existing = _idempotency_lookup(local_id)
	if existing:
		row = frappe.db.get_value(
			DOCTYPE, existing, ["docstatus", "journal_entry"], as_dict=True
		)
		return ok(
			{
				"name": existing,
				"doc_type": DOCTYPE,
				"docstatus": row.docstatus if row else 0,
				"journal_entry": row.journal_entry if row else None,
				"duplicate": True,
			},
			en="Expense already exists — returning prior result",
			ar="المصروف موجود مسبقاً — إرجاع النتيجة السابقة",
		)

	# --- Build --------------------------------------------------------------
	doc_data: dict = {"doctype": DOCTYPE, "amount": amount}
	for f in CREATE_FIELDS:
		if f == "amount":
			continue
		if payload.get(f) not in (None, ""):
			doc_data[f] = payload[f]

	if frappe.db.has_column(DOCTYPE, "custom_mobile_local_id"):
		doc_data["custom_mobile_local_id"] = local_id

	# Tag with the Dist POS Profile and inherit its cost center when the
	# caller didn't send one (the controller keeps any value we set here).
	from match_erp.api.mobile._voucher import resolve_profile_name

	profile_name = resolve_profile_name(payload)
	if profile_name:
		if frappe.db.has_column(DOCTYPE, PROFILE_DB_FIELD):
			doc_data[PROFILE_DB_FIELD] = profile_name
		if not doc_data.get("cost_center"):
			profile_cc = frappe.db.get_value(
				"Dist POS Profile", profile_name, "cost_center"
			)
			if profile_cc:
				doc_data["cost_center"] = profile_cc

	doc = frappe.get_doc(doc_data)
	doc.insert(ignore_permissions=False)

	# Submitting posts the Journal Entry (handled by the Expense controller).
	if frappe.has_permission(DOCTYPE, "submit", doc=doc):
		try:
			doc.submit()
		except frappe.PermissionError:
			pass

	frappe.db.commit()

	return ok(
		{
			"name": doc.name,
			"doc_type": DOCTYPE,
			"docstatus": doc.docstatus,
			"amount": flt(doc.amount),
			"expense_account": doc.expense_account,
			"credit_account": doc.credit_account,
			"cost_center": doc.cost_center,
			"journal_entry": doc.get("journal_entry"),
			"duplicate": False,
		},
		en="Expense created",
		ar="تم إنشاء المصروف",
	)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
@frappe.whitelist()
@mobile_endpoint
def get_expenses(**kwargs):
	"""Paginated expense list.

	Optional: dist_pos_profile (scopes results), page, page_size, name,
	expense_type, company, party, from_date, to_date, modified_after, search.
	"""
	guard = _ensure_doctype()
	if guard:
		return guard

	body = parse_body()
	page, page_size = _pagination(body)

	filters: dict = {}

	# Optional tenant scope — only applied when a profile is supplied.
	profile = body.get(PROFILE_API_FIELD)
	if profile:
		filters[PROFILE_DB_FIELD] = profile

	client_filters = body.get("filters") or {}
	if isinstance(client_filters, dict):
		for key, value in client_filters.items():
			if key in FILTER_FIELDS and value not in (None, ""):
				filters[key] = value
	# Top-level convenience params.
	for key in ("name", "expense_type", "company", "party", "mode_of_payment"):
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

	total_records = frappe.db.count(DOCTYPE, filters=filters)
	total_pages = math.ceil(total_records / page_size) if total_records else 0

	rows = frappe.get_all(
		DOCTYPE,
		filters=filters,
		fields=_existing_fields(DOCTYPE, LIST_FIELDS),
		order_by="posting_date desc, modified desc",
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size,
	)
	rows = [_alias_profile(r) for r in rows]

	return ok(
		{
			"page": page,
			"page_size": page_size,
			"total_records": total_records,
			"total_pages": total_pages,
			"data": rows,
		},
		en="Expense list fetched",
		ar="تم جلب قائمة المصاريف",
	)


@frappe.whitelist()
@mobile_endpoint
def get_expense(**kwargs):
	"""One expense by name — same shape as a list row.

	Body: { "name": "EXP-2026-0001" }  (dist_pos_profile optional guard)
	"""
	guard = _ensure_doctype()
	if guard:
		return guard

	body = parse_body()
	name = body.get("name")
	if not name:
		return fail("name is required", "اسم المستند مطلوب")
	if not frappe.db.exists(DOCTYPE, name):
		return fail("Expense not found", "المصروف غير موجود")

	req_profile = body.get(PROFILE_API_FIELD)
	if req_profile:
		doc_profile = frappe.db.get_value(DOCTYPE, name, PROFILE_DB_FIELD)
		if (doc_profile or None) != req_profile:
			return fail("Expense not found", "المصروف غير موجود")

	if not frappe.has_permission(DOCTYPE, "read", doc=name):
		return fail("Permission denied", "ليس لديك صلاحية")

	rows = frappe.get_all(
		DOCTYPE,
		filters={"name": name},
		fields=_existing_fields(DOCTYPE, LIST_FIELDS),
		limit_page_length=1,
	)
	if not rows:
		return fail("Expense not found", "المصروف غير موجود")

	return ok(_alias_profile(rows[0]), en="Expense fetched", ar="تم جلب المصروف")


@frappe.whitelist()
@mobile_endpoint
def get_expense_types(**kwargs):
	"""Expense Types for the mobile picker.

	Body: { "company": "My Company" }  (optional — filters the account rows)
	Returns each type with the account row(s) that apply, so the app can show
	only types that are actually configured for the company.
	"""
	guard = _ensure_doctype()
	if guard:
		return guard

	body = parse_body()
	company = body.get("company")

	types = frappe.get_all(
		"Expense Type",
		fields=["name", "expense_type_name", "description", "modified"],
		order_by="expense_type_name asc",
		limit_page_length=0,
	)

	acct_filters: dict = {"parenttype": "Expense Type"}
	if company:
		acct_filters["company"] = company
	accounts = frappe.get_all(
		"Expense Type Account",
		filters=acct_filters,
		fields=[
			"parent",
			"company",
			"expense_account",
			"credit_account",
			"party_type",
			"default_cost_center",
		],
		limit_page_length=0,
	)

	by_parent: dict[str, list[dict]] = {}
	for a in accounts:
		by_parent.setdefault(a.pop("parent"), []).append(a)

	out = []
	for t in types:
		rows = by_parent.get(t["name"], [])
		# When a company filter is given, hide types with no matching row —
		# creating an expense with them would fail validation anyway.
		if company and not rows:
			continue
		t["accounts"] = rows
		out.append(t)

	return ok(
		{"items": out, "total": len(out)},
		en="Expense types fetched",
		ar="تم جلب أنواع المصاريف",
	)
