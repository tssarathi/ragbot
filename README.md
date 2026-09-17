# ragbot

Frappe, ERPNext, Frappe Drive and a local AI agent as a reproducible container stack.

## Quick start

Requires Docker Compose 2.24 or newer, 12 GB of free disk space, and the model in
`AI_MODEL` already pulled on the host with Ollama.

```sh
make setup
```

This builds the images from source, starts the stack, creates the site and wires the agent.
Around twenty minutes on a first run.

Sign in at <http://demo.localhost:8080> as `Administrator` / `admin`. ERPNext is at `/app`,
Drive at `/drive`, and the AI sidebar is the button in the ERPNext navbar.

`make help` lists the individual targets.

## Configuration

| File | Purpose |
| --- | --- |
| `apps.json` | Frappe applications compiled into the image |
| `rag/` | The knowledge base app, bind-mounted into the containers |
| `mcp/config.yaml` | MCP server settings |
| `.env` | Ports, passwords and `AI_MODEL`, generated from `.env.example` on first run |

Ollama runs in a container but mounts `~/.ollama/models` read-only, so `AI_MODEL` must name
a model you have already pulled.

Applications in `apps.json` are baked into the image, so editing it requires a rebuild and a
clean site: `make destroy`, then `make setup`. `rag/` is mounted instead, so changes there
need only a container restart.

## License

[AGPL-3.0](LICENSE)

ragbot builds on Frappe Drive (AGPL-3.0) and ERPNext (GPL-3.0).
