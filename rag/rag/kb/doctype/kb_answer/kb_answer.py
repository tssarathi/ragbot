import frappe
from frappe.model.document import Document

from rag.search import search


class KBAnswer(Document):
	"""Virtual: there is no table. Listing it runs a search, so every read is ACL filtered.

	This exists so the AI sidebar can reach the knowledge base through the MCP server's
	list_documents tool. The obvious route, a report via run_report, cannot work: that tool
	unmarshals report rows as arrays while frappe always returns objects.
	"""

	@staticmethod
	def get_list(filters=None, limit_page_length=None, **kwargs):
		question = _question(filters)
		if not question:
			return []
		rows = search(question, limit=limit_page_length or 5)
		for row in rows:
			row["name"] = row["file"]
			row["question"] = question
			row["file_name"] = frappe.db.get_value("File", row["file"], "file_name")
		return rows

	@staticmethod
	def get_count(filters=None, **kwargs):
		return len(KBAnswer.get_list(filters=filters, **kwargs))

	def load_from_db(self):
		rows = KBAnswer.get_list(filters={"question": self.name})
		super(Document, self).__init__(rows[0] if rows else {"doctype": "KB Answer", "name": self.name})


def _question(filters):
	"""Pull the question out of whichever filter shape frappe hands us."""
	if isinstance(filters, str):
		filters = frappe.parse_json(filters)
	if isinstance(filters, dict):
		value = filters.get("question")
		return (value[1] if isinstance(value, list | tuple) else value) or ""
	for row in filters or []:
		if len(row) >= 3 and row[-3] == "question":
			return row[-1]
		if len(row) == 2 and row[0] == "question":
			return row[1]
	return ""
