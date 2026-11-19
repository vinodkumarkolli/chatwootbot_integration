# Copyright (c) 2026, Vinod Kumar K and contributors
# For license information, please see license.txt

"""Right-to-erasure for the Swarna chatbot subject.

`erase_subject` is the ONLY way to remove a chatbot subject's personal data.
A plain REST DELETE is not enough: Frappe keeps a full JSON copy of every
deleted record in `Deleted Document`, plus `Version`, `Comment`, `Activity Log`
and attached `File` rows. This method deletes the records, then purges those.

Where a retention purge job for leads that never claim would hook in: call
`erase_subject` (or `erase_lead` below, which it uses) from a scheduler event
in hooks.py once the retention rule is decided. Nothing is scheduled here.
"""

import json
import re

import frappe
from frappe import _
from core_customizations.core_customizations.caller_gate import last_10_digits

CLEANUP_ROLE = "Cleanup Role"
E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")

# Records that belong to the subject's own footprint or are purged below; they
# never count as "something else references the Lead".
NOT_BLOCKING = {
	"Trial Order",
	"Contact",
	"Address",
	"Dynamic Link",
	"Comment",
	"Version",
	"Activity Log",
	"File",
	"Deleted Document",
	"DocShare",
	"ToDo",
	"Notification Log",
	"CRM Notification",
	"View Log",
	"CRM Status Change Log",
}

# (doctype, reference-doctype column, reference-name column) rows to purge for every
# record the subject owned (deleted or anonymised).
REFERENCE_TABLES = [
	("Comment", "reference_doctype", "reference_name"),
	("Activity Log", "reference_doctype", "reference_name"),
	("Activity Log", "timeline_doctype", "timeline_name"),
	("Activity Log", "link_doctype", "link_name"),
	("ToDo", "reference_type", "reference_name"),
	("Notification Log", "document_type", "document_name"),
	("CRM Notification", "reference_doctype", "reference_name"),
	("CRM Notification", "notification_type_doctype", "notification_type_doc"),
	("DocShare", "share_doctype", "share_name"),
	("View Log", "reference_doctype", "reference_name"),
	("File", "attached_to_doctype", "attached_to_name"),
	("Version", "ref_doctype", "docname"),
	("Deleted Document", "deleted_doctype", "deleted_name"),
]
# Tables whose rows may carry the number itself; matched on the last 10 digits.
TEXT_TABLES = [
	("Deleted Document", "data", ("deleted_doctype", "deleted_name")),
	("Version", "data", ("ref_doctype", "docname")),
	("Comment", "content", ("reference_doctype", "reference_name")),
	("Comment", "subject", ("reference_doctype", "reference_name")),
	("Activity Log", "subject", ("reference_doctype", "reference_name")),
	("Activity Log", "content", ("reference_doctype", "reference_name")),
]

# Legal hold: a subject whose Customer has any of these keeps the Customer (and the Contact/Address
# those documents point at); everything else is erased as usual.
HOLD_REASON = "sales records"
SALES_DOCTYPES = ("Sales Order", "Sales Invoice", "Delivery Note")  # linked by `customer`
HOLD_CONTACT_FIELDS = ("contact_person",)
HOLD_ADDRESS_FIELDS = ("customer_address", "shipping_address_name")

PERSONAL_LEAD_FIELDS = [
	"salutation", "middle_name", "last_name", "email", "mobile_no", "phone", "organization", "website",
	"job_title", "gender", "image", "custom_whatsapp_verified", "custom_chatwoot_contact_id",
	"custom_marketing_opt_in", "custom_consent_at", "custom_policy_version", "custom_first_conversation_id",
	"custom_caller_preferences",
]  # fmt: skip


