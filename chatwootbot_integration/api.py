# Copyright (c) 2026, Vinod Kumar K and contributors
# For license information, please see license.txt

"""Right-to-erasure for the Swarna chatbot subject.

`erase_subject` is the ONLY way to remove a chatbot subject's personal data.
A plain REST DELETE is not enough: Frappe keeps a full JSON copy of every
deleted record in `Deleted Document`, plus `Version`, `Comment`, `Activity Log`
and attached `File` rows. This method deletes the records, then purges those.

It is plan-and-execute on ONE code path: `dry_run=True` walks exactly the steps a real
run takes, but only records them. So the dry-run plan cannot drift from what a real run does.

Where a retention purge job for leads that never claim would hook in: call
`erase_subject` from a scheduler event in hooks.py once the retention rule is decided.
Nothing is scheduled here.
"""

import json
import re
import secrets

import frappe
from frappe import _
from core_customizations.core_customizations.caller_gate import last_10_digits

CLEANUP_ROLE = "Cleanup Role"
E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")

# Never count as "something else references the record": the subject's own footprint, or rows
# purged below (Communication and Integration Request payloads are purged, not preserved).
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
	"Communication",
	"Communication Link",
	"Integration Request",
}
# Children of a CRM Deal that go with it.
DEAL_OWNED = {"CRM Task", "FCRM Note"}

