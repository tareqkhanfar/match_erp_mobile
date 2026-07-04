"""Shared voucher-document helper for sales / purchase / return endpoints.

The mobile client sends a generic payload for every transactional voucher:

    {
        "local_id":        "uuid-ish idempotency key",
        "voucher_type":    "sales_invoice",        # registry id, informational
        "doc_type":        "Sales Invoice",         # ERPNext DocType name
        "party_type":      "Customer" | "Supplier",
        "party":           "<customer or supplier name>",
        "posting_date":    "YYYY-MM-DD",
        "company":         "<company name>",
        "price_list":      "<price list name>",
        "currency":        "ILS",
        "is_paid":         true | false,            # Sales/Purchase Invoice
        "mode_of_payment": "Cash",                  # required if is_paid
        "return_against":  "SI-001",                # returns only
        "total_discount_pct": 0,
        "total_discount_amt": 0,
        "notes":           "free text",
        "items": [
            {
                "item_code":          "ITEM-001",
                "uom":                "Carton",
                "conversion_factor":  12,
                "qty":                2,
                "rate":               120.0,
                "discount_percentage": 0,
                "discount_amount":    0
            }
        ]
    }

Responsibilities:
- Idempotency on `custom_mobile_local_id`.
- Honor `conversion_factor` — ERPNext computes stock_qty/stock_uom_rate
  automatically when both uom and conversion_factor are set on the line.
- Map `party` → `customer` or `supplier` depending on doctype.
- Handle Sales Order's `transaction_date` vs `posting_date`.
- Handle `is_paid` + `mode_of_payment` on Sales/Purchase Invoice.
- For returns: set `is_return = 1` and `return_against`; the client must
  send negative qty.
"""

from __future__ import annotations

import frappe

from match_erp.api.mobile.envelope import fail, ok


# Doctypes we support through this helper.
SALES_DOCTYPES = {"Sales Order", "Sales Invoice"}
PURCHASE_DOCTYPES = {"Purchase Order", "Purchase Invoice"}
ORDER_DOCTYPES = {"Sales Order", "Purchase Order"}  # use transaction_date
INVOICE_DOCTYPES = {"Sales Invoice", "Purchase Invoice"}  # support is_paid


def _idempotency_lookup(doctype: str, local_id: str) -> str | None:
	if not local_id:
		return None
	if not frappe.db.has_column(doctype, "custom_mobile_local_id"):
		return None
	existing = frappe.db.get_value(doctype, {"custom_mobile_local_id": local_id}, "name")
	return existing or None


