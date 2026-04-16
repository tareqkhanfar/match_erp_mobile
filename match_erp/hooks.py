app_name = "match_erp"
app_title = "Match Erp"
app_publisher = "match systems"
app_description = "Match ERP — backend for Match ERP Mobile (Flutter) client"
app_email = "matchprosys@gmail.com"
app_license = "mit"

# Fixtures — ship Custom Fields for mobile idempotency
fixtures = [
	{
		"doctype": "Custom Field",
		"filters": [
			[
				"name",
				"in",
				[
					"Sales Order-custom_mobile_local_id",
					"Sales Invoice-custom_mobile_local_id",
					"Purchase Order-custom_mobile_local_id",
					"Purchase Invoice-custom_mobile_local_id",
					"Payment Entry-custom_mobile_local_id",
				],
			]
		],
	}
]