# (doctype, reference-doctype column, reference-name column) rows to purge for every
# record the subject owned (deleted or anonymised).
REFERENCE_TABLES = [
	("Comment", "reference_doctype", "reference_name"),
	("Communication", "reference_doctype", "reference_name"),
	("Integration Request", "reference_doctype", "reference_docname"),
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
# Tables whose rows may carry the number itself; matched on the last 10 digits. Each carries the
# columns that say which document the row is about, so held documents can be excluded.
_COMM = ("reference_doctype", "reference_name")
_INTG = ("reference_doctype", "reference_docname")
TEXT_TABLES = [
	("Deleted Document", "data", ("deleted_doctype", "deleted_name")),
	("Version", "data", ("ref_doctype", "docname")),
	("Comment", "content", _COMM),
	("Comment", "subject", _COMM),
	("Activity Log", "subject", _COMM),
	("Activity Log", "content", _COMM),
	("Communication", "content", _COMM),
	("Communication", "subject", _COMM),
	("Communication", "phone_no", _COMM),
	("Integration Request", "data", _INTG),
	("Integration Request", "output", _INTG),
	("Integration Request", "error", _INTG),
]
# Deleted through delete_doc so child rows and files on disk go too.
DELETE_VIA_DOC = {"File", "Communication"}

# Legal hold: a subject whose Customer has any of these keeps the Customer (and the Contact/Address
# those documents point at); everything else is erased as usual.
HOLD_REASON = "sales records"
HOLD_DOCTYPES = ("Sales Order", "Sales Invoice", "Delivery Note", "POS Invoice")  # linked by `customer`
HOLD_CONTACT_FIELDS = ("contact_person",)
HOLD_ADDRESS_FIELDS = ("customer_address", "shipping_address_name")

PERSONAL_LEAD_FIELDS = [
	"salutation", "middle_name", "last_name", "email", "mobile_no", "phone", "organization", "website",
	"job_title", "gender", "image", "custom_whatsapp_verified", "custom_chatwoot_contact_id",
	"custom_marketing_opt_in", "custom_consent_at", "custom_policy_version", "custom_first_conversation_id",
	"custom_caller_preferences",
]  # fmt: skip
PERSONAL_CUSTOMER_FIELDS = [
	"mobile_no", "email_id", "website", "tax_id", "gstin", "pan", "image", "customer_details",
	"customer_primary_contact", "customer_primary_address", "primary_address", "crm_deal", "lead_name",
]  # fmt: skip


def _flag(value):
	if isinstance(value, str):
		return value.strip().lower() in ("1", "true", "yes")
	return bool(value)


@frappe.whitelist(methods=["POST"])
def erase_subject(
	mobile_no: str,
	bot_refs: list[str] | None = None,
	dry_run: bool = False,
	confirm_quotations: bool = False,
):
	"""Erase everything held about the subject with this E.164 number.

	Always: the Trial Orders, the Contact/Address, the CRM Lead (anonymised instead if something
	other than the subject's own Deals/Quotations still references it), then the Deleted Document /
	Version / Comment / Activity Log / File / Communication / Integration Request rows.

	Legal hold: a Customer with any sales record (Sales Order, Sales Invoice, Delivery Note, POS
	Invoice, Payment Entry, Journal Entry, GL Entry) is KEPT, minimised, and reported under `held`.

	A Customer WITHOUT such records, and the subject's Deals and Quotations, are erased only when it
	is safe: every Quotation draft or cancelled, every Deal still at its initial status, Customer Type
	not Company. Otherwise only the safe part runs and `needs_review` lists why. Submitted Quotations
	are cancelled and deleted only with `confirm_quotations=True` (Company customers and later-stage
	Deals always stay for a human).

	`dry_run=True` changes nothing and returns {would_delete, would_anonymise, would_cancel, held,
	needs_review, retained, would_purge}. One transaction, idempotent. Callable by Cleanup Role only.
	"""
	frappe.only_for(CLEANUP_ROLE)

	mobile_no = (mobile_no or "").strip()
	if not E164.match(mobile_no):
		frappe.throw(_("mobile_no must be E.164, e.g. +919876543210"), frappe.ValidationError)
	if isinstance(bot_refs, str):
		bot_refs = json.loads(bot_refs) if bot_refs.strip() else []
	bot_refs = [r.strip() for r in (bot_refs or []) if r and r.strip()]

	ctx = _discover(mobile_no, bot_refs)
	return _process(ctx, execute=not _flag(dry_run), confirm=_flag(confirm_quotations))


# ----------------------------------------------------------------- finding


def _phone_match(column):
	return f"right(regexp_replace(ifnull({column}, ''), '[^0-9]', ''), 10) = %(last10)s"


def _discover(mobile_no, bot_refs):
	"""Read-only: who and what belongs to this number."""
	ctx = frappe._dict(mobile_no=mobile_no, bot_refs=bot_refs, skipped=[])
	ctx.leads = list(_find_leads(mobile_no))
	ctx.orders = _find_trial_orders(mobile_no, ctx.leads, bot_refs, ctx.skipped)
	ctx.contacts = _find_contacts(mobile_no, ctx.leads)

	# Addresses: referenced by a Trial Order, linked to a lead/contact, or carrying the number.
	addresses = set(frappe.get_all("Address", filters={"phone": mobile_no}, pluck="name"))
	for o in ctx.orders:
		address = frappe.db.get_value("Trial Order", o, "address")
		if address:
			addresses.add(address)
	for doctype, names in (("CRM Lead", ctx.leads), ("Contact", ctx.contacts)):
		addresses |= _linked_parents("Address", doctype, names)
	ctx.addresses = addresses

	# Customers: held (sales records) or plain.
	ctx.held, ctx.plain = {}, []
	for customer in _find_customers(mobile_no, ctx.contacts):
		info = _sales_records(customer)
		if info.records or info.counts:
			ctx.held[customer] = info
		else:
			ctx.plain.append(customer)

	# Deals and Quotations of the subject: its Leads' Deals, its plain Customers' Deals and Quotations.
	deals = set(frappe.get_all("CRM Deal", filters={"lead": ["in", ctx.leads]}, pluck="name")) if ctx.leads else set()
	for customer in ctx.plain:
		deals |= set(frappe.get_all("CRM Deal", filters={"erpnext_customer": customer}, pluck="name"))
		crm_deal = frappe.db.get_value("Customer", customer, "crm_deal")
		if crm_deal and frappe.db.exists("CRM Deal", crm_deal):
			deals.add(crm_deal)
	ctx.deals = {d: frappe.db.get_value("CRM Deal", d, "status") for d in sorted(deals)}
	quotations = set()
	for party_type, parties in (("CRM Deal", list(deals)), ("CRM Lead", ctx.leads), ("Customer", ctx.plain)):
		if parties:
			quotations |= set(
				frappe.get_all("Quotation", filters={"quotation_to": party_type, "party_name": ["in", parties]}, pluck="name")
			)
	ctx.quotations = {q: frappe.db.get_value("Quotation", q, "docstatus") for q in sorted(quotations)}
	return ctx


def _find_leads(mobile_no):
	if mobile_no.startswith("+91"):
		last10 = last_10_digits(mobile_no)
		return frappe.db.sql(
			f"select name from `tabCRM Lead` where {_phone_match('mobile_no')} or {_phone_match('phone')}",
			{"last10": last10},
			pluck=True,
		)
	return frappe.get_all("CRM Lead", or_filters={"mobile_no": mobile_no, "phone": mobile_no}, pluck="name")


def _find_contacts(mobile_no, leads):
	names = set(frappe.get_all("Contact", or_filters={"mobile_no": mobile_no, "phone": mobile_no}, pluck="name"))
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


def _find_trial_orders(mobile_no, leads, bot_refs, skipped):
	orders = set()
	if leads:
		orders |= set(frappe.get_all("Trial Order", filters={"lead": ["in", list(leads)]}, pluck="name"))
	orders |= set(frappe.get_all("Trial Order", filters={"mobile_no": mobile_no}, pluck="name"))
	for ref in bot_refs:
		name = frappe.db.get_value("Trial Order", {"bot_ref": ref}, "name")
		if not name or name in orders:
			continue  # already gone (idempotent) or already ours
		# A bot_ref from another subject must never be erased under this subject's number.
		skipped.append(ref)
	return sorted(orders)


def _find_customers(mobile_no, contacts):
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
	return sorted(names)


def _sales_records(customer):
	"""What makes a Customer legally held. `records` are documents we can name (and protect);
	`counts` is the per-doctype tally, which also covers GL Entry rows (too many to list)."""
	records, counts = [], {}

	def add(doctype, names):
		if names:
			records.extend((doctype, n) for n in names)
			counts[doctype] = counts.get(doctype, 0) + len(names)

	for doctype in HOLD_DOCTYPES:  # any docstatus: a cancelled record is still a record
		add(doctype, frappe.get_all(doctype, filters={"customer": customer}, pluck="name"))
	add("Payment Entry", frappe.get_all("Payment Entry", filters={"party_type": "Customer", "party": customer}, pluck="name"))
	add(
		"Journal Entry",
		sorted(
			set(
				frappe.get_all(
					"Journal Entry Account",
					filters={"party_type": "Customer", "party": customer, "parenttype": "Journal Entry"},
					pluck="parent",
				)
			)
		),
	)
	gl = frappe.db.count("GL Entry", {"party_type": "Customer", "party": customer})
	if gl:
		counts["GL Entry"] = gl
	return frappe._dict(records=records, counts=counts)


def _used_by_sales_records(held):
	"""Contact and Address names the held sales documents point at."""
	contacts, addresses = set(), set()
	for info in held.values():
		for doctype, name in info.records:
			meta = frappe.get_meta(doctype)
			for fields, bucket in ((HOLD_CONTACT_FIELDS, contacts), (HOLD_ADDRESS_FIELDS, addresses)):
				for field in fields:
					value = meta.has_field(field) and frappe.db.get_value(doctype, name, field)
					if value:
						bucket.add(value)
	return contacts, addresses


def _initial_deal_status():
	if frappe.db.exists("CRM Deal Status", "Qualification"):
		return "Qualification"  # what CRM Deal.validate_status gives a new Deal
	rows = frappe.get_all("CRM Deal Status", filters={"type": "Open"}, order_by="position", pluck="name")
	return rows[0] if rows else None


def _review_reasons(ctx):
	"""[(doctype, name, reason, clearable_by_confirm)] - why a human should look first."""
	reasons = []
	for name, docstatus in ctx.quotations.items():
		if docstatus == 1:
			reasons.append(("Quotation", name, "submitted Quotation", True))
	initial = _initial_deal_status()
	for name, status in ctx.deals.items():
		if status != initial:
			reasons.append(("CRM Deal", name, f"Deal past its initial status ({status})", False))
	for customer in ctx.plain:
		if frappe.db.get_value("Customer", customer, "customer_type") == "Company":
			reasons.append(("Customer", customer, "Customer Type is Company", False))
	return reasons


# ---------------------------------------------------------- references


def _references(target_doctype, target_name):
	"""(doctype, name) of every document that Links / Dynamic-Links to the target."""
	found = set()
	link_fields = frappe.get_all(
		"DocField", filters={"fieldtype": "Link", "options": target_doctype}, fields=["parent", "fieldname"]
	)
	link_fields += [
		{"parent": d.dt, "fieldname": d.fieldname}
		for d in frappe.get_all(
			"Custom Field", filters={"fieldtype": "Link", "options": target_doctype}, fields=["dt", "fieldname"]
		)
	]
	for f in link_fields:
		for ref in _rows(f["parent"], f"`{f['fieldname']}` = %s", (target_name,)):
			found.add(ref)

	dyn = frappe.get_all("DocField", filters={"fieldtype": "Dynamic Link"}, fields=["parent", "fieldname", "options"])
	dyn += [
		{"parent": d.dt, "fieldname": d.fieldname, "options": d.options}
		for d in frappe.get_all("Custom Field", filters={"fieldtype": "Dynamic Link"}, fields=["dt", "fieldname", "options"])
	]
	for f in dyn:
		if not f.get("options"):
			continue
		for ref in _rows(f["parent"], f"`{f['options']}` = %s and `{f['fieldname']}` = %s", (target_doctype, target_name)):
			found.add(ref)
	found.discard((target_doctype, target_name))
	return found


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
	return [((r[2], r[1]) if is_child else (doctype, r[0])) for r in rows]


def _blockers(doctype, name, gone, ignore_doctypes=()):
	"""What still points at the record once everything already planned/done is gone."""
	return sorted(
		f"{dt} {n}"
		for dt, n in _references(doctype, name)
		if dt not in NOT_BLOCKING and dt not in ignore_doctypes and (dt, n) not in gone
	)


# ---------------------------------------------------------------- the run


def _process(ctx, execute, confirm):
	R = {"deleted": [], "anonymised": [], "cancelled": [], "retained": [], "held": [], "needs_review": []}
	gone, owned = set(), []

	def drop(doctype, name, **extra):
		if execute:
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, delete_permanently=True)
		gone.add((doctype, name))
		owned.append((doctype, name))
		R["deleted"].append({"doctype": doctype, "name": name, **extra})

	# --- legal hold bookkeeping
	protected, keep = set(), {"Contact": set(), "Address": set()}
	contacts, addresses = set(ctx.contacts), set(ctx.addresses)
	if ctx.held:
		protected |= {("Customer", c) for c in ctx.held}
		for info in ctx.held.values():
			protected |= set(info.records)
		used_contacts, used_addresses = _used_by_sales_records(ctx.held)
		keep["Contact"], keep["Address"] = contacts & used_contacts, addresses & used_addresses
		contacts -= keep["Contact"]
		addresses -= keep["Address"]
		protected |= {("Contact", n) for n in keep["Contact"]} | {("Address", n) for n in keep["Address"]}

	# --- is it safe to erase the Deals / Quotations / plain Customers?
	reasons = _review_reasons(ctx)
	blocking = [r for r in reasons if not r[3]]
	confirmable = [r for r in reasons if r[3]]
	proceed = not blocking and (not confirmable or confirm)
	if not proceed:
		R["needs_review"] = [
			{"doctype": dt, "name": n, "reason": why, **({"clear_with": "confirm_quotations=True"} if ok else {})}
			for dt, n, why, ok in reasons
		]
	erasing_customers = set(ctx.plain) if proceed else set()

	# --- 1. Trial Orders
	for name in ctx.orders:
		drop("Trial Order", name)

	# --- 2. Address / Contact: delete unless something else uses them
	ignore_links = {("CRM Lead", x) for x in ctx.leads} | {("Customer", x) for x in [*ctx.held, *erasing_customers]}
	for doctype, names in (("Address", addresses), ("Contact", contacts)):
		for name in sorted(names):
			others = _other_links(doctype, name, ignore_links)
			owned.append((doctype, name))
			if others:
				if execute and ctx.leads:
					_unlink_leads(doctype, [name], ctx.leads)
				R["retained"].append(
					{"doctype": doctype, "name": name, "reason": "also linked to " + ", ".join(others)}
				)
			else:
				drop(doctype, name)

	if proceed:
		_erase_quotations_deals_customers(ctx, R, gone, owned, drop, execute)

	# --- 6. Leads: delete, or anonymise if something else still points at them
	for lead in ctx.leads:
		blockers = _blockers("CRM Lead", lead, gone)
		owned.append(("CRM Lead", lead))
		if blockers:
			if execute:
				_anonymise_lead(lead)
			R["anonymised"].append({"doctype": "CRM Lead", "name": lead, "referenced_by": blockers})
		else:
			drop("CRM Lead", lead)

	# --- 7. held side
	if ctx.held:
		_finish_hold(ctx, keep, R, execute)

	# --- 8. purge LAST: deleting the records above can itself write Comment/Version/feed rows
	purged = _purge(owned, ctx.mobile_no, ctx.bot_refs, protected, execute)
	return _result(R, ctx, purged, execute)


