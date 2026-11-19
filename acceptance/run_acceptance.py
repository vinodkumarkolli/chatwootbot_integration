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
		sync = Client(*make_user(sync_email, "Sales User"))
		clean = Client(*make_user(clean_email, "Cleanup Role"))
		owner = sync_email
	else:
		sync = Client(os.environ["SYNC_KEY"], os.environ["SYNC_SECRET"])
		clean = Client(os.environ["CLEANUP_KEY"], os.environ["CLEANUP_SECRET"])
		owner = os.environ["LEAD_OWNER"]
	status = os.environ.get("LEAD_STATUS", "New")
	item = frappe.db.get_value("Item", {"disabled": 0}, "name")
	campaign = "SUJITHA10"
	print(f"mode={'temp mirror users' if temp else 'real identities'} item={item}\n")

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
		check(f"5 DELETE {dt} -> 403", c == 403, f"{c} {short(b)}")
	fresh()
	for dt, name in (("Trial Order", to), ("Contact", contact), ("Address", address)):  # a delete that wrongly succeeded
		if not frappe.db.exists(dt, name):
			print(f"   NOTE: {dt} {name} was deleted by the sync user")
	if not frappe.db.exists("Contact", contact):  # keep the erase test meaningful
		c, b = sync.call("POST", "/api/resource/Contact", json={"first_name": NAME, "mobile_no": PHONE, "links": [{"link_doctype": "CRM Lead", "link_name": lead}]})  # fmt: skip
		contact = b.get("data", {}).get("name")

	print("\n== 6 Reads that should be blocked (sync user) ==")
	for dt in ("Sales Invoice", "Journal Entry", "Customer"):
		c, b = sync.call("GET", f"/api/resource/{dt}", params={"limit_page_length": 1})
		check(f"6 GET {dt} -> 403", c == 403, f"{c}")

	print("\n== 10a Cleanup user reads (expect 403) / sync user cannot erase ==")
	for dt in ("Sales Invoice", "Customer", "Journal Entry"):
		c, b = clean.call("GET", f"/api/resource/{dt}", params={"limit_page_length": 1})
		check(f"10 cleanup GET {dt} -> 403", c == 403, f"{c}")
	c, b = sync.call("POST", "/api/method/chatwootbot_integration.api.erase_subject", json={"mobile_no": PHONE})
	check("10 sync user erase_subject -> 403", c == 403, f"{c}")

	print("\n== 8 erase_subject (subject WITH Trial Order) ==")
	# an extra Lead-less comment, an attachment-free Version trail: edit the Trial Order to create Version rows
	sync.call("PUT", f"/api/resource/Trial Order/{to}", json={"notes": "edited for version"})
	c, b = clean.call("POST", "/api/method/chatwootbot_integration.api.erase_subject", json={"mobile_no": PHONE, "bot_refs": [BOT]})
	print("   response:", str(b.get("message"))[:700])
	m = b.get("message") or {}
	deleted = {(d["doctype"], d["name"]) for d in m.get("deleted", [])}
	check("8 call -> 200", c == 200, f"{c}")
	check("8 Trial Order, Address, Contact, Lead deleted", {("Trial Order", to), ("Address", address), ("Contact", contact), ("CRM Lead", lead)} <= deleted, "")
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

	print("\n== 8c Lead referenced by a Deal is anonymised, not deleted ==")
	c, b = sync.call("POST", "/api/resource/CRM Lead", json={"first_name": "Zzdeal Testperson", "mobile_no": "+919900000003", "lead_owner": owner, "status": status})  # fmt: skip
	lead3 = b.get("data", {}).get("name")
	deal = frappe.get_doc({"doctype": "CRM Deal", "lead": lead3, "status": frappe.get_all("CRM Deal Status", filters={"type": "Open"}, pluck="name")[0], "deal_owner": owner}).insert(ignore_permissions=True)  # fmt: skip
	frappe.db.commit()
	c, b = clean.call("POST", "/api/method/chatwootbot_integration.api.erase_subject", json={"mobile_no": "+919900000003"})
	m = b.get("message") or {}
	fresh()
	vals = frappe.db.get_value("CRM Lead", lead3, ["first_name", "lead_name", "mobile_no", "email"], as_dict=True)
	check("8c Lead anonymised (Erased, no mobile/email) and reported", c == 200 and vals and vals.first_name == "Erased" and not vals.mobile_no and m.get("anonymised"), f"{c} {dict(vals or {})} {str(m.get('anonymised'))[:200]}")  # fmt: skip
	frappe.delete_doc("CRM Deal", deal.name, force=True, ignore_permissions=True, delete_permanently=True)
	frappe.delete_doc("CRM Lead", lead3, force=True, ignore_permissions=True, delete_permanently=True)
	frappe.db.commit()

	print("\n== 9 full-database search for leftovers ==")
	needles = [LAST10, "9900000002", NAME, NAME2, STREET, BOT, BOT2]
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
