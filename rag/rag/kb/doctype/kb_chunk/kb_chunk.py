import click
import frappe
from frappe.model.document import Document


class KBChunk(Document):
	pass


def on_doctype_update():
	"""Add the vector column KB Chunk holds but its doctype JSON must never declare.

	A DocField would make schema sync MODIFY the column towards longtext, since type_map has
	no vector entry. Not a patch either: bench new-site marks patches done without running them.
	"""
	from rag.ingest import EMBED_DIM, health

	# Nullable here, NOT NULL below: ADD COLUMN ... NOT NULL zero-fills existing rows silently,
	# and a zero vector is the nearest neighbour of every query. Only MODIFY refuses.
	frappe.db.sql_ddl(f"ALTER TABLE `tabKB Chunk` ADD COLUMN IF NOT EXISTS embedding VECTOR({EMBED_DIM})")

	declared = frappe.db.sql(
		"SELECT COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()"
		" AND TABLE_NAME = 'tabKB Chunk' AND COLUMN_NAME = 'embedding'"
	)
	if declared and declared[0][0].lower() != f"vector({EMBED_DIM})":
		# ADD COLUMN above skips an existing column and MariaDB cannot redeclare a vector's
		# width, so without this every insert fails with "Incorrect vector value", per file,
		# forever, while migrate and health both report success.
		frappe.throw(
			f"`embedding` is {declared[0][0]}, not vector({EMBED_DIM}): drop the column, migrate"
			" again, then run rag.ingest.reindex_all."
		)

	for problem in health()["problems"]:
		click.secho(f"KB Chunk: {problem}", fg="yellow")

	if frappe.db.sql("SELECT 1 FROM `tabKB Chunk` WHERE embedding IS NULL LIMIT 1"):
		return  # MODIFY would refuse, and an index built now would miss those rows
	if _nullable():
		# skipped once applied: MODIFY rebuilds the table and the vector index with it
		frappe.db.sql_ddl(f"ALTER TABLE `tabKB Chunk` MODIFY embedding VECTOR({EMBED_DIM}) NOT NULL")
	# Must be named `embedding`: IF NOT EXISTS matches on name and MariaDB allows one vector
	# index per table, so M and DISTANCE are immutable after the first deploy.
	frappe.db.sql_ddl(
		"ALTER TABLE `tabKB Chunk` "
		"ADD VECTOR INDEX IF NOT EXISTS embedding (embedding) M=16 DISTANCE=cosine"
	)


def _nullable() -> bool:
	return bool(
		frappe.db.sql(
			"SELECT 1 FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()"
			" AND TABLE_NAME = 'tabKB Chunk' AND COLUMN_NAME = 'embedding' AND IS_NULLABLE = 'YES'"
		)
	)