def _result(R, ctx, purged, execute):
	out = {"skipped_bot_refs": ctx.skipped, "retained": R["retained"]}
	if execute:
		out.update(deleted=R["deleted"], anonymised=R["anonymised"], purged=purged)
		if R["cancelled"]:
			out["cancelled"] = R["cancelled"]
		if R["held"]:
			out["held"] = R["held"]
		if R["needs_review"]:
			out["needs_review"] = R["needs_review"]
	else:
		out.update(
			dry_run=True,
			would_delete=R["deleted"],
			would_anonymise=R["anonymised"],
			would_cancel=R["cancelled"],
			held=R["held"],
			needs_review=R["needs_review"],
			would_purge=purged,  # rows that exist now; the deletions themselves add Deleted Document/Version rows that a real run also purges
		)
	if R["held"]:
		out["hold_reason"] = HOLD_REASON
	return out


def _other_links(doctype, name, ignore_links):
	rows = frappe.db.sql(
		"""select link_doctype, link_name from `tabDynamic Link` where parenttype = %s and parent = %s""",
		(doctype, name),
		as_dict=True,
	)
	return [f"{r.link_doctype} {r.link_name}" for r in rows if (r.link_doctype, r.link_name) not in ignore_links]


def _unlink_leads(doctype, names, leads):
	frappe.db.sql(
		"""delete from `tabDynamic Link` where parenttype = %s and parent in %s
		   and link_doctype = 'CRM Lead' and link_name in %s""",
		(doctype, tuple(names), tuple(leads)),
	)


