import click
import frappe
from frappe.model.document import Document


class KBChunk(Document):
	pass


def on_doctype_update():
	"""Add the vector column KB Chunk holds but its doctype JSON must never declare.

	frappe.db.type_map has no vector entry, so an `embedding` DocField would make schema sync
	MODIFY the column towards longtext. Not a patch either: bench new-site marks every patch
	complete without running it, so a patch would never run on a fresh site.
	"""
	from rag.ingest import EMBED_DIM, health

	# ADD COLUMN, never MODIFY, for the NOT NULL: ADD COLUMN ... NOT NULL zero-fills existing
	# rows with no warning even in strict mode, and a zero vector is the nearest neighbour of
	# every query. MODIFY is the only form that refuses rather than silently poisoning the index.
	frappe.db.sql_ddl(f"ALTER TABLE `tabKB Chunk` ADD COLUMN IF NOT EXISTS embedding VECTOR({EMBED_DIM})")

	for problem in health()["problems"]:
		click.secho(f"KB Chunk: {problem}", fg="yellow")

	if frappe.db.sql("SELECT 1 FROM `tabKB Chunk` WHERE embedding IS NULL LIMIT 1"):
		# the MODIFY below would refuse, and an index built now would be missing those rows
		return
	if _nullable():
		# only when it is still nullable: MODIFY is not free, and running it on every migrate
		# would rebuild the table and the whole vector index with it
		frappe.db.sql_ddl(f"ALTER TABLE `tabKB Chunk` MODIFY embedding VECTOR({EMBED_DIM}) NOT NULL")
	# the index must be named `embedding`: IF NOT EXISTS matches on name, and any other name
	# gives ERROR 1235 "multiple VECTOR indexes". M and DISTANCE are therefore immutable after
	# first deploy; changing them here silently does nothing to an existing site.
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
