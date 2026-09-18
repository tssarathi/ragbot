import frappe
from frappe.model.document import Document
from frappe.utils import cint

from rag.search import search


class KBAnswer(Document):
	"""Virtual: there is no table. Listing it runs a search, so every read is ACL filtered.

	This exists so the AI sidebar can reach the knowledge base through the MCP server's
	search_documents tool. The obvious route, a report via run_report, cannot work: that tool
	unmarshals report rows as arrays while frappe always returns objects.

	This doctype's own DocPerms are an entry gate and nothing more. The filtering that matters
	happens inside search(), against Drive, per file, on every call.
	"""

	@staticmethod
	def get_list(filters=None, limit_page_length=None, start=0, limit_start=0, **kwargs):
		question = _question(filters)
		if not question:
			return []
		# Page by fetching up to the end of the page and dropping the head, or page 2 is page 1
		# again and a paging client reads the same rows forever. Both offset names, because the
		# two query builders disagree: qb_query.py sends `start`, db_query.py `limit_start`.
		# cint everything: over REST they arrive as strings out of the query string.
		offset = cint(start) or cint(limit_start)
		rows = search(question, limit=offset + (cint(limit_page_length) or 5))[offset:]
		for row in rows:
			# file alone is not unique: a document long enough to chunk twice would hand the
			# caller two rows sharing a primary key
			row["name"] = f"{row['file']}:{row['seq']}"
			row["question"] = question
			row["file_name"] = frappe.db.get_value("File", row["file"], "file_name")
		return rows

	@staticmethod
	def get_count(filters=None, **kwargs):
		return len(KBAnswer.get_list(filters=filters, **kwargs))

	def load_from_db(self):
		# Reading one "document" means asking the question its name carries, so keep that name:
		# the row's own name is the passage it came from, and returning it would answer a
		# different name than the caller asked for. A search always matches something, so the
		# only empty case is a corpus this user cannot read at all.
		rows = KBAnswer.get_list(filters={"question": self.name})
		if not rows:
			raise frappe.DoesNotExistError(f"KB Answer {self.name} not found")
		super(Document, self).__init__(rows[0] | {"name": self.name})


# A model choosing a filter field reaches for any of these, and they all mean the question.
# Anything else it sends alongside them is a detail about the answer it expects, not the query.
QUESTION_FIELDS = ("question", "title", "name")


def _question(filters) -> str:
	"""Pull the question out of whichever filter shape and fieldname frappe hands us.

	A fieldname the doctype does not declare never reaches this function: frappe's
	validate_filters rejects the whole request first, which the sidebar reports as a failed
	search. So take the text from any field, but only after the named ones have had their turn.
	"""
	if isinstance(filters, str):
		filters = frappe.parse_json(filters)
	if isinstance(filters, dict):
		# {"title": "x"} or {"title": ["like", "%x%"]}
		pairs = list(filters.items())
	else:
		# ["title", "x"], ["title", "like", "%x%"] or ["KB Answer", "title", "like", "%x%"]:
		# rebuilt into the dict form so the operator travels with its value
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
	"""The searchable text in one filter value, or "" if there is none."""
	if isinstance(value, list | tuple):
		# an operator pair, ["like", "%parental leave%"]. Only here is a % a wildcard rather
		# than part of the question, so only here is it stripped.
		return value[1].strip().strip("%").strip() if len(value) == 2 and isinstance(value[1], str) else ""
	return value.strip() if isinstance(value, str) else ""