def _erase_quotations_deals_customers(ctx, R, gone, owned, drop, execute):
	# --- 3. Quotations: drafts and cancelled go; submitted only with confirm_quotations
	for name, docstatus in ctx.quotations.items():
		blockers = _blockers("Quotation", name, gone)
		if blockers:
			R["retained"].append({"doctype": "Quotation", "name": name, "reason": "referenced by " + ", ".join(blockers)})
			continue
		try:
			if docstatus == 1:
				if execute:
					quotation = frappe.get_doc("Quotation", name)
					quotation.flags.ignore_permissions = True
					quotation.cancel()
				R["cancelled"].append({"doctype": "Quotation", "name": name})
			drop("Quotation", name, docstatus=docstatus)
		except Exception as e:  # report, do not abort the whole erasure
			R["retained"].append({"doctype": "Quotation", "name": name, "reason": f"{type(e).__name__}: {str(e)[:160]}"})

	# --- 4. Deals (with their own Tasks and Notes)
	for name in ctx.deals:
		owned_children = {ref for ref in _references("CRM Deal", name) if ref[0] in DEAL_OWNED}
		blockers = _blockers("CRM Deal", name, gone | {("Customer", c) for c in ctx.plain}, ignore_doctypes=DEAL_OWNED)
		if blockers:
			R["retained"].append({"doctype": "CRM Deal", "name": name, "reason": "referenced by " + ", ".join(blockers)})
			continue
		if execute:
			frappe.db.sql("update `tabCustomer` set crm_deal = null where crm_deal = %s", name)
		for dt, child in sorted(owned_children):
			drop(dt, child)
		drop("CRM Deal", name)

	# --- 5. plain Customers: delete, or anonymise if something else still points at them
	for customer in ctx.plain:
		blockers = _blockers("Customer", customer, gone)
		owned.append(("Customer", customer))
		if blockers:
			new_name = _anonymise_customer(customer) if execute else None
			if new_name:
				owned.append(("Customer", new_name))
			R["anonymised"].append(
				{"doctype": "Customer", "name": customer, **({"renamed_to": new_name} if new_name else {}), "referenced_by": blockers}
			)
		else:
			drop("Customer", customer)


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


