"""Purchase document endpoints for Match ERP Mobile.

Same generic payload shape as sales.* but with party_type=Supplier. See
`_voucher.py` for the full payload contract.

- create_purchase_order   → Purchase Order
- create_purchase_invoice → Purchase Invoice (supports is_paid + mode_of_payment)
- create_purchase_return  → Purchase Invoice with is_return=1 + return_against
"""

from __future__ import annotations

import frappe

from match_erp.api.mobile._voucher import create_voucher
from match_erp.api.mobile.envelope import mobile_endpoint, parse_body


@frappe.whitelist()
@mobile_endpoint
def create_purchase_order(**kwargs):
	return create_voucher("Purchase Order", parse_body())


@frappe.whitelist()
@mobile_endpoint
def create_purchase_invoice(**kwargs):
	return create_voucher("Purchase Invoice", parse_body())


@frappe.whitelist()
@mobile_endpoint
def create_purchase_return(**kwargs):
	# A purchase return is a Purchase Invoice with is_return=1 and negative
	# qty, submitted against the original invoice via return_against — the
	# mirror of create_sales_return.
	return create_voucher("Purchase Invoice", parse_body(), is_return=True)
