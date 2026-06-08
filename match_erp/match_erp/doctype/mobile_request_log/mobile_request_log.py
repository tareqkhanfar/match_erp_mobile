"""Mobile Request Log — controller.

Every call that passes through `match_erp.api.mobile.*` (and the
customer-app endpoints) is recorded here by the `mobile_endpoint`
decorator. The log captures the raw JSON body, the response, the HTTP
status, and timing.

The request body is editable through a custom HTML/JS JSON editor on the
form (see mobile_request_log.js). That editor writes the edited payload
straight back into `request_body`, so the controller only needs to:

1. **Validate** that `request_body` is parseable JSON on save (so a
   later replay can rely on it).
2. **Replay** — re-dispatch the (possibly edited) request to its
   original endpoint, server-side, as the original user.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class MobileRequestLog(Document):
	def validate(self):
		"""Keep `request_body` as valid, pretty-printed JSON. The visual
		editor already emits clean JSON, but a user may also hand-edit the
		raw field — normalise it here and reject anything unparseable so a
		replay never trips on bad input."""
		if not self.request_body:
			return
		try:
			data = json.loads(self.request_body)
		except (json.JSONDecodeError, TypeError, ValueError):
			frappe.throw(_("Request Body is not valid JSON."))
		self.request_body = json.dumps(data, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Replay action (called from the desk button).
# ---------------------------------------------------------------------------
@frappe.whitelist()
def replay_request(name: str) -> dict:
	"""Re-dispatch a logged request to its original endpoint.

	Runs the original whitelisted function server-side with the (possibly
	edited) request body, impersonating the original user so permission
	checks and session-customer scoping behave exactly as they did for
	the real call. Returns the new response envelope.
	"""
	frappe.only_for("System Manager")

	log = frappe.get_doc("Mobile Request Log", name)
	endpoint = log.endpoint
	if not endpoint:
		frappe.throw(_("This log has no endpoint to replay."))

	try:
		body = json.loads(log.request_body) if log.request_body else {}
	except (json.JSONDecodeError, TypeError):
		frappe.throw(_("Request Body is not valid JSON. Fix it and save first."))

	if not isinstance(body, dict):
		frappe.throw(_("Only object (dict) request bodies can be replayed."))

	# Resolve the dotted endpoint path to a callable. `endpoint` is stored
	# as the full module path, e.g.
	# "match_erp.api.mobile.sales.create_sales_invoice".
	try:
		fn = frappe.get_attr(endpoint)
	except Exception:
		frappe.throw(_("Cannot resolve endpoint: {0}").format(endpoint))

	# Impersonate the original user so the replay sees the same identity.
	original_user = frappe.session.user
	target_user = log.request_user or original_user

	# Push the edited body into form_dict so parse_body() inside the
	# endpoint reads our values, then restore afterwards.
	saved_form = dict(frappe.local.form_dict)
	result_envelope = None
	error = None
	try:
		if target_user and target_user != original_user:
			frappe.set_user(target_user)
		frappe.local.form_dict = frappe._dict(body)
		# The wrapped function is `mobile_endpoint(fn)` — calling it returns
		# the envelope dict directly (it never raises for handled errors).
		result_envelope = fn()
	except Exception as e:  # noqa: BLE001 — surface any replay failure
		error = str(e)
		frappe.log_error(
			title=f"Mobile Request Replay failed: {endpoint}",
			message=frappe.get_traceback(),
		)
	finally:
		frappe.local.form_dict = frappe._dict(saved_form)
		if target_user and target_user != original_user:
			frappe.set_user(original_user)

	# Record the replay outcome on the log row.
	log.reload()
	log.replay_count = (log.replay_count or 0) + 1
	log.last_replayed_at = now_datetime()
	log.status = "Replayed"
	if result_envelope is not None:
		try:
			log.response_body = json.dumps(
				result_envelope, ensure_ascii=False, indent=2, default=str
			)
		except Exception:
			log.response_body = str(result_envelope)
		ok = bool(result_envelope.get("success")) if isinstance(result_envelope, dict) else False
		log.http_status = 200 if ok else 400
		log.error_message = None if ok else (
			result_envelope.get("message_en") if isinstance(result_envelope, dict) else None
		)
	if error:
		log.error_message = error
		log.http_status = 500
	log.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"ok": error is None,
		"envelope": result_envelope,
		"error": error,
	}
