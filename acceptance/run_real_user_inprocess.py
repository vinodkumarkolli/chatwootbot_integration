"""upsert_lead bypass, tested as the REAL gate user (automation.crm@sravie.in), in-process.

Over REST this needs that user's secret (which this repo never holds), so the call is made
in-process with frappe.set_user. Run from the bench `sites` folder:

    ../env/bin/python ../apps/chatwootbot_integration/acceptance/run_real_user_inprocess.py development.localhost

Checks: (1) the gate user finds, without duplicating, a Lead owned by another user, which a plain
query cannot see; (2) a non-gate user holding CRM User Custom does NOT get that bypass.
"""

import sys

import frappe

SITE = sys.argv[1] if len(sys.argv) > 1 else "development.localhost"
PHONE = "+919900000008"
results = []


def check(label, ok, detail=""):
	results.append(bool(ok))
	print(f"[{'PASS' if ok else 'FAIL'}] {label}  {detail}")


def main():
	frappe.init(site=SITE, sites_path=".")
	frappe.connect()
	from chatwootbot_integration.api import erase_subject, upsert_lead
	from core_customizations.core_customizations.caller_gate import get_automation_user

	gate = get_automation_user()
	print(f"gate user = {gate}")
	frappe.set_user("Administrator")
	other = frappe.db.get_value("User", {"enabled": 1, "user_type": "System User", "name": ["not in", ["Administrator", "Guest", gate]]}, "name")
	lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "Zzreal Otherowner", "mobile_no": PHONE, "lead_owner": other, "status": "New"}).insert().name  # fmt: skip
	tmp = "zz.acc.crmcustom@example.com"
	if not frappe.db.exists("User", tmp):
		u = frappe.get_doc({"doctype": "User", "email": tmp, "first_name": "AcceptanceTmp", "enabled": 1, "send_welcome_email": 0, "user_type": "System User", "roles": [{"role": "CRM User Custom"}]})  # fmt: skip
		u.flags.no_welcome_mail = True
		u.insert(ignore_permissions=True)
	frappe.db.commit()
	try:
		frappe.set_user(gate)
		check("plain get_list as the gate user cannot see the other user's Lead", not frappe.get_list("CRM Lead", filters={"mobile_no": PHONE}, limit=1))
		r = upsert_lead(PHONE, first_name="Zzreal Otherowner", lead_owner=gate, status="New", intent="info")
		check("gate user: upsert_lead finds it (same name, created=False), no duplicate", r["name"] == lead and r["created"] is False and frappe.db.count("CRM Lead", {"mobile_no": PHONE}) == 1, str(r))  # fmt: skip
		frappe.set_user(tmp)
		try:
			upsert_lead(PHONE, first_name="x", lead_owner=tmp, status="New", intent="other")
			check("CRM User Custom holder (not the gate user): refused", False, "no error raised")
		except frappe.PermissionError:
			check("CRM User Custom holder (not the gate user): refused with PermissionError", True)
		check("...and the Lead was not changed", frappe.db.get_value("CRM Lead", lead, "custom_latest_intent") == "info", "")
	finally:
		frappe.set_user("Administrator")
		erase_subject(PHONE)
		frappe.delete_doc("User", tmp, force=True, ignore_permissions=True, delete_permanently=True)
		frappe.db.commit()
	print(f"\n{sum(results)}/{len(results)} checks passed")
	sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
	main()
