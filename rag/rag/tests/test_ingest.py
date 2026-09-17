import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from rag import ingest
from rag.ingest import CHUNK_CHARS, chunk, extract_text

TEXT = "Zurich café à la carte “quoted” – résumé"


def _doc(raw, mime="text/plain"):
	return SimpleNamespace(mime_type=mime, manager=SimpleNamespace(get_file=lambda d: BytesIO(raw)))


class TestExtractText(unittest.TestCase):
	def test_decodes_what_drive_users_upload(self):
		for enc in ("utf-8", "utf-8-sig", "utf-16", "cp1252"):
			self.assertEqual(extract_text(_doc(TEXT.encode(enc))), TEXT, enc)

	def test_non_ascii_past_the_first_page(self):
		# Sniffing a prefix would call this ASCII and mangle the tail.
		self.assertTrue(extract_text(_doc(b"a" * 70000 + TEXT.encode())).endswith(TEXT))

	def test_pdf_that_is_not_a_pdf(self):
		# mime_type is assigned from the extension, so anything can arrive as application/pdf.
		for raw in (b"", b"Leave policy: 20 days.\n", b"%PDF-1.7\n1 0 obj"):
			self.assertEqual(extract_text(_doc(raw, "application/pdf")), "", raw[:8])

	def test_unreadable_format_is_never_read(self):
		def boom(doc):
			raise AssertionError("read the blob for a format we cannot parse")

		doc = SimpleNamespace(mime_type="image/png", manager=SimpleNamespace(get_file=boom))
		self.assertEqual(extract_text(doc), "")


class TestChunk(unittest.TestCase):
	def test_packs_paragraphs_without_losing_text(self):
		doc = "\n\n".join(f"Section {i}. " + "words " * 80 for i in range(12))
		chunks = chunk(doc)
		self.assertGreater(len(chunks), 1)
		self.assertTrue(all(len(c) <= CHUNK_CHARS for c in chunks))
		self.assertEqual(" ".join(chunks).split(), doc.split())

	def test_paragraph_longer_than_a_chunk(self):
		# A PDF page often arrives as one blob with no blank line to split on.
		chunks = chunk("x" * 5000)
		self.assertEqual("".join(chunks), "x" * 5000)
		self.assertTrue(all(len(c) <= CHUNK_CHARS for c in chunks))

	def test_long_paragraph_splits_on_word_boundaries(self):
		soup = " ".join(["word"] * 2000)
		chunks = chunk(soup)
		self.assertEqual(" ".join(chunks).split(), soup.split())
		self.assertFalse(any(c.startswith(" ") or c.endswith(" ") for c in chunks))

	def test_windows_line_endings(self):
		paras = [f"Paragraph {i}. " + "word " * 60 for i in range(8)]
		lf = "\n\n".join(paras)
		self.assertEqual(chunk(lf.replace("\n", "\r\n")), chunk(lf))
		self.assertEqual(chunk(lf.replace("\n", "\r")), chunk(lf))
		self.assertFalse(any("\r" in c for c in chunk(lf.replace("\n", "\r\n"))))

	def test_other_line_separators(self):
		# A .txt upload can carry form feeds and Unicode separators; none should survive.
		for sep in ("\f", "\v", "\x1c", "\x1d", "\x1e", "\u2028", "\u2029", "\u0085"):
			self.assertEqual(chunk("A" * 20 + sep + "B" * 20), ["A" * 20 + "\n" + "B" * 20], repr(sep))

	def test_nothing_to_chunk(self):
		self.assertEqual(chunk(""), [])
		self.assertEqual(chunk("   \n\n  "), [])


def _reply(vectors):
	return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"embeddings": vectors})


class TestEmbed(unittest.TestCase):
	def test_short_reply_is_not_silently_zipped_away(self):
		with patch.object(ingest.requests, "post", return_value=_reply([[0.0] * 768])):
			self.assertRaises(frappe.ValidationError, ingest.embed, ["a", "b"])

	def test_wrong_dimension_names_the_model(self):
		with patch.object(ingest.requests, "post", return_value=_reply([[0.0] * 384])):
			self.assertRaises(frappe.ValidationError, ingest.embed, ["a"])


class TestIndexFile(unittest.TestCase):
	def test_a_chunk_row_survives_the_real_schema(self):
		"""index_file builds its INSERT by hand because the ORM cannot write a VECTOR column.

		Every other test here is pure-function, so a schema change that rejects that INSERT
		would break all ingestion while the suite stayed green.
		"""
		files = frappe.get_all(
			"File",
			filters={"is_folder": 0, "status": "Active", "team": ["is", "set"]},
			pluck="name",
			limit=1,
		)
		if not files:
			self.skipTest("no Active Drive file on this site")
		try:
			result = ingest.index_file(files[0])
			self.assertGreater(result["chunks"], 0)
			embedded = frappe.db.sql(
				"SELECT COUNT(*) FROM `tabKB Chunk` WHERE file = %s AND embedding IS NOT NULL",
				(files[0],),
			)[0][0]
			self.assertEqual(embedded, result["chunks"])
		finally:
			frappe.db.rollback()

