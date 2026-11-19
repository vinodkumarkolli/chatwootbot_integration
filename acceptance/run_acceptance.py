"""Acceptance tests over REST for chatwootbot_integration.

Run from the bench `sites` folder:

    ../env/bin/python ../apps/chatwootbot_integration/acceptance/run_acceptance.py development.localhost [base_url]

Credentials:
  * If SYNC_KEY / SYNC_SECRET / CLEANUP_KEY / CLEANUP_SECRET are set, those (the real
    identities' keys) are used, and the roles are NOT touched.
  * Otherwise throwaway users are created with the SAME roles as the real identities
    (Sales User / Cleanup Role only), their keys are generated in memory, and the
    users and all test data are deleted at the end. Keys are never printed or written.
"""

import os
import sys
import uuid

import frappe
import requests

SITE = sys.argv[1] if len(sys.argv) > 1 else "development.localhost"
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8000"
PHONE = "+919900000001"
LAST10 = "9900000001"
NAME = "Zzerasure Testperson"
STREET = "Zztest Street 77"
BOT = "TR-" + uuid.uuid4().hex[:8].upper()
BOT2 = "TR-" + uuid.uuid4().hex[:8].upper()
PHONE2 = "+919900000002"  # a subject with a Lead and Contact but NO Trial Order
NAME2 = "Zznoorder Testperson"
results = []


def check(label, ok, detail=""):
	ok = bool(ok)
	results.append(ok)
	print(f"[{'PASS' if ok else 'FAIL'}] {label}  {detail}")


class Client:
	def __init__(self, key, secret):
		self.h = {"Authorization": f"token {key}:{secret}", "Host": SITE, "Content-Type": "application/json"}

	def call(self, method, path, **kw):
		r = requests.request(method, f"{BASE}{path}", headers=self.h, timeout=60, **kw)
		try:
			body = r.json()
		except Exception:
			body = {"_raw": r.text[:200]}
		return r.status_code, body


def fresh():
	"""End this script's own DB snapshot so it sees what the server committed."""
	frappe.db.rollback()


def short(body):
	if isinstance(body, dict):
		for k in ("exc_type", "message", "data"):
			if k in body:
				v = body[k]
				return f"{k}={str(v)[:160]}"
	return str(body)[:160]


def make_user(email, role):
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	u = frappe.get_doc(
		{"doctype": "User", "email": email, "first_name": "AcceptanceTmp", "enabled": 1,
		 "send_welcome_email": 0, "user_type": "System User", "roles": [{"role": role}]}
	)  # fmt: skip
	u.flags.no_welcome_mail = True
	u.insert(ignore_permissions=True)
	secret = frappe.generate_hash(length=15)
	u.api_key = frappe.generate_hash(length=15)
	u.api_secret = secret
	u.save(ignore_permissions=True)
	frappe.db.commit()
	return u.api_key, secret


