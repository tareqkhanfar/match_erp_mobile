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
import time
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

	Frappe v15 parses JSON bodies and populates frappe.form_dict with the
	decoded keys — so form_dict is the authoritative source for both
	Content-Type: application/json and form-encoded requests.

	We also try the raw request.data directly (for cases where form_dict
	hasn't been populated yet, e.g. bench execute).
	"""
	# Primary: frappe.form_dict is populated by Frappe for every request type
	# including JSON bodies (Frappe decodes JSON → form_dict automatically).
	form = getattr(frappe, "form_dict", {}) or {}
	result = {k: v for k, v in form.items() if k not in ("cmd", "data")}
	if result:
		return result

	# Fallback: raw request body (bench execute / curl with explicit JSON)
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

	return {}


# ---------------------------------------------------------------------------
# Request logging — every mobile call is recorded to "Mobile Request Log".
#
# Logging must NEVER break the real request, so every step is wrapped in a
# broad try/except. Login bodies are scrubbed of passwords. We skip logging
# the log doctype's own replay path implicitly because that calls the
# endpoint directly, and skip read-heavy sync GETs by default to keep the
# table small (controlled by _SHOULD_LOG).
# ---------------------------------------------------------------------------

# Endpoints we don't log by default — high-frequency catalog pulls that
# would flood the table and aren't useful to replay. Matched as substrings
# against the dotted endpoint path.
_SKIP_LOG_SUBSTRINGS = (
	".sync.",            # catalog sync pulls (items, customers, …)
	".catalog.",         # customer-app catalog reads
	".auth.get_current", # session ping
)


def _should_log(endpoint: str) -> bool:
	# Allow an explicit on/off switch via site_config without a code change.
	flag = frappe.conf.get("mobile_request_logging")
	if flag is False:
		return False
	if not endpoint:
		return False
	return not any(s in endpoint for s in _SKIP_LOG_SUBSTRINGS)


def _scrub(body: dict) -> dict:
	"""Redact secrets before persisting the request body."""
	if not isinstance(body, dict):
		return {}
	redacted = dict(body)
	for k in ("pwd", "password", "new_password", "api_secret"):
		if k in redacted:
			redacted[k] = "***"
	return redacted


def _record_request_log(
	endpoint: str,
	body: dict,
	result: Any,
	duration_ms: int,
	error: str | None = None,
) -> None:
	"""Persist one call to Mobile Request Log. Fully defensive — any
	failure here is swallowed so it can never affect the API response."""
	try:
		if not _should_log(endpoint):
			return

		success = bool(result.get("success")) if isinstance(result, dict) else False
		http_status = 200 if success else 400
		if error:
			http_status = 500

		# Pull a couple of useful fields out of the body/response so the
		# list view is scannable without opening each row.
		local_id = ""
		if isinstance(body, dict):
			local_id = str(body.get("local_id") or "")
		created_doc = ""
		if isinstance(result, dict):
			data = result.get("data")
			if isinstance(data, dict):
				created_doc = str(data.get("name") or "")

		try:
			req_json = json.dumps(_scrub(body), ensure_ascii=False, indent=2)
		except Exception:
			req_json = "{}"
		try:
			res_json = json.dumps(result, ensure_ascii=False, indent=2, default=str)
		except Exception:
			res_json = str(result)

		err_msg = error
		if not err_msg and isinstance(result, dict) and not success:
			err_msg = result.get("message_en")

		# frappe.request is a werkzeug LocalProxy that raises when accessed
		# outside an HTTP context (e.g. bench execute / background jobs).
		# Guard it so logging works in every caller.
		http_method = "POST"
		try:
			http_method = getattr(frappe.request, "method", None) or "POST"
		except Exception:
			pass

		doc = frappe.get_doc({
			"doctype": "Mobile Request Log",
			"method": http_method,
			"endpoint": endpoint,
			"http_status": http_status,
			"status": "Failed" if (error or not success) else "Success",
			"request_user": frappe.session.user,
			"duration_ms": duration_ms,
			"local_id": local_id,
			"created_doc": created_doc,
			"request_body": req_json,
			"response_body": res_json,
			"error_message": err_msg,
		})
		doc.insert(ignore_permissions=True)
		# Commit so the log survives even if the outer request later rolls
		# back (e.g. a validation error after a partial write).
		frappe.db.commit()
	except Exception:
		# Last resort — don't let logging failures bubble up. Record to the
		# native Error Log so we can still see that logging itself broke.
		try:
			frappe.log_error(
				title="Mobile Request Log write failed",
				message=traceback.format_exc(),
			)
		except Exception:
			pass


# ---------------------------------------------------------------------------
# Exception decorator for whitelisted endpoints
# ---------------------------------------------------------------------------
def mobile_endpoint(fn):
	"""Wrap an endpoint so uncaught exceptions come back as a fail envelope
	with a bilingual message and the full traceback logged to Error Log.

	Also records the call to Mobile Request Log (request body, response,
	status, timing) so failed requests can be inspected, edited, and
	replayed from the desk.

	Use this on everything inside `match_erp.api.mobile.*` except endpoints
	that legitimately need Frappe's native 401/403 (auth.login uses this too,
	since we want JSON even on auth failure).
	"""

	# Keys Frappe injects into every whitelisted call — strip before forwarding.
	_FRAPPE_INTERNAL_KEYS = frozenset({"cmd", "sid", "csrf_token", "type", "http_status_code"})

	endpoint_path = f"{fn.__module__}.{fn.__name__}"

	def wrapper(*args, **kwargs):
		# Drop Frappe's routing/session keys so they don't bleed into fn().
		clean_kwargs = {k: v for k, v in kwargs.items() if k not in _FRAPPE_INTERNAL_KEYS}
		# Snapshot the request body up-front for logging — the endpoint may
		# mutate form_dict.
		try:
			logged_body = parse_body()
		except Exception:
			logged_body = {}

		started = time.monotonic()
		result: Any = None
		error: str | None = None
		try:
			result = fn(*args, **clean_kwargs)
			return result
		except frappe.ValidationError as e:
			en = str(e) or "Validation error"
			error = en
			frappe.log_error(
				title=f"match_erp.{endpoint_path} ValidationError",
				message=traceback.format_exc(),
			)
			result = fail(en)
			return result
		except frappe.PermissionError as e:
			error = str(e) or "Permission denied"
			frappe.log_error(
				title=f"match_erp.{endpoint_path} PermissionError",
				message=traceback.format_exc(),
			)
			result = fail(str(e) or "Permission denied", "ليس لديك صلاحية")
			return result
		except Exception as e:
			error = str(e) or "Internal error"
			frappe.log_error(
				title=f"match_erp.{endpoint_path} Error",
				message=traceback.format_exc(),
			)
			result = fail(str(e) or "Internal error", translate_error(str(e)) or "حدث خطأ داخلي")
			return result
		finally:
			duration_ms = int((time.monotonic() - started) * 1000)
			_record_request_log(
				endpoint_path, logged_body, result, duration_ms, error
			)

	wrapper.__name__ = fn.__name__
	wrapper.__module__ = fn.__module__
	wrapper.__doc__ = fn.__doc__
	# Preserve whitelist attribute if already applied (shouldn't be, but safe)
	for attr in ("whitelisted", "allow_guest"):
		if hasattr(fn, attr):
			setattr(wrapper, attr, getattr(fn, attr))
	return wrapper




