import frappe
from frappe.model.document import Document
from frappe.utils import cint

from rag.search import search

# The model picks the filter field itself, and any of these means the question.
QUESTION_FIELDS = ("question", "title", "name")


class KBAnswer(Document):
	"""Virtual: there is no table, listing it runs a search.

	Its DocPerms are only an entry gate. The filtering that matters is in search(), per file,
	against Drive, on every call.
	"""

	@staticmethod
	def get_list(filters=None, limit_page_length=None, start=0, limit_start=0, **kwargs):
		question = _question(filters)
		if not question:
			return []
		# qb_query.py sends the offset as `start`, db_query.py as `limit_start`, REST sends
		# both as strings.
		offset = cint(start) or cint(limit_start)
		rows = search(question, limit=offset + (cint(limit_page_length) or 5))[offset:]
		for row in rows:
			row["name"] = f"{row['file']}:{row['seq']}"  # file alone repeats across its chunks
			row["question"] = question
			row["file_name"] = frappe.db.get_value("File", row["file"], "file_name")
		return rows

	@staticmethod
	def get_count(filters=None, **kwargs):
		return len(KBAnswer.get_list(filters=filters, **kwargs))

	def load_from_db(self):
		# The name is the question, so keep it: the row's own name is the passage it came from.
		rows = KBAnswer.get_list(filters={"question": self.name})
		if not rows:
			raise frappe.DoesNotExistError(f"KB Answer {self.name} not found")
		super(Document, self).__init__(rows[0] | {"name": self.name})


def _question(filters) -> str:
	"""The question out of whichever filter shape and fieldname frappe hands us.

	An undeclared fieldname never arrives: validate_filters rejects the request first, which
	the sidebar reports as a failed search. So fall back to any field, named ones first.
	"""
	if isinstance(filters, str):
		filters = frappe.parse_json(filters)
	if isinstance(filters, dict):
		pairs = list(filters.items())
	else:
		# ["title", "x"], ["title", "like", "%x%"] and ["KB Answer", "title", "like", "%x%"],
		# rebuilt as dict pairs so the operator travels with its value
		pairs = [
			(row[-3], list(row[-2:])) if len(row) >= 3 else (row[0], row[1])
			for row in filters or []
			if len(row) >= 2
		]

	for wanted in QUESTION_FIELDS:
		for field, value in pairs:
			if field == wanted and (text := _text(value)):
				return text
	for _field, value in pairs:
		if text := _text(value):
			return text
	return ""


def _text(value) -> str:
	if isinstance(value, list | tuple):
		# ["like", "%parental leave%"]: only here is a % a wildcard and not part of the question
		return value[1].strip().strip("%").strip() if len(value) == 2 and isinstance(value[1], str) else ""
	return value.strip() if isinstance(value, str) else ""
