import frappe
from drive.utils import is_site_file

WATCHED = ("file_url", "file_name", "folder", "status", "team", "file_size")


def on_file_update(doc, method=None):
	"""Queue a Drive file for (re)indexing when its content or placement changes."""
	if doc.is_folder or is_site_file(doc):
		return
	if doc.get_doc_before_save() and not any(doc.has_value_changed(f) for f in WATCHED):
		return
	# dotted string, not the callable: RQ unpickles a callable before frappe.init(),
	# and importing drive.utils that early raises "object is not bound"
	frappe.enqueue(
		"rag.ingest.index_file",
		queue="long",
		enqueue_after_commit=True,
		deduplicate=True,
		job_id=f"rag-index-{doc.name}",
		name=doc.name,
	)


def index_file(name: str):
	frappe.logger("rag").info(f"index {name}")
