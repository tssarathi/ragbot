import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from rag import ingest
from rag.ingest import CHUNK_CHARS, chunk, extract_text
from rag.search import _reachable, _readable, search

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

	def test_utf_32_is_not_mistaken_for_utf_16(self):
		# UTF-32 LE opens with the UTF-16 LE BOM, so order of the two tests decides this
		self.assertEqual(extract_text(_doc(TEXT.encode("utf-32"))), TEXT)

	def test_a_real_pdf_keeps_its_pages_in_reading_order(self):
		import pymupdf

		pdf = pymupdf.open()
		for page_text in ("First page: the allowance is 600 AUD.", "Second page: the excess is 250 AUD."):
			pdf.new_page().insert_text((72, 72), page_text)
		raw = pdf.tobytes()
		pdf.close()

		text = extract_text(_doc(raw, "application/pdf"))
		self.assertIn("600 AUD", text)
		self.assertIn("250 AUD", text)
		self.assertLess(text.index("600 AUD"), text.index("250 AUD"))

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


class TestEmbedRequest(unittest.TestCase):
	"""What actually goes to Ollama. Every other embed test mocks this away."""

	def _post(self, texts, prefix=None):
		# one reply per request, sized to that request: batching means several
		def answer(url, json, timeout):
			return _reply([[0.0] * 768 for _ in json["input"]])

		with patch.object(ingest.requests, "post", side_effect=answer) as post:
			ingest.embed(texts, **({"prefix": prefix} if prefix else {}))
		return post

	def test_the_prefix_nomic_needs_and_the_flags_that_fail_loudly(self):
		"""Swap the two prefixes and retrieval quietly degrades with every test still green."""
		post = self._post(["a"])
		body = post.call_args.kwargs["json"]
		self.assertEqual(body["input"], ["search_document: a"])
		self.assertIs(body["truncate"], False)
		self.assertEqual(post.call_args.kwargs["timeout"], ingest.EMBED_TIMEOUT)

		self.assertEqual(self._post(["a"], "search_query").call_args.kwargs["json"]["input"],
			["search_query: a"])

	def test_a_long_document_is_split_across_requests(self):
		"""One request for a whole book blows the read timeout and loses all of the work."""
		post = self._post(["x"] * (ingest.EMBED_BATCH * 2 + 1))
		self.assertEqual(post.call_count, 3)
		self.assertEqual(len(post.call_args_list[0].kwargs["json"]["input"]), ingest.EMBED_BATCH)
		self.assertEqual(len(post.call_args_list[-1].kwargs["json"]["input"]), 1)


class TestUpdateHook(unittest.TestCase):
	"""on_file_update decides what gets queued. It runs on every File save on the site."""

	def _doc(self, changed=(), team="team1", is_folder=0, new=False):
		return SimpleNamespace(
			name="file1",
			team=team,
			is_folder=is_folder,
			get_doc_before_save=lambda: None if new else object(),
			has_value_changed=lambda field: field in changed,
		)

	def test_the_job_is_queued_by_name_and_never_deduplicated(self):
		"""The dotted string is load-bearing: RQ unpickles a callable before frappe.init().

		deduplicate is equally load-bearing by its absence: it also skips an enqueue while an
		earlier job is STARTED, and that job has already read the old bytes.
		"""
		with patch.object(ingest.frappe, "enqueue") as enqueue:
			ingest.on_file_update(self._doc(changed=("status",)))
		args, kwargs = enqueue.call_args
		self.assertEqual(args[0], "rag.ingest.index_file")
		self.assertEqual(kwargs["queue"], "long")
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertEqual(kwargs["job_id"], "rag-index-file1")
		self.assertNotIn("deduplicate", kwargs)

	def test_an_unrelated_save_queues_nothing(self):
		"""Drive rewrites every descendant when a folder moves. Each was an Ollama run."""
		with patch.object(ingest.frappe, "enqueue") as enqueue:
			ingest.on_file_update(self._doc(changed=("file_name", "modified")))
		enqueue.assert_not_called()

	def test_a_new_file_is_always_queued(self):
		with patch.object(ingest.frappe, "enqueue") as enqueue:
			ingest.on_file_update(self._doc(new=True))
		enqueue.assert_called_once()

	def test_a_full_queue_does_not_fail_the_upload(self):
		"""frappe checks the queue depth inside enqueue, before the after-commit deferral, so
		this exception used to come out of on_update and abort the Drive save itself."""
		with patch.object(
			ingest.frappe, "enqueue", side_effect=frappe.QueueOverloaded("too many")
		), patch.object(ingest.frappe, "log_error") as log_error:
			ingest.on_file_update(self._doc(changed=("status",)))
		log_error.assert_called_once()
		self.assertIn("file1", log_error.call_args.args)

	def test_a_file_that_leaves_drive_loses_its_chunks(self):
		"""No team means no Drive. The rows would otherwise sit in the index forever."""
		with patch.object(ingest.frappe.db, "delete") as delete, patch.object(ingest.frappe, "enqueue") as enqueue:
			ingest.on_file_update(self._doc(changed=("team",), team=None))
		delete.assert_called_once_with("KB Chunk", {"file": "file1"})
		enqueue.assert_not_called()


