"""Erasure scenarios: Customers without sales records, Deals/Quotations, legal-hold catch-all, dry_run.

Run from the bench `sites` folder (same credentials rules as run_acceptance.py):

    ../env/bin/python ../apps/chatwootbot_integration/acceptance/run_erasure_scenarios.py development.localhost [base_url]

Fixtures are created in-process as Administrator (some are inserted at DB level, as submitted, because
the site's own rules block a normal insert and this creates no ledger entries); `erase_subject` is
called over REST as a throwaway user holding ONLY Cleanup Role. Everything is removed at the end.
"""

import json
import os
import sys

import frappe

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_acceptance as ra  # noqa: E402  (reuses Client, make_user, sweep, fresh)

fresh = ra.fresh

ERASE = "/api/method/chatwootbot_integration.api.erase_subject"
COMPANY = "Sravi Enterprises - Kolapakkam"
ITEM = "SASB-50INR-PACK-20"
results = []
made = {"docs": [], "rows": []}  # fixtures to tear down if the run left them behind


def check(label, ok, detail=""):
	ok = bool(ok)
	results.append(ok)
	print(f"[{'PASS' if ok else 'FAIL'}] {label}  {detail}")


def pairs(items, key="doctype"):
	return {(d[key], d["name"]) for d in (items or [])}


# ------------------------------------------------------------------ fixtures


def lead(phone, name):
	doc = frappe.get_doc({"doctype": "CRM Lead", "first_name": name, "mobile_no": phone, "lead_owner": "Administrator", "status": "New"}).insert()  # fmt: skip
	return doc.name


def customer(name, phone, ctype="Individual"):
	doc = frappe.get_doc({"doctype": "Customer", "customer_name": name, "customer_type": ctype, "mobile_no": phone, "customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"), "territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"), "default_price_list": frappe.db.get_value("Price List", {"selling": 1, "enabled": 1}, "name")}).insert()  # fmt: skip
	return doc.name


def deal(lead_name, status="Qualification"):
	return frappe.get_doc({"doctype": "CRM Deal", "lead": lead_name, "status": status}).insert().name


def quotation(name, party_type, party, docstatus):
	doc = frappe.get_doc({"doctype": "Quotation", "name": name, "quotation_to": party_type, "party_name": party, "customer_name": party, "company": COMPANY, "transaction_date": frappe.utils.today(), "currency": "INR", "docstatus": docstatus, "items": [{"doctype": "Quotation Item", "item_code": ITEM, "qty": 1, "rate": 10, "parent": name, "parenttype": "Quotation", "parentfield": "items", "idx": 1}]})  # fmt: skip
	doc.db_insert()
	for row in doc.items:
		row.db_insert()
	made["rows"].append(("Quotation", name))
	return name


def exists(doctype, name):
	fresh()
	return bool(frappe.db.exists(doctype, name))


# ------------------------------------------------------------------ main