def _validate_payload(payload: dict, doctype: str, is_return: bool) -> tuple[bool, str, str]:
	if not payload.get("local_id"):
		return False, "local_id is required for idempotency", "معرّف محلي مطلوب لمنع التكرار"

	# Accept either new `party` or legacy `customer`/`supplier`.
	party = payload.get("party") or payload.get("customer") or payload.get("supplier")
	if not party:
		return False, "party is required", "الطرف (عميل/مورد) مطلوب"

	if not payload.get("company"):
		return False, "company is required", "الشركة مطلوبة"

	if is_return and not payload.get("return_against"):
		return (
			False,
			"return_against is required for return documents",
			"يجب تحديد المستند الأصلي للمرتجع",
		)

	items = payload.get("items") or []
	if not isinstance(items, list) or not items:
		return False, "At least one item is required", "يجب إضافة صنف واحد على الأقل"

	for i, line in enumerate(items, start=1):
		if not line.get("item_code"):
			return (
				False,
				f"item_code is required on line {i}",
				f"رمز الصنف مطلوب في السطر {i}",
			)
		try:
			qty = float(line.get("qty") or 0)
		except (TypeError, ValueError):
			return False, f"Invalid qty on line {i}", f"كمية غير صالحة في السطر {i}"
		# For returns, qty must be negative; for everything else, positive.
		if is_return:
			if qty >= 0:
				return (
					False,
					f"qty must be negative on return line {i}",
					f"يجب أن تكون الكمية سالبة في سطر المرتجع {i}",
				)
		else:
			if qty <= 0:
				return (
					False,
					f"qty must be greater than 0 on line {i}",
					f"يجب أن تكون الكمية أكبر من صفر في السطر {i}",
				)

	if payload.get("is_paid"):
		mop = payload.get("mode_of_payment")
		if not mop:
			return (
				False,
				"mode_of_payment is required when is_paid = true",
				"وسيلة الدفع مطلوبة عند تفعيل خيار مدفوع",
			)
		# A paid invoice creates a Payment Entry, which needs the mode of
		# payment to have a bank/cash account configured for this company.
		# Validate UP FRONT so we never post an invoice that we then can't
		# mark paid (which previously left it silently "Unpaid").
		if doctype in INVOICE_DOCTYPES:
			company = payload.get("company")
			account = frappe.db.get_value(
				"Mode of Payment Account",
				{"parent": mop, "company": company},
				"default_account",
			)
			if not account:
				return (
					False,
					f"Mode of Payment '{mop}' has no default account for "
					f"company '{company}'. Configure it in ERPNext before "
					f"taking paid invoices.",
					f"وسيلة الدفع '{mop}' ليس لها حساب افتراضي للشركة "
					f"'{company}'. يرجى ضبطها في ERPNext قبل إنشاء فواتير مدفوعة.",
				)

	return True, "", ""


def _build_items(items_payload: list[dict], schedule_date: str | None = None) -> list[dict]:
	rows = []
	for line in items_payload:
		base_rate = float(line.get("rate") or 0)
		disc_pct = float(line.get("discount_percentage") or 0)
		disc_amt = float(line.get("discount_amount") or 0)

		row = {
			"item_code": line["item_code"],
			"qty": float(line.get("qty") or 0),
			"uom": line.get("uom") or None,
		}

		# Per-item discount handling. The client sends the ORIGINAL unit price
		# in `rate` plus a separate per-unit discount. For ERPNext to actually
		# apply and RECORD the discount on the line, we set `price_list_rate`
		# to the base price and provide the discount; ERPNext then derives the
		# net `rate`. We also pin `rate` to the computed net so a server-side
		# price-list/pricing-rule lookup can't silently change the figure the
		# user approved (the header sets ignore_pricing_rule=1 for the same
		# reason).
		if disc_pct > 0:
			net_rate = base_rate * (1 - disc_pct / 100.0)
			row["price_list_rate"] = base_rate
			row["base_price_list_rate"] = base_rate
			row["discount_percentage"] = disc_pct
			row["rate"] = net_rate
		elif disc_amt > 0:
			net_rate = base_rate - disc_amt
			if net_rate < 0:
				net_rate = 0.0
			row["price_list_rate"] = base_rate
			row["base_price_list_rate"] = base_rate
			row["discount_amount"] = disc_amt
			row["rate"] = net_rate
		else:
			row["rate"] = base_rate

		cf = line.get("conversion_factor")
		if cf is not None:
			try:
				row["conversion_factor"] = float(cf) or 1.0
			except (TypeError, ValueError):
				row["conversion_factor"] = 1.0
		# Sales Order lines need `delivery_date`; Purchase Order lines need
		# `schedule_date`. Fall back to the header-level value if the line
		# doesn't supply its own.
		line_date = line.get("delivery_date") or line.get("schedule_date") or schedule_date
		if line_date:
			row["delivery_date"] = line_date
			row["schedule_date"] = line_date
		rows.append(row)
	return rows


