"""Mobile Request Field — child row of Mobile Request Log.

Holds one flattened key/value pair from the request JSON. The key path
is read-only (it mirrors the JSON structure); the value is editable so a
System Manager can fix a bad payload and replay it.
"""

from __future__ import annotations

from frappe.model.document import Document


class MobileRequestField(Document):
	pass
