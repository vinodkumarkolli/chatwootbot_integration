"""record_claim and upsert_lead(with_contact=True): REST acceptance tests.

Run from the bench `sites` folder, same credential rules as run_acceptance.py:

    ../env/bin/python ../apps/chatwootbot_integration/acceptance/run_record_claim.py development.localhost [base_url]

With SYNC_KEY/SYNC_SECRET/CLEANUP_KEY/CLEANUP_SECRET/LEAD_OWNER set (the real identities) the caller is the
real gate user (automation.crm); otherwise throwaway users with ONLY CRM User Custom / Cleanup Role are used
(then the gate-user bypass does not apply, and the "Lead owned by another user" case expects a refusal).
Every test subject is erased at the end with erase_subject and the database is swept for leftovers.
"""

import os
import sys
import uuid

import frappe

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_acceptance as ra  # noqa: E402

fresh = ra.fresh
CLAIM = "/api/method/chatwootbot_integration.api.record_claim"
UPSERT = "/api/method/chatwootbot_integration.api.upsert_lead"
ERASE = "/api/method/chatwootbot_integration.api.erase_subject"
ITEM = "SASB-50INR-PACK-20"
TAG = uuid.uuid4().hex[:6].upper()
results = []
PHONES = {k: f"+91990000007{i}" for i, k in enumerate("ABCDEFG")}  # +919900000070 ...


def check(label, ok, detail=""):
	ok = bool(ok)
	results.append(ok)
	print(f"[{'PASS' if ok else 'FAIL'}] {label}  {detail}")


def ref(name):
	return f"TR-{TAG}{name}"


def counts(phone):
	"""What exists in the database for this number, right now."""
	fresh()
	leads = frappe.get_all("CRM Lead", filters={"mobile_no": phone}, pluck="name")
	contacts = set(frappe.get_all("Contact Phone", filters={"phone": phone, "parenttype": "Contact"}, pluck="parent"))
	addresses = set(frappe.get_all("Dynamic Link", filters={"parenttype": "Address", "link_doctype": "CRM Lead", "link_name": ["in", leads or [""]]}, pluck="parent"))
	orders = frappe.get_all("Trial Order", filters={"lead": ["in", leads or [""]]}, pluck="name")
	return {"leads": len(leads), "contacts": len(contacts), "addresses": len(addresses), "orders": len(orders)}


ZERO = {"leads": 0, "contacts": 0, "addresses": 0, "orders": 0}


