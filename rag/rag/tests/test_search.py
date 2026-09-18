"""The search side: what the AI sidebar actually calls, and what it gets back.

Most of these stub `search` itself. The point is the contract around it, which is where the
sidebar's failures have come from: a filter shape nobody anticipated, a page that repeats, a
row identity that collides, a hook frappe refuses to call.
"""

import unittest
from unittest.mock import patch

import frappe

from rag import search as search_module
from rag.kb.doctype.kb_answer.kb_answer import KBAnswer, _question


def _rows(*pairs):
	"""Fake search() output: (file, seq) pairs in rank order."""
	return [
		{"file": f, "seq": s, "content": f"passage {f}:{s}", "distance": 0.1 * i}
		for i, (f, s) in enumerate(pairs)
	]


class TestQuestionFromFilters(unittest.TestCase):
	def test_a_named_question_field_beats_whatever_else_arrives(self):
		"""The model sends more than one filter, and only one of them is the question.

		Taking the first value instead searched for "policy.pdf" and answered confidently from
		the wrong document, which is worse than the error it replaced.
		"""
		q = "what is the replacement excess for a lost laptop"
		for filters in (
			{"file_name": "policy.pdf", "question": q},
			{"content": "laptop", "title": q},
			{"distance": 0.4, "name": q},
			[["KB Answer", "file_name", "=", "policy.pdf"], ["KB Answer", "title", "like", f"%{q}%"]],
		):
			self.assertEqual(_question(filters), q, filters)

	def test_a_percent_sign_in_the_question_is_not_a_wildcard(self):
		# only a `like` value has wildcards to strip
		self.assertEqual(_question({"question": "is the cap 100%"}), "is the cap 100%")
		self.assertEqual(_question({"title": ["like", "%is the cap 100%%"]}), "is the cap 100")


class TestKBAnswerRows(unittest.TestCase):
	def test_two_chunks_of_one_file_are_two_documents(self):
		"""name used to be the file id, so a document long enough to chunk twice collided."""
		with patch(
			"rag.kb.doctype.kb_answer.kb_answer.search", return_value=_rows(("f1", 0), ("f1", 1))
		):
			rows = KBAnswer.get_list(filters={"question": "anything"})
		self.assertEqual(len({r["name"] for r in rows}), 2, rows)

	def test_page_two_is_not_page_one(self):
		"""frappe's live query builder sends the offset as `start`, not `limit_start`."""
		with patch(
			"rag.kb.doctype.kb_answer.kb_answer.search",
			side_effect=lambda q, limit: _rows(("f1", 0), ("f2", 0), ("f3", 0))[:limit],
		):
			page1 = KBAnswer.get_list(filters={"question": "q"}, limit_page_length=1, start=0)
			page2 = KBAnswer.get_list(filters={"question": "q"}, limit_page_length=1, start=1)
			legacy = KBAnswer.get_list(filters={"question": "q"}, limit_page_length=1, limit_start=1)
		self.assertNotEqual(page1[0]["name"], page2[0]["name"])
		self.assertEqual(page2[0]["name"], legacy[0]["name"])

	def test_rest_sends_every_number_as_a_string(self):
		"""/api/resource puts the query string straight into the call, so paging arithmetic on
		it raised TypeError and the sidebar reported "no records found"."""
		with patch(
			"rag.kb.doctype.kb_answer.kb_answer.search",
			side_effect=lambda q, limit: _rows(("f1", 0), ("f2", 0), ("f3", 0))[:limit],
		):
			rows = KBAnswer.get_list(filters={"title": "q"}, limit_page_length="1", start="1")
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["name"], "f2:0")

	def test_no_question_means_no_search(self):
		with patch("rag.kb.doctype.kb_answer.kb_answer.search") as spy:
			self.assertEqual(KBAnswer.get_list(filters={"distance": 0.5}), [])
		spy.assert_not_called()


class TestLinkQuery(unittest.TestCase):
	def test_the_hook_frappe_calls_must_stay_whitelisted(self):
		"""frappe checks is_whitelisted on a standard_queries target (desk/search.py).

		Without the decorator the sidebar's search does not error: it answers a 404 web page
		and the model is told the search failed.
		"""
		from frappe import is_whitelisted

		is_whitelisted(search_module.search_link_query)

	def test_an_empty_term_does_not_reach_the_model_or_ollama(self):
		with patch("rag.search.search") as spy:
			self.assertEqual(search_module.search_link_query(doctype="KB Answer", txt=""), [])
			self.assertEqual(search_module.search_link_query(doctype="KB Answer", txt="   "), [])
		spy.assert_not_called()

	def test_the_page_length_the_mcp_tool_sends_is_capped(self):
		"""Its default is 20. Twenty passages is ~4KB of prompt and the sidebar times out."""
		with patch("rag.search.search", return_value=_rows(*[("f", i) for i in range(20)])) as spy:
			search_module.search_link_query(doctype="KB Answer", txt="q", page_length=20)
		self.assertLessEqual(spy.call_args.kwargs["limit"], 5)

	def test_both_result_shapes(self):
		with patch("rag.search.search", return_value=_rows(("f1", 0))), patch.object(
			frappe.db, "get_value", return_value="policy.txt"
		):
			tuples = search_module.search_link_query(doctype="KB Answer", txt="q")
			dicts = search_module.search_link_query(doctype="KB Answer", txt="q", as_dict=True)
		self.assertEqual(tuples, [("policy.txt", "passage f1:0")])
		self.assertEqual(dicts[0]["value"], "policy.txt")


class TestIndexHealth(unittest.TestCase):
	def test_a_zeroed_vector_is_reported(self):
		"""What a restored backup leaves behind: frappe's mariadb-dump has no --hex-blob, so
		every vector comes back as zeroes, with no error, and then answers every question."""
		from uuid import uuid7

		from rag.ingest import EMBED_DIM, health

		self.assertEqual(health()["problems"], [], "this site should be healthy before the test")
		try:
			frappe.db.sql(
				"INSERT INTO `tabKB Chunk`"
				" (name, creation, modified, owner, modified_by, file, team, seq, content, model, embedding)"
				" SELECT %s, NOW(6), NOW(6), 'Administrator', 'Administrator',"
				" file, team, 999, 'probe', model, VEC_FromText(%s) FROM `tabKB Chunk` LIMIT 1",
				(str(uuid7()), "[" + ",".join(["0"] * EMBED_DIM) + "]"),
			)
			problems = health()["problems"]
		finally:
			frappe.db.rollback()
		self.assertTrue(any("all-zero" in p for p in problems), problems)
		self.assertTrue(any("reindex_all" in p for p in problems), problems)
