"""Install / uninstall plumbing for chatwootbot_integration.

Everything here is idempotent: safe to run on install, on migrate, and again.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.permissions import add_permission, update_permission_property

MODULE = "Chatwootbot Integration"
CLEANUP_ROLE = "Cleanup Role"
CLEANUP_USER = "automation.cleanup@sravie.in"
CRM_ROLE = "CRM User Custom"

OWN_DOCTYPES = ["Trial Order", "Trial Order Item", "Conversation Intent"]  # permissions live in their JSON

# Cleanup Role: read + delete on these (other apps' doctypes; Trial Order's rows are in its JSON);
# CRM Lead also gets write (anonymising).
CLEANUP_PERMS = {
	"Contact": ("read", "delete"),
	"Address": ("read", "delete"),
	"CRM Lead": ("read", "delete", "write"),
	"Deleted Document": ("read", "delete"),
	"Version": ("read", "delete"),
	"Comment": ("read", "delete"),
	"Activity Log": ("read", "delete"),
	"File": ("read", "delete"),
}

# CRM User Custom: the role for the CRM automation account (Sarvam voice agent + the chatbot
# sync). Built from what the Sarvam code and the sync actually use, NOT copied from Sales User.
# Customer create, ToDo and File are deliberately absent: they are added only if a test proves
# they are needed. No delete anywhere.
_RWC = ("read", "write", "create")
CRM_PERMS = {
	**{dt: _RWC for dt in (
		"CRM Lead", "CRM Deal", "CRM Task", "FCRM Note", "CRM Call Log", "CRM Organization",
		"Contact", "Address", "Quotation",
	)},
	**{dt: ("read",) for dt in (
		"CRM Telephony Agent", "Customer", "Item", "Price List", "Company", "Territory", "Customer Group",
		"CRM Lead Status", "CRM Deal Status", "CRM Lead Source", "Campaign", "User",
		"Sarvam Transfer Attempt", "Sales Taxes and Charges Template", "Account",
	)},
	"Blocked Contact Request": ("read", "create", "write", "submit"),
}  # fmt: skip
# permission rows that only apply to the user's own records
CRM_IF_OWNER = {"Blocked Contact Request": ("write", "submit")}

CRM_LEAD_FIELDS = [
	dict(fieldname="custom_chatbot_tab", fieldtype="Tab Break", label="Chatbot"),
	dict(fieldname="custom_campaign", fieldtype="Link", options="Campaign", label="Campaign",
		in_standard_filter=1),
	dict(fieldname="custom_whatsapp_verified", fieldtype="Check", label="WhatsApp Verified"),
	dict(fieldname="custom_chatwoot_contact_id", fieldtype="Data", label="Chatwoot Contact ID",
		search_index=1),
	dict(fieldname="custom_marketing_opt_in", fieldtype="Check", label="Marketing Opt-in"),
	dict(fieldname="custom_trial_order_count", fieldtype="Int", label="Trial Order Count",
		read_only=1, default="0", no_copy=1),
	dict(fieldname="custom_chatbot_col", fieldtype="Column Break"),
	dict(fieldname="custom_consent_at", fieldtype="Datetime", label="Consent At",
		description="When the Lead was created, i.e. terms accepted. Full evidence stays in the bot DB."),
	dict(fieldname="custom_policy_version", fieldtype="Data", label="Policy Version"),
	dict(fieldname="custom_first_intent", fieldtype="Link", options="Conversation Intent",
		label="First Intent", description="Set once on creation; never overwritten."),
	dict(fieldname="custom_latest_intent", fieldtype="Link", options="Conversation Intent",
		label="Latest Intent"),
	dict(fieldname="custom_first_conversation_id", fieldtype="Data", label="First Conversation ID",
		description="Set once on creation; never overwritten."),
]
CRM_LEAD_FIELD_NAMES = [d["fieldname"] for d in CRM_LEAD_FIELDS]

# Set by erase_subject when a Customer is kept under legal hold (sales records exist).
CUSTOMER_FIELDS = [
	dict(fieldname="custom_erasure_requested_on", fieldtype="Date", label="Erasure Requested On",
		read_only=1, no_copy=1, insert_after="customer_group",
		description="Set when an erasure was requested but the Customer is kept for its sales records."),
]  # fmt: skip
CUSTOMER_FIELD_NAMES = [d["fieldname"] for d in CUSTOMER_FIELDS]


def before_install():
	# The Trial Order permissions name these roles, so they must exist before doctypes sync.
	ensure_role()
	ensure_crm_role()


def after_install():
	setup_all()


def heal_own_doctype_perms():
	"""Our own doctypes take their permissions from their JSON. A Custom DocPerm copy would
	mask it (Frappe then ignores the JSON), so make sure none exists."""
	frappe.db.delete("Custom DocPerm", {"parent": ["in", OWN_DOCTYPES]})


def setup_all():
	heal_own_doctype_perms()
	ensure_role()
	ensure_crm_role()
	seed_conversation_intents()
	seed_campaign()
	create_lead_fields()
	grant_cleanup_permissions()
	grant_crm_permissions()
	ensure_cleanup_user()


INTENTS = [
	("trial_claim", "Trial Claim", "Trial"),
	("info", "Information", "Enquiry"),
	("shop", "Shop", "Enquiry"),
	("b2b", "B2B", "Enquiry"),
	("feedback", "Feedback", "Support"),
	("rights", "Data Rights", "Support"),
	("concierge", "Concierge", "Support"),
	("other", "Other", "Other"),
]


def seed_conversation_intents():
	for code, label, group in INTENTS:
		if not frappe.db.exists("Conversation Intent", code):
			frappe.get_doc(
				{
					"doctype": "Conversation Intent",
					"intent_code": code,
					"label": label,
					"funnel_group": group,
					"active": 1,
				}
			).insert(ignore_permissions=True)


def seed_campaign():
	"""Campaign name IS the bot's campaign code. Create only if absent; an existing one is never edited."""
	if not frappe.db.exists("Campaign", "SUJITHA10"):
		frappe.get_doc({"doctype": "Campaign", "campaign_name": "SUJITHA10"}).insert(ignore_permissions=True)


