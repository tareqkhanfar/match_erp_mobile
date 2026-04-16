"""Sales document endpoints for Match ERP Mobile.

All three endpoints share the generic voucher payload (see `_voucher.py`)
and are idempotent on `custom_mobile_local_id`.

- create_sales_order    → Sales Order
- create_sales_invoice  → Sales Invoice (supports is_paid + mode_of_payment)
- create_sales_return   → Sales Invoice with is_return=1 + return_against
"""

from __future__ import annotations

import frappe

from match_erp.api.mobile._voucher import create_voucher
from match_erp.api.mobile.envelope import mobile_endpoint, parse_body


@frappe.whitelist()
@mobile_endpoint
def create_sales_order(**kwargs):
	return create_voucher("Sales Order", parse_body())


@frappe.whitelist()
@mobile_endpoint
def create_sales_invoice(**kwargs):
	return create_voucher("Sales Invoice", parse_body())


@frappe.whitelist()
@mobile_endpoint
def create_sales_return(**kwargs):
	# A sales return is a Sales Invoice with is_return=1 and negative qty,
	# submitted against the original invoice via return_against.
	return create_voucher("Sales Invoice", parse_body(), is_return=True)
