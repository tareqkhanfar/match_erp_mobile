"""Mobile Request Log — controller.

Every call that passes through `match_erp.api.mobile.*` (and the
customer-app endpoints) is recorded here by the `mobile_endpoint`
decorator. The log captures the raw JSON body, the response, the HTTP
status, and timing.

Two extra capabilities make it a debugging/repair tool, not just an
audit trail:

1. **Flatten / rebuild** — on load we explode the JSON body into a flat
   list of dotted key paths (`items.0.qty`) so a System Manager can edit
   individual values in a grid without hand-editing JSON. On save we
   fold the grid back into JSON. Nested objects and arrays are fully
   supported.

2. **Replay** — re-dispatch the (possibly edited) request to its original
   endpoint, server-side, as the original user. Lets you fix a failed
   request and re-run it without touching the mobile device.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class MobileRequestLog(Document):
	def onload(self):
		"""Populate the editable key/value grid from the request body so
		the user always sees the current JSON exploded into rows. We do
		this on load (not on save) so the grid reflects external edits to
		the raw Code field too."""
		self._sync_kv_from_body()

	def validate(self):
		"""If the user edited the key/value grid, fold it back into the
		request_body JSON before saving. The grid is the source of truth
		when both changed in the same save — value edits are the common
		repair workflow."""
		if self.kv_pairs:
			rebuilt = _rebuild_from_kv(self.kv_pairs)
			if rebuilt is not None:
				self.request_body = json.dumps(rebuilt, ensure_ascii=False, indent=2)

	def _sync_kv_from_body(self):
		try:
			data = json.loads(self.request_body) if self.request_body else {}
		except (json.JSONDecodeError, TypeError):
			return
		if not isinstance(data, (dict, list)):
			return
		self.set("kv_pairs", [])
		for path, value in _flatten(data):
			vtype, vstr = _encode_value(value)
			self.append("kv_pairs", {
				"key_path": path,
				"value_type": vtype,
				"value": vstr,
			})


# ---------------------------------------------------------------------------
# JSON flatten / rebuild — shared by the controller and the replay action.
# ---------------------------------------------------------------------------
def _flatten(obj, prefix: str = ""):
	"""Yield (dotted_path, scalar_value) pairs for a nested dict/list.

	Lists use numeric path segments: `items.0.qty`. Scalars (str/int/
	float/bool/None) are yielded directly; containers recurse.
	"""
	if isinstance(obj, dict):
		for k, v in obj.items():
			path = f"{prefix}.{k}" if prefix else str(k)
			if isinstance(v, (dict, list)):
				yield from _flatten(v, path)
			else:
				yield path, v
	elif isinstance(obj, list):
		for i, v in enumerate(obj):
			path = f"{prefix}.{i}" if prefix else str(i)
			if isinstance(v, (dict, list)):
				yield from _flatten(v, path)
			else:
				yield path, v


def _encode_value(value) -> tuple[str, str]:
	"""Map a Python scalar to (value_type, value_string) for the grid."""
	if value is None:
		return "null", ""
	if isinstance(value, bool):
		return "bool", "true" if value else "false"
	if isinstance(value, (int, float)):
		return "number", str(value)
	return "string", str(value)


def _decode_value(value_type: str, value_str: str):
	"""Inverse of _encode_value — turn a grid row back into a scalar."""
	if value_type == "null":
		return None
	if value_type == "bool":
		return str(value_str).strip().lower() in ("1", "true", "yes")
	if value_type == "number":
		s = (value_str or "").strip()
		if s == "":
			return 0
		try:
			# Keep ints as ints so the payload round-trips cleanly.
			if "." in s or "e" in s.lower():
				return float(s)
			return int(s)
		except ValueError:
			try:
				return float(s)
			except ValueError:
				return s
	return value_str or ""


def _rebuild_from_kv(rows) -> dict | list | None:
	"""Fold the flat key/value rows back into a nested structure.

	A path segment that is an integer index implies a list at that level;
	a string segment implies a dict. We grow lists as needed so order is
	preserved. Returns None if there are no rows to rebuild.
	"""
	if not rows:
		return None

	# Decide whether the root is a list or a dict by looking at the first
	# segment of the first path.
	def seg_is_index(seg: str) -> bool:
		return seg.isdigit()

	root: dict | list
	first_seg = (rows[0].key_path or "").split(".")[0]
	root = [] if seg_is_index(first_seg) else {}

	for row in rows:
		path = row.key_path or ""
		if not path:
			continue
		segments = path.split(".")
		value = _decode_value(row.value_type, row.value)
		_assign(root, segments, value)
	return root


def _assign(container, segments: list[str], value):
	"""Walk/create the nested container along `segments` and set `value`."""
	seg = segments[0]
	is_last = len(segments) == 1
	idx = int(seg) if seg.isdigit() else None

	if is_last:
		if idx is not None and isinstance(container, list):
			_ensure_list_len(container, idx)
			container[idx] = value
		elif isinstance(container, dict):
			container[seg] = value
		return

	# Need to descend — determine the child container type from the NEXT
	# segment (index → list, else dict).
	next_is_index = segments[1].isdigit()
	if idx is not None and isinstance(container, list):
		_ensure_list_len(container, idx)
		if not isinstance(container[idx], (dict, list)):
			container[idx] = [] if next_is_index else {}
		_assign(container[idx], segments[1:], value)
	elif isinstance(container, dict):
		if seg not in container or not isinstance(container[seg], (dict, list)):
			container[seg] = [] if next_is_index else {}
		_assign(container[seg], segments[1:], value)


def _ensure_list_len(lst: list, idx: int):
	while len(lst) <= idx:
		lst.append(None)


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