@frappe.whitelist(methods=["POST"])
def erase_subject(mobile_no: str, bot_refs: list[str] | None = None):
	"""Erase everything held about the subject with this E.164 number.

	Deletes (permanently) the Trial Orders, their Address and Contact records and the
	CRM Lead; if something else references the Lead (Deal, Call Log, Communication ...)
	it is anonymised instead. Then purges Deleted Document / Version / Comment /
	Activity Log / File rows. One transaction (the request), idempotent.

	Callable by Cleanup Role only. `bot_refs` is optional; a bot_ref only counts if
	its Trial Order really belongs to this subject.
	"""
	frappe.only_for(CLEANUP_ROLE)

	mobile_no = (mobile_no or "").strip()
	if not E164.match(mobile_no):
		frappe.throw(_("mobile_no must be E.164, e.g. +919876543210"), frappe.ValidationError)
	if isinstance(bot_refs, str):
		bot_refs = json.loads(bot_refs) if bot_refs.strip() else []
	bot_refs = [r.strip() for r in (bot_refs or []) if r and r.strip()]

	result = {"deleted": [], "anonymised": [], "retained": [], "held": [], "skipped_bot_refs": [], "purged": {}}
	owned = []  # (doctype, name) of everything the subject owned, deleted or anonymised

	leads = _find_leads(mobile_no)
	orders = _find_trial_orders(mobile_no, leads, bot_refs, result)
	contacts = _find_contacts(mobile_no, leads)

	# Addresses: referenced by a Trial Order, linked to a lead/contact, or carrying the number.
	addresses = set(frappe.get_all("Address", filters={"phone": mobile_no}, pluck="name"))
	order_docs = [frappe.get_doc("Trial Order", name) for name in orders]
	addresses |= {o.address for o in order_docs if o.address}
	for doctype, names in (("CRM Lead", leads), ("Contact", contacts)):
		addresses |= _linked_parents("Address", doctype, names)

	# Legal hold: a Customer with sales records is kept, with the Contact/Address those records use.
	held = _find_held_customers(mobile_no, contacts, result)
	protected = set()  # (doctype, name) of held documents: never purged, never erased
	keep = {"Contact": set(), "Address": set()}
	if held:
		protected |= {("Customer", c) for c in held}
		for records in held.values():
			protected |= set(records)
		used_contacts, used_addresses = _used_by_sales_records(held)
		keep["Contact"] = contacts & used_contacts
		keep["Address"] = addresses & used_addresses
		contacts -= keep["Contact"]
		addresses -= keep["Address"]
		protected |= {("Contact", n) for n in keep["Contact"]} | {("Address", n) for n in keep["Address"]}

	for o in order_docs:
		owned.append(("Trial Order", o.name))
		frappe.delete_doc(
			"Trial Order", o.name, force=True, ignore_permissions=True, delete_permanently=True
		)
		result["deleted"].append({"doctype": "Trial Order", "name": o.name})

	for doctype, names in (("Address", addresses), ("Contact", contacts)):
		for name in sorted(names):
			outcome = _erase_attached(doctype, name, set(leads), held)
			owned.append((doctype, name))
			result[outcome[0]].append(outcome[1])

	for lead in leads:
		owned.append(("CRM Lead", lead))
		blockers = _lead_references(lead)
		if blockers:
			_anonymise_lead(lead)
			result["anonymised"].append(
				{"doctype": "CRM Lead", "name": lead, "referenced_by": blockers}
			)
		else:
			frappe.delete_doc(
				"CRM Lead", lead, force=True, ignore_permissions=True, delete_permanently=True
			)
			result["deleted"].append({"doctype": "CRM Lead", "name": lead})

	if held:
		_finish_hold(held, keep, leads, result)

	# Purge LAST: deleting the records above can itself write Comment/Version/feed rows.
	result["purged"] = _purge(owned, mobile_no, bot_refs, protected)
	if not result["held"]:
		del result["held"]
	return result


# ----------------------------------------------------------------- finding


def _phone_match(column):
	return f"right(regexp_replace(ifnull({column}, ''), '[^0-9]', ''), 10) = %(last10)s"


def _find_leads(mobile_no):
	if mobile_no.startswith("+91"):
		last10 = last_10_digits(mobile_no)
		return frappe.db.sql(
			f"select name from `tabCRM Lead` where {_phone_match('mobile_no')} or {_phone_match('phone')}",
			{"last10": last10},
			pluck=True,
		)
	return frappe.get_all(
		"CRM Lead", or_filters={"mobile_no": mobile_no, "phone": mobile_no}, pluck="name"
	)


def _find_contacts(mobile_no, leads):
	names = set(
		frappe.get_all("Contact", or_filters={"mobile_no": mobile_no, "phone": mobile_no}, pluck="name")
	)
	names |= set(frappe.get_all("Contact Phone", filters={"phone": mobile_no, "parenttype": "Contact"}, pluck="parent"))
	if mobile_no.startswith("+91"):
		last10 = last_10_digits(mobile_no)
		names |= set(
			frappe.db.sql(
				f"""select name from `tabContact` where {_phone_match('mobile_no')} or {_phone_match('phone')}
				    union select parent from `tabContact Phone`
				    where parenttype = 'Contact' and {_phone_match('phone')}""",
				{"last10": last10},
				pluck=True,
			)
		)
	names |= _linked_parents("Contact", "CRM Lead", leads)
	return names


