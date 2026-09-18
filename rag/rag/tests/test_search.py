"""The index's own health, which is about the vectors rather than any caller."""

import unittest

import frappe


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
