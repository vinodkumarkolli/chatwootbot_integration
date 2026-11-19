"""Campaign trial item: custom fields, Campaign validation, record_claim without `items`.

Run from the bench `sites` folder (same credential rules as run_acceptance.py):

    ../env/bin/python ../apps/chatwootbot_integration/acceptance/run_trial_item.py development.localhost [base_url]

Needs the Campaign SUJITHA10 configured as a free-trial Campaign (custom_free_trial = 1,
custom_trial_item = "SASB - 1.6 ML - Sample"). Throwaway Campaigns are created and removed; no other
Campaign is changed. Test numbers are erased at the end and the database is swept.
"""

import os
import sys
import uuid

import frappe

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_acceptance as ra  # noqa: E402

fresh = ra.fresh
CLAIM = "/api/method/chatwootbot_integration.api.record_claim"
ERASE = "/api/method/chatwootbot_integration.api.erase_subject"
TRIAL_ITEM = "SASB - 1.6 ML - Sample"
OTHER_ITEM = "SASB-50INR-PACK-20"
DISABLED_ITEM = "SASB-1.6 ML"  # an existing, disabled Item
TAG = uuid.uuid4().hex[:6].upper()
results = []
PHONES = [f"+91990000008{i}" for i in range(9)]


def check(label, ok, detail=""):
	ok = bool(ok)
	results.append(ok)
	print(f"[{'PASS' if ok else 'FAIL'}] {label}  {detail}")


def new_campaign(name, **fields):
	doc = frappe.get_doc({"doctype": "Campaign", "campaign_name": name, **fields}).insert()
	return doc.name


