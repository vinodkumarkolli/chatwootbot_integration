import frappe

ONCE_ONLY = ("custom_first_intent", "custom_first_conversation_id")


def keep_first_values(doc, method=None):
	"""CRM Lead.validate: first_intent / first_conversation_id are set once and
	never overwritten by later syncs. A later value is silently put back (so an
	idempotent sync that resends the whole payload still succeeds); an empty
	value may still be filled in once."""
	if doc.is_new():
		return
	before = doc.get_doc_before_save()
	if not before:
		return
	for field in ONCE_ONLY:
		original = before.get(field)
		if original and doc.get(field) != original:
			doc.set(field, original)