def resolve_profile_name(payload: dict) -> str | None:
	"""Resolve the Dist POS Profile name that should tag a voucher.

	Order:
	  1. `dist_pos_profile` from the payload (the device's active profile),
	     if it names an existing profile.
	  2. The server-resolved profile for the session user.
	Returns None when neither resolves — the voucher is then left untagged
	(i.e. treated as created directly in ERPNext).
	"""
	supplied = payload.get("dist_pos_profile")
	if supplied and frappe.db.exists("Dist POS Profile", supplied):
		return supplied
	try:
		from match_erp.match_erp.doctype.dist_pos_profile.dist_pos_profile import (
			resolve_for_user,
		)

		profile = resolve_for_user(frappe.session.user)
		if profile is not None:
			return profile.get("name")
	except Exception:
		pass
	return None


def _enforce_profile(doctype: str, payload: dict) -> tuple[bool, str, str]:
	"""Apply Dist POS Profile rules server-side. The mobile UI already
	hides/disables actions per profile, but we re-check here because the
	client is untrusted — an old build or a tampered request must not be
	able to exceed the configured discount cap.

	Returns (ok, en, ar). Only enforces on sales documents (profiles are
	a sales-side concept); purchase docs pass through.
	"""
	if doctype not in SALES_DOCTYPES:
		return True, "", ""
	try:
		from match_erp.match_erp.doctype.dist_pos_profile.dist_pos_profile import (
			resolve_for_user,
		)

		profile = resolve_for_user(frappe.session.user)
	except Exception:
		profile = None
	if profile is None:
		return True, "", ""

	max_disc = float(profile.get("max_discount_pct") or 100)
	allow_discount_change = bool(profile.get("allow_discount_change"))
	block_oversell = bool(profile.get("block_sale_beyond_available_qty"))

	for i, line in enumerate(payload.get("items") or [], start=1):
		try:
			disc = float(line.get("discount_percentage") or 0)
		except (TypeError, ValueError):
			disc = 0.0
		# If the profile forbids discount edits, any non-zero discount is
		# rejected outright.
		if not allow_discount_change and disc > 0:
			return (
				False,
				f"Discount is not permitted by your profile (line {i}).",
				f"الخصم غير مسموح به وفق ملفك (السطر {i}).",
			)
		if disc > max_disc:
			return (
				False,
				f"Discount {disc:g}% exceeds the allowed maximum of {max_disc:g}% (line {i}).",
				f"الخصم {disc:g}% يتجاوز الحد المسموح {max_disc:g}% (السطر {i}).",
			)
		# Block selling more than is on hand when the profile demands it.
		if block_oversell:
			code = line.get("item_code")
			try:
				qty = float(line.get("qty") or 0)
			except (TypeError, ValueError):
				qty = 0.0
			if code and qty > 0:
				on_hand = frappe.db.sql(
					"SELECT COALESCE(SUM(actual_qty),0) FROM `tabBin` WHERE item_code=%s",
					(code,),
				)
				available = float(on_hand[0][0]) if on_hand and on_hand[0] else 0.0
				if qty > available:
					return (
						False,
						f"Only {available:g} of {code} available (line {i}).",
						f"المتوفر فقط {available:g} من {code} (السطر {i}).",
					)
	return True, "", ""


