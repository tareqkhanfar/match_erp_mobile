# Match ERP

Backend custom app for the Match ERP Mobile (Flutter) client.

Exposes a mobile-friendly API namespace at `match_erp.api.mobile.*`:

- `auth.login` / `auth.logout` / `auth.get_current_user_info`
- `company.get_companies`
- `sync.get_customers` / `sync.get_items` / `sync.get_item_barcodes` / `sync.get_uoms`
- `sales.create_sales_order` / `sales.create_sales_invoice` (idempotent on `custom_mobile_local_id`)
- `customer.create` / `customer.update`
- `item.create` / `item.update`

All endpoints return the flat envelope:

```json
{ "success": true, "data": ..., "message_en": "...", "message_ar": "..." }
```

Install:

```bash
bench get-app match_erp /home/frappe/matcherp-v15/apps/match_erp
bench --site <site> install-app match_erp
```
