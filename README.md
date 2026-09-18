# ragbot

Frappe, ERPNext, Frappe Drive and a local AI agent as a reproducible container stack, with a
knowledge base over Drive: upload a document, ask about it in the ERPNext AI sidebar, and the
answer comes from that document, limited to files Drive says you can read.

## Quick start

Requires Docker Compose 2.24 or newer, and on the host:

- **~25 GB of free disk**: 20 GB of images plus build cache. The demo image alone is 5.2 GB
  and the Ollama image is 7 GB.
- **RAM for the model**: `AI_MODEL` is a 26B model at about 19 GB resident, on top of the
  Frappe stack. A smaller model is a false economy here: measured across seven local models,
  the 8B and 12B ones either never search the knowledge base or stop calling tools at all for
  ordinary ERPNext questions.
- **Ports 8080 and 8484** free.
- **Both models already pulled**, `AI_MODEL` and `EMBED_MODEL`. Ollama runs in a container
  but mounts `~/.ollama/models` read-only, so it cannot download them itself. `make up`
  refuses to start without them.

```sh
ollama pull gemma4:26b && ollama pull nomic-embed-text
make setup
```

This builds the images from source, starts the stack, creates the site and wires the agent.
Around twenty minutes on a first run.

Sign in at <http://demo.localhost:8080> as `Administrator` / `admin`. ERPNext is at `/app`,
Drive at `/drive`, and the AI sidebar is the button in the ERPNext navbar.

`make help` lists the individual targets. `make check` runs the quality gates: lint, the test
suite, the index health check, and an empty Error Log.

## Using the knowledge base

Upload a `.txt` or `.pdf` to Drive and it indexes itself: the text is chunked, embedded with
`EMBED_MODEL` through the Ollama container, and stored in `KB Chunk` with a MariaDB
`VECTOR(768)` column. Ask about it in the sidebar and name the knowledge base in the question,
for example *"Search the KB Answer doctype: what is the home office allowance?"*.

The naming is needed because the agent has no catalogue of what it can search: nothing in its
prompt or its ten generic tools mentions this knowledge base. Measured across four local
models, a question that does not name it never reaches the index; one paragraph in the agent's
system prompt takes that from 0 to 11 of 12. That paragraph lives in the agent's own repo.

Two operator commands:

```sh
make bench ARGS="--site demo.localhost execute rag.ingest.health"       # is the index sound
make bench ARGS="--site demo.localhost execute rag.ingest.reindex_all"  # rebuild it
```

**After restoring a backup, reindex.** `bench backup` shells out to `mariadb-dump` without
`--hex-blob`, so every vector restores as zeroes: no error, no warning, and a zero vector is
the nearest neighbour of every question. `rag.ingest.health` reports it, `bench migrate` warns
about it, and `reindex_all` fixes it.

## Configuration

| File | Purpose |
| --- | --- |
| `apps.json` | Frappe applications compiled into the image |
| `rag/` | The knowledge base app, bind-mounted into the containers |
| `mcp/config.yaml` | MCP server settings |
| `nginx-realtime-auth.conf` | Lets the realtime service authenticate sockets, see the file |
| `.env` | Ports, passwords and models, generated from `.env.example` on first run |

`EMBED_MODEL` must produce 768-dimension vectors, because that is the width of the column.
A model of another width fails loudly on the next indexing run; a different model of the same
width is worse, so every chunk records the model that embedded it and `health` reports a
mismatch.

Applications in `apps.json` are baked into the image, so editing it requires a rebuild and a
clean site: `make destroy`, then `make setup`. `rag/` is mounted instead, so changes there
need only a container restart.

## License

[AGPL-3.0](LICENSE)

ragbot builds on Frappe Drive (AGPL-3.0) and ERPNext (GPL-3.0).