def main():
	frappe.init(site=ra.SITE, sites_path=".")
	frappe.connect()
	real = bool(os.environ.get("SYNC_KEY"))
	if real:
		sync = ra.Client(os.environ["SYNC_KEY"], os.environ["SYNC_SECRET"])
		clean = ra.Client(os.environ["CLEANUP_KEY"], os.environ["CLEANUP_SECRET"])
		owner = os.environ["LEAD_OWNER"]
	else:
		sync_email, clean_email = "zz.rc.sync@example.com", "zz.rc.clean@example.com"
		sync = ra.Client(*ra.make_user(sync_email, "CRM User Custom"))
		clean = ra.Client(*ra.make_user(clean_email, "Cleanup Role"))
		owner = sync_email
	status = os.environ.get("LEAD_STATUS", "New")
	print(f"mode={'real identities (caller IS the gate user)' if real else 'throwaway CRM User Custom user (NOT the gate user)'}\n")

	def body(bot, phone, **over):
		d = dict(bot_ref=ref(bot), mobile_no=phone, first_name="Zzclaim Person", lead_owner=owner, status=status, campaign="SUJITHA10", intent="trial_claim", first_intent="trial_claim", chatwoot_contact_id="cw-1", chatwoot_conversation_id="conv-1", whatsapp_verified=1, marketing_opt_in=1, consent_at="2026-11-19 10:00:00", policy_version="v1", terms_message_id="m-terms", claim_message_id="m-claim", address={"house_no": "12", "building_name": "Block A", "street": "Gandhi Road", "landmark": "Near Temple", "city": "Chennai", "pincode": "600001"}, items=[{"item": ITEM, "qty": 1}])  # fmt: skip
		d.update(over)
		return d

	def msg(b):
		return b.get("message") or {}

	A, B, C, D, E, F = (PHONES[k] for k in "ABCDEF")

	print("== R1 first claim creates Lead, Contact, Address and Trial Order ==")
	c, b = sync.call("POST", CLAIM, json=body("1", A))
	m = msg(b)
	ok = c == 200 and all(m.get("created", {}).get(k) for k in ("lead", "contact", "address", "trial_order"))
	check("R1 200; created = {lead, contact, address, trial_order} all True", ok, f"{c} {ra.short(b) if c != 200 else m.get('created')}")
	fresh()
	if ok:
		lead = frappe.db.get_value("CRM Lead", m["lead"], ["mobile_no", "custom_first_intent", "custom_latest_intent", "custom_first_conversation_id", "custom_campaign", "custom_trial_order_count", "custom_chatwoot_contact_id", "custom_marketing_opt_in", "custom_whatsapp_verified", "custom_policy_version"], as_dict=True)
		to = frappe.db.get_value("Trial Order", m["trial_order"], ["lead", "address", "campaign", "intent", "pincode", "bot_ref", "terms_message_id", "claim_message_id", "chatwoot_conversation_id", "phone_verified", "status"], as_dict=True)
		ad = frappe.db.get_value("Address", m["address"], ["address_line1", "address_line2", "pincode", "state", "city"], as_dict=True)
		linked = frappe.db.exists("Dynamic Link", {"parenttype": "Contact", "parent": m["contact"], "link_doctype": "CRM Lead", "link_name": m["lead"]})
		check("R1 Lead fields: number, first/latest intent, first conversation, campaign, chatwoot id, consent flags, count == 1", lead.mobile_no == A and lead.custom_first_intent == "trial_claim" and lead.custom_latest_intent == "trial_claim" and lead.custom_first_conversation_id == "conv-1" and lead.custom_campaign == "SUJITHA10" and lead.custom_chatwoot_contact_id == "cw-1" and lead.custom_marketing_opt_in == 1 and lead.custom_whatsapp_verified == 1 and lead.custom_trial_order_count == 1, str(dict(lead)))  # fmt: skip
		check("R1 Contact linked to the Lead; Address linked, line1/line2/state stored", bool(linked) and ad.address_line1 == "12, Block A" and ad.address_line2 == "Gandhi Road, Near Temple" and ad.state == "Tamil Nadu", f"linked={bool(linked)} {dict(ad)}")  # fmt: skip
		check("R1 Trial Order: lead, address, campaign, intent, message ids, status Claimed", to.lead == m["lead"] and to.address == m["address"] and to.campaign == "SUJITHA10" and to.intent == "trial_claim" and to.terms_message_id == "m-terms" and to.claim_message_id == "m-claim" and to.chatwoot_conversation_id == "conv-1" and to.phone_verified == 1 and to.status == "Claimed", str(dict(to)))  # fmt: skip

	print("\n== R2 replaying the same bot_ref ==")
	before = counts(A)
	c, b = sync.call("POST", CLAIM, json=body("1", A, first_name="Changed Name", intent="info"))
	m2 = msg(b)
	same = all(m2.get(k) == m.get(k) for k in ("lead", "contact", "address", "trial_order"))
	check("R2 replay: 200, the SAME four documents, created all False", c == 200 and same and not any(m2.get("created", {}).values()), f"{c} {m2}")
	check("R2 replay created nothing and changed nothing (counts equal, latest_intent still trial_claim)", counts(A) == before and frappe.db.get_value("CRM Lead", m["lead"], "custom_latest_intent") == "trial_claim", f"{before} -> {counts(A)}")  # fmt: skip
	c, b = sync.call("POST", CLAIM, json=body("1", B))
	check("R2 the same bot_ref for ANOTHER number is refused (409 DUPLICATE_BOT_REF) and creates nothing", c == 409 and "DUPLICATE_BOT_REF" in str(b) and counts(B) == ZERO, f"{c} {ra.short(b)}")  # fmt: skip

	print("\n== R3 failures create nothing, including a new Lead ==")
	c, b = sync.call("POST", CLAIM, json=body("3", B, address=dict(body("x", B)["address"], pincode="012345")))
	check("R3 invalid pincode 012345 -> 4xx, nothing created for the number", 400 <= c < 500 and counts(B) == ZERO, f"{c} {ra.short(b)} {counts(B)}")  # fmt: skip
	c, b = sync.call("POST", CLAIM, json=body("3b", B, items=[{"item": "NO-SUCH-ITEM-ZZ", "qty": 1}]))
	check("R3 late failure (unknown item, after Lead/Contact/Address were written) -> 4xx and EVERYTHING rolled back", 400 <= c < 500 and counts(B) == ZERO, f"{c} {ra.short(b)} {counts(B)}")  # fmt: skip
	c, b = sync.call("POST", CLAIM, json=body("3c", B, campaign="NOSUCHCAMPAIGN"))
	check("R3 unknown campaign -> 4xx, nothing left", 400 <= c < 500 and counts(B) == ZERO, f"{c} {counts(B)}")
	c, b = sync.call("POST", CLAIM, json=body("3d", B, items=[]))
	check("R3 no items -> 4xx, nothing created", 400 <= c < 500 and counts(B) == ZERO, f"{c}")
	c, b = sync.call("POST", CLAIM, json=body("3e", B, address=dict(body("x", B)["address"], pincode="160001")))
	check("R3 pincode whose prefix maps to two states (Punjab/Chandigarh) and no address.state -> 4xx, nothing created", 400 <= c < 500 and "state" in str(b).lower() and counts(B) == ZERO, f"{c} {ra.short(b)}")  # fmt: skip
	c, b = sync.call("POST", CLAIM, json=body("3f", B, address=dict(body("x", B)["address"], pincode="160001", state="Chandigarh")))
	check("R3 same pincode WITH address.state -> created", c == 200 and msg(b).get("created", {}).get("address"), f"{c} {ra.short(b) if c != 200 else ''}")  # fmt: skip

	print("\n== R4 blocked / foreign number ==")
	c, b = sync.call("POST", CLAIM, json=body("4", "+14155550123"))
	check("R4 non-+91 number -> 422 CALLER_BLOCKED (CallerBlockedError), nothing created", c == 422 and "CALLER_BLOCKED" in str(b) and b.get("exc_type") == "CallerBlockedError" and counts("+14155550123") == ZERO, f"{c} {ra.short(b)}")  # fmt: skip

	print("\n== R5 a second claim for the same number reuses Lead, Contact and Address ==")
	c, b = sync.call("POST", CLAIM, json=body("5", A, address={"house_no": "12", "building_name": "Tower B", "street": "Gandhi Road", "city": "Chennai", "pincode": "600001"}, intent="info"))
	m5 = msg(b)
	cn = counts(A)
	check("R5 new bot_ref, same pincode+house_no+street: Lead/Contact/Address reused, only the Trial Order is new", c == 200 and m5.get("created") == {"lead": False, "contact": False, "address": False, "trial_order": True} and m5.get("lead") == m.get("lead") and m5.get("address") == m.get("address") and cn["leads"] == 1 and cn["contacts"] == 1 and cn["addresses"] == 1 and cn["orders"] == 2, f"{c} {m5.get('created')} {cn}")  # fmt: skip
	fresh()
	lead = frappe.db.get_value("CRM Lead", m["lead"], ["custom_trial_order_count", "custom_first_intent", "custom_latest_intent"], as_dict=True)
	check("R5 custom_trial_order_count == 2; first_intent kept, latest_intent updated", lead.custom_trial_order_count == 2 and lead.custom_first_intent == "trial_claim" and lead.custom_latest_intent == "info", str(dict(lead)))  # fmt: skip
	c, b = sync.call("POST", CLAIM, json=body("5b", A, address={"house_no": "99", "street": "Other Street", "city": "Chennai", "pincode": "600002"}))
	cn = counts(A)
	check("R5 a different address -> a NEW Address on the same Lead", c == 200 and msg(b).get("created", {}).get("address") and cn["addresses"] == 2 and cn["orders"] == 3 and cn["leads"] == 1, f"{c} {cn}")  # fmt: skip

	print("\n== R6 a Lead owned by another user ==")
	other = frappe.db.get_value("User", {"enabled": 1, "user_type": "System User", "name": ["not in", ["Administrator", "Guest", owner]]}, "name")
	frappe.set_user("Administrator")
	foreign = frappe.get_doc({"doctype": "CRM Lead", "first_name": "Zzclaim Otherowner", "mobile_no": C, "lead_owner": other, "status": status}).insert().name
	frappe.db.commit()
	c, b = sync.call("POST", CLAIM, json=body("6", C))
	m6 = msg(b)
	cn = counts(C)
	if real:
		check("R6 gate user: the other user's Lead is REUSED (created.lead False, same name), one Lead only, Trial Order created", c == 200 and m6.get("lead") == foreign and m6["created"]["lead"] is False and m6["created"]["trial_order"] and cn["leads"] == 1 and cn["orders"] == 1, f"{c} {m6.get('created')} {cn}")  # fmt: skip
	else:
		check("R6 not the gate user: refused (403), nothing created or changed, still one Lead", c == 403 and b.get("exc_type") == "PermissionError" and cn == {"leads": 1, "contacts": 0, "addresses": 0, "orders": 0}, f"{c} {ra.short(b)} {cn}")  # fmt: skip

	print("\n== R7 upsert_lead(with_contact) ==")
	c, b = sync.call("POST", UPSERT, json={"mobile_no": D, "first_name": "Zzclaim Contact", "lead_owner": owner, "status": status, "with_contact": True})
	m7 = msg(b)
	cn = counts(D)
	check("R7 with_contact=True creates Lead and Contact (linked), returns contact + contact_created", c == 200 and m7.get("created") and m7.get("contact") and m7.get("contact_created") and cn["leads"] == 1 and cn["contacts"] == 1, f"{c} {m7} {cn}")  # fmt: skip
	c, b = sync.call("POST", UPSERT, json={"mobile_no": D, "intent": "info", "with_contact": True})
	m7b = msg(b)
	check("R7 again: same Lead and Contact, nothing new", c == 200 and m7b.get("name") == m7.get("name") and m7b.get("contact") == m7.get("contact") and m7b.get("contact_created") is False and counts(D) == cn, f"{c} {m7b}")  # fmt: skip
	c, b = sync.call("POST", UPSERT, json={"mobile_no": E, "first_name": "Zzclaim Plain", "lead_owner": owner, "status": status})
	check("R7 default (with_contact False): output unchanged and NO Contact created", c == 200 and set(msg(b)) == {"name", "created", "updated_fields"} and counts(E)["contacts"] == 0, f"{c} {sorted(msg(b))}")  # fmt: skip

	print("\n== R8 who may call it ==")
	c, b = clean.call("POST", CLAIM, json=body("8", F))
	check("R8 the cleanup identity cannot call record_claim (403), nothing created", c == 403 and counts(F) == ZERO, f"{c}")

	print("\n== R9 erase_subject removes everything record_claim created ==")
	for phone in (A, B, C, D, E, F):
		c, b = clean.call("POST", ERASE, json={"mobile_no": phone})
		if c != 200:
			print("   erase", phone[-2:], c, ra.short(b))
	gone = all(counts(p) == ZERO for p in (A, B, C, D, E, F))
	check("R9 no Lead, Contact, Address or Trial Order left for any of the test numbers", gone, str({p[-2:]: counts(p) for p in (A, B, C, D, E, F)}))
	needles = [p[-10:] for p in PHONES.values()] + ["Zzclaim", TAG]
	left = ra.sweep(needles)
	check("R9 full-database sweep: nothing of the test subjects left anywhere", not left, str(left)[:500])

	if not real:
		for e in (sync_email, clean_email):
			frappe.delete_doc("User", e, force=True, ignore_permissions=True, delete_permanently=True)
		frappe.db.commit()
	print(f"\n{sum(results)}/{len(results)} checks passed")
	sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
	main()
