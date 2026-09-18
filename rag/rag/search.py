import json

import frappe
from drive.api.permissions import user_has_permission
from drive.utils import STATUS_ACTIVE
from frappe.utils import cint

from rag.ingest import embed

# Ranking happens before the permission filter, so over-fetch. WIDEN is the second pass for a
# user who can read too little of the corpus for 10x to find anything of theirs.
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
	# Exactly ORDER BY VEC_DISTANCE_COSINE(...) ASC LIMIT n: any DESC, arithmetic wrapper or
	# missing LIMIT and MariaDB drops the vector index. The NULL guard stays because
	# VEC_DISTANCE_COSINE(NULL, v) is NULL and NULL sorts first; it keeps the index, measured.
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
	"""The first `limit` rows this user may read, Drive deciding. Once per file, not per chunk:
	each call walks the folder ancestry with a recursive CTE."""
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
