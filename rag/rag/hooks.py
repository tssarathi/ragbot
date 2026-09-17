app_name = "rag"
app_title = "RAG"
app_publisher = "Sarathi Thirumalai Soundararajan"
app_description = "Knowledge base over Frappe Drive"
app_license = "agpl-3.0"
required_apps = ["drive"]

doc_events = {
	"File": {
		"on_update": "rag.ingest.on_file_update",
		"on_trash": "rag.ingest.on_file_trash",
	}
}