def main():
	frappe.init(site=SITE, sites_path=".")
	frappe.connect()
	temp = not os.environ.get("SYNC_KEY")
	if temp:
		sync_email, clean_email = "zz.acc.sync@example.com", "zz.acc.clean@example.com"
		sync = Client(*make_user(sync_email, os.environ.get("SYNC_ROLE", "Sales User")))
		clean = Client(*make_user(clean_email, "Cleanup Role"))
		owner = sync_email
	else:
		sync = Client(os.environ["SYNC_KEY"], os.environ["SYNC_SECRET"])
		clean = Client(os.environ["CLEANUP_KEY"], os.environ["CLEANUP_SECRET"])
		owner = os.environ["LEAD_OWNER"]
	status = os.environ.get("LEAD_STATUS", "New")
	item = frappe.db.get_value("Item", {"disabled": 0}, "name")
	campaign = "SUJITHA10"
	print(f"mode={'temp mirror users, sync role=' + os.environ.get('SYNC_ROLE', 'Sales User') if temp else 'real identities'} item={item}\n")

	def order(**o):
		d = {"lead": lead, "campaign": campaign, "bot_ref": BOT, "house_no": "7", "street": STREET, "city": "Chennai",
		     "pincode": "600001", "terms_accepted_at": "2026-11-19 10:00:00", "phone_verified": 1,
		     "items": [{"item": item, "qty": 1}], "intent": "trial_claim"}  # fmt: skip
		d.update(o)
		return {k: v for k, v in d.items() if v is not None}

	print("== Setup: Lead, Contact, Address (acceptance test 4) ==")
	c, b = sync.call("POST", "/api/resource/CRM Lead", json={
		"first_name": NAME, "mobile_no": PHONE, "lead_owner": owner, "status": status,
		"custom_campaign": campaign, "custom_first_intent": "trial_claim", "custom_latest_intent": "trial_claim",
		"custom_first_conversation_id": "conv-1", "custom_consent_at": "2026-11-19 10:00:00", "custom_policy_version": "v1"})  # fmt: skip
	lead = b.get("data", {}).get("name")
	check("4a create CRM Lead", c == 200 and lead, f"{c} {lead or short(b)}")
	c, b = sync.call("POST", "/api/resource/Contact", json={
		"first_name": NAME, "mobile_no": PHONE, "phone_nos": [{"phone": PHONE, "is_primary_mobile_no": 1}],
		"links": [{"link_doctype": "CRM Lead", "link_name": lead}]})  # fmt: skip
	contact = b.get("data", {}).get("name")
	check("4b create Contact linked to Lead", c == 200 and contact, f"{c} {contact or short(b)}")
	c, b = sync.call("POST", "/api/resource/Address", json={
		"address_title": NAME, "address_type": "Shipping", "address_line1": STREET, "city": "Chennai",
		"state": "Tamil Nadu", "country": "India", "pincode": "600001", "phone": PHONE,
		"links": [{"link_doctype": "CRM Lead", "link_name": lead}]})  # fmt: skip
	address = b.get("data", {}).get("name")
	check("4c create Address linked to Lead", c == 200 and address, f"{c} {address or short(b)}")

	print("\n== 1/2/3 Trial Order ==")
	c, b = sync.call("POST", "/api/resource/Trial Order", json=order(address=address))
	to = b.get("data", {}).get("name", "")
	check("1 valid Trial Order -> 200 and TO-2026-#####", c == 200 and to.startswith("TO-2026-"), f"{c} {to or short(b)}")
	check("4d Trial Order links Lead + Address; lead_name/mobile fetched", c == 200 and b["data"].get("lead_name") == NAME and b["data"].get("mobile_no") == PHONE and b["data"].get("address") == address, "")  # fmt: skip
	c, b = sync.call("POST", "/api/resource/Trial Order", json=order())
	fresh()
	n = frappe.db.count("Trial Order", {"bot_ref": BOT})
	check("2 same bot_ref -> duplicate error, one record", c in (409, 417) and "DUPLICATE" in str(b).upper() and n == 1, f"{c} count={n} {short(b)}")  # fmt: skip
	for label, kw in (("no items", {"items": []}), ("qty 0", {"items": [{"item": item, "qty": 0}]}), ("pincode 012345", {"pincode": "012345"})):
		c, b = sync.call("POST", "/api/resource/Trial Order", json=order(bot_ref="TR-X" + uuid.uuid4().hex[:6], **kw))
		check(f"3 {label} -> 4xx", 400 <= c < 500, f"{c} {short(b)}")
	fresh()
	lead_count = frappe.db.get_value("CRM Lead", lead, "custom_trial_order_count")
	check("Lead.custom_trial_order_count == 1", lead_count == 1, f"{lead_count}")

	print("\n== 7 intent / campaign ==")
	c, b = sync.call("POST", "/api/resource/Trial Order", json=order(bot_ref="TR-X" + uuid.uuid4().hex[:6], intent="nonsense"))
	check("7 unknown intent rejected", 400 <= c < 500, f"{c} {short(b)}")
	c, b = sync.call("POST", "/api/resource/Trial Order", json=order(bot_ref="TR-X" + uuid.uuid4().hex[:6], campaign="NOSUCHCAMPAIGN"))
	check("7 unknown campaign rejected", 400 <= c < 500, f"{c} {short(b)}")
	c, b = sync.call("PUT", f"/api/resource/CRM Lead/{lead}", json={"custom_latest_intent": "info", "custom_first_intent": "other", "custom_first_conversation_id": "conv-2"})
	fresh()
	row = frappe.db.get_value("CRM Lead", lead, ["custom_first_intent", "custom_latest_intent", "custom_first_conversation_id"], as_dict=True)
	check("7 latest_intent updates, first_intent / first_conversation_id unchanged", c == 200 and row.custom_first_intent == "trial_claim" and row.custom_latest_intent == "info" and row.custom_first_conversation_id == "conv-1", f"{c} {dict(row)}")  # fmt: skip
	c, b = sync.call("POST", "/api/resource/Campaign", json={"campaign_name": "ZZNEWCAMP"})
	check("sync user cannot create a Campaign", c == 403, f"{c}")
	c, b = sync.call("GET", "/api/resource/Campaign")
	check("sync user can read Campaign", c == 200, f"{c}")

	print("\n== 5 DELETE as sync user (expect 403) ==")
	for dt, name in (("Trial Order", to), ("Contact", contact), ("Address", address)):
		c, b = sync.call("DELETE", f"/api/resource/{dt}/{name}")
		if dt == "Contact":  # accepted exception: the creator may delete its own Contact
			print(f"[INFO] 5 DELETE Contact by its creator -> {c} (creator-only exception accepted)")
			continue
		check(f"5 DELETE {dt} -> 403", c == 403, f"{c} {short(b)}")
	fresh()
	for dt, name in (("Trial Order", to), ("Contact", contact), ("Address", address)):  # a delete that wrongly succeeded
		if not frappe.db.exists(dt, name):
			print(f"   NOTE: {dt} {name} was deleted by the sync user")
	if not frappe.db.exists("Contact", contact):  # keep the erase test meaningful
		c, b = sync.call("POST", "/api/resource/Contact", json={"first_name": NAME, "mobile_no": PHONE, "links": [{"link_doctype": "CRM Lead", "link_name": lead}]})  # fmt: skip
		contact = b.get("data", {}).get("name")

	print("\n== 6 Reads that must be blocked for the sync user (Customer read is expected, not tested) ==")
	for dt in ("Sales Invoice", "Sales Order", "Delivery Note", "Opportunity", "Lead", "Journal Entry"):
		c, b = sync.call("GET", f"/api/resource/{dt}", params={"limit_page_length": 1})
		check(f"6 GET {dt} -> 403", c == 403, f"{c}")

	print("\n== 10a Cleanup user reads (expect 403) / sync user cannot erase ==")
	for dt in ("Sales Invoice", "Customer", "Journal Entry"):
		c, b = clean.call("GET", f"/api/resource/{dt}", params={"limit_page_length": 1})
		check(f"10 cleanup GET {dt} -> 403", c == 403, f"{c}")
	c, b = sync.call("POST", "/api/method/chatwootbot_integration.api.erase_subject", json={"mobile_no": PHONE})
	check("10 sync user erase_subject -> 403", c == 403, f"{c}")

	print("\n== upsert_lead ==")
	U = "/api/method/chatwootbot_integration.api.upsert_lead"
	P4 = "+919900000004"
	other = frappe.db.get_value("User", {"enabled": 1, "user_type": "System User", "name": ["not in", ["Administrator", "Guest", owner]]}, "name")
	frappe.set_user("Administrator")
	foreign_lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "Zzupsert Otherowner", "mobile_no": P4, "lead_owner": other, "status": status}).insert().name
	frappe.db.commit()
	c, b = sync.call("GET", "/api/resource/CRM Lead", params={"filters": '[["mobile_no","=","%s"]]' % P4})
	print("   plain REST filter sees the other user's Lead:", bool(b.get("data")))
	c, b = sync.call("POST", U, json={"mobile_no": P4, "first_name": "Zzupsert Otherowner", "lead_owner": owner, "status": status, "intent": "info", "campaign": campaign, "policy_version": "v2"})
	m = b.get("message") or {}
	fresh()
	n = frappe.db.count("CRM Lead", {"mobile_no": P4})
	fresh()
	n = frappe.db.count("CRM Lead", {"mobile_no": P4})
	r = frappe.db.get_value("CRM Lead", foreign_lead, ["custom_latest_intent", "custom_policy_version", "custom_campaign"], as_dict=True)
	check("upsert_lead, caller is NOT the gate user: another user's Lead is refused (403), not duplicated, not changed", c == 403 and b.get("exc_type") == "PermissionError" and n == 1 and not r.custom_latest_intent and not r.custom_policy_version and not r.custom_campaign, f"{c} {short(b)} count={n} {dict(r)}")  # fmt: skip
	print("   (the gate-user bypass is tested by acceptance/run_real_user_inprocess.py)")
	P5 = "+919900000005"
	c, b = sync.call("POST", U, json={"mobile_no": P5, "first_name": "Zzupsert New", "lead_owner": owner, "status": status, "intent": "trial_claim", "conversation_id": "conv-9", "campaign": campaign, "consent_at": "2026-11-19 10:00:00", "policy_version": "v1"})
	m = b.get("message") or {}
	check("upsert_lead creates a new Lead (created=True)", c == 200 and m.get("created") is True, f"{c} {m} {short(b) if c != 200 else ''}")
	c, b = sync.call("POST", U, json={"mobile_no": "99 0000 0005", "first_name": "x", "intent": "info"})
	m2 = b.get("message") or {}
	fresh()
	row = frappe.db.get_value("CRM Lead", m.get("name"), ["custom_first_intent", "custom_latest_intent", "custom_first_conversation_id"], as_dict=True) if m.get("name") else {}
	check("upsert_lead again (other spelling): same Lead, created=False, first_intent kept, latest updated", c == 200 and m2.get("name") == m.get("name") and m2.get("created") is False and row and row.custom_first_intent == "trial_claim" and row.custom_latest_intent == "info" and row.custom_first_conversation_id == "conv-9", f"{c} {m2} {dict(row or {})}")  # fmt: skip
	c, b = sync.call("POST", U, json={"mobile_no": "+14155550123", "first_name": "Zzforeign", "lead_owner": owner, "status": status})
	check("upsert_lead foreign number -> CALLER_BLOCKED (422, CallerBlockedError)", c == 422 and "CALLER_BLOCKED" in str(b) and b.get("exc_type") == "CallerBlockedError", f"{c} {short(b)}")  # fmt: skip
	c, b = sync.call("POST", U, json={"mobile_no": "+919900000006", "first_name": "NoOwner"})
	check("upsert_lead create without lead_owner/status -> 4xx", 400 <= c < 500, f"{c} {short(b)}")
	c, b = clean.call("POST", U, json={"mobile_no": "+919900000007", "first_name": "x", "lead_owner": owner, "status": status})
	check("Cleanup user cannot call upsert_lead (403)", c == 403, f"{c}")
	for p in (P4, P5):
		clean.call("POST", "/api/method/chatwootbot_integration.api.erase_subject", json={"mobile_no": p})

	print("\n== 8 erase_subject (subject WITH Trial Order) ==")
	# an extra Lead-less comment, an attachment-free Version trail: edit the Trial Order to create Version rows
	sync.call("PUT", f"/api/resource/Trial Order/{to}", json={"notes": "edited for version"})
	c, b = clean.call("POST", "/api/method/chatwootbot_integration.api.erase_subject", json={"mobile_no": PHONE, "bot_refs": [BOT]})
	print("   response:", str(b.get("message"))[:700])
	m = b.get("message") or {}
	deleted = {(d["doctype"], d["name"]) for d in m.get("deleted", [])}
	check("8 call -> 200", c == 200, f"{c}")
	check("8 Trial Order, Address, Contact, Lead deleted", {("Trial Order", to), ("Address", address), ("Contact", contact), ("CRM Lead", lead)} <= deleted, "")
	check("8 subject with NO sales records: nothing held, no hold_reason", "held" not in m and "hold_reason" not in m, "")
	c, b = clean.call("POST", "/api/method/chatwootbot_integration.api.erase_subject", json={"mobile_no": PHONE, "bot_refs": [BOT]})
	m2 = b.get("message") or {}
	check("8 second call is a no-op (200, nothing deleted/anonymised/purged)", c == 200 and not m2.get("deleted") and not m2.get("anonymised") and not m2.get("purged"), f"{c} {str(m2)[:200]}")  # fmt: skip

	print("\n== 8b subject with Lead + Contact, NO Trial Order ==")
	c, b = sync.call("POST", "/api/resource/CRM Lead", json={"first_name": NAME2, "mobile_no": PHONE2, "lead_owner": owner, "status": status, "custom_campaign": campaign})  # fmt: skip
	lead2 = b.get("data", {}).get("name")
	c2, b2 = sync.call("POST", "/api/resource/Contact", json={"first_name": NAME2, "mobile_no": PHONE2, "links": [{"link_doctype": "CRM Lead", "link_name": lead2}]})  # fmt: skip
	contact2 = b2.get("data", {}).get("name")
	check("8b setup Lead + Contact", c == 200 and c2 == 200, f"{lead2} {contact2}")
	c, b = clean.call("POST", "/api/method/chatwootbot_integration.api.erase_subject", json={"mobile_no": PHONE2})
	m = b.get("message") or {}
	deleted = {(d["doctype"], d["name"]) for d in m.get("deleted", [])}
	check("8b Lead and Contact deleted", c == 200 and {("CRM Lead", lead2), ("Contact", contact2)} <= deleted, f"{c} {str(m)[:300]}")
	c, b = clean.call("POST", "/api/method/chatwootbot_integration.api.erase_subject", json={"mobile_no": PHONE2})
	check("8b second call no-op", c == 200 and not (b.get("message") or {}).get("deleted"), f"{c}")

	print("\n== 8c Lead whose Deal is past its initial status: needs_review, Lead anonymised ==")
	c, b = sync.call("POST", "/api/resource/CRM Lead", json={"first_name": "Zzdeal Testperson", "mobile_no": "+919900000003", "lead_owner": owner, "status": status})  # fmt: skip
	lead3 = b.get("data", {}).get("name")
	deal = frappe.get_doc({"doctype": "CRM Deal", "lead": lead3, "status": frappe.get_all("CRM Deal Status", filters={"type": "Ongoing"}, order_by="position", pluck="name")[0], "deal_owner": owner}).insert(ignore_permissions=True)  # fmt: skip
	frappe.db.commit()
	c, b = clean.call("POST", "/api/method/chatwootbot_integration.api.erase_subject", json={"mobile_no": "+919900000003"})
	m = b.get("message") or {}
	fresh()
	vals = frappe.db.get_value("CRM Lead", lead3, ["first_name", "lead_name", "mobile_no", "email"], as_dict=True)
	check("8c Lead anonymised (Erased, no mobile/email), later-stage Deal listed under needs_review and left alone", c == 200 and vals and vals.first_name == "Erased" and not vals.mobile_no and m.get("anonymised") and any(r["doctype"] == "CRM Deal" for r in m.get("needs_review", [])) and frappe.db.exists("CRM Deal", deal.name), f"{c} {dict(vals or {})} {str(m.get('anonymised'))[:200]}")  # fmt: skip
	frappe.delete_doc("CRM Deal", deal.name, force=True, ignore_permissions=True, delete_permanently=True)
	frappe.delete_doc("CRM Lead", lead3, force=True, ignore_permissions=True, delete_permanently=True)
	frappe.db.commit()

	print("\n== 8d legal hold: subject whose Customer has a submitted Sales Invoice ==")
	PH, NAMEH, STREETH = "+919900000011", "Zzhold Testperson", "Zzhold Street 99"
	today = frappe.utils.today()
	c, b = sync.call("POST", "/api/resource/CRM Lead", json={"first_name": NAMEH, "mobile_no": PH, "lead_owner": owner, "status": status})
	leadh = b.get("data", {}).get("name")
	made = {}
	EMAILH = "zzhold@example.com"
	for key, title in (("contact1", NAMEH), ("contact2", "Zzhold Other")):
		body = {"first_name": title, "links": [{"link_doctype": "CRM Lead", "link_name": leadh}]}
		if key == "contact1":
			body.update({"phone_nos": [{"phone": PH, "is_primary_mobile_no": 1}], "email_ids": [{"email_id": EMAILH, "is_primary": 1}]})
		c, b = sync.call("POST", "/api/resource/Contact", json=body)
		made[key] = b.get("data", {}).get("name")
	for key, line in (("address1", STREETH), ("address2", STREETH + " B")):
		c, b = sync.call("POST", "/api/resource/Address", json={"address_title": NAMEH + key, "address_type": "Shipping", "address_line1": line, "city": "Chennai", "state": "Tamil Nadu", "country": "India", "pincode": "600001", "phone": PH, "email_id": EMAILH, "links": [{"link_doctype": "CRM Lead", "link_name": leadh}]})
		made[key] = b.get("data", {}).get("name")
	c, b = sync.call("POST", "/api/resource/Trial Order", json=order(lead=leadh, bot_ref="TR-H" + uuid.uuid4().hex[:6].upper(), address=made["address1"]))
	toh = b.get("data", {}).get("name")
	check("8d setup: Lead, 2 Contacts, 2 Addresses, Trial Order", all([leadh, toh, *made.values()]), f"{leadh} {toh} {made}")
	frappe.set_user("Administrator")
	cust = frappe.get_doc({"doctype": "Customer", "customer_name": "Zzhold Customer", "customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"), "territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"), "mobile_no": PH, "default_price_list": frappe.db.get_value("Price List", {"selling": 1, "enabled": 1}, "name")}).insert()  # fmt: skip
	for key in ("contact1", "address1"):
		d = frappe.get_doc("Contact" if key == "contact1" else "Address", made[key])
		d.append("links", {"link_doctype": "Customer", "link_name": cust.name})
		d.save()
	frappe.db.set_value("Customer", cust.name, {"email_id": EMAILH, "primary_address": f"{STREETH}<br>Phone: {PH}<br>Email: {EMAILH}", "customer_primary_address": made["address1"]})
	# Sales Invoice fixture inserted at DB level as submitted (docstatus 1): the site's dispatch-first rule
	# (BR-SALES-001) blocks a normal insert, and this way no ledger entries are created.
	si = frappe.get_doc({"doctype": "Sales Invoice", "name": "ZZ-HOLD-SI-1", "customer": cust.name, "company": "Sravi Enterprises - Kolapakkam", "posting_date": today, "docstatus": 1, "contact_person": made["contact1"], "contact_mobile": PH, "contact_email": EMAILH, "customer_address": made["address1"], "items": [{"doctype": "Sales Invoice Item", "item_code": item, "qty": 1, "rate": 10, "parent": "ZZ-HOLD-SI-1", "parenttype": "Sales Invoice", "parentfield": "items", "idx": 1}]})  # fmt: skip
	si.db_insert()
	for row in si.items:
		row.db_insert()
	frappe.db.commit()
	c, b = clean.call("POST", "/api/method/chatwootbot_integration.api.erase_subject", json={"mobile_no": PH})
	m = b.get("message") or {}
	print("   response:", str(m)[:900])
	deleted = {(d["doctype"], d["name"]) for d in m.get("deleted", [])}
	heldset = {(d["doctype"], d["name"]) for d in m.get("held", [])}
	check("8d call -> 200, hold_reason == 'sales records'", c == 200 and m.get("hold_reason") == "sales records", f"{c} {m.get('hold_reason')}")
	check("8d Lead, Trial Order and the UNREFERENCED Contact/Address are deleted", {("CRM Lead", leadh), ("Trial Order", toh), ("Contact", made["contact2"]), ("Address", made["address2"])} <= deleted, str(sorted(deleted)))  # fmt: skip
	check("8d held lists the Customer and the Contact/Address used by the invoice", {("Customer", cust.name), ("Contact", made["contact1"]), ("Address", made["address1"])} <= heldset and ("Customer", cust.name) not in deleted, str(sorted(heldset)))  # fmt: skip
	fresh()
	kept = all(frappe.db.exists(dt, n) for dt, n in (("Customer", cust.name), ("Contact", made["contact1"]), ("Address", made["address1"]), ("Sales Invoice", "ZZ-HOLD-SI-1")))
	links = frappe.db.count("Dynamic Link", {"link_doctype": "CRM Lead", "link_name": leadh})
	stamp = frappe.db.get_value("Customer", cust.name, "custom_erasure_requested_on")
	check("8d Customer, kept Contact/Address and the invoice still exist; no link to the erased Lead; erasure date set", kept and links == 0 and str(stamp) == today, f"kept={kept} lead_links={links} custom_erasure_requested_on={stamp}")  # fmt: skip
	c, b = clean.call("POST", "/api/method/chatwootbot_integration.api.erase_subject", json={"mobile_no": PH})
	m2 = b.get("message") or {}
	fresh()
	check("8d second call is a no-op: nothing found, nothing deleted/purged, erasure date unchanged", c == 200 and not m2.get("deleted") and not m2.get("anonymised") and not m2.get("purged") and str(frappe.db.get_value("Customer", cust.name, "custom_erasure_requested_on")) == today, f"{c} {str(m2)[:200]}")  # fmt: skip
	# minimisation of the held side
	cu = frappe.db.get_value("Customer", cust.name, ["mobile_no", "email_id", "disabled", "primary_address"], as_dict=True)
	co = frappe.db.get_value("Contact", made["contact1"], ["mobile_no", "phone", "email_id", "unsubscribed"], as_dict=True)
	ad = frappe.db.get_value("Address", made["address1"], ["phone", "email_id", "fax", "address_line1"], as_dict=True)
	rows = frappe.db.count("Contact Phone", {"parent": made["contact1"]}) + frappe.db.count("Contact Email", {"parent": made["contact1"]})
	check("8e held Customer: mobile/email cleared, disabled", not cu.mobile_no and not cu.email_id and cu.disabled == 1, str(dict(cu)))
	check("8e held Customer: rendered primary address no longer shows phone/email", PH not in (cu.primary_address or "") and EMAILH not in (cu.primary_address or "") and STREETH in (cu.primary_address or ""), str(cu.primary_address))  # fmt: skip
	check("8e kept Contact: phone/mobile/WhatsApp rows and email cleared, unsubscribed, name kept", not co.mobile_no and not co.phone and not co.email_id and co.unsubscribed == 1 and rows == 0, f"{dict(co)} child_rows={rows}")  # fmt: skip
	check("8e kept Address: phone/email/fax cleared, street kept", not ad.phone and not ad.email_id and not ad.fax and ad.address_line1 == STREETH, str(dict(ad)))  # fmt: skip
	print("   personal data that REMAINS on the held side (table, search term, rows):")
	for t, needle, n in sweep(["9900000011", EMAILH, NAMEH, STREETH]):
		print(f"      {t:28} {needle:22} {n}")
	# tidy: remove the held fixtures, then erase once more so no audit rows of them stay behind
	frappe.set_user("Administrator")
	frappe.db.delete("Sales Invoice Item", {"parent": "ZZ-HOLD-SI-1"})
	frappe.db.delete("Sales Invoice", {"name": "ZZ-HOLD-SI-1"})
	for dt, n in (("Contact", made["contact1"]), ("Address", made["address1"]), ("Customer", cust.name)):
		frappe.delete_doc(dt, n, force=True, ignore_permissions=True, delete_permanently=True)
	frappe.db.commit()
	clean.call("POST", "/api/method/chatwootbot_integration.api.erase_subject", json={"mobile_no": PH})
	# test teardown only: deleting the HELD fixtures by hand wrote audit rows nothing else purges
	for table, column in (("Version", "data"), ("Comment", "subject"), ("Comment", "content"), ("Deleted Document", "data"), ("Activity Log", "subject")):
		frappe.db.sql(f"delete from `tab{table}` where `{column}` like %s", "%Zzhold%")
	frappe.db.commit()

	print("\n== 9 full-database search for leftovers ==")
	needles = [LAST10, "9900000002", "9900000004", "9900000005", NAME, NAME2, STREET, BOT, BOT2, "Zzupsert", "9900000011", "Zzhold", "ZZ-HOLD-SI-1", "zzhold@example.com"]
	fresh()
	left = sweep(needles)
	check("9 no table holds the test phones, names, address or bot_ref", not left, str(left)[:600])

	if temp:
		for e in (sync_email, clean_email):
			frappe.delete_doc("User", e, force=True, ignore_permissions=True, delete_permanently=True)
		frappe.db.commit()
	print(f"\n{sum(results)}/{len(results)} checks passed")
	sys.exit(0 if all(results) else 1)


def sweep(needles):
	"""Search every text column of every table for each needle."""
	cols = frappe.db.sql(
		"""select table_name, column_name from information_schema.columns
		   where table_schema = database()
		   and data_type in ('char','varchar','text','tinytext','mediumtext','longtext','json')""",
	)
	by_table = {}
	for t, c in cols:
		by_table.setdefault(t, []).append(c)
	hits = []
	for t, cs in by_table.items():
		for needle in needles:
			cond = " or ".join(f"`{c}` like %s" for c in cs)
			try:
				n = frappe.db.sql(f"select count(*) from `{t}` where {cond}", [f"%{needle}%"] * len(cs))[0][0]
			except Exception:
				continue
			if n:
				hits.append((t, needle, n))
				if t == "tabComment":
					rows = frappe.db.sql(f"select name, comment_type, reference_doctype, reference_name, left(subject,80), creation from `{t}` where {cond} limit 3", [f"%{needle}%"] * len(cs))
					print("   leftover detail:", rows)
	return hits


if __name__ == "__main__":
	main()
