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
	frappe.db.sql_ddl("ALTER TABLE `tabKB Chunk` ADD COLUMN IF NOT EXISTS embedding VECTOR(768)")