def main():
	frappe.init(site=ra.SITE, sites_path=".")
	frappe.connect()
	frappe.set_user("Administrator")
	email = "zz.era.clean@example.com"
	clean = ra.Client(*ra.make_user(email, "Cleanup Role"))

	def erase(phone, **kw):
		c, b = clean.call("POST", ERASE, json={"mobile_no": phone, **kw})
		return c, (b.get("message") or {}), b

	print("== S1 Customer with only a DRAFT Quotation -> erased ==")
	P = "+919900000031"
	l1, c1 = lead(P, "Zzera Draftonly"), customer("Zzera Draftonly Customer", P)
	quotation("ZZ-Q-S1", "Customer", c1, 0)
	frappe.db.commit()
	code, m, b = erase(P)
	d = pairs(m.get("deleted"))
	check("S1 200; Lead, Customer and the draft Quotation deleted; no needs_review", code == 200 and {("CRM Lead", l1), ("Customer", c1), ("Quotation", "ZZ-Q-S1")} <= d and "needs_review" not in m, f"{code} {str(sorted(d))[:300]} {m.get('needs_review')}")  # fmt: skip
	check("S1 nothing of them left", not any(exists(*x) for x in (("CRM Lead", l1), ("Customer", c1), ("Quotation", "ZZ-Q-S1"))), "")

	print("\n== S2 Customer with a SUBMITTED Quotation -> needs_review, only the safe part ==")
	P = "+919900000032"
	l2, c2 = lead(P, "Zzera Submitted"), customer("Zzera Submitted Customer", P)
	to2 = frappe.get_doc({"doctype": "Trial Order", "lead": l2, "campaign": "SUJITHA10", "bot_ref": "TR-ZZERA02", "house_no": "1", "street": "Zzera St", "city": "Chennai", "pincode": "600001", "terms_accepted_at": "2026-11-19 10:00:00", "items": [{"item": ITEM, "qty": 1}]}).insert().name  # fmt: skip
	quotation("ZZ-Q-S2", "Customer", c2, 1)
	frappe.db.commit()
	code, dry, b = erase(P, dry_run=True)
	check("S2 dry_run: needs_review lists the submitted Quotation (with clear_with); Trial Order and Lead planned", code == 200 and any(r["name"] == "ZZ-Q-S2" and r.get("clear_with") for r in dry.get("needs_review", [])) and ("Trial Order", to2) in pairs(dry.get("would_delete")) and ("Customer", c2) not in pairs(dry.get("would_delete")), str(dry)[:350])  # fmt: skip
	check("S2 dry_run changed nothing", all(exists(*x) for x in (("CRM Lead", l2), ("Trial Order", to2), ("Customer", c2), ("Quotation", "ZZ-Q-S2"))), "")  # fmt: skip
	code, m, b = erase(P)
	check("S2 real run: needs_review, Trial Order and Lead erased, Customer and Quotation untouched", code == 200 and m.get("needs_review") and ("Trial Order", to2) in pairs(m.get("deleted")) and ("CRM Lead", l2) in pairs(m.get("deleted")) and exists("Customer", c2) and exists("Quotation", "ZZ-Q-S2") and frappe.db.get_value("Quotation", "ZZ-Q-S2", "docstatus") == 1, f"{code} {str(m.get('needs_review'))[:200]}")  # fmt: skip
	code, m, b = erase(P, confirm_quotations=True)
	check("S2 with confirm_quotations=True: the Quotation is cancelled then deleted, the Customer erased", code == 200 and ("Quotation", "ZZ-Q-S2") in pairs(m.get("cancelled")) and ("Customer", c2) in pairs(m.get("deleted")) and not exists("Quotation", "ZZ-Q-S2") and not exists("Customer", c2), f"{code} {str(m)[:300]}")  # fmt: skip

	print("\n== S3 held: POS Invoice only / Journal Entry only / GL Entry only ==")
	for i, kind in enumerate(("POS Invoice", "Journal Entry", "GL Entry"), start=1):
		P = f"+91990000004{i}"
		lh, ch = lead(P, f"Zzera Held{i}"), customer(f"Zzera Held{i} Customer", P)
		ref = f"ZZ-HELD-{i}"
		if kind == "POS Invoice":
			frappe.get_doc({"doctype": "POS Invoice", "name": ref, "customer": ch, "company": COMPANY, "docstatus": 1, "posting_date": frappe.utils.today()}).db_insert()  # fmt: skip
		elif kind == "Journal Entry":
			frappe.get_doc({"doctype": "Journal Entry", "name": ref, "company": COMPANY, "docstatus": 1, "posting_date": frappe.utils.today()}).db_insert()  # fmt: skip
			frappe.get_doc({"doctype": "Journal Entry Account", "name": ref + "-A", "parent": ref, "parenttype": "Journal Entry", "parentfield": "accounts", "idx": 1, "party_type": "Customer", "party": ch}).db_insert()  # fmt: skip
		else:
			frappe.get_doc({"doctype": "GL Entry", "name": ref, "party_type": "Customer", "party": ch, "company": COMPANY, "posting_date": frappe.utils.today(), "docstatus": 1}).db_insert()  # fmt: skip
		made["rows"].append((kind, ref))
		frappe.db.commit()
		code, dry, b = erase(P, dry_run=True)
		fresh_cust = frappe.db.get_value("Customer", ch, ["mobile_no", "disabled"], as_dict=True)
		check(f"S3 {kind}: dry_run reports it held and changes nothing", code == 200 and ("Customer", ch) in pairs(dry.get("held")) and fresh_cust.mobile_no == P and not fresh_cust.disabled and exists("CRM Lead", lh), f"{code} {str(dry.get('held'))[:200]}")  # fmt: skip
		code, m, b = erase(P)
		fresh()
		cu = frappe.db.get_value("Customer", ch, ["mobile_no", "disabled", "custom_erasure_requested_on"], as_dict=True)
		check(f"S3 {kind}: Customer HELD (kept, minimised, disabled, stamped), Lead deleted, hold_reason", code == 200 and m.get("hold_reason") == "sales records" and cu and not cu.mobile_no and cu.disabled == 1 and cu.custom_erasure_requested_on and ("CRM Lead", lh) in pairs(m.get("deleted")) and ("Customer", ch) not in pairs(m.get("deleted")), f"{code} held={m.get('held')} {dict(cu or {})}")  # fmt: skip

	print("\n== S4 Lead blocked only by its OWN draft Quotation and initial-status Deal -> deleted; dry_run changes nothing ==")
	P = "+919900000051"
	l4 = lead(P, "Zzera Ownstuff")
	d4 = deal(l4)
	quotation("ZZ-Q-S4", "CRM Deal", d4, 0)
	comm = frappe.get_doc({"doctype": "Communication", "communication_type": "Communication", "communication_medium": "Phone", "subject": "Zzera call", "content": f"called {P}", "reference_doctype": "CRM Lead", "reference_name": l4, "sent_or_received": "Received", "sender": "zz@example.com"}).insert(ignore_permissions=True).name  # fmt: skip
	ir = frappe.get_doc({"doctype": "Integration Request", "integration_request_service": "Zzera", "reference_doctype": "CRM Lead", "reference_docname": l4, "data": json.dumps({"phone": P}), "status": "Queued"}).insert(ignore_permissions=True).name  # fmt: skip
	frappe.db.commit()
	before = {x: exists(*x) for x in (("CRM Lead", l4), ("CRM Deal", d4), ("Quotation", "ZZ-Q-S4"), ("Communication", comm), ("Integration Request", ir))}
	code, dry, b = erase(P, dry_run=True)
	planned = pairs(dry.get("would_delete"))
	after = {x: exists(*x) for x in before}
	check("S4 dry_run returns would_delete/would_anonymise/held/needs_review and plans Lead, Deal and Quotation", code == 200 and all(k in dry for k in ("would_delete", "would_anonymise", "held", "needs_review")) and {("CRM Lead", l4), ("CRM Deal", d4), ("Quotation", "ZZ-Q-S4")} <= planned and not dry["would_anonymise"], f"{code} {str(sorted(planned))[:300]}")  # fmt: skip
	check("S4 dry_run changed nothing (all five fixtures still there)", before == after and all(after.values()), str(after))
	code, m, b = erase(P)
	did = pairs(m.get("deleted"))
	check("S4 real run: Lead DELETED (not anonymised) with its Deal and Quotation; the dry-run plan was exactly what ran", code == 200 and {("CRM Lead", l4), ("CRM Deal", d4), ("Quotation", "ZZ-Q-S4")} <= did and not m.get("anonymised") and planned <= did, f"{code} anonymised={m.get('anonymised')}")  # fmt: skip
	check("S4 Communication and Integration Request about the subject purged", not exists("Communication", comm) and not exists("Integration Request", ir) and m.get("purged", {}).get("Communication") and m.get("purged", {}).get("Integration Request"), str(m.get("purged")))  # fmt: skip

	print("\n== S5 Lead with a Deal PAST its initial status -> needs_review, Lead anonymised, Deal kept ==")
	P = "+919900000052"
	l5 = lead(P, "Zzera Latedeal")
	d5 = deal(l5, status=frappe.get_all("CRM Deal Status", filters={"type": "Ongoing"}, order_by="position", pluck="name")[0])
	quotation("ZZ-Q-S5", "CRM Deal", d5, 0)
	frappe.db.commit()
	code, m, b = erase(P, confirm_quotations=True)  # confirm must NOT override a later-stage Deal
	check("S5 needs_review names the Deal; Deal and Quotation kept; Lead anonymised", code == 200 and any(r["doctype"] == "CRM Deal" for r in m.get("needs_review", [])) and exists("CRM Deal", d5) and exists("Quotation", "ZZ-Q-S5") and m.get("anonymised"), f"{code} {str(m.get('needs_review'))[:200]}")  # fmt: skip

	print("\n== S6 Customer Type = Company -> needs_review, Customer kept ==")
	P = "+919900000053"
	c6 = customer("Zzera Company Customer", P, ctype="Company")
	frappe.db.commit()
	code, m, b = erase(P)
	check("S6 needs_review names the Company Customer; Customer kept", code == 200 and any(r["doctype"] == "Customer" and r["name"] == c6 for r in m.get("needs_review", [])) and exists("Customer", c6), f"{code} {str(m.get('needs_review'))[:200]}")  # fmt: skip

	print("\n== S7 Customer that cannot be deleted (a Timesheet points at it) -> anonymised and renamed ==")
	P = "+919900000054"
	c7 = customer("Zzera Blocked Customer", P)
	frappe.get_doc({"doctype": "Timesheet", "name": "ZZ-TS-1", "customer": c7, "docstatus": 0}).db_insert()
	made["rows"].append(("Timesheet", "ZZ-TS-1"))
	frappe.db.commit()
	code, m, b = erase(P)
	an = [a for a in m.get("anonymised", []) if a["doctype"] == "Customer"]
	new = an[0].get("renamed_to") if an else None
	fresh()
	check("S7 Customer anonymised (blockers listed), renamed away from the person's name", code == 200 and an and new and "Timesheet ZZ-TS-1" in an[0]["referenced_by"] and not frappe.db.exists("Customer", c7) and frappe.db.exists("Customer", new), f"{code} {an}")  # fmt: skip
	if new:
		cu = frappe.db.get_value("Customer", new, ["customer_name", "mobile_no", "email_id", "disabled"], as_dict=True)
		check("S7 anonymised Customer: name 'Erased-...', mobile/email cleared, disabled; the Timesheet now points at the new name", cu.customer_name.startswith("Erased") and not cu.mobile_no and not cu.email_id and cu.disabled == 1 and frappe.db.get_value("Timesheet", "ZZ-TS-1", "customer") == new, str(dict(cu)))  # fmt: skip

	print("\n== teardown of what the API (rightly) refused to erase, then the leftover sweep ==")
	teardown(clean, [l5, c6])
	for e in (email,):
		frappe.delete_doc("User", e, force=True, ignore_permissions=True, delete_permanently=True)
	frappe.db.commit()
	needles = ["990000003", "99000000041", "99000000042", "99000000043", "99000000051", "99000000052", "99000000053", "99000000054", "Zzera", "ZZ-HELD", "ZZ-Q-S", "TR-ZZERA"]
	left = ra.sweep(needles)
	check("full-database sweep: nothing of the test subjects left (incl. Communication, Integration Request, Deleted Document, Version)", not left, str(left)[:500])
	print(f"\n{sum(results)}/{len(results)} checks passed")
	sys.exit(0 if all(results) else 1)