def ensure_role():
	if not frappe.db.exists("Role", CLEANUP_ROLE):
		frappe.get_doc(
			{"doctype": "Role", "role_name": CLEANUP_ROLE, "desk_access": 0, "is_custom": 1}
		).insert(ignore_permissions=True)


def ensure_crm_role():
	"""desk_access=1 so a user whose only role is this one stays a System User."""
	if not frappe.db.exists("Role", CRM_ROLE):
		frappe.get_doc(
			{"doctype": "Role", "role_name": CRM_ROLE, "desk_access": 1, "is_custom": 1}
		).insert(ignore_permissions=True)


def grant_crm_permissions():
	for doctype, ptypes in CRM_PERMS.items():
		_grant_row(doctype, CRM_ROLE, ptypes, if_owner=doctype in CRM_IF_OWNER)


def _grant_row(doctype, role, ptypes, if_owner=False):
	"""Give `role` exactly `ptypes` on `doctype` (level 0). Writes the Custom DocPerm row
	directly: `add_permission` re-validates the whole doctype and refuses doctypes that
	already carry an invalid row (e.g. Blocked Contact Request: Cancel without Submit)."""
	from frappe.permissions import setup_custom_perms

	setup_custom_perms(doctype)  # copies the standard permissions first if none are custom yet
	name = frappe.db.get_value("Custom DocPerm", {"parent": doctype, "role": role, "permlevel": 0})
	if not name:
		row = frappe.get_doc(
			{
				"doctype": "Custom DocPerm",
				"parent": doctype,
				"parenttype": "DocType",
				"parentfield": "permissions",
				"role": role,
				"permlevel": 0,
			}
		)
		row.flags.ignore_validate = True
		row.insert(ignore_permissions=True)
		name = row.name
	values = {p: int(p in ptypes) for p in ("read", "write", "create", "delete", "submit", "cancel", "amend")}
	values["if_owner"] = int(if_owner)
	frappe.db.set_value("Custom DocPerm", name, values, update_modified=False)
	frappe.clear_cache(doctype=doctype)


