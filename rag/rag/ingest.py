import json
import os
from uuid import uuid7

import frappe
import requests
from drive.utils import STATUS_ACTIVE, is_site_file

OLLAMA_URL = os.environ.get("RAG_OLLAMA_URL", "http://ollama:11434")
EMBED_MODEL = os.environ.get("RAG_EMBED_MODEL", "nomic-embed-text")
EMBED_DIM = 768  # must match the VECTOR(768) column in kb/doctype/kb_chunk/kb_chunk.py
EMBED_TIMEOUT = (5, 600)  # /api/embed stays silent until it has computed every vector

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


def embed(texts: list[str], prefix: str = "search_document") -> list[list[float]]:
	"""One call for a whole batch. Ollama does not add nomic's task prefix, so we do.

	Indexing uses search_document, querying uses search_query. Mixing them ruins retrieval.
	"""
	# not frappe's make_post_request: it passes no timeout at all, so a hung Ollama would park
	# this worker until RQ's 1500s death penalty fires
	res = requests.post(
		f"{OLLAMA_URL}/api/embed",
		json={
			"model": EMBED_MODEL,
			"input": [f"{prefix}: {t}" for t in texts],
			"truncate": False,  # a silent half-embedding is worse than a loud failure
		},
		timeout=EMBED_TIMEOUT,
	)
	res.raise_for_status()
	vectors = res.json()["embeddings"]
	if len(vectors) != len(texts):
		frappe.throw(f"Ollama returned {len(vectors)} embeddings for {len(texts)} chunks")
	if len(vectors[0]) != EMBED_DIM:
		frappe.throw(f"{EMBED_MODEL} returns {len(vectors[0])}-dim vectors, the column is VECTOR({EMBED_DIM})")
	return vectors


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
	vectors = embed(chunks) if chunks else []
	frappe.db.delete("KB Chunk", {"file": name})
	for seq, (content, vector) in enumerate(zip(chunks, vectors)):
		# Built by hand, not through the ORM: `embedding` is not a DocField, so an ORM insert
		# omits it and a NOT NULL column then rejects the row. One statement also means the
		# vector can never be missing for a row that exists.
		frappe.db.sql(
			"INSERT INTO `tabKB Chunk`"
			" (name, creation, modified, owner, modified_by, file, team, seq, content, embedding)"
			" VALUES (%s, NOW(6), NOW(6), %s, %s, %s, %s, %s, %s, VEC_FromText(%s))",
			(
				str(uuid7()),
				frappe.session.user,
				frappe.session.user,
				name,
				doc.team,
				seq,
				content,
				json.dumps(vector, allow_nan=False),
			),
		)
	return {"file": name, "chunks": len(chunks)}


def reindex_all():
	"""Queue every Active Drive file for indexing.

	bench --site <site> execute rag.ingest.reindex_all

	Frappe caps the queue at MAX_QUEUED_JOBS, so a big Drive stops early and reports
	what is left. Re-run once the long queue drains. On any other failure use
	`bench console`: bench execute hides the traceback behind a bogus NameError.
	"""
	names = frappe.get_all(
		"File",
		filters={"is_folder": 0, "status": STATUS_ACTIVE, "team": ["is", "set"]},
		pluck="name",
	)
	queued = 0
	for i, name in enumerate(names):
		try:
			job = frappe.enqueue(
				"rag.ingest.index_file",
				queue="long",
				deduplicate=True,
				job_id=f"rag-index-{name}",
				name=name,
			)
		except frappe.QueueOverloaded:
			return {"queued": queued, "remaining": len(names) - i}
		queued += job is not None  # None means deduplicate skipped one already in flight
	return {"queued": queued, "remaining": 0}
