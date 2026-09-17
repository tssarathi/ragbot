import frappe

from rag.search import search


def execute(filters=None):
	"""Semantic search over the Drive knowledge base.

	A report rather than a whitelisted method so the AI sidebar can reach it through the MCP
	server's existing run_report tool, rather than needing a new tool of its own.
	"""
	filters = filters or {}
	columns = [
		{"fieldname": "file_name", "label": "File", "fieldtype": "Data", "width": 240},
		{"fieldname": "distance", "label": "Distance", "fieldtype": "Float", "precision": 4, "width": 90},
		{"fieldname": "content", "label": "Content", "fieldtype": "Long Text", "width": 600},
	]
	query = (filters.get("query") or "").strip()
	if not query:
		return columns, []
	rows = search(query, limit=filters.get("limit") or 5)
	for row in rows:
		row["file_name"] = frappe.db.get_value("File", row["file"], "file_name")
	return columns, rows