def create_lead_fields():
	# Chain the fields in order, after the last field that is not ours.
	meta = frappe.get_meta("CRM Lead")
	mine = set(CRM_LEAD_FIELD_NAMES)
	after = [f.fieldname for f in meta.fields if f.fieldname not in mine][-1]
	fields = []
	for d in CRM_LEAD_FIELDS:
		fields.append(dict(d, module=MODULE, insert_after=after))
		after = d["fieldname"]
	create_custom_fields(
		{"CRM Lead": fields, "Customer": [dict(d, module=MODULE) for d in CUSTOMER_FIELDS]}, update=True
	)


def grant_cleanup_permissions():
	for doctype, ptypes in CLEANUP_PERMS.items():
		# add_permission first copies the standard perms into Custom DocPerm if needed
		# (Frappe ignores standard DocPerm once any Custom DocPerm exists).
		add_permission(doctype, CLEANUP_ROLE, 0)
		for ptype in ptypes:
			update_permission_property(doctype, CLEANUP_ROLE, 0, ptype, 1)


def ensure_cleanup_user():
	"""The user holds ONLY Cleanup Role. API key/secret are generated by a person
	(User form > API Access > Generate Keys); this code never creates or prints them."""
	if frappe.db.exists("User", CLEANUP_USER):
		return
	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": CLEANUP_USER,
			"first_name": "Automation",
			"last_name": "Cleanup",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
			"roles": [{"role": CLEANUP_ROLE}],
		}
	)
	user.flags.no_welcome_mail = True
	user.insert(ignore_permissions=True)


def before_uninstall():
	"""Leave no orphans: custom fields, Cleanup Role permission rows, the user, the role."""
	for doctype, names in (("CRM Lead", CRM_LEAD_FIELD_NAMES), ("Customer", CUSTOMER_FIELD_NAMES)):
		for name in frappe.get_all("Custom Field", filters={"dt": doctype, "fieldname": ["in", names]}, pluck="name"):
			frappe.delete_doc("Custom Field", name, force=True, ignore_permissions=True)
		# Deleting a Custom Field leaves its column behind in the table: drop ours.
		for fieldname in names:
			if frappe.db.has_column(doctype, fieldname):
				frappe.db.sql_ddl(f"alter table `tab{doctype}` drop column `{fieldname}`")
	heal_own_doctype_perms()
	frappe.db.delete("Custom DocPerm", {"role": ["in", [CLEANUP_ROLE, CRM_ROLE]]})
	for doctype in {*CLEANUP_PERMS, *CRM_PERMS}:
		_reset_perms_if_untouched(doctype)
	if frappe.db.exists("User", CLEANUP_USER):
		frappe.delete_doc("User", CLEANUP_USER, force=True, ignore_permissions=True)
	for role in (CLEANUP_ROLE, CRM_ROLE):
		frappe.db.delete("Has Role", {"role": role})
		if frappe.db.exists("Role", role):
			frappe.delete_doc("Role", role, force=True, ignore_permissions=True)
	frappe.clear_cache()


PERM_COLUMNS = [
	"role", "permlevel", "if_owner", "read", "write", "create", "delete", "submit", "cancel", "amend",
	"report", "export", "import", "share", "print", "email", "select",
]  # fmt: skip


def _perm_rows(table, doctype):
	rows = frappe.get_all(table, filters={"parent": doctype}, fields=PERM_COLUMNS)
	return sorted(tuple(str(r[c]) for c in PERM_COLUMNS) for r in rows)


def _reset_perms_if_untouched(doctype):
	"""add_permission() copies a doctype's standard permissions into Custom DocPerm the
	first time. Once our Cleanup Role row is gone, a Custom DocPerm set that is identical
	to the standard one is only that copy: drop it so the doctype is back to stock."""
	custom = _perm_rows("Custom DocPerm", doctype)
	if custom and custom == _perm_rows("DocPerm", doctype):
		frappe.db.delete("Custom DocPerm", {"parent": doctype})
