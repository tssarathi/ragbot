import json

import frappe
import requests
from drive.api.permissions import user_has_permission
from drive.utils import STATUS_ACTIVE
from frappe.utils import cint

from rag.ingest import CHUNK_CHARS, embed

# Ranking happens before the permission filter, so over-fetch. WIDEN is the second pass for a
# user who can read too little of the corpus for 10x to find anything of theirs.
# ponytail: WIDEN is also the recall ceiling, and lifting it needs the permission filter in
# SQL. `c.team IN (get_teams(user))` looks like that filter but is not: measured, it drops a
# file shared directly with a user who is not in the owning team.
CANDIDATES = 10
WIDEN = 500


@frappe.whitelist(methods=["GET"])
def search(query: str = "", limit: int = 5) -> list[dict]:
	"""Chunks most similar to `query`, restricted to Drive files this user may read."""
	frappe.has_permission("KB Chunk", "report", throw=True)
	# A missing question is a 500 and a leaked traceback without the default; an over-long one is
	# a 400 from Ollama, which has the same effect. Both arrive from the model, not a person.
	query = (query or "").strip()[:CHUNK_CHARS]
	if not query:
		return []  # embedding whitespace answers confidently from an unrelated document
	limit = max(1, min(cint(limit), 20))
	try:
		vector = json.dumps(embed([query], prefix="search_query")[0], allow_nan=False)
	except requests.RequestException as exc:
		frappe.throw(f"the knowledge base cannot be searched right now ({type(exc).__name__})")

	allowed, out = {}, []
	for candidates in (limit * CANDIDATES, WIDEN):
		rows = _nearest(vector, candidates)
		out = _readable(rows, limit, allowed)
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


def _readable(rows: list[dict], limit: int, allowed: dict) -> list[dict]:
	"""The first `limit` rows this user may read, Drive deciding. Once per file, not per chunk:
	each call walks the folder ancestry with a recursive CTE. `allowed` carries those answers
	across the two passes, which overlap by the whole of the first."""
	out = []
	for r in rows:
		if r.file not in allowed:
			allowed[r.file] = _reachable(r.file) and bool(user_has_permission(r.file, "read"))
		if allowed[r.file]:
			out.append(r)
		if len(out) == limit:
			break
	return out


def _reachable(file: str) -> bool:
	"""Still in Drive and not inside a trashed folder. Drive trashes a folder without touching
	its children, and its permission check never looks at an ancestor, so a file the user thinks
	they deleted keeps answering questions. `<=>` because status is nullable and a plain `!=`
	yields NULL, which SUM skips -- a missing status would read as active."""
	trashed, found = frappe.db.sql(
		"""
		WITH RECURSIVE ancestry AS (
			SELECT name, folder, status FROM `tabFile` WHERE name = %(f)s
			UNION ALL
			SELECT p.name, p.folder, p.status FROM ancestry a JOIN `tabFile` p ON p.name = a.folder
		)
		SELECT SUM(NOT (status <=> %(active)s)), COUNT(*) FROM ancestry
		""",
		{"f": file, "active": STATUS_ACTIVE},
	)[0]
	return bool(found) and not trashed
