"""Dist POS Profile endpoint for Match ERP Mobile.

Returns the resolved settings profile for the logged-in user. The mobile
app treats these as read-only — they are administered in ERPNext via the
Dist POS Profile doctype and synced down.
"""

from __future__ import annotations

import frappe

from match_erp.api.mobile.envelope import mobile_endpoint, ok
from match_erp.match_erp.doctype.dist_pos_profile.dist_pos_profile import (
	resolve_for_user,
	serialize,
)


@frappe.whitelist()
@mobile_endpoint
def get_settings(**kwargs):
	"""Resolve and return the settings profile for the current user."""
	profile = resolve_for_user(frappe.session.user)
	return ok(serialize(profile), en="Settings loaded", ar="تم تحميل الإعدادات")
