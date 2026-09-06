"""Dist POS Profile — central settings profile for Match ERP Mobile.

Combines the ideas of ERPNext's POS Profile (per-user binding, company,
price list, warehouse, permission toggles) and POS Settings (global
behaviour flags) into a single doctype that the distributor mobile app
reads. The mobile app NEVER writes these — they are administered here in
ERPNext and synced down read-only.

Resolution for a given user (see `resolve_for_user`):
  1. A profile whose `applicable_for_users` lists the user (first match).
  2. Otherwise the profile flagged `is_default` (for the user's company
     if resolvable, else any).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class DistPOSProfile(Document):
	def validate(self):
		self._validate_discount()
		self._validate_single_default()
		self._validate_unique_users()

	def _validate_discount(self):
		# The desk posts numeric fields as strings before Frappe coerces
		# them; cast defensively so validation never trips on a str.
		try:
			pct = float(self.max_discount_pct) if self.max_discount_pct not in (None, "") else 100.0
		except (TypeError, ValueError):
			frappe.throw(_("Max Discount % must be a number between 0 and 100."))
		if pct < 0 or pct > 100:
			frappe.throw(_("Max Discount % must be between 0 and 100."))
		self.max_discount_pct = pct

	def _validate_single_default(self):
		"""At most one default profile per company. Keeps resolution
		deterministic — otherwise a user with no explicit mapping could
		match two defaults."""
		if not self.is_default:
			return
		existing = frappe.get_all(
			"Dist POS Profile",
			filters={
				"is_default": 1,
				"company": self.company,
				"name": ["!=", self.name or ""],
				"disabled": 0,
			},
			pluck="name",
		)
		if existing:
			frappe.throw(
				_("{0} is already the default profile for {1}. Only one default per company is allowed.")
				.format(existing[0], self.company)
			)

	def _validate_unique_users(self):
		"""A user should map to at most one profile so resolution is
		unambiguous. Block adding a user already bound elsewhere."""
		users = [r.user for r in (self.applicable_for_users or []) if r.user]
		if not users:
			return
		# Within this doc — no dupes.
		if len(users) != len(set(users)):
			frappe.throw(_("The same user is listed more than once."))
		# Across other profiles.
		clash = frappe.db.sql(
			"""
			SELECT DISTINCT ppu.user
			FROM `tabPOS Profile User` ppu
			INNER JOIN `tabDist POS Profile` p ON p.name = ppu.parent
			WHERE ppu.parenttype = 'Dist POS Profile'
			  AND p.name != %s
			  AND p.disabled = 0
			  AND ppu.user IN %s
			""",
			(self.name or "", tuple(users)),
			as_dict=True,
		)
		if clash:
			frappe.throw(
				_("User {0} is already assigned to another profile.")
				.format(clash[0]["user"])
			)


# ---------------------------------------------------------------------------
# Resolution + serialization — shared by the mobile API.
# ---------------------------------------------------------------------------

# Maps doctype fieldnames → the keys the mobile client expects. Keeping a
# single dict here means the API, login payload, and any future consumer
# all agree on the shape.
_SETTING_FIELDS = [
	"default_doc_type",
	"selling_price_list",
	"buying_price_list",
	"default_warehouse",
	"default_customer",
	"currency",
	"max_discount_pct",
	"allow_customer_create",
	"allow_customer_edit",
	"allow_item_create",
	"allow_item_edit",
	"confirm_before_submit",
	"allow_rate_change",
	"allow_discount_change",
	"enable_bonus_qty",
	"validate_stock_on_save",
	"show_customer_balance_on_voucher",
	"disable_round_amount",
	"compact_mode",
	"show_images",
	"sync_page_size",
	"auto_sync_on_launch",
	"sync_over_mobile_data",
	"company",
	"company_address",
	"company_phone",
	"company_email",
	"company_tax_id",
	"company_logo",
	"app_logo",
	# POS / advanced flags
	"hide_unavailable_items",
	"auto_add_item_to_cart",
	"ignore_pricing_rule",
	"print_receipt_on_order_complete",
	"allow_partial_payment",
	"block_sale_beyond_available_qty",
	"apply_discount_on",
	# Voucher-type visibility — the mobile hub hides anything unticked.
	"show_sales_order",
	"show_sales_invoice",
	"show_sales_return",
	"show_purchase_order",
	"show_purchase_invoice",
	"show_purchase_return",
	"show_payment_entry",
	"show_payment_receipt",
	"show_expense",
	"show_stock_entry",
]

_BOOL_FIELDS = {
	"show_sales_order",
	"show_sales_invoice",
	"show_sales_return",
	"show_purchase_order",
	"show_purchase_invoice",
	"show_purchase_return",
	"show_payment_entry",
	"show_payment_receipt",
	"show_expense",
	"show_stock_entry",
	"allow_customer_create",
	"allow_customer_edit",
	"allow_item_create",
	"allow_item_edit",
	"confirm_before_submit",
	"allow_rate_change",
	"allow_discount_change",
	"enable_bonus_qty",
	"validate_stock_on_save",
	"show_customer_balance_on_voucher",
	"disable_round_amount",
	"compact_mode",
	"show_images",
	"auto_sync_on_launch",
	"sync_over_mobile_data",
	"hide_unavailable_items",
	"auto_add_item_to_cart",
	"ignore_pricing_rule",
	"print_receipt_on_order_complete",
	"allow_partial_payment",
	"block_sale_beyond_available_qty",
}


def resolve_for_user(user: str | None = None) -> "DistPOSProfile | None":
	"""Return the Dist POS Profile that governs `user`, or None.

	Order: explicit user mapping → company default → any default.
	"""
	user = user or frappe.session.user
	if not user or user in ("Guest",):
		return None

	# 1. Explicit user binding.
	mapped = frappe.db.sql(
		"""
		SELECT p.name
		FROM `tabPOS Profile User` ppu
		INNER JOIN `tabDist POS Profile` p ON p.name = ppu.parent
		WHERE ppu.parenttype = 'Dist POS Profile'
		  AND ppu.user = %s
		  AND p.disabled = 0
		ORDER BY ppu.`default` DESC, p.modified DESC
		LIMIT 1
		""",
		(user,),
		as_dict=True,
	)
	if mapped:
		return frappe.get_cached_doc("Dist POS Profile", mapped[0]["name"])

	# 2. Company default — use the user's default company when available.
	company = (
		frappe.defaults.get_user_default("Company", user)
		or frappe.db.get_single_value("Global Defaults", "default_company")
	)
	if company:
		row = frappe.get_all(
			"Dist POS Profile",
			filters={"is_default": 1, "disabled": 0, "company": company},
			pluck="name",
			limit=1,
		)
		if row:
			return frappe.get_cached_doc("Dist POS Profile", row[0])

	# 3. Any default.
	row = frappe.get_all(
		"Dist POS Profile",
		filters={"is_default": 1, "disabled": 0},
		pluck="name",
		limit=1,
	)
	if row:
		return frappe.get_cached_doc("Dist POS Profile", row[0])
	return None


def is_allowed(flag: str, default: bool = True) -> bool:
	"""Convenience for endpoints: does the current user's profile grant
	`flag` (e.g. 'allow_customer_create')? When no profile is configured
	we return `default` so a fresh install isn't accidentally locked
	down."""
	try:
		profile = resolve_for_user(frappe.session.user)
	except Exception:
		profile = None
	if profile is None:
		return default
	val = profile.get(flag)
	return bool(val) if val is not None else default


def serialize(profile: "DistPOSProfile | None") -> dict:
	"""Flat settings dict the mobile client consumes. When no profile is
	found we return sane defaults so the app still works (it just won't be
	locked down)."""
	if profile is None:
		return _default_settings()

	out: dict = {"profile_name": profile.name, "has_profile": True}
	for f in _SETTING_FIELDS:
		val = profile.get(f)
		if f in _BOOL_FIELDS:
			out[f] = bool(val)
		elif f == "max_discount_pct":
			out[f] = float(val if val is not None else 100)
		elif f == "sync_page_size":
			out[f] = int(val or 500)
		else:
			out[f] = val if val is not None else ""
	# Absolutize logo paths so the client can fetch them directly.
	if out.get("company_logo"):
		out["company_logo"] = _absolutize(out["company_logo"])
	if out.get("app_logo"):
		out["app_logo"] = _absolutize(out["app_logo"])
	# Item / customer group filters → plain string lists.
	out["item_groups"] = [
		r.item_group for r in (profile.get("item_groups") or []) if r.get("item_group")
	]
	out["customer_groups"] = [
		r.customer_group
		for r in (profile.get("customer_groups") or [])
		if r.get("customer_group")
	]
	return out


def _default_settings() -> dict:
	"""Fallback when no profile is configured — mirrors the app's old
	hard-coded defaults so behaviour is unchanged until an admin sets up
	a profile."""
	return {
		"profile_name": None,
		"has_profile": False,
		"default_doc_type": "Sales Invoice",
		"selling_price_list": "",
		"buying_price_list": "",
		"default_warehouse": "",
		"default_customer": "",
		"currency": "",
		"max_discount_pct": 100.0,
		"allow_customer_create": True,
		"allow_customer_edit": True,
		"allow_item_create": False,
		"allow_item_edit": False,
		"confirm_before_submit": True,
		"allow_rate_change": True,
		"allow_discount_change": True,
		"validate_stock_on_save": False,
		"show_customer_balance_on_voucher": False,
		"enable_bonus_qty": False,
		"disable_round_amount": False,
		"show_sales_order": True,
		"show_sales_invoice": True,
		"show_sales_return": True,
		"show_purchase_order": True,
		"show_purchase_invoice": True,
		"show_purchase_return": True,
		"show_payment_entry": True,
		"show_payment_receipt": True,
		"show_expense": True,
		"show_stock_entry": True,

		"compact_mode": False,
		"show_images": True,
		"sync_page_size": 500,
		"auto_sync_on_launch": True,
		"sync_over_mobile_data": True,
		"company": "",
		"company_address": "",
		"company_phone": "",
		"company_email": "",
		"company_tax_id": "",
		"company_logo": "",
		"app_logo": "",
		"hide_unavailable_items": False,
		"auto_add_item_to_cart": False,
		"ignore_pricing_rule": False,
		"print_receipt_on_order_complete": False,
		"allow_partial_payment": False,
		"block_sale_beyond_available_qty": False,
		"apply_discount_on": "Grand Total",
		"item_groups": [],
		"customer_groups": [],
	}


def _absolutize(file_url: str | None) -> str | None:
	if not file_url:
		return None
	if file_url.startswith(("http://", "https://")):
		return file_url
	try:
		from frappe.utils import get_url

		return f"{get_url().rstrip('/')}{file_url if file_url.startswith('/') else '/' + file_url}"
	except Exception:
		return file_url
