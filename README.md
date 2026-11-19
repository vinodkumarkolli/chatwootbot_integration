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

`CRM User Custom` is created and permissioned by the install but assigned to nobody.

Swap (dev first):

    bench --site <site> console
    >>> u = frappe.get_doc("User", "automation.crm@sravie.in")
    >>> u.add_roles("CRM User Custom"); u.remove_roles("Sales User"); frappe.db.commit()

Then smoke-test: Windmill `upsert_lead` + Trial Order create, a Sarvam test call, and
`acceptance/run_acceptance.py` with the real keys. API keys are unaffected by a role change.
Follow-up once swapped: remove the `Sales User` row from Trial Order's permissions (trial_order.json)
so ordinary sales staff stop seeing Trial Orders.

Rollback:

    >>> u.add_roles("Sales User"); u.remove_roles("CRM User Custom"); frappe.db.commit()

Re-add `Sales User` BEFORE uninstalling the app (uninstall deletes the role and its assignments).