def _linked_parents(parenttype, link_doctype, link_names):
	if not link_names:
		return set()
	return set(
		frappe.get_all(
			"Dynamic Link",
			filters={"parenttype": parenttype, "link_doctype": link_doctype, "link_name": ["in", list(link_names)]},
			pluck="parent",
		)
	)


def _find_trial_orders(mobile_no, leads, bot_refs, result):
	orders = set()
	if leads:
		orders |= set(frappe.get_all("Trial Order", filters={"lead": ["in", list(leads)]}, pluck="name"))
	orders |= set(frappe.get_all("Trial Order", filters={"mobile_no": mobile_no}, pluck="name"))
	for ref in bot_refs:
		row = frappe.db.get_value("Trial Order", {"bot_ref": ref}, ["name", "lead", "mobile_no"], as_dict=True)
		if not row:
			continue  # already gone: idempotent
		if row.name in orders:
			continue
		# A bot_ref from another subject must never be erased under this subject's number.
		result["skipped_bot_refs"].append(ref)
	return sorted(orders)


# ---------------------------------------------------------------- erasing


def _erase_attached(doctype, name, subject_leads, held=None):
	"""Delete an Address/Contact unless it is also used by something other than
	this subject's Lead; then only unlink it and report it as retained."""
	others = frappe.db.sql(
		"""select link_doctype, link_name from `tabDynamic Link`
		   where parenttype = %s and parent = %s
		     and not (link_doctype = 'CRM Lead' and link_name in %s)
		     and not (link_doctype = 'Customer' and link_name in %s)""",
		(doctype, name, tuple(subject_leads) or ("",), tuple(held or ()) or ("",)),
		as_dict=True,
	)
	if others:
		if subject_leads:
			frappe.db.sql(
				"""delete from `tabDynamic Link` where parenttype = %s and parent = %s
				   and link_doctype = 'CRM Lead' and link_name in %s""",
				(doctype, name, tuple(subject_leads)),
			)
		return "retained", {
			"doctype": doctype,
			"name": name,
			"reason": "also linked to " + ", ".join(f"{o.link_doctype} {o.link_name}" for o in others),
		}
	frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, delete_permanently=True)
	return "deleted", {"doctype": doctype, "name": name}


def _lead_references(lead):
	"""Things other than the subject's own footprint that point at this Lead."""
	found = []

	def add(doctype, name):
		if doctype not in NOT_BLOCKING and doctype != "CRM Lead":
			found.append(f"{doctype} {name}")

	link_fields = frappe.get_all("DocField", filters={"fieldtype": "Link", "options": "CRM Lead"}, fields=["parent", "fieldname"])
	link_fields += [
		{"parent": d.dt, "fieldname": d.fieldname}
		for d in frappe.get_all("Custom Field", filters={"fieldtype": "Link", "options": "CRM Lead"}, fields=["dt", "fieldname"])
	]
	for f in link_fields:
		for parent, name, ptype in _rows(f["parent"], f"`{f['fieldname']}` = %s", (lead,)):
			add(ptype or f["parent"], parent or name)

	dyn = frappe.get_all("DocField", filters={"fieldtype": "Dynamic Link"}, fields=["parent", "fieldname", "options"])
	dyn += [
		{"parent": d.dt, "fieldname": d.fieldname, "options": d.options}
		for d in frappe.get_all("Custom Field", filters={"fieldtype": "Dynamic Link"}, fields=["dt", "fieldname", "options"])
	]
	for f in dyn:
		if not f.get("options") or f["parent"] in NOT_BLOCKING:
			continue
		for parent, name, ptype in _rows(
			f["parent"], f"`{f['options']}` = 'CRM Lead' and `{f['fieldname']}` = %s", (lead,)
		):
			add(ptype or f["parent"], parent or name)
	return sorted(set(found))


def _rows(doctype, condition, values):
	if doctype in NOT_BLOCKING or not frappe.db.table_exists(doctype):
		return []
	try:
		is_child = frappe.get_meta(doctype).istable
	except Exception:
		return []
	cols = "name, parent, parenttype" if is_child else "name, null, null"
	try:
		rows = frappe.db.sql(f"select {cols} from `tab{doctype}` where {condition}", values)
	except Exception:
		return []
	return [(r[1], r[0], r[2]) for r in rows]


