from chatwootbot_integration.setup import create_app_custom_fields


def execute():
	"""Custom fields on Campaign (idempotent; a fresh install gets them from after_install)."""
	create_app_custom_fields()