def _a_drive_file(test, count=1):
	"""Active Drive files, or skip. These tests need the real schema, not a fake doc."""
	files = frappe.get_all(
		"File",
		filters={"is_folder": 0, "status": "Active", "team": ["is", "set"]},
		pluck="name",
		limit=count,
	)
	if len(files) < count:
		test.skipTest(f"need {count} Active Drive file(s) on this site")
	return files


class TestIndexFile(unittest.TestCase):
	def test_a_chunk_row_survives_the_real_schema(self):
		"""index_file builds its INSERT by hand because the ORM cannot write a VECTOR column.

		Every other test here is pure-function, so a schema change that rejects that INSERT
		would break all ingestion while the suite stayed green.
		"""
		files = _a_drive_file(self)
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

	def test_indexing_twice_does_not_double_the_chunks(self):
		"""index_file deletes before it inserts, and the job runs again on every save."""
		name = _a_drive_file(self)[0]
		try:
			first = ingest.index_file(name)["chunks"]
			second = ingest.index_file(name)["chunks"]
			rows = frappe.db.count("KB Chunk", {"file": name})
			self.assertEqual((second, rows), (first, first))
		finally:
			frappe.db.rollback()

	def test_a_file_out_of_drive_loses_its_chunks(self):
		name = _a_drive_file(self)[0]
		try:
			ingest.index_file(name)
			self.assertGreater(frappe.db.count("KB Chunk", {"file": name}), 0)
			frappe.db.set_value("File", name, "status", "Removed", update_modified=False)
			self.assertEqual(ingest.index_file(name)["chunks"], 0)
			self.assertEqual(frappe.db.count("KB Chunk", {"file": name}), 0)
		finally:
			frappe.db.rollback()

	def test_an_ollama_outage_asks_the_queue_to_retry(self):
		"""Otherwise a restart of Ollama leaves the file unindexed until someone notices."""
		name = _a_drive_file(self)[0]
		try:
			with patch.object(ingest.requests, "post", side_effect=ingest.requests.ConnectionError("down")):
				self.assertRaises(frappe.RetryBackgroundJobError, ingest.index_file, name)
		finally:
			frappe.db.rollback()

	def test_an_unreadable_file_keeps_the_index_it_already_had(self):
		"""Read before delete. Deleting first committed an empty index on any storage blip."""
		name = _a_drive_file(self)[0]
		try:
			before = ingest.index_file(name)["chunks"]
			with patch.object(ingest, "extract_text", side_effect=OSError("storage is down")):
				self.assertRaises(OSError, ingest.index_file, name)
			self.assertEqual(frappe.db.count("KB Chunk", {"file": name}), before)
		finally:
			frappe.db.rollback()


class TestSearchPermissions(unittest.TestCase):
	"""The ACL is this feature's security boundary, so it needs checks that outlive me."""

	PROBE = "rag-acl-probe@example.com"

	def _probe_user(self):
		if not frappe.db.exists("User", self.PROBE):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": self.PROBE,
					"first_name": "Probe",
					"send_welcome_email": 0,
					"roles": [{"role": "Drive User"}],
				}
			).insert(ignore_permissions=True)
		return self.PROBE

	def test_a_user_only_sees_files_shared_with_them(self):
		files = _a_drive_file(self, 2)
		try:
			email = self._probe_user()
			for name in files:
				ingest.index_file(name)  # both indexed, or "did not leak" would mean nothing
			frappe.get_doc("File", files[0]).share(user=email, read=1)
			frappe.set_user(email)
			seen = {row["file"] for row in search("policy", limit=20)}
			self.assertIn(files[0], seen, "shared file should be visible")
			self.assertNotIn(files[1], seen, "unshared file leaked into results")
		finally:
			frappe.set_user("Administrator")
			frappe.db.rollback()

	def test_a_file_in_a_trashed_folder_stops_answering(self):
		"""Drive trashes a folder without touching its children, and its permission check never
		looks at an ancestor, so the child kept quoting itself after the folder was deleted."""
		name = _a_drive_file(self)[0]
		parent = frappe.db.get_value("File", name, "folder")
		rows = [frappe._dict(file=name, seq=0, content="policy", distance=0.1)]
		try:
			self.assertTrue(_reachable(name), "an untouched file must stay readable")
			self.assertEqual(_readable(rows, 5, {}), rows)
			frappe.db.set_value("File", parent, "status", "Removed", update_modified=False)
			self.assertFalse(_reachable(name))
			self.assertEqual(_readable(rows, 5, {}), [])
			# status is nullable, and `SUM(status != 'Active')` skips NULLs, so a missing
			# status used to count as active and the file stayed readable
			frappe.db.sql("UPDATE `tabFile` SET status = NULL WHERE name = %s", parent)
			self.assertFalse(_reachable(name), "a NULL status must not read as active")
		finally:
			frappe.db.rollback()

	def test_the_chunk_table_itself_is_not_readable(self):
		"""Drive User has `report` on KB Chunk and deliberately not `read`. The filtering lives
		in search(), so a generic list call must never hand back raw rows."""
		try:
			frappe.set_user(self._probe_user())
			self.assertRaises(frappe.PermissionError, frappe.get_list, "KB Chunk", fields=["content"])
		finally:
			frappe.set_user("Administrator")
			frappe.db.rollback()