def _anonymise_lead(lead):
	meta = frappe.get_meta("CRM Lead")
	values = {f: None for f in PERSONAL_LEAD_FIELDS if meta.has_field(f)}
	values.update({k: "Erased" for k in ("first_name", "lead_name", "title") if meta.has_field(k)})
	for f in ("custom_whatsapp_verified", "custom_marketing_opt_in"):
		if meta.has_field(f):
			values[f] = 0
	frappe.db.set_value("CRM Lead", lead, values, update_modified=False)
	# the denormalised JSON columns hold comment text and names
	for column in ("_comments", "_assign", "_liked_by", "_user_tags"):
		if frappe.db.has_column("CRM Lead", column):
			frappe.db.sql(f"update `tabCRM Lead` set `{column}` = null where name = %s", lead)


# ---------------------------------------------------------------- purging


def _purge(owned, mobile_no, bot_refs, protected=()):
	purged = {}

	def count(doctype, n):
		if n:
			purged[doctype] = purged.get(doctype, 0) + n

	for doctype, ref_dt, ref_name in REFERENCE_TABLES:
		if not frappe.db.table_exists(doctype):
			continue
		for dt, name in owned:
			if doctype == "File":  # through delete_doc so the file on disk goes too
				for file in frappe.get_all(doctype, filters={ref_dt: dt, ref_name: name}, pluck="name"):
					frappe.delete_doc("File", file, force=True, ignore_permissions=True, delete_permanently=True)
					count(doctype, 1)
				continue
			count(doctype, _delete_where(doctype, f"`{ref_dt}` = %s and `{ref_name}` = %s", (dt, name)))

	# Contact and Address names embed the person's name; Frappe's "Deleted" feed Comment
	# for them carries that name in its text and has no reference_name to match on.
	needles = [last_10_digits(mobile_no) or mobile_no, *bot_refs]
	needles += [name for dt, name in owned if dt in ("Contact", "Address")]
	for doctype, column, (ref_dt, ref_name) in TEXT_TABLES:
		if not frappe.db.table_exists(doctype):
			continue
		for needle in needles:
			condition, values = f"`{column}` like %s", [f"%{needle}%"]
			if protected:  # never touch the audit trail of documents under legal hold
				pairs = sorted(protected)
				condition += f" and (coalesce(`{ref_dt}`, ''), coalesce(`{ref_name}`, '')) not in (" + ", ".join(["(%s, %s)"] * len(pairs)) + ")"
				values += [x for pair in pairs for x in pair]
			count(doctype, _delete_where(doctype, condition, values))
	return purged


def _delete_where(doctype, condition, values):
	n = frappe.db.sql(f"select count(*) from `tab{doctype}` where {condition}", values)[0][0]
	if n:
		frappe.db.sql(f"delete from `tab{doctype}` where {condition}", values)
	return n


# =============================================================== upsert_lead

class CallerBlockedError(frappe.ValidationError):
	"""The number is on the blocked list or is not an Indian number (BR-CRM-002).
	HTTP 422; the message always starts with the fixed token `CALLER_BLOCKED:`."""

	http_status_code = 422


# (parameter, CRM Lead field) pairs refreshed on every call when a value is sent
_UPDATE_FIELDS = (
	("intent", "custom_latest_intent"),
	("consent_at", "custom_consent_at"),
	("policy_version", "custom_policy_version"),
	("campaign", "custom_campaign"),
	("chatwoot_contact_id", "custom_chatwoot_contact_id"),
	("marketing_opt_in", "custom_marketing_opt_in"),
	("whatsapp_verified", "custom_whatsapp_verified"),
)


