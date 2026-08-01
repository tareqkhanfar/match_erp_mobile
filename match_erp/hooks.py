app_name = "match_erp"
app_title = "Match Erp"
app_publisher = "match systems"
app_description = "Match ERP — backend for Match ERP Mobile (Flutter) client"
app_email = "matchprosys@gmail.com"
app_license = "mit"

# Fixtures — ship Custom Fields for mobile idempotency + the Dist POS
# Profile link that tags every voucher created from Match ERP Mobile.
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
					"Sales Order-custom_dist_pos_profile",
					"Sales Invoice-custom_dist_pos_profile",
					"Purchase Order-custom_dist_pos_profile",
					"Purchase Invoice-custom_dist_pos_profile",
					"Payment Entry-custom_dist_pos_profile",
					"Item-custom_item_images_section",
					"Item-custom_item_images",
					"Expense-custom_mobile_local_id",
					"Expense-custom_dist_pos_profile",
				],
			]
		],
	}
]

# Ensure the mobile custom fields exist after every migrate, even before
# fixtures sync — defensive so the fetch/create endpoints never hit a
# missing column.
after_migrate = "match_erp.setup.after_migrate"