def create_voucher(doctype: str, payload: dict, is_return: bool = False) -> dict:
	"""Create a sales/purchase document (or return) from the generic mobile payload."""

	valid, en, ar = _validate_payload(payload, doctype, is_return)
	if not valid:
		return fail(en, ar)

	# Server-side profile enforcement (discount cap, discount permission).
	ok_profile, pen, par = _enforce_profile(doctype, payload)
	if not ok_profile:
		return fail(pen, par)

	local_id = payload["local_id"]

	existing = _idempotency_lookup(doctype, local_id)
	if existing:
		status = frappe.db.get_value(doctype, existing, "status") or ""
		return ok(
			{
				"name": existing,
				"doc_type": doctype,
				"status": status,
				"duplicate": True,
			},
			en="Document already exists — returning prior result",
			ar="المستند موجود مسبقاً — إرجاع النتيجة السابقة",
		)

	# --- Party mapping ------------------------------------------------------
	party = payload.get("party") or payload.get("customer") or payload.get("supplier")

	# Sales Order needs `delivery_date`, Purchase Order needs `schedule_date`.
	# Fall back to posting_date if the client didn't send a specific value.
	header_schedule_date = (
		payload.get("delivery_date")
		or payload.get("schedule_date")
		or payload.get("posting_date")
	)

	doc_data: dict = {
		"doctype": doctype,
		"company": payload["company"],
		"currency": payload.get("currency"),
		"items": _build_items(payload.get("items") or [], schedule_date=header_schedule_date),
		"custom_mobile_local_id": local_id,
	}

	# Tag the voucher with the Dist POS Profile that created it from mobile.
	# Empty stays empty → "created in ERPNext, not from mobile".
	profile_name = resolve_profile_name(payload)
	if profile_name and frappe.db.has_column(doctype, "custom_dist_pos_profile"):
		doc_data["custom_dist_pos_profile"] = profile_name

	# Exchange rate: when the voucher currency matches the company currency
	# the rate is 1.0 — pass it explicitly so ERPNext's validate doesn't
	# reject the doc with "Conversion Rate required". Multi-currency setups
	# can still override via the payload.
	company_currency = frappe.db.get_value(
		"Company", payload["company"], "default_currency"
	)
	voucher_currency = payload.get("currency") or company_currency
	if voucher_currency:
		if "conversion_rate" not in doc_data and payload.get("conversion_rate") is None:
			doc_data["conversion_rate"] = (
				1.0 if voucher_currency == company_currency else float(
					payload.get("conversion_rate") or 1.0
				)
			)
		# Price-list currency conversion rate — same logic.
		if payload.get("plc_conversion_rate") is not None:
			doc_data["plc_conversion_rate"] = float(payload["plc_conversion_rate"])
		else:
			doc_data["plc_conversion_rate"] = 1.0

	# Discount — accept both new names (total_discount_*) and old
	# (additional_discount_percentage / discount_amount).
	pct = payload.get("total_discount_pct")
	if pct is None:
		pct = payload.get("additional_discount_percentage")
	amt = payload.get("total_discount_amt")
	if amt is None:
		amt = payload.get("discount_amount")
	if pct is not None:
		doc_data["additional_discount_percentage"] = float(pct or 0)
	if amt is not None:
		doc_data["discount_amount"] = float(amt or 0)

	if payload.get("notes"):
		# Frappe Sales/Purchase doctypes use `remarks` for free-text notes.
		doc_data["remarks"] = payload["notes"]

	# --- Customer vs Supplier ----------------------------------------------
	if doctype in SALES_DOCTYPES:
		doc_data["customer"] = party
		if payload.get("price_list"):
			doc_data["selling_price_list"] = payload["price_list"]
		# The client sends the exact per-item rate/discount the user approved.
		# Skip Pricing Rules so ERPNext doesn't override those figures with an
		# automatic rule during validate.
		doc_data["ignore_pricing_rule"] = 1
	elif doctype in PURCHASE_DOCTYPES:
		doc_data["supplier"] = party
		if payload.get("price_list"):
			doc_data["buying_price_list"] = payload["price_list"]
	else:
		return fail(f"Unsupported doctype: {doctype}", f"نوع المستند غير مدعوم: {doctype}")

	# --- Date field: Order vs Invoice --------------------------------------
	posting_date = payload.get("posting_date")
	if posting_date:
		if doctype in ORDER_DOCTYPES:
			doc_data["transaction_date"] = posting_date
		else:
			doc_data["posting_date"] = posting_date

	# Sales Order wants `delivery_date` on the header; Purchase Order wants
	# `schedule_date`. Both default to posting_date/delivery_date/schedule_date
	# as sent in the payload.
	if doctype == "Sales Order":
		doc_data["delivery_date"] = header_schedule_date
	elif doctype == "Purchase Order":
		doc_data["schedule_date"] = header_schedule_date

	# --- Return handling ----------------------------------------------------
	if is_return:
		doc_data["is_return"] = 1
		doc_data["return_against"] = payload["return_against"]

	# --- Create -------------------------------------------------------------
	doc = frappe.get_doc(doc_data)
	doc.insert(ignore_permissions=False)

	if frappe.has_permission(doctype, "submit", doc=doc):
		try:
			doc.submit()
		except frappe.PermissionError:
			pass

	# --- is_paid: create a Payment Entry after submit ---------------------
	# ERPNext v15 non-POS invoices don't use the `payments` child table.
	# The correct flow is to submit the invoice then create a Payment Entry
	# against it, which sets outstanding_amount → 0 and status → Paid.
	if doctype in INVOICE_DOCTYPES and payload.get("is_paid") and doc.docstatus == 1:
		try:
			mop = payload["mode_of_payment"]
			bank_account = frappe.db.get_value(
				"Mode of Payment Account",
				{"parent": mop, "company": doc.company},
				"default_account",
			)
			# Defensive: _validate_payload already checked this, but guard
			# again so we never insert a Payment Entry with an empty
			# paid_to/paid_from (which would post the invoice as unpaid).
			if not bank_account:
				raise frappe.ValidationError(
					f"Mode of Payment '{mop}' has no default account for "
					f"company '{doc.company}'."
				)
			is_sales = doctype == "Sales Invoice"
			pe_data = {
				"doctype": "Payment Entry",
				"payment_type": "Receive" if is_sales else "Pay",
				"posting_date": doc.posting_date or frappe.utils.today(),
				"company": doc.company,
				"mode_of_payment": mop,
				"party_type": "Customer" if is_sales else "Supplier",
				"party": doc.customer if is_sales else doc.supplier,
				"paid_amount": doc.grand_total,
				"received_amount": doc.grand_total,
				"references": [{
					"reference_doctype": doctype,
					"reference_name": doc.name,
					"allocated_amount": doc.grand_total,
				}],
				"reference_no": doc.name,
				"reference_date": doc.posting_date or frappe.utils.today(),
			}
			# Carry the same Dist POS Profile tag onto the auto-payment.
			if profile_name and frappe.db.has_column(
				"Payment Entry", "custom_dist_pos_profile"
			):
				pe_data["custom_dist_pos_profile"] = profile_name
			# Sales Invoice → money flows INTO a bank/cash account (paid_to).
			# Purchase Invoice → money flows OUT of a bank/cash account (paid_from).
			if is_sales:
				pe_data["paid_to"] = bank_account
			else:
				pe_data["paid_from"] = bank_account
			pe = frappe.get_doc(pe_data)
			pe.insert(ignore_permissions=True)
			pe.submit()
		except Exception as pe_err:
			# The client asked for a PAID invoice. If we can't create the
			# Payment Entry we must NOT leave a posted-but-unpaid invoice
			# and report success — that silently loses the payment. Roll
			# the whole transaction back and surface the error so the
			# client can fix the configuration and retry.
			frappe.log_error(
				title=f"Payment Entry failed for {doc.name}",
				message=frappe.get_traceback(),
			)
			frappe.db.rollback()
			return fail(
				f"Invoice not created: could not record payment for mode "
				f"'{payload.get('mode_of_payment')}'. {pe_err}",
				f"لم يتم إنشاء الفاتورة: تعذّر تسجيل الدفعة لوسيلة الدفع "
				f"'{payload.get('mode_of_payment')}'.",
			)

	frappe.db.commit()

	# Re-fetch status after payment entry may have changed it.
	final_status = frappe.db.get_value(doctype, doc.name, "status") or doc.status or "Draft"

	return ok(
		{
			"name": doc.name,
			"doc_type": doctype,
			"status": final_status,
			"duplicate": False,
		},
		en=f"{doctype} created",
		ar=f"تم إنشاء {doctype}",
	)
