import frappe
from drive.utils import STATUS_ACTIVE, is_site_file

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


def extract_text(doc) -> str:
	"""Plain text for a Drive file, or "" for a format we cannot read yet."""
	mime = doc.mime_type or ""
	if mime != "application/pdf" and not mime.startswith("text/"):
		return ""
	data = doc.manager.get_file(doc).read()
	if mime.startswith("text/"):
		if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
			return data.decode("utf-16", "replace")
		try:
			return data.decode("utf-8-sig")
		except UnicodeDecodeError:
			return data.decode("cp1252", "replace")
	import pymupdf  # 39MB RSS, keep it out of the web workers

	try:
		with pymupdf.open(stream=data, filetype="pdf") as pdf:
			return "" if pdf.needs_pass else "\n\n".join(page.get_text() for page in pdf)
	except pymupdf.FileDataError:  # mime_type comes from the extension, so this may not be a pdf
		return ""


def index_file(name: str):
	doc = frappe.get_doc("File", name)
	# ponytail: trashed files just skip; once the KB table lands this has to delete their rows
	if doc.is_folder or is_site_file(doc) or doc.status != STATUS_ACTIVE or doc._not_in_disk():
		return
	return {"file": name, "team": doc.team, "mime_type": doc.mime_type, "chars": len(extract_text(doc))}
