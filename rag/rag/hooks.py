app_name = "rag"
app_title = "RAG"
app_publisher = "Sarathi Thirumalai Soundararajan"
app_description = "Knowledge base over Frappe Drive"
app_license = "agpl-3.0"
required_apps = ["drive"]
# on_doctype_update only fires when kb_chunk.json's md5 changes, which covers a fresh install
# but not an ordinary migrate: schema sync skips an unchanged doctype by migration_hash.
after_migrate = ["rag.kb.doctype.kb_chunk.kb_chunk.on_doctype_update"]

# search_widget calls this instead of its own LIKE query, which is how the AI sidebar reaches
# semantic search: the MCP server's run_report cannot decode a report that returns rows.
standard_queries = {"KB Answer": "rag.search.search_link_query"}

doc_events = {
	"File": {
		"on_update": "rag.ingest.on_file_update",
		"on_trash": "rag.ingest.on_file_trash",
	}
}
