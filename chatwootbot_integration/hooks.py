app_name = "chatwootbot_integration"
app_title = "Chatwootbot Integration"
app_publisher = "Vinod Kumar K"
app_description = "ERPNext side of the Swarna WhatsApp chatbot integration: Trial Orders on Frappe CRM"
app_email = "vinodkumarkolli@gmail.com"
app_license = "bsd-3-clause"

# core_customizations supplies the caller-phone helpers and the one-lead-per-phone guard (BR-CRM-001).
required_apps = ["erpnext", "crm", "core_customizations"]

before_install = "chatwootbot_integration.setup.before_install"
after_install = "chatwootbot_integration.setup.after_install"
before_uninstall = "chatwootbot_integration.setup.before_uninstall"

doc_events = {
	"CRM Lead": {"validate": "chatwootbot_integration.lead_events.keep_first_values"},
	"Campaign": {"validate": "chatwootbot_integration.campaign_events.validate_free_trial"},
}
