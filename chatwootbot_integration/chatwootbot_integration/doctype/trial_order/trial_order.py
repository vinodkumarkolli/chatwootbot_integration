# Copyright (c) 2026, Vinod Kumar K and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document

PINCODE = re.compile(r"^[1-9][0-9]{5}$")


class TrialOrder(Document):
	"""A free trial-pack claim made on WhatsApp.

	Deliberately NOT submittable and freely deletable. It carries no Customer,
	Company, price list, sales person or amounts: it is a commercial *claim*
	record attached to a CRM Lead, not an accounting document.

	EXTENSION POINT - "Create Sales Order" (not built):
		When a trial claim converts into a real order, add a whitelisted method
		on this class (e.g. `create_sales_order`) that, and only then,
		1. creates (or reuses) the ERPNext Customer from `self.lead`
		   (Lead -> Customer conversion belongs here, never at claim time),
		2. builds a Sales Order from `self.items` with prices from the price list
		   chosen at that moment, and
		3. links the Sales Order back (a new Link field on this doctype).
		Nothing in this class or the sync may create a Customer or a Sales Order.
	"""

	def before_insert(self):
		self.validate_unique_bot_ref()

	def validate(self):
		self.bot_ref = (self.bot_ref or "").strip()
		self.validate_items()
		self.validate_pincode()
		self.fetch_lead_details()

	# ------------------------------------------------------------------ rules

	def validate_unique_bot_ref(self):
		"""Raise a clear DuplicateEntryError; the sync relies on it for idempotency."""
		existing = frappe.db.get_value("Trial Order", {"bot_ref": (self.bot_ref or "").strip()}, "name")
		if existing:
			frappe.throw(
				_("DUPLICATE_BOT_REF: bot_ref {0} already exists as {1}").format(self.bot_ref, existing),
				frappe.DuplicateEntryError,
			)

	def validate_items(self):
		if not self.items:
			frappe.throw(_("A Trial Order needs at least one item."), frappe.ValidationError)
		for row in self.items:
			if not row.qty or row.qty <= 0:
				frappe.throw(
					_("Row {0}: Qty must be greater than 0.").format(row.idx), frappe.ValidationError
				)

	def validate_pincode(self):
		if not PINCODE.match(self.pincode or ""):
			frappe.throw(
				_("Pincode must be 6 digits and cannot start with 0."), frappe.ValidationError
			)

	def fetch_lead_details(self):
		values = frappe.db.get_value("CRM Lead", self.lead, ["lead_name", "mobile_no"], as_dict=True)
		if values:
			self.lead_name = values.lead_name
			self.mobile_no = values.mobile_no

	# ------------------------------------------------- CRM Lead counter hook

	def after_insert(self):
		update_lead_trial_count(self.lead)

	def on_update(self):
		before = self.get_doc_before_save()
		if before and before.lead != self.lead:
			update_lead_trial_count(before.lead)
			update_lead_trial_count(self.lead)

	def on_trash(self):
		self.flags.lead_to_recount = self.lead

	def after_delete(self):
		update_lead_trial_count(self.flags.get("lead_to_recount") or self.lead)


def update_lead_trial_count(lead):
	"""Keep CRM Lead.custom_trial_order_count in step. Direct DB write: no Lead
	save, so no Lead hooks, notifications or modified-timestamp churn."""
	if not lead or not frappe.db.exists("CRM Lead", lead):
		return
	frappe.db.set_value(
		"CRM Lead",
		lead,
		"custom_trial_order_count",
		frappe.db.count("Trial Order", {"lead": lead}),
		update_modified=False,
	)
