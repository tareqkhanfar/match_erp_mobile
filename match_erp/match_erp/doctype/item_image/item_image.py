# Copyright (c) 2026, match systems and contributors
# For license information, please see license.txt

from __future__ import annotations

from frappe.model.document import Document


class ItemImage(Document):
	"""One image in an Item's gallery.

	The Item's own `image` field remains the PRIMARY image. This child table
	holds the additional gallery images shown after it.
	"""

	pass
