import frappe
from frappe import _


def validate_free_trial(doc, method=None):
	"""Campaign.validate. A free-trial Campaign must name an enabled Item; otherwise the Item is cleared.
	The Link filter on the field is only a UI hint, so `disabled = 0` is enforced here."""
	if not doc.get("custom_free_trial"):
		doc.custom_trial_item = None
		return
	if not doc.get("custom_trial_item"):
		frappe.throw(_("Trial Item is required when Free Trial is set."), frappe.ValidationError)
	disabled = frappe.db.get_value("Item", doc.custom_trial_item, "disabled")
	if disabled is None:
		frappe.throw(_("Trial Item {0} does not exist.").format(doc.custom_trial_item), frappe.ValidationError)
	if disabled:
		frappe.throw(_("Trial Item {0} is disabled.").format(doc.custom_trial_item), frappe.ValidationError)
