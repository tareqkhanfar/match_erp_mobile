"""Authentication endpoints for Match ERP Mobile.

Supports both session-cookie (login/logout) and API key / secret auth.
All endpoints return the flat envelope from `envelope.py`.
"""

from __future__ import annotations

import frappe
from frappe.auth import LoginManager

from match_erp.api.mobile.envelope import fail, mobile_endpoint, ok, parse_body


def _user_info(user: str) -> dict:
	"""Shared shape for login responses and get_current_user_info.

	Includes the resolved Dist POS Profile under `settings` so the mobile
	app gets its (read-only) configuration in the same call it
	authenticates — no extra round-trip on launch.
	"""
	if not user or user == "Guest":
		return {}
	full_name, email, user_image = frappe.db.get_value(
		"User", user, ["full_name", "email", "user_image"]
	) or (None, None, None)
	default_company = (
		frappe.defaults.get_user_default("Company", user)
		or frappe.db.get_single_value("Global Defaults", "default_company")
	)

	# Resolve the settings profile. Never let a profile-resolution error
	# block login — fall back to defaults.
	settings = None
	try:
		from match_erp.match_erp.doctype.dist_pos_profile.dist_pos_profile import (
			resolve_for_user,
			serialize,
		)

		settings = serialize(resolve_for_user(user))
	except Exception:
		settings = None

	# Prefer the profile's company when it sets one.
	if settings and settings.get("company"):
		default_company = settings["company"] or default_company

	return {
		"user_name": user,
		"full_name": full_name,
		"email": email,
		"user_image": user_image,
		"roles": frappe.get_roles(user),
		"default_company": default_company,
		"settings": settings,
	}


@frappe.whitelist(allow_guest=True)
@mobile_endpoint
def login(usr: str = "", pwd: str = ""):
	# Frappe injects JSON body keys as kwargs; parse_body() is a fallback for
	# bench execute / curl with form-encoded bodies.
	if not usr or not pwd:
		body = parse_body()
		usr = (body.get("usr") or usr or "").strip()
		pwd = body.get("pwd") or pwd or ""
	usr = (usr or "").strip()
	if not usr or not pwd:
		return fail(
			"Username and password are required",
			"اسم المستخدم وكلمة المرور مطلوبان",
		)

	try:
		lm = LoginManager()
		lm.authenticate(user=usr, pwd=pwd)
		lm.post_login()
	except frappe.AuthenticationError:
		return fail(
			"Invalid username or password",
			"اسم المستخدم أو كلمة المرور غير صحيحة",
		)
	except frappe.SecurityException as e:
		return fail(str(e) or "Security error", "خطأ أمني")

	return ok(
		_user_info(frappe.session.user),
		en="Login successful",
		ar="تم تسجيل الدخول بنجاح",
	)


@frappe.whitelist()
@mobile_endpoint
def logout(**kwargs):
	if getattr(frappe.local, "login_manager", None):
		frappe.local.login_manager.logout()
	else:
		LoginManager().logout()
	frappe.db.commit()
	return ok(None, en="Logged out", ar="تم تسجيل الخروج")


@frappe.whitelist()
@mobile_endpoint
def get_current_user_info(**kwargs):
	user = frappe.session.user
	if not user or user == "Guest":
		return fail(
			"Session expired. Please login again.",
			"انتهت الجلسة. يرجى تسجيل الدخول مجدداً.",
		)
	return ok(_user_info(user), en="OK", ar="تم")
