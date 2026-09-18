import json
import os
from itertools import batched
from uuid import uuid7

import frappe
import requests
from drive.utils import STATUS_ACTIVE, is_site_file

OLLAMA_URL = os.environ.get("RAG_OLLAMA_URL", "http://ollama:11434")
EMBED_MODEL = os.environ.get("RAG_EMBED_MODEL", "nomic-embed-text")
EMBED_DIM = 768  # must match the VECTOR(768) column in kb/doctype/kb_chunk/kb_chunk.py
EMBED_TIMEOUT = (5, 600)  # /api/embed stays silent until it has computed every vector
EMBED_BATCH = 64  # ~0.45s per chunk, so a whole book in one request would blow that timeout

WATCHED = ("status", "team")
CHUNK_CHARS = 2000  # ~580 tokens of nomic-embed-text's 2048, sized for retrieval not for the window


def on_file_update(doc, method=None):
	"""Queue a Drive file for (re)indexing when its content, status or team changes."""
	if doc.is_folder or is_site_file(doc):
		# a file that just left Drive would keep chunks nobody can ever match
		if doc.get_doc_before_save() and doc.has_value_changed("team"):
			frappe.db.delete("KB Chunk", {"file": doc.name})
		return
	if doc.get_doc_before_save() and not any(doc.has_value_changed(f) for f in WATCHED):
		return
	# No deduplicate: it also skips while an earlier job is STARTED, and that job read the old
	# bytes, so trash-then-restore loses the restore. index_file is idempotent instead.
	# Dotted string, not the callable: RQ unpickles before frappe.init(), and importing
	# drive.utils that early raises "object is not bound".
	try:
		frappe.enqueue(
			"rag.ingest.index_file",
			queue="long",
			enqueue_after_commit=True,
			job_id=f"rag-index-{doc.name}",
			name=doc.name,
		)
	except frappe.QueueOverloaded:
		# frappe checks the queue depth inside enqueue, before the after-commit deferral, so
		# letting this out fails the upload itself. Name the file instead: reindex_all catches up.
		frappe.log_error("rag: long queue full, file left unindexed", doc.name)


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
		# UTF-32 LE opens with the UTF-16 LE BOM, so it has to be tested first
		if data[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):
			return data.decode("utf-32", "replace")
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
			# "blocks" keeps paragraph boundaries, sort=True puts them in visual order: without
			# it a PDF that draws its footer first gets indexed that way.
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
	"""Embed every text, in batches. Ollama does not add nomic's task prefix, so we do.

	Indexing uses search_document, querying uses search_query. Mixing them ruins retrieval.
	"""
	vectors = []
	for batch in batched(texts, EMBED_BATCH, strict=False):  # a short last batch is the point
		vectors += _embed_batch(list(batch), prefix)
	return vectors


def _embed_batch(texts: list[str], prefix: str) -> list[list[float]]:
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
	if not frappe.db.exists("File", name):
		# hard-deleted between the enqueue and this job. on_file_trash already took the chunks.
		return {"file": name, "chunks": 0}
	doc = frappe.get_doc("File", name)
	if doc.is_folder or is_site_file(doc) or doc.status != STATUS_ACTIVE or doc._not_in_disk():
		frappe.db.delete("KB Chunk", {"file": name})
		return {"file": name, "chunks": 0}
	# Read before deleting, and do not catch: Drive maps every read failure onto
	# DoesNotExistError, so an S3 blip would otherwise commit an empty index.
	chunks = chunk(extract_text(doc))
	try:
		vectors = embed(chunks)
	except requests.RequestException as exc:
		if exc.response is not None and exc.response.status_code < 500:
			# Ollama rejected the request, so five more attempts reject it five times, each one
			# re-reading the blob and re-extracting the text. Fail now and log it once.
			raise
		# an Ollama restart should not leave the file unindexed: execute_job re-runs this up to
		# five times with backoff. Retrying here as well would multiply the attempts.
		raise frappe.RetryBackgroundJobError(f"Ollama unreachable: {exc}") from exc
	if not frappe.db.exists("File", name):
		# embedding a large file holds this job for minutes; a hard delete in that window would
		# otherwise leave rows pointing at nothing, which nothing ever cleans up
		return {"file": name, "chunks": 0}
	frappe.db.delete("KB Chunk", {"file": name})
	for seq, (content, vector) in enumerate(zip(chunks, vectors, strict=True)):
		# Raw SQL because `embedding` is not a DocField: an ORM insert would omit it.
		frappe.db.sql(
			"INSERT INTO `tabKB Chunk`"
			" (name, creation, modified, owner, modified_by, file, team, seq, content, model, embedding)"
			" VALUES (%s, NOW(6), NOW(6), %s, %s, %s, %s, %s, %s, %s, VEC_FromText(%s))",
			(
				str(uuid7()),
				frappe.session.user,
				frappe.session.user,
				name,
				doc.team,
				seq,
				content,
				EMBED_MODEL,
				json.dumps(vector, allow_nan=False),
			),
		)
	return {"file": name, "chunks": len(chunks)}


def health():
	"""Everything that would make search quietly wrong, in one place.

	bench --site <site> execute rag.ingest.health
	"""
	zero = "[" + ",".join(["0"] * EMBED_DIM) + "]"
	stats = frappe.db.sql(
		"""
		SELECT COUNT(*) AS chunks,
		       COALESCE(SUM(embedding IS NULL), 0) AS unembedded,
		       COALESCE(SUM(embedding = VEC_FromText(%(zero)s)), 0) AS zeroed,
		       COALESCE(SUM(model IS NULL OR model != %(model)s), 0) AS other_model
		FROM `tabKB Chunk`
		""",
		{"zero": zero, "model": EMBED_MODEL},
		as_dict=True,
	)[0]
	stats = {k: int(v) for k, v in stats.items()}
	stats["indexed"] = bool(
		frappe.db.sql(
			"SELECT 1 FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE()"
			" AND TABLE_NAME = 'tabKB Chunk' AND INDEX_NAME = 'embedding'"
		)
	)
	problems = []
	if stats["unembedded"]:
		problems.append(
			f"{stats['unembedded']} chunks have no vector, and no vector sorts ahead of every real match"
		)
	if stats["zeroed"]:
		problems.append(
			f"{stats['zeroed']} chunks have an all-zero vector, which is the nearest neighbour of"
			" every question. A restored backup does this: frappe's mariadb-dump call has no"
			" --hex-blob, so every vector comes back as zeroes with no error"
		)
	if stats["other_model"]:
		problems.append(
			f"{stats['other_model']} chunks were embedded by another model, not {EMBED_MODEL}."
			" Two models put the same sentence in different places, so the distances mean nothing"
		)
	if stats["chunks"] and not stats["indexed"]:
		problems.append("there is no vector index, so every search reads the whole table")
	if problems:
		problems.append("fix with `bench --site <site> execute rag.ingest.reindex_all`, then migrate")
	stats["problems"] = problems
	return stats


def reindex_all():
	"""Queue every Active Drive file for indexing.

	bench --site <site> execute rag.ingest.reindex_all

	Frappe caps the queue, so a big Drive stops early and reports what is left. Re-run once
	the long queue drains.
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
