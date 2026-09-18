"""The public search endpoint's own contract, and the index's health."""

import unittest
from unittest.mock import patch

import frappe
import requests

from rag.ingest import CHUNK_CHARS
from rag.search import search


class TestSearchArguments(unittest.TestCase):
	"""search() is a public HTTP endpoint another repo calls, so its edges are the contract."""

	def test_a_blank_question_costs_nothing_and_answers_nothing(self):
		"""It used to embed the whitespace and answer from whichever document was nearest."""
		with patch("rag.search.embed") as embed:
			for blank in ("", "   ", "\n\t "):
				self.assertEqual(search(blank), [], repr(blank))
		embed.assert_not_called()

	def test_a_missing_question_is_not_a_traceback(self):
		"""`?limit=5` with no query used to answer 500 with the whole traceback in the body."""
		with patch("rag.search.embed") as embed:
			self.assertEqual(search(), [])
		embed.assert_not_called()

	def test_an_over_long_question_is_cut_to_what_the_model_accepts(self):
		"""Ollama answers 400 past its context, which surfaced as a 500 naming its own URL."""
		with patch("rag.search.embed", return_value=[[0.0] * 768]) as embed, patch(
			"rag.search._nearest", return_value=[]
		):
			search("policy " * 3000)
		self.assertEqual(len(embed.call_args.args[0][0]), CHUNK_CHARS)

	def test_an_unreachable_model_is_reported_not_raised(self):
		with patch(
			"rag.search.embed", side_effect=requests.ConnectionError("no route")
		), self.assertRaises(frappe.ValidationError) as caught:
			search("mileage")
		self.assertNotIn("no route", str(caught.exception), "the message must not carry the URL")

	def test_the_limit_is_clamped(self):
		"""A caller asking for thousands would hand the model the whole corpus. Anything that
		is not an integer never gets here: frappe's own type check answers 417 first."""
		with patch("rag.search.embed", return_value=[[0.0] * 768]), patch(
			"rag.search._nearest", return_value=[]
		) as nearest:
			for given, wanted in ((999, 20), (0, 1), (-5, 1)):
				search("mileage", limit=given)
				self.assertEqual(nearest.call_args.args[1], wanted * 10, f"limit={given!r}")


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
