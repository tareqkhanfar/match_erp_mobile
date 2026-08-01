"""Install / migrate-time setup for Match ERP.

Ensures the mobile custom fields exist on the transactional voucher
doctypes even before fixtures are exported/synced. Idempotent — safe to run
on every migrate.

Custom fields ensured here:
  - custom_mobile_local_id   (Data)  — idempotency key for the mobile sync
  - custom_dist_pos_profile  (Link)  — the Dist POS Profile that created the
                                       voucher from Match ERP Mobile. Empty
                                       means the voucher was created in
                                       ERPNext (not from mobile).
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

VOUCHER_DOCTYPES = [
	"Sales Order",
	"Sales Invoice",
	"Purchase Order",
	"Purchase Invoice",
	"Payment Entry",
]


def _field_specs():
	mobile_local_id = {
		"fieldname": "custom_mobile_local_id",
		"label": "Mobile Local Id",
		"fieldtype": "Data",
		"length": 140,
		"insert_after": "title",
		"read_only": 1,
		"no_copy": 1,
		"print_hide": 1,
		"unique": 1,
		"search_index": 1,
		"is_system_generated": 1,
		"description": (
			"Client-generated stable id used by the Match ERP Mobile sync "
			"queue for idempotent retries."
		),
	}
	dist_profile = {
		"fieldname": "custom_dist_pos_profile",
		"label": "Dist POS Profile",
		"fieldtype": "Link",
		"options": "Dist POS Profile",
		"insert_after": "custom_mobile_local_id",
		"read_only": 1,
		"no_copy": 1,
		"print_hide": 1,
		"in_standard_filter": 1,
		"search_index": 1,
		"is_system_generated": 1,
		"description": (
			"Dist POS Profile that created this voucher from Match ERP "
			"Mobile. Empty means it was created in ERPNext (not from mobile)."
		),
	}
	specs = {dt: [dict(mobile_local_id), dict(dist_profile)] for dt in VOUCHER_DOCTYPES}

	# Item image gallery — a section with a table of additional images. The
	# Item's own `image` field stays the PRIMARY image; these are extras.
	specs["Item"] = [
		{
			"fieldname": "custom_item_images_section",
			"label": "Item Images",
			"fieldtype": "Section Break",
			"insert_after": "image",
			"collapsible": 1,
			"is_system_generated": 1,
		},
		{
			"fieldname": "custom_item_images",
			"label": "Images",
			"fieldtype": "Table",
			"options": "Item Image",
			"insert_after": "custom_item_images_section",
			"is_system_generated": 1,
			"description": (
				"Additional images for this item. The Item's own Image field "
				"above is the primary image."
			),
		},
	]
	return specs


def ensure_custom_fields():
	"""Create/repair the mobile custom fields. Idempotent."""
	create_custom_fields(_field_specs(), ignore_validate=True)
	frappe.db.commit()


def after_migrate():
	ensure_custom_fields()