@frappe.whitelist(methods=["POST"])
def upsert_lead(
	mobile_no: str,
	first_name: str | None = None,
	last_name: str | None = None,
	lead_owner: str | None = None,
	status: str | None = None,
	campaign: str | None = None,
	intent: str | None = None,
	conversation_id: str | None = None,
	consent_at: str | None = None,
	policy_version: str | None = None,
	chatwoot_contact_id: str | None = None,
	marketing_opt_in: int | None = None,
	whatsapp_verified: int | None = None,
):
	"""Find the CRM Lead for this E.164 number, or create it. One number, one Lead.

	Reuses the Sarvam voice agent's logic rather than re-implementing it:
	- the Lead is found on the last 10 digits of `mobile_no`/`phone`, any spelling
	  (`voice_agent._find_lead`) and, like `caller_gate.lookup_by_phone`, WITHOUT the
	  own/assigned-records restriction, so a Lead owned by another user is still found
	  (a plain REST filter cannot see it) - for the configured Sarvam gate user ONLY
	  (`Sarvam Voice Agent Settings.caller_gate_automation_user`). Any other caller,
	  including holders of `CRM User Custom`, must be able to read the Lead normally,
	  otherwise `PermissionError` (and nothing is created or changed);
	- BR-CRM-002: a blocked or foreign number raises `CallerBlockedError`
	  (HTTP 422, message `CALLER_BLOCKED: reason=...; mobile_no=...`), on create and update;
	- BR-CRM-001 and the phone normalisation run as the CRM Lead's own hooks on insert.

	On create: `lead_owner` and `status` must be sent (this app invents no defaults);
	`custom_first_intent` is set from `intent` and `custom_first_conversation_id` from
	`conversation_id`, once, never overwritten later.
	On later calls: only the values sent are refreshed (latest intent, consent_at,
	policy_version, campaign, chatwoot_contact_id, marketing_opt_in, whatsapp_verified).

	Returns {"name": <CRM Lead>, "created": bool, "updated_fields": [...]}.
	"""
	from core_customizations.core_customizations.caller_gate import (
		evaluate,
		get_automation_user,
	)
	from core_customizations.core_customizations.phone_normalise import normalise
	from core_customizations.core_customizations.voice_agent import _find_lead

	frappe.has_permission("CRM Lead", "create", throw=True)

	phone = normalise((mobile_no or "").strip())
	if not E164.match(phone or ""):
		frappe.throw(_("mobile_no must be E.164, e.g. +919876543210"), frappe.ValidationError)

	gate = evaluate(phone)
	if not gate["allowed"]:
		frappe.throw(f"CALLER_BLOCKED: reason={gate['reason']}; mobile_no={phone}", CallerBlockedError)

	values = {
		"custom_campaign": campaign,
		"custom_latest_intent": intent,
		"custom_consent_at": consent_at,
		"custom_policy_version": policy_version,
		"custom_chatwoot_contact_id": chatwoot_contact_id,
		"custom_marketing_opt_in": None if marketing_opt_in is None else int(bool(int(marketing_opt_in))),
		"custom_whatsapp_verified": None if whatsapp_verified is None else int(bool(int(whatsapp_verified))),
	}
	values = {k: v for k, v in values.items() if v not in (None, "")}

	bypass = frappe.session.user == get_automation_user()
	last10 = last_10_digits(phone)

	existing = _find_lead(last10)
	if existing:
		return _update_lead(existing.name, values, bypass)

	if not lead_owner or not status or not first_name:
		frappe.throw(
			_("first_name, lead_owner and status are required to create a Lead."), frappe.ValidationError
		)
	doc = frappe.get_doc(
		{
			"doctype": "CRM Lead",
			"first_name": first_name,
			"last_name": last_name,
			"mobile_no": phone,
			"lead_owner": lead_owner,
			"status": status,
			"custom_first_intent": intent or None,
			"custom_first_conversation_id": conversation_id or None,
			**values,
		}
	)
	try:
		doc.insert()
	except frappe.DuplicateEntryError:
		# lost a race with another call for the same number: it is now an update
		existing = _find_lead(last10)
		if existing:
			return _update_lead(existing.name, values, bypass)
		raise
	return {"name": doc.name, "created": True, "updated_fields": []}


def _update_lead(name, values, bypass):
	if not bypass and not frappe.has_permission("CRM Lead", "read", name):
		frappe.throw(_("You are not permitted to access this Lead."), frappe.PermissionError)
	doc = frappe.get_doc("CRM Lead", name)
	changed = [f for f, v in values.items() if doc.get(f) != v]
	if changed:
		for f in changed:
			doc.set(f, values[f])
		# custom_first_intent / custom_first_conversation_id are never touched here
		doc.save(ignore_permissions=bypass)
	return {"name": name, "created": False, "updated_fields": changed}


# ----------------------------------------------------------------- legal hold


def _find_held_customers(mobile_no, contacts, result):
	"""{customer: [(doctype, name), ...]} for the subject's Customers that have sales records.
	A Customer WITHOUT sales records is not touched here and is reported as retained."""
	names = set()
	if mobile_no.startswith("+91"):
		names |= set(
			frappe.db.sql(
				f"select name from `tabCustomer` where {_phone_match('mobile_no')}",
				{"last10": last_10_digits(mobile_no)},
				pluck=True,
			)
		)
	else:
		names |= set(frappe.get_all("Customer", filters={"mobile_no": mobile_no}, pluck="name"))
	if contacts:
		names |= set(
			frappe.get_all(
				"Dynamic Link",
				filters={"parenttype": "Contact", "parent": ["in", list(contacts)], "link_doctype": "Customer"},
				pluck="link_name",
			)
		)
	held = {}
	for customer in sorted(names):
		records = _sales_records(customer)
		if records:
			held[customer] = records
		else:
			result["retained"].append(
				{"doctype": "Customer", "name": customer, "reason": "Customer has no sales records; not erased by this method"}
			)
	return held


