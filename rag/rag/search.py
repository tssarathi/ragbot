import json

import frappe
from drive.api.permissions import user_has_permission
from drive.utils import STATUS_ACTIVE
from frappe.utils import cint

from rag.ingest import embed

# Ranking happens before the permission filter, so ask the index for more chunks than we
# return. 10x covers a user who can read most of the corpus; WIDEN is the second pass for one
# who can read very little of it, where 10x comes back empty even though their file holds the
# only matching passage. It is bounded because each new file costs a recursive ancestry walk.
CANDIDATES = 10
WIDEN = 500


@frappe.whitelist()
def search(query: str, limit: int = 5) -> list[dict]:
	"""Chunks most similar to `query`, restricted to Drive files this user may read."""
	frappe.has_permission("KB Chunk", "report", throw=True)
	limit = max(1, min(cint(limit), 20))
	vector = json.dumps(embed([query], prefix="search_query")[0], allow_nan=False)

	out = []
	for candidates in (limit * CANDIDATES, WIDEN):
		rows = _nearest(vector, candidates)
		out = _readable(rows, limit)
		if len(out) == limit or len(rows) < candidates:
			break  # enough, or the whole index has been read
	return out


def _nearest(vector: str, candidates: int) -> list[dict]:
	# exactly ORDER BY VEC_DISTANCE_COSINE(...) ASC LIMIT n: any DESC, arithmetic wrapper or
	# missing LIMIT makes MariaDB ignore the vector index and scan the table. The IS NOT NULL
	# guard is free, measured: EXPLAIN still reports key=embedding. It matters because
	# VEC_DISTANCE_COSINE(NULL, v) is NULL and NULL sorts first, so on a site where migrate
	# refused to build the index every un-embedded row would outrank every real match.
	return frappe.db.sql(
		"""
		SELECT c.file, c.seq, c.content,
		       VEC_DISTANCE_COSINE(c.embedding, VEC_FromText(%(v)s)) AS distance
		FROM `tabKB Chunk` c
		WHERE c.embedding IS NOT NULL
		ORDER BY VEC_DISTANCE_COSINE(c.embedding, VEC_FromText(%(v)s))
		LIMIT %(n)s
		""",
		{"v": vector, "n": candidates},
		as_dict=True,
	)


def _readable(rows: list[dict], limit: int) -> list[dict]:
	"""The first `limit` rows this user may read. Drive's own check is the authority.

	Asked once per distinct file rather than per chunk: each call walks the folder ancestry
	with a recursive CTE.
	"""
	allowed = {}
	out = []
	for r in rows:
		if r.file not in allowed:
			status = frappe.db.get_value("File", r.file, "status")
			allowed[r.file] = status == STATUS_ACTIVE and bool(user_has_permission(r.file, "read"))
		if allowed[r.file]:
			out.append(r)
		if len(out) == limit:
			break
	return out


@frappe.whitelist()
def search_link_query(
	doctype=None, txt=None, searchfield=None, start=0, page_length=10, filters=None, **kwargs
):
	"""Answer a `search_documents` call on KB Answer with semantic search.

	Registered as a `standard_queries` hook, which is what frappe.desk.search.search_widget
	calls in place of building its own LIKE query. That is the only route the MCP server's
	tools expose to arbitrary server-side logic: its run_report tool cannot decode any report
	that returns rows, and list_documents passes no free-text term.
	"""
	if not (txt or "").strip():
		# focusing an empty link field would otherwise embed "" and hand back five unrelated
		# passages, one Ollama round trip at a time
		return []
	# The MCP tool defaults page_length to 20, which would hand the model every chunk in the
	# corpus. The sidebar front end gives up after 120s, so keep the prompt small.
	rows = search(txt, limit=min(cint(page_length) or 3, 5))
	for row in rows:
		row["file_name"] = frappe.db.get_value("File", row["file"], "file_name")
	if kwargs.get("as_dict"):
		return [
			{"value": r["file_name"], "description": r["content"], "distance": r["distance"]}
			for r in rows
		]
	return [(r["file_name"], r["content"]) for r in rows]
