import unittest
from io import BytesIO
from types import SimpleNamespace

from rag.ingest import extract_text

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