def _sales_records(customer):
	records = []
	for doctype in SALES_DOCTYPES:  # any docstatus: a cancelled record is still a record
		records += [(doctype, n) for n in frappe.get_all(doctype, filters={"customer": customer}, pluck="name")]
	records += [
		("Payment Entry", n)
		for n in frappe.get_all("Payment Entry", filters={"party_type": "Customer", "party": customer}, pluck="name")
	]
	return records


def _used_by_sales_records(held):
	"""Contact and Address names the held sales documents point at."""
	contacts, addresses = set(), set()
	for records in held.values():
		for doctype, name in records:
			meta = frappe.get_meta(doctype)
			for fields, bucket in ((HOLD_CONTACT_FIELDS, contacts), (HOLD_ADDRESS_FIELDS, addresses)):
				for field in fields:
					value = meta.has_field(field) and frappe.db.get_value(doctype, name, field)
					if value:
						bucket.add(value)
	return contacts, addresses


def _finish_hold(held, keep, leads, result):
	"""Unlink the erased Leads from the kept Contact/Address, minimise what is kept, mark the
	Customer, report."""
	for doctype, names in keep.items():
		if names and leads:
			frappe.db.sql(
				"""delete from `tabDynamic Link` where parenttype = %s and parent in %s
				   and link_doctype = 'CRM Lead' and link_name in %s""",
				(doctype, tuple(names), tuple(leads)),
			)
	_minimise_held_contacts(held, keep)
	for customer, records in held.items():
		if not frappe.db.get_value("Customer", customer, "custom_erasure_requested_on"):
			frappe.db.set_value(
				"Customer", customer, "custom_erasure_requested_on", frappe.utils.today(), update_modified=False
			)
		# never leave the Customer pointing at a Contact/Address we deleted
		for field, doctype, text in (
			("customer_primary_contact", "Contact", None),
			("customer_primary_address", "Address", "primary_address"),
		):
			value = frappe.db.get_value("Customer", customer, field)
			if value and not frappe.db.exists(doctype, value):
				frappe.db.set_value("Customer", customer, field, None, update_modified=False)
				if text:
					frappe.db.set_value("Customer", customer, text, None, update_modified=False)
		counts = {}
		for doctype, _name in records:
			counts[doctype] = counts.get(doctype, 0) + 1
		result["held"].append({"doctype": "Customer", "name": customer, "sales_records": counts})
	for doctype, names in keep.items():
		for name in sorted(names):
			result["held"].append({"doctype": doctype, "name": name, "reason": "used by held sales documents"})
	result["hold_reason"] = HOLD_REASON


def _minimise_held_contacts(held, keep):
	"""Held side keeps only what the invoice itself carries (name, address). Contact channels are
	cleared so nothing can reach the person again: phone, mobile and WhatsApp number (they live
	in the Contact's phone rows), email, and the Address's phone/email/fax. The Customer is
	disabled and its Contact marked unsubscribed."""
	from frappe.contacts.doctype.address.address import get_address_display

	for name in keep["Contact"]:
		frappe.db.set_value(
			"Contact",
			name,
			{"mobile_no": None, "phone": None, "email_id": None, "unsubscribed": 1},
			update_modified=False,
		)
		frappe.db.delete("Contact Phone", {"parenttype": "Contact", "parent": name})
		frappe.db.delete("Contact Email", {"parenttype": "Contact", "parent": name})
	for name in keep["Address"]:
		frappe.db.set_value("Address", name, {"phone": None, "email_id": None, "fax": None}, update_modified=False)
	for customer in held:
		values = {"mobile_no": None, "email_id": None, "disabled": 1}
		primary = frappe.db.get_value("Customer", customer, "customer_primary_address")
		if primary and primary in keep["Address"]:  # the rendered text shows phone/email: re-render
			values["primary_address"] = get_address_display(frappe.get_doc("Address", primary).as_dict())
		frappe.db.set_value("Customer", customer, values, update_modified=False)
