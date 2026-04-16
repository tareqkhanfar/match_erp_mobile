"""Shared response-envelope helpers for match_erp mobile API.

All mobile endpoints must return the flat envelope:

    {
        "success": bool,
        "data": <any JSON-serializable value>,
        "message_en": str,
        "message_ar": str,
    }

Frappe wraps the return value in {"message": <envelope>}; the mobile client
unwraps that automatically. Never return bare lists/strings.
"""

from __future__ import annotations

import json
import re
import traceback
from typing import Any

import frappe


# ---------------------------------------------------------------------------
# Error translations (EN -> AR)
#
# The mobile client does NOT auto-translate. The server must populate
# message_ar for every user-facing failure. Extend this table as new error
# paths surface. Keys are substrings matched case-insensitively against the
# English error message.
# ---------------------------------------------------------------------------
ERROR_TRANSLATIONS: dict[str, str] = {
	# Auth
	"Invalid login credentials": "بيانات تسجيل الدخول غير صحيحة",
	"Invalid username or password": "اسم المستخدم أو كلمة المرور غير صحيحة",
	"User disabled or missing": "المستخدم معطل أو غير موجود",
	"Session expired": "انتهت الجلسة. يرجى تسجيل الدخول مجدداً",
	"Not permitted": "ليس لديك صلاحية",
	"Permission denied": "ليس لديك صلاحية",
	"You do not have enough permissions": "ليس لديك صلاحيات كافية",
	# Not found
	"Customer not found": "العميل غير موجود",
	"Item not found": "الصنف غير موجود",
	"Company not found": "الشركة غير موجودة",
	"UOM not found": "وحدة القياس غير موجودة",
	"Price List not found": "قائمة الأسعار غير موجودة",
	"not found": "غير موجود",
	"does not exist": "غير موجود",
	# Sales / stock
	"Credit limit exceeded": "تم تجاوز الحد الائتماني",
	"Insufficient stock": "الكمية غير كافية في المخزون",
	"This record was modified by someone else": "تم تعديل السجل من قِبل مستخدم آخر",
	"Document has been modified": "تم تعديل المستند بعد فتحه",
	"Cannot edit submitted document": "لا يمكن تعديل مستند تم تقديمه",
	"Cannot cancel submitted document": "لا يمكن إلغاء مستند تم تقديمه",
	# Validation
	"is a mandatory field": "حقل إلزامي",
	"Mandatory fields required": "حقول إلزامية مطلوبة",
	"Missing required field": "حقل إلزامي مفقود",
	"Value missing for": "قيمة مفقودة لـ",
	"Field not permitted in query": "الحقل غير مسموح في الاستعلام",
}


STATUS_TRANSLATIONS: dict[str, str] = {
	"Open": "مفتوح",
	"Draft": "مسودة",
	"Submitted": "مقدم",
	"Cancelled": "ملغي",
	"Approved": "موافق عليه",
	"Rejected": "مرفوض",
	"Paid": "مدفوع",
	"Unpaid": "غير مدفوع",
	"Overdue": "متأخر",
	"Completed": "مكتمل",
}


# ---------------------------------------------------------------------------
# Envelope builders
# ---------------------------------------------------------------------------
def ok(data: Any = None, en: str = "OK", ar: str = "تم") -> dict:
	"""Success envelope."""
	return {
		"success": True,
		"data": data,
		"message_en": en,
		"message_ar": ar,
	}


def fail(en: str, ar: str | None = None, data: Any = None) -> dict:
	"""Failure envelope. If `ar` is not provided, we try to translate `en`."""
	if not ar:
		ar = translate_error(en) or en
	return {
		"success": False,
		"data": data,
		"message_en": en,
		"message_ar": ar,
	}


# ---------------------------------------------------------------------------
# Translation helper
# ---------------------------------------------------------------------------
def translate_error(en_message: str) -> str:
	"""Find the best Arabic translation for an English error substring."""
	if not en_message:
		return ""
	clean = re.sub(r"<[^>]+>", "", en_message).strip()
	lowered = clean.lower()
	# Prefer longer key matches first.
	for key in sorted(ERROR_TRANSLATIONS.keys(), key=len, reverse=True):
		if key.lower() in lowered:
			return ERROR_TRANSLATIONS[key]
	return ""


# ---------------------------------------------------------------------------
# JSON body parsing
# ---------------------------------------------------------------------------
def parse_body() -> dict:
	"""Parse the request body as JSON.

	The mobile client always sends Content-Type: application/json. We also
	accept form-encoded args as a fallback so endpoints are testable from
	the Frappe desk / curl without a body.
	"""
	raw = getattr(frappe.request, "data", None) if getattr(frappe, "request", None) else None
	if raw:
		try:
			if isinstance(raw, (bytes, bytearray)):
				raw = raw.decode("utf-8")
			data = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
			if isinstance(data, dict):
				return data
		except (json.JSONDecodeError, ValueError):
			pass
	# Fallback to form args
	form = getattr(frappe, "form_dict", {}) or {}
	# frappe.form_dict is a frappe._dict; strip the `cmd` key that Frappe injects
	return {k: v for k, v in form.items() if k != "cmd"}


# ---------------------------------------------------------------------------
# Exception decorator for whitelisted endpoints
# ---------------------------------------------------------------------------
def mobile_endpoint(fn):
	"""Wrap an endpoint so uncaught exceptions come back as a fail envelope
	with a bilingual message and the full traceback logged to Error Log.

	Use this on everything inside `match_erp.api.mobile.*` except endpoints
	that legitimately need Frappe's native 401/403 (auth.login uses this too,
	since we want JSON even on auth failure).
	"""

	def wrapper(*args, **kwargs):
		try:
			return fn(*args, **kwargs)
		except frappe.ValidationError as e:
			en = str(e) or "Validation error"
			frappe.log_error(
				title=f"match_erp.{fn.__module__}.{fn.__name__} ValidationError",
				message=traceback.format_exc(),
			)
			return fail(en)
		except frappe.PermissionError as e:
			frappe.log_error(
				title=f"match_erp.{fn.__module__}.{fn.__name__} PermissionError",
				message=traceback.format_exc(),
			)
			return fail(str(e) or "Permission denied", "ليس لديك صلاحية")
		except Exception as e:
			frappe.log_error(
				title=f"match_erp.{fn.__module__}.{fn.__name__} Error",
				message=traceback.format_exc(),
			)
			return fail(str(e) or "Internal error", translate_error(str(e)) or "حدث خطأ داخلي")

	wrapper.__name__ = fn.__name__
	wrapper.__module__ = fn.__module__
	wrapper.__doc__ = fn.__doc__
	# Preserve whitelist attribute if already applied (shouldn't be, but safe)
	for attr in ("whitelisted", "allow_guest"):
		if hasattr(fn, attr):
			setattr(wrapper, attr, getattr(fn, attr))
	return wrapper
