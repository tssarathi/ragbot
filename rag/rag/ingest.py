import frappe
from drive.utils import STATUS_ACTIVE, is_site_file

WATCHED = ("status", "team")
CHUNK_CHARS = 2000  # ~580 tokens of nomic-embed-text's 2048, sized for retrieval not for the window


def on_file_update(doc, method=None):
	"""Queue a Drive file for (re)indexing when its content, status or team changes."""
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


def on_file_trash(doc, method=None):
	# Drive soft-deletes to status=Removed, so this only fires for a framework-level
	# frappe.delete_doc, which would otherwise hit LinkExistsError on KB Chunk.file.
	frappe.db.delete("KB Chunk", {"file": doc.name})


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
			if pdf.needs_pass:
				return ""
			# "blocks" keeps paragraph boundaries; plain get_text() returns a whole page as
			# one run-on paragraph that chunk() would cut mid-sentence. sort=True orders by
			# (y1, x0): without it blocks arrive in content-stream order, so a PDF that writes
			# its footer first indexes the footer first.
			return "\n\n".join(
				b[4].strip()
				for page in pdf
				for b in page.get_text("blocks", sort=True)
				if b[6] == 0
			)
	except pymupdf.FileDataError:
		return ""


def chunk(text: str) -> list[str]:
	"""Whole paragraphs packed up to CHUNK_CHARS, splitting any paragraph too long to fit."""
	text = "\n".join(text.splitlines())  # a Windows upload has no "\n\n" at all
	paras = []
	for para in text.split("\n\n"):
		para = para.strip()
		while len(para) > CHUNK_CHARS:
			cut = para.rfind(" ", 0, CHUNK_CHARS) + 1 or CHUNK_CHARS
			paras.append(para[:cut].strip())
			para = para[cut:].strip()
		if para:
			paras.append(para)

	out, buf, size = [], [], 0
	for para in paras:
		if buf and size + len(para) > CHUNK_CHARS:
			out.append("\n\n".join(buf))
			buf, size = [], 0
		buf.append(para)
		size += len(para) + 2
	if buf:
		out.append("\n\n".join(buf))
	return out


def index_file(name: str):
	doc = frappe.get_doc("File", name)
	if doc.is_folder or is_site_file(doc):
		return
	if doc.status != STATUS_ACTIVE or doc._not_in_disk():
		frappe.db.delete("KB Chunk", {"file": name})
		return {"file": name, "chunks": 0}
	# Read before deleting. Drive maps every read failure onto DoesNotExistError, so catching it
	# here would commit an empty index on an S3 503 or an EIO. Let it raise and the job rolls back.
	chunks = chunk(extract_text(doc))
	frappe.db.delete("KB Chunk", {"file": name})
	for seq, content in enumerate(chunks):
		frappe.get_doc(
			{"doctype": "KB Chunk", "file": name, "team": doc.team, "seq": seq, "content": content}
		).insert(ignore_permissions=True)
	return {"file": name, "chunks": len(chunks)}
