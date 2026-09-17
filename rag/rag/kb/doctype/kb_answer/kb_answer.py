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


def _question(filters) -> str:
	"""Pull the question out of whichever filter shape and fieldname frappe hands us.

	The MCP search tool lets the model pick the filter field and it guesses: `title` and
	`name` come up as often as `question`, and they all mean the same thing here, so take
	whichever text arrives. A fieldname the doctype does not declare never reaches this
	function at all, frappe's validate_filters rejects the request first.
	"""
	if isinstance(filters, str):
		filters = frappe.parse_json(filters)
	if isinstance(filters, dict):
		values = list(filters.values())
	else:
		values = [row[-1] for row in filters or [] if len(row) >= 2]
	for value in values:
		if isinstance(value, list | tuple):  # ["like", "%parental leave%"]
			value = value[-1]
		if isinstance(value, str) and value.strip("% "):
			return value.strip("% ")
	return ""
