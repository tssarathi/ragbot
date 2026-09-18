# ragbot

A Docker Compose demo of Frappe, ERPNext and Drive with a knowledge base over your Drive files.

Upload a document to Drive, ask about it in the ERPNext AI sidebar, and the answer is drawn from
that document, limited to the files Drive says you may read.

## Install

You need Docker Compose 2.24 or newer, because the compose file uses the `!reset` tag. Allow
about 25 GB of disk for the images, and keep ports 8080 and 8484 free.

Both models must already be on the host. Ollama runs in a container but mounts
`~/.ollama/models` read-only, so it cannot download them, and `make up` refuses to start
without them.

```sh
ollama pull gemma4:26b && ollama pull nomic-embed-text
make setup
```

`AI_MODEL` is a 26B model needing roughly 19 GB of memory while it answers. A first run takes
about twenty minutes, mostly building images.

Sign in at <http://demo.localhost:8080> as `Administrator` / `admin`. ERPNext is at `/app`,
Drive at `/drive`, and the AI sidebar is the button in the ERPNext navbar.

## Usage

Upload a `.txt` or `.pdf` to Drive and it indexes itself. Then ask about it in the sidebar in
your own words, with no special phrasing: the sidebar reaches the index through a
`search_knowledge_base` tool on the MCP server, the service that gives the agent its tools.

The last two use `bench`, Frappe's command line, inside the running container:

```sh
make check                                                              # every gate below
make bench ARGS="--site demo.localhost execute rag.ingest.health"       # is the index sound
make bench ARGS="--site demo.localhost execute rag.ingest.reindex_all"  # rebuild it
```

Restoring a database backup taken without `mariadb-client.cnf` leaves every vector as zeroes,
which `rag.ingest.health` reports and `reindex_all` repairs.

## Configuration

| File | Purpose |
| --- | --- |
| `.env` | Ports, passwords and model names, copied from `.env.example` on first run |
| `apps.json` | Frappe apps baked into the image; changing it needs `make destroy && make setup` |
| `rag/` | The knowledge base app, bind-mounted, so changes need only a container restart |
| `mcp/config.yaml` | MCP server settings, including the session auth that runs each tool call as the caller |
| `mariadb-client.cnf` | Makes database dumps preserve vectors |
| `nginx-realtime-auth.conf` | Lets the realtime service authorise the sidebar's connection |

`EMBED_MODEL` must produce 768-dimension vectors, the width of the database column.

## Contributing

`make check` must pass: `ruff` on the host, the test suite, the index health check, no new
Error Log rows, the search endpoint's HTTP contract and a dump client that preserves vectors.
Commit messages are one-line Conventional Commits with a scope.

## License

[AGPL-3.0](LICENSE), copyright Sarathi Thirumalai Soundararajan.

ragbot builds on Frappe Drive (AGPL-3.0) and ERPNext (GPL-3.0).