def teardown(clean, leads):
	"""Remove fixtures the API refused to erase, plus held fixtures, then scrub the audit rows that
	hand-deleting them wrote (nothing else would purge those)."""
	frappe.set_user("Administrator")
	fresh()
	for name in frappe.get_all("CRM Deal", filters={"lead": ["in", leads]}, pluck="name"):
		frappe.db.delete("Quotation Item", {"parent": ["in", frappe.get_all("Quotation", filters={"party_name": name}, pluck="name")]})
		frappe.db.delete("Quotation", {"party_name": name})
		frappe.delete_doc("CRM Deal", name, force=True, ignore_permissions=True, delete_permanently=True)
	for dt, name in made["rows"]:
		child = {"Quotation": "Quotation Item", "Journal Entry": "Journal Entry Account"}.get(dt)
		if child:
			frappe.db.delete(child, {"parent": name})
		frappe.db.delete(dt, {"name": name})
	for cust in frappe.get_all("Customer", filters={"customer_name": ["like", "Zzera%"]}, pluck="name") + frappe.get_all("Customer", filters={"customer_name": "Erased"}, pluck="name"):
		frappe.delete_doc("Customer", cust, force=True, ignore_permissions=True, delete_permanently=True)
	for lead_name in leads:
		if frappe.db.exists("CRM Lead", lead_name):
			frappe.delete_doc("CRM Lead", lead_name, force=True, ignore_permissions=True, delete_permanently=True)
	for dt in ("Contact", "Address"):
		for name in frappe.get_all(dt, filters={"name": ["like", "Zzera%"]}, pluck="name"):
			frappe.delete_doc(dt, name, force=True, ignore_permissions=True, delete_permanently=True)
	frappe.db.commit()
	for table, column in (("Version", "data"), ("Comment", "subject"), ("Comment", "content"), ("Deleted Document", "data"), ("Activity Log", "subject"), ("Communication", "content"), ("Integration Request", "data")):
		for needle in ("%Zzera%", "%ZZ-Q-S%", "%ZZ-HELD%", "%TR-ZZERA%"):
			frappe.db.sql(f"delete from `tab{table}` where `{column}` like %s", needle)
	frappe.db.commit()


if __name__ == "__main__":
	main()