def _anonymise_customer(customer):
	"""Customer that cannot be deleted: blank it, disable it, rename the document so the person's
	name is not the record's id. Returns the new name (or None if the rename was refused)."""
	meta = frappe.get_meta("Customer")
	values = {f: None for f in PERSONAL_CUSTOMER_FIELDS if meta.has_field(f)}
	values.update({"customer_name": "Erased", "disabled": 1})
	if meta.has_field("custom_erasure_requested_on"):
		values["custom_erasure_requested_on"] = frappe.utils.today()
	frappe.db.set_value("Customer", customer, values, update_modified=False)
	for column in ("_comments", "_assign", "_liked_by", "_user_tags"):
		if frappe.db.has_column("Customer", column):
			frappe.db.sql(f"update `tabCustomer` set `{column}` = null where name = %s", customer)
	from frappe.model.rename_doc import rename_doc

	try:
		new_name = _erasure_name()
		for _attempt in range(5):  # 48 random bits: a clash is vanishingly rare, but never overwrite
			if not frappe.db.exists("Customer", new_name):
				break
			new_name = _erasure_name()
		rename_doc("Customer", customer, new_name, force=True, ignore_permissions=True, show_alert=False)
		return new_name
	except Exception:
		return None


ERASED_NAME = re.compile(r"^Erased-[0-9a-f]{12}$")


def _erasure_name():
	"""`Erased-` + 12 random hex characters (48 bits from the OS CSPRNG).

	Deliberately takes NO input: it is not derived from the name, phone, address or any other value of
	the person, so there is nothing to reverse and no salt/secret to protect."""
	return f"Erased-{secrets.token_hex(6)}"


# ----------------------------------------------------------------- legal hold


def _finish_hold(ctx, keep, R, execute):
	"""Unlink the erased Leads from the kept Contact/Address, minimise what is kept, mark the
	Customer, report."""
	if execute:
		for doctype, names in keep.items():
			if names and ctx.leads:
				_unlink_leads(doctype, names, ctx.leads)
		_minimise_held(ctx.held, keep)
	for customer, info in ctx.held.items():
		if execute:
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
		R["held"].append({"doctype": "Customer", "name": customer, "sales_records": info.counts})
	for doctype, names in keep.items():
		for name in sorted(names):
			R["held"].append({"doctype": doctype, "name": name, "reason": "used by held sales documents"})


def _minimise_held(held, keep):
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


# ---------------------------------------------------------------- purging


def _purge(owned, mobile_no, bot_refs, protected, execute):
	"""Remove (or, in a dry run, count) the audit/communication rows about what the subject owned."""
	purged = {}

	def count(doctype, n):
		if n:
			purged[doctype] = purged.get(doctype, 0) + n

	owned = list(dict.fromkeys(owned))
	for doctype, ref_dt, ref_name in REFERENCE_TABLES:
		if not frappe.db.table_exists(doctype):
			continue
		for dt, name in owned:
			count(doctype, _purge_rows(doctype, f"`{ref_dt}` = %s and `{ref_name}` = %s", [dt, name], execute))
	# Communications can also be tied to a record only through their Communication Link rows
	for dt, name in owned:
		names = frappe.db.sql(
			"select distinct parent from `tabCommunication Link` where parenttype = 'Communication' and link_doctype = %s and link_name = %s",
			(dt, name),
			pluck=True,
		)
		count("Communication", _remove_named("Communication", names, execute))

	# Contact and Address names embed the person's name; Frappe's "Deleted" feed Comment
	# for them carries that name in its text and has no reference_name to match on.
	needles = [last_10_digits(mobile_no) or mobile_no, *bot_refs]
	needles += [name for dt, name in owned if dt in ("Contact", "Address")]
	for doctype, column, (ref_dt, ref_name) in TEXT_TABLES:
		if not frappe.db.table_exists(doctype) or not frappe.db.has_column(doctype, column):
			continue
		for needle in needles:
			condition, values = f"`{column}` like %s", [f"%{needle}%"]
			if protected:  # never touch the audit trail of documents under legal hold
				pairs = sorted(protected)
				condition += f" and (coalesce(`{ref_dt}`, ''), coalesce(`{ref_name}`, '')) not in (" + ", ".join(["(%s, %s)"] * len(pairs)) + ")"
				values += [x for pair in pairs for x in pair]
			count(doctype, _purge_rows(doctype, condition, values, execute))
	return purged