def main():
	frappe.init(site=ra.SITE, sites_path=".")
	frappe.connect()
	frappe.set_user("Administrator")
	real = bool(os.environ.get("SYNC_KEY"))
	if real:
		sync = ra.Client(os.environ["SYNC_KEY"], os.environ["SYNC_SECRET"])
		clean = ra.Client(os.environ["CLEANUP_KEY"], os.environ["CLEANUP_SECRET"])
		owner = os.environ["LEAD_OWNER"]
	else:
		sync_email, clean_email = "zz.ti.sync@example.com", "zz.ti.clean@example.com"
		sync = ra.Client(*ra.make_user(sync_email, "CRM User Custom"))
		clean = ra.Client(*ra.make_user(clean_email, "Cleanup Role"))
		owner = sync_email
	status = os.environ.get("LEAD_STATUS", "New")
	print(f"mode={'real identities' if real else 'throwaway users'}\n")

	def body(bot, phone, campaign="SUJITHA10", **over):
		d = dict(bot_ref=f"TR-{TAG}{bot}", mobile_no=phone, first_name="Zztrial Person", lead_owner=owner, status=status, campaign=campaign, intent="trial_claim", consent_at="2026-11-19 10:00:00", address={"house_no": "7", "street": "Trial Street", "city": "Chennai", "pincode": "600001"})  # fmt: skip
		d.update(over)
		return d

	def order_items(name):
		fresh()
		return [(r.item, r.qty) for r in frappe.get_doc("Trial Order", name).items]

	def counts(phone):
		fresh()
		leads = frappe.get_all("CRM Lead", filters={"mobile_no": phone}, pluck="name")
		return {"leads": len(leads), "orders": frappe.db.count("Trial Order", {"lead": ["in", leads or [""]]})}

	print("== Campaign custom fields and validation ==")
	meta = frappe.get_meta("Campaign")
	ft, ti = meta.get_field("custom_free_trial"), meta.get_field("custom_trial_item")
	check("fields exist: custom_free_trial Check default 0; custom_trial_item Link to Item with the depends_on / mandatory_depends_on / link filter", ft and ft.fieldtype == "Check" and str(ft.default) == "0" and ti and ti.options == "Item" and ti.depends_on == "eval:doc.custom_free_trial" and ti.mandatory_depends_on == "eval:doc.custom_free_trial" and "disabled" in (ti.link_filters or ""), f"{ti.link_filters if ti else ''}")  # fmt: skip
	order = [f.fieldname for f in meta.fields]
	check("placed right after campaign_name", order.index("custom_free_trial") == order.index("campaign_name") + 1 and order.index("custom_trial_item") == order.index("custom_free_trial") + 1, str(order[:5]))  # fmt: skip
	sample = frappe.get_doc("Campaign", "SUJITHA10")
	check("SUJITHA10 is a free-trial Campaign with the Sample item (the Item exists and is enabled)", sample.custom_free_trial == 1 and sample.custom_trial_item == TRIAL_ITEM and frappe.db.get_value("Item", TRIAL_ITEM, "disabled") == 0, f"{sample.custom_free_trial} {sample.custom_trial_item}")  # fmt: skip
	try:
		new_campaign(f"ZZ-NOITEM-{TAG}", custom_free_trial=1)
		check("saving a Campaign with Free Trial set and NO Item fails", False, "saved")
	except frappe.ValidationError as e:
		check("saving a Campaign with Free Trial set and NO Item fails", True, str(e)[:90])
	try:
		new_campaign(f"ZZ-DIS-{TAG}", custom_free_trial=1, custom_trial_item=DISABLED_ITEM)
		check("saving a Campaign with a DISABLED Item fails", False, "saved")
	except frappe.ValidationError as e:
		check("saving a Campaign with a DISABLED Item fails", True, str(e)[:90])
	frappe.db.rollback()
	off = new_campaign(f"ZZ-OFF-{TAG}", custom_free_trial=0, custom_trial_item=TRIAL_ITEM)
	check("with Free Trial unset the Item is cleared on save", frappe.db.get_value("Campaign", off, "custom_trial_item") in (None, ""), "")
	on = new_campaign(f"ZZ-ON-{TAG}", custom_free_trial=1, custom_trial_item=TRIAL_ITEM)
	gone_item = new_campaign(f"ZZ-GONE-{TAG}", custom_free_trial=1, custom_trial_item=TRIAL_ITEM)
	frappe.db.set_value("Campaign", gone_item, "custom_trial_item", DISABLED_ITEM)  # the Item was disabled after the Campaign was saved
	noitem = new_campaign(f"ZZ-HALF-{TAG}", custom_free_trial=0)
	frappe.db.set_value("Campaign", noitem, "custom_free_trial", 1)  # on, but no Item
	frappe.db.commit()

	print("\n== record_claim without items ==")
	A, B, C, D, E, F, G = PHONES[:7]
	c, b = sync.call("POST", CLAIM, json=body("1", A))  # no items key at all
	m = b.get("message") or {}
	check("T1 no items on the trial Campaign: 200, Trial Order built with the Campaign's Item, qty 1", c == 200 and m.get("trial_order") and order_items(m["trial_order"]) == [(TRIAL_ITEM, 1.0)], f"{c} {order_items(m['trial_order']) if m.get('trial_order') else b}")  # fmt: skip
	c, b = sync.call("POST", CLAIM, json=body("1e", B, items=[]))
	m = b.get("message") or {}
	check("T1 an EMPTY items list means the same", c == 200 and m.get("trial_order") and order_items(m["trial_order"]) == [(TRIAL_ITEM, 1.0)], f"{c}")  # fmt: skip
	c, b = sync.call("POST", CLAIM, json=body("2", C, items=[{"item": OTHER_ITEM, "qty": 2}]))
	m = b.get("message") or {}
	check("T2 explicit items WINS over the Campaign's trial Item", c == 200 and m.get("trial_order") and order_items(m["trial_order"]) == [(OTHER_ITEM, 2.0)], f"{c}")  # fmt: skip

	print("\n== the three error codes (417, code first, no phone in the message) ==")
	texts = {}
	for key, camp, phone in (("CAMPAIGN_NOT_FOUND", "ZZ-NOSUCH-CAMPAIGN", D), ("CAMPAIGN_NO_TRIAL", off, D), ("CAMPAIGN_NO_TRIAL (free trial on, no Item)", noitem, D), ("TRIAL_ITEM_DISABLED", gone_item, D)):
		c, b = sync.call("POST", CLAIM, json=body("e", phone, campaign=camp))
		text = b.get("exception", "") or ""
		texts[key] = text.split("ValidationError: ", 1)[-1]
		code = key.split(" ")[0]
		expected_tail = SAMPLE_TAIL = {"CAMPAIGN_NOT_FOUND": camp, "CAMPAIGN_NO_TRIAL": camp, "TRIAL_ITEM_DISABLED": DISABLED_ITEM}[code]
		check(f"{key}: 417 ValidationError, message `{code}: {expected_tail}`, nothing created, no phone number in the response", c == 417 and b.get("exc_type") == "ValidationError" and f"{code}: {expected_tail}" in text and phone[-10:] not in str(b) and counts(phone) == {"leads": 0, "orders": 0}, f"{c} {texts[key][:90]}")  # fmt: skip

	print("\n== idempotency: a replay never re-reads the Campaign ==")
	c, b = sync.call("POST", CLAIM, json=body("r", E, campaign=on))
	first = b.get("message") or {}
	frappe.db.set_value("Campaign", on, "custom_free_trial", 0)  # break the Campaign: a re-read would now raise CAMPAIGN_NO_TRIAL
	frappe.db.commit()
	c2, b2 = sync.call("POST", CLAIM, json=body("r", E, campaign=on))
	again = b2.get("message") or {}
	check("T3 replay of a bot_ref after the Campaign stopped offering the trial: 200, same documents, nothing created", c == 200 and c2 == 200 and all(again.get(k) == first.get(k) for k in ("lead", "contact", "address", "trial_order")) and not any(again.get("created", {}).values()) and counts(E)["orders"] == 1, f"{c2} {again.get('created')}")  # fmt: skip

	print("\n== the caller needs no read access to Campaign ==")
	from chatwootbot_integration.api import _trial_items

	frappe.set_user("automation.cleanup@sravie.in")  # a user that cannot read Campaign
	can_read = frappe.has_permission("Campaign", "read")
	got = _trial_items("SUJITHA10")
	frappe.set_user("Administrator")
	check("a user WITHOUT Campaign read still gets the trial Item (the read ignores permissions)", not can_read and got == [{"item": TRIAL_ITEM, "qty": 1}], f"can_read={can_read} {got}")  # fmt: skip

	print("\n== cleanup ==")
	for phone in PHONES:
		clean.call("POST", ERASE, json={"mobile_no": phone})
	for camp in (off, on, gone_item, noitem):
		frappe.delete_doc("Campaign", camp, force=True, ignore_permissions=True, delete_permanently=True)
	frappe.db.delete("UTM Campaign", {"name": ["like", f"%-{TAG}"]})  # ERPNext makes one per Campaign and leaves it behind
	frappe.db.commit()
	for table, column in (("Version", "data"), ("Comment", "subject"), ("Deleted Document", "data")):
		frappe.db.sql(f"delete from `tab{table}` where `{column}` like %s", "%ZZ-%-" + TAG + "%")
	frappe.db.commit()
	check("every test subject erased", all(counts(p) == {"leads": 0, "orders": 0} for p in PHONES), "")
	left = ra.sweep([p[-10:] for p in PHONES] + ["Zztrial", TAG])
	check("full-database sweep: nothing of the test subjects or throwaway Campaigns left", not left, str(left)[:400])
	check("SUJITHA10 untouched by the tests", frappe.db.get_value("Campaign", "SUJITHA10", "custom_trial_item") == TRIAL_ITEM and frappe.db.get_value("Campaign", "SUJITHA10", "custom_free_trial") == 1, "")  # fmt: skip
	if not real:
		for e in (sync_email, clean_email):
			frappe.delete_doc("User", e, force=True, ignore_permissions=True, delete_permanently=True)
		frappe.db.commit()
	print("\nexact error messages:")
	for k, v in texts.items():
		print(f"   {k}: {v.strip()}")
	print(f"\n{sum(results)}/{len(results)} checks passed")
	sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
	main()
