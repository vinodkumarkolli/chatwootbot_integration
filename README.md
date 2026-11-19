# chatwootbot_integration

ERPNext / Frappe CRM side of the Swarna WhatsApp chatbot (Chatwoot) integration.
A Windmill workflow writes to it over REST; this app has no Windmill or bot code.

## Install (dev or production bench that already has erpnext, crm, core_customizations)

    bench get-app <repo-url> --branch <branch>      # or: pip install -e apps/chatwootbot_integration
    bench --site <site> install-app chatwootbot_integration
    bench restart                                   # a running server must re-read the app list

Creates: `Trial Order`, `Trial Order Item`, `Conversation Intent` (+ 8 intents), `custom_*` fields on CRM Lead
(Chatbot tab), role `Cleanup Role`, user `automation.cleanup@sravie.in` (no API key), the Campaign
`SUJITHA10` (only if absent), and Cleanup Role permissions. All idempotent (also run as a patch).

API keys: generate them yourself in User > automation.cleanup@sravie.in > API Access > Generate Keys.

## Uninstall

    bench --site <site> uninstall-app chatwootbot_integration

Removes the doctypes and tables, the CRM Lead custom fields **and their columns**, the Cleanup Role permission
rows (restoring stock permissions for doctypes we copied), the role and the user. The Campaign is left in place.

## Acceptance tests (REST)

    cd sites && ../env/bin/python ../apps/chatwootbot_integration/acceptance/run_acceptance.py <site> [base_url]

Uses throwaway mirror users, or the real keys if SYNC_KEY/SYNC_SECRET/CLEANUP_KEY/CLEANUP_SECRET/LEAD_OWNER are set.

## Role swap for automation.crm@sravie.in (NOT done by the app; do it deliberately)

`CRM User Custom` is created and permissioned by the install but assigned to nobody. `Trial Order`
grants read/write/create to `CRM User Custom` and read/write to `Sales Manager`; it grants nothing to
`Sales User`. A site that was installed before that change keeps its old `Sales User` row until the
doctype is reloaded by `bench migrate`, so the ORDER below matters: until step 1 is done the live
user still needs that row, and after step 2 it must be gone.

1. SWAP the role (dev first):

       bench --site <site> console
       >>> u = frappe.get_doc("User", "automation.crm@sravie.in")
       >>> u.add_roles("CRM User Custom"); u.remove_roles("Sales User"); frappe.db.commit()

2. Then apply the Trial Order permissions: `bench --site <site> migrate`
   (or `bench --site <site> reload-doc chatwootbot_integration doctype trial_order`).
3. VERIFY the `Sales User` row is gone and the new ones are present:

       >>> frappe.db.sql("select role,`read`,`write`,`create`,`delete` from tabDocPerm where parent='Trial Order'")

   Expected roles: CRM User Custom (r/w/c), Sales Manager (r/w), Cleanup Role (r/d), System Manager (full) -
   no `Sales User`. Also make sure there is no `Custom DocPerm` row for Trial Order.
4. Smoke-test: Windmill `upsert_lead` + Trial Order create, a Sarvam test call, and
   `acceptance/run_acceptance.py` (SYNC_ROLE unset only if you still want the Sales User comparison).

API keys are unaffected by a role change.

Rollback: `u.add_roles("Sales User"); u.remove_roles("CRM User Custom")`, and restore the Sales User row
in trial_order.json (git revert of the permissions commit) before `bench migrate`.
Re-add `Sales User` BEFORE uninstalling the app (uninstall deletes the role and its assignments).

## erase_subject

`chatwootbot_integration.api.erase_subject(mobile_no, bot_refs=[], dry_run=False, confirm_quotations=False)`
(Cleanup Role only). One transaction; re-running is a no-op. There is no scheduled purge anywhere in this app.

**Always erased:** the subject's Trial Orders, Contact/Address, CRM Lead, and the Deleted Document, Version,
Comment, Activity Log, File, Communication, Integration Request, ToDo, Notification and CRM Notification rows about
them. A Lead is anonymised instead of deleted only if something other than its own initial-status Deals and
Quotations still points at it.

**Legal hold** - a Customer with ANY of Sales Order, Sales Invoice, Delivery Note, POS Invoice, Payment Entry,
Journal Entry (party) or GL Entry (any docstatus) is KEPT, with the Contact/Address those documents point at.
It is minimised: Customer mobile/email, the Contact's phone/mobile/WhatsApp/email rows and the Address's
phone/email/fax are cleared, the Customer is disabled, the Contact unsubscribed, `Customer.custom_erasure_requested_on`
set; the audit trail of held documents is never purged. Response: `held: [...]`, `hold_reason: "sales records"`.
Not touched: the submitted documents' own snapshot of phone/email/address, the ledgers, the names.

**Customer without those records** (and the subject's Deals and Quotations) are erased only when it is safe:
every Quotation draft or cancelled, every Deal still at its initial status (Qualification), Customer Type not
Company. Deals go with their Tasks and Notes. If a Customer cannot be deleted because something else points at it
(e.g. a Timesheet) it is anonymised and renamed `Erased-` + 12 random hex characters (48 bits from `secrets`, no input, not derived from any personal value), and the blockers are listed.

**needs_review** - if any Quotation is submitted, any Deal is past its initial status, or the Customer is a
Company, nothing of that kind is touched; only the safe part runs (Trial Orders, Contact/Address, Lead,
held-side minimisation) and `needs_review: [{doctype, name, reason}]` says why. `confirm_quotations=True` clears
ONLY the "submitted Quotation" reason (they are cancelled, then deleted); later-stage Deals and Company
customers always stay for a human.

**dry_run=True** changes nothing and returns `{would_delete, would_anonymise, would_cancel, held, needs_review,
retained, would_purge}`. It runs the same code path as a real run, recording instead of acting, so the plan
cannot drift. (`would_purge` counts rows existing now; the deletions themselves add Deleted Document/Version rows
that a real run also purges.)

## Tests

    cd sites
    ../env/bin/python ../apps/chatwootbot_integration/acceptance/run_acceptance.py <site>            # REST, SYNC_ROLE="CRM User Custom" for the post-swap world
    ../env/bin/python ../apps/chatwootbot_integration/acceptance/run_erasure_scenarios.py <site>     # erasure scenarios, dry_run, hold catch-all
    ../env/bin/python ../apps/chatwootbot_integration/acceptance/run_real_user_inprocess.py <site>   # gate-user bypass as the real automation.crm

Do not uninstall/reinstall the app on a site whose `automation.cleanup@sravie.in` already has API keys: uninstall
deletes that user (and its keys).