def _purge_rows(doctype, condition, values, execute):
	if doctype in DELETE_VIA_DOC:
		names = frappe.db.sql(f"select name from `tab{doctype}` where {condition}", values, pluck=True)
		return _remove_named(doctype, names, execute)
	n = frappe.db.sql(f"select count(*) from `tab{doctype}` where {condition}", values)[0][0]
	if n and execute:
		frappe.db.sql(f"delete from `tab{doctype}` where {condition}", values)
	return n


def _remove_named(doctype, names, execute):
	n = 0
	for name in names:
		if frappe.db.exists(doctype, name):
			if execute:
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, delete_permanently=True)
			n += 1
	return n


# =============================================================== upsert_lead / record_claim

SYNC_ROLES = ("CRM User Custom", "System Manager")
PINCODE = re.compile(r"^[1-9][0-9]{5}$")


class CallerBlockedError(frappe.ValidationError):
	"""The number is on the blocked list or is not an Indian number (BR-CRM-002).
	HTTP 422; the message always starts with the fixed token `CALLER_BLOCKED:`."""

	http_status_code = 422


def _gate_checked_phone(mobile_no):
	"""Permission, E.164 and BR-CRM-002 gate: returns the normalised number or raises."""
	from core_customizations.core_customizations.caller_gate import evaluate
	from core_customizations.core_customizations.phone_normalise import normalise

	frappe.has_permission("CRM Lead", "create", throw=True)
	phone = normalise((mobile_no or "").strip())
	if not E164.match(phone or ""):
		frappe.throw(_("mobile_no must be E.164, e.g. +919876543210"), frappe.ValidationError)
	gate = evaluate(phone)
	if not gate["allowed"]:
		frappe.throw(f"CALLER_BLOCKED: reason={gate['reason']}; mobile_no={phone}", CallerBlockedError)
	return phone


def _bypass():
	"""The own/assigned-records bypass: the configured Sarvam gate user ONLY."""
	from core_customizations.core_customizations.caller_gate import get_automation_user

	return frappe.session.user == get_automation_user()


def _lead_values(campaign, intent, consent_at, policy_version, chatwoot_contact_id, marketing_opt_in, whatsapp_verified):
	values = {
		"custom_campaign": campaign,
		"custom_latest_intent": intent,
		"custom_consent_at": consent_at,
		"custom_policy_version": policy_version,
		"custom_chatwoot_contact_id": chatwoot_contact_id,
		"custom_marketing_opt_in": None if marketing_opt_in is None else int(bool(int(marketing_opt_in))),
		"custom_whatsapp_verified": None if whatsapp_verified is None else int(bool(int(whatsapp_verified))),
	}
	return {k: v for k, v in values.items() if v not in (None, "")}


