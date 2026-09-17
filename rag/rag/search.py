import json

import frappe
from drive.api.permissions import user_has_permission
from drive.utils import STATUS_ACTIVE
from frappe.utils import cint

from rag.ingest import embed

# Ask the index for more chunks than we return, because permission filtering happens after
# ranking and may drop most of them.
# ponytail: 10x only helps a user who can read more than a tenth of the corpus. Someone who
# can read 1 file in 1000 gets nothing even when the perfect chunk exists.
CANDIDATES = 10


@frappe.whitelist()
def search(query: str, limit: int = 5) -> list[dict]:
	"""Chunks most similar to `query`, restricted to Drive files this user may read."""
	frappe.has_permission("KB Chunk", "report", throw=True)
	limit = max(1, min(cint(limit), 20))
	vector = json.dumps(embed([query], prefix="search_query")[0], allow_nan=False)

	# exactly ORDER BY VEC_DISTANCE_COSINE(...) ASC LIMIT n: any DESC, arithmetic wrapper or
	# missing LIMIT makes MariaDB ignore the vector index and scan the table
	rows = frappe.db.sql(
		"""
		SELECT c.file, c.seq, c.content,
		       VEC_DISTANCE_COSINE(c.embedding, VEC_FromText(%(v)s)) AS distance
		FROM `tabKB Chunk` c
		ORDER BY VEC_DISTANCE_COSINE(c.embedding, VEC_FromText(%(v)s))
		LIMIT %(n)s
		""",
		{"v": vector, "n": limit * CANDIDATES},
		as_dict=True,
	)

	# Drive's own check is the authority. Ask it once per distinct file rather than per chunk:
	# each call walks the folder ancestry with a recursive CTE.
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
	# The MCP tool defaults page_length to 20, which would hand the model every chunk in the
	# corpus. The sidebar front end gives up after 120s, so keep the prompt small.
	rows = search(txt or "", limit=min(cint(page_length) or 3, 5))
	for row in rows:
		row["file_name"] = frappe.db.get_value("File", row["file"], "file_name")
	if kwargs.get("as_dict"):
		return [
			{"value": r["file_name"], "description": r["content"], "distance": r["distance"]}
			for r in rows
		]
	return [(r["file_name"], r["content"]) for r in rows]