def _atomic(label, fn):
	"""Run `fn` so that, if it raises, everything it wrote is rolled back (a savepoint inside the
	request's own transaction). Background jobs queued by the undone writes find nothing to do."""
	frappe.db.savepoint(label)
	try:
		return fn()
	except Exception:
		frappe.db.rollback(save_point=label)
		raise


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
	with_contact: bool = False,
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

	`with_contact=True` also finds-or-creates the Contact for the same E.164 number and links it
	to the Lead, in the same transaction (everything is undone if either part fails).

	Returns {"name": <CRM Lead>, "created": bool, "updated_fields": [...]}; with `with_contact`
	also {"contact": <Contact>, "contact_created": bool}.
	"""
	phone = _gate_checked_phone(mobile_no)
	values = _lead_values(campaign, intent, consent_at, policy_version, chatwoot_contact_id, marketing_opt_in, whatsapp_verified)

	def work():
		result = _upsert_lead(phone, first_name, last_name, lead_owner, status, intent, conversation_id, values)
		if _flag(with_contact):
			contact, created = _ensure_contact(result["name"], phone, first_name, last_name)
			result.update(contact=contact, contact_created=created)
		return result

	return _atomic("upsert_lead", work) if _flag(with_contact) else work()


def _upsert_lead(phone, first_name, last_name, lead_owner, status, first_intent, conversation_id, values):
	from core_customizations.core_customizations.voice_agent import _find_lead

	bypass = _bypass()
	last10 = last_10_digits(phone)
	existing = _find_lead(last10)
	if existing:
		return _update_lead(existing.name, values, bypass)

	if not lead_owner or not status or not first_name:
		frappe.throw(_("first_name, lead_owner and status are required to create a Lead."), frappe.ValidationError)
	doc = frappe.get_doc(
		{
			"doctype": "CRM Lead",
			"first_name": first_name,
			"last_name": last_name,
			"mobile_no": phone,
			"lead_owner": lead_owner,
			"status": status,
			"custom_first_intent": first_intent or None,
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


def _ensure_contact(lead, phone, first_name, last_name):
	"""The Contact for this E.164 number, linked to the Lead: found by the Sarvam lookup (last 10
	digits, any spelling) or created. Returns (name, created)."""
	from core_customizations.core_customizations.voice_agent import _find_contact

	bypass = _bypass()
	existing = _find_contact(last_10_digits(phone))
	if existing:
		linked = frappe.db.exists(
			"Dynamic Link",
			{"parenttype": "Contact", "parent": existing.name, "link_doctype": "CRM Lead", "link_name": lead},
		)
		if not linked:
			if not bypass and not frappe.has_permission("Contact", "write", existing.name):
				frappe.throw(_("You are not permitted to access this Contact."), frappe.PermissionError)
			doc = frappe.get_doc("Contact", existing.name)
			doc.append("links", {"link_doctype": "CRM Lead", "link_name": lead})
			doc.save(ignore_permissions=bypass)
		return existing.name, False

	if not first_name:
		first_name, last_name = frappe.db.get_value("CRM Lead", lead, ["first_name", "last_name"]) or (None, None)
	doc = frappe.get_doc(
		{
			"doctype": "Contact",
			"first_name": first_name or phone,
			"last_name": last_name,
			"phone_nos": [{"phone": phone, "is_primary_mobile_no": 1}],
			"links": [{"link_doctype": "CRM Lead", "link_name": lead}],
		}
	)
	doc.insert()
	return doc.name, True


# ------------------------------------------------------------------ record_claim


def _json(value, kind):
	if isinstance(value, str):
		value = json.loads(value) if value.strip() else kind()
	return value if isinstance(value, kind) else kind()


def _state_from_pincode(pincode):
	"""India Compliance's own pincode-range table (the e-Invoice master): the state when the first
	three digits belong to exactly one state, else None (the caller must send address.state)."""
	from india_compliance.gst_india.constants import STATE_PINCODE_MAPPING

	prefix = int(pincode[:3])
	states = []
	for state, rng in STATE_PINCODE_MAPPING.items():
		for lo, hi in (rng,) if isinstance(rng[0], int) else rng:
			if lo <= prefix <= hi:
				states.append(state)
	return states[0] if len(states) == 1 else None


def _norm(text):
	return " ".join((text or "").lower().split())


def _line_matches(stored, head):
	"""`stored` is `head` or `head, <more>`: how record_claim writes address_line1/2."""
	stored, head = _norm(stored), _norm(head)
	return stored == head or stored.startswith(head + ",")


def _ensure_address(lead, a, title):
	"""The Address linked to the Lead that matches pincode + house_no + street, or a new one."""
	names = frappe.get_all(
		"Dynamic Link",
		filters={"parenttype": "Address", "link_doctype": "CRM Lead", "link_name": lead},
		pluck="parent",
	)
	for row in frappe.get_all(
		"Address", filters={"name": ["in", names], "pincode": a["pincode"]}, fields=["name", "address_line1", "address_line2"]
	) if names else []:
		if _line_matches(row.address_line1, a["house_no"]) and _line_matches(row.address_line2, a["street"]):
			return row.name, False

	state = a.get("state") or _state_from_pincode(a["pincode"])
	if not state:
		frappe.throw(
			_("The state cannot be derived from pincode {0}; send address.state.").format(a["pincode"]),
			frappe.ValidationError,
		)
	doc = frappe.get_doc(
		{
			"doctype": "Address",
			"address_title": title,
			"address_type": "Shipping",
			"address_line1": ", ".join(x for x in (a["house_no"], a.get("building_name")) if x),
			"address_line2": ", ".join(x for x in (a["street"], a.get("landmark")) if x),
			"city": a["city"],
			"state": state,
			"country": "India",
			"pincode": a["pincode"],
			"links": [{"link_doctype": "CRM Lead", "link_name": lead}],
		}
	)
	doc.insert()
	return doc.name, True


def _validate_claim_input(bot_ref, consent_at, address, items):
	"""Everything checkable without touching the database, so a bad request writes nothing."""
	if not bot_ref:
		frappe.throw(_("bot_ref is required."), frappe.ValidationError)
	if not consent_at:
		frappe.throw(_("consent_at is required."), frappe.ValidationError)
	for key in ("house_no", "street", "city", "pincode"):
		if not (address.get(key) or "").strip():
			frappe.throw(_("address.{0} is required.").format(key), frappe.ValidationError)
	if not PINCODE.match(address["pincode"]):
		frappe.throw(_("Pincode must be 6 digits and cannot start with 0."), frappe.ValidationError)
	if not items:
		frappe.throw(_("At least one item is required."), frappe.ValidationError)
	for i, row in enumerate(items, start=1):
		try:
			ok = bool(row.get("item")) and float(row.get("qty") or 0) > 0
		except (TypeError, ValueError):
			ok = False
		if not ok:
			frappe.throw(_("Item row {0}: item and a qty greater than 0 are required.").format(i), frappe.ValidationError)


def _claim_documents(trial_order, phone):
	row = frappe.db.get_value("Trial Order", trial_order, ["name", "lead", "address", "mobile_no"], as_dict=True)
	from core_customizations.core_customizations.voice_agent import _find_contact

	contact = _find_contact(last_10_digits(phone))
	return row, contact.name if contact else None


@frappe.whitelist(methods=["POST"])
def record_claim(
	bot_ref: str,
	mobile_no: str,
	first_name: str,
	lead_owner: str,
	status: str,
	campaign: str,
	intent: str,
	consent_at: str,
	address: dict,
	items: list,
	last_name: str | None = None,
	first_intent: str | None = None,
	chatwoot_contact_id: str | None = None,
	chatwoot_conversation_id: str | None = None,
	whatsapp_verified: int | None = None,
	marketing_opt_in: int | None = None,
	policy_version: str | None = None,
	terms_message_id: str | None = None,
	claim_message_id: str | None = None,
):
	"""Record one trial-pack claim in ONE transaction: Lead, Contact, Address, Trial Order.

	- Lead and Contact: `upsert_lead`'s logic, gate and guards (CALLER_BLOCKED behaves the same;
	  the gate user finds a Lead owned by someone else, other callers do not; first_intent and the
	  first conversation id are set only when the Lead is created).
	- Address: the one linked to the Lead that matches pincode + house_no + street, else a new one.
	  Stored as address_line1 = house_no[, building_name], address_line2 = street[, landmark]; the
	  state comes from `address.state` or, when the pincode prefix maps to exactly one state,
	  from India Compliance's pincode table.
	- Trial Order: idempotent on `bot_ref`. If it already exists the existing documents are
	  returned and NOTHING is created or changed (same bot_ref for another number is refused).
	- custom_trial_order_count is kept by the Trial Order itself.

	Any failure rolls back everything this call created. Callable by CRM User Custom.

	Returns {lead, contact, address, trial_order, created: {lead, contact, address, trial_order}}.
	"""
	frappe.only_for(SYNC_ROLES)

	bot_ref = (bot_ref or "").strip()
	address = {k: (v.strip() if isinstance(v, str) else v) for k, v in _json(address, dict).items()}
	items = _json(items, list)
	_validate_claim_input(bot_ref, consent_at, address, items)
	phone = _gate_checked_phone(mobile_no)

	def replay():
		row, contact = _claim_documents(frappe.db.get_value("Trial Order", {"bot_ref": bot_ref}), phone)
		if last_10_digits(row.mobile_no) != last_10_digits(phone):
			frappe.throw(
				_("DUPLICATE_BOT_REF: bot_ref {0} already belongs to another subject").format(bot_ref),
				frappe.DuplicateEntryError,
			)
		return {
			"lead": row.lead,
			"contact": contact,
			"address": row.address,
			"trial_order": row.name,
			"created": {"lead": False, "contact": False, "address": False, "trial_order": False},
		}

	if frappe.db.exists("Trial Order", {"bot_ref": bot_ref}):
		return replay()

	values = _lead_values(campaign, intent, consent_at, policy_version, chatwoot_contact_id, marketing_opt_in, whatsapp_verified)

	def work():
		lead = _upsert_lead(phone, first_name, last_name, lead_owner, status, first_intent or intent, chatwoot_conversation_id, values)
		contact, contact_created = _ensure_contact(lead["name"], phone, first_name, last_name)
		addr, addr_created = _ensure_address(lead["name"], address, first_name)
		order = frappe.get_doc(
			{
				"doctype": "Trial Order",
				"lead": lead["name"],
				"campaign": campaign,
				"bot_ref": bot_ref,
				"intent": intent or None,
				"address": addr,
				"house_no": address["house_no"],
				"building_name": address.get("building_name"),
				"street": address["street"],
				"landmark": address.get("landmark"),
				"city": address["city"],
				"pincode": address["pincode"],
				"phone_verified": int(bool(int(whatsapp_verified or 0))),
				"marketing_opt_in": int(bool(int(marketing_opt_in or 0))),
				"terms_accepted_at": consent_at,
				"policy_version": policy_version,
				"terms_message_id": terms_message_id,
				"claim_message_id": claim_message_id,
				"chatwoot_conversation_id": chatwoot_conversation_id,
				"items": [{"item": r["item"], "qty": float(r["qty"])} for r in items],
			}
		).insert()
		return {
			"lead": lead["name"],
			"contact": contact,
			"address": addr,
			"trial_order": order.name,
			"created": {"lead": lead["created"], "contact": contact_created, "address": addr_created, "trial_order": True},
		}

	try:
		return _atomic("record_claim", work)
	except frappe.DuplicateEntryError:
		# another request with the same bot_ref won the race: ours is undone, return the winner's
		if frappe.db.exists("Trial Order", {"bot_ref": bot_ref}):
			return replay()
		raise
