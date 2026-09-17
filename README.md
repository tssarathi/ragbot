# ragbot

Frappe and ERPNext as a reproducible container stack.

## Requirements

| Requirement | Version |
| --- | --- |
| Docker Engine | 23.0 or newer |
| Free disk space | 5 GB |

Supported on macOS, Linux and Windows with WSL2. No other host dependencies.

## Quick start

```sh
make image
make up
make site
```

| Field | Value |
| --- | --- |
| URL | <http://demo.localhost:8080> |
| Username | `Administrator` |
| Password | `admin` |

Approximate durations: `make image` 5 minutes, `make up` 30 seconds, `make site` 3 minutes.

## Commands

| Command | Description |
| --- | --- |
| `make help` | List all targets |
| `make image` | Build the image from `apps.json` |
| `make up` | Start the stack |
| `make down` | Stop the stack, retain data |
| `make destroy` | Stop the stack, delete all data |
| `make site` | Create the site |
| `make apps` | List the apps baked into the image |
| `make logs` | Follow container logs |
| `make shell` | Open a shell in the backend container |
| `make bench ARGS="<args>"` | Run a bench command |

```console
$ make bench ARGS="--site demo.localhost list-apps"
frappe  16.34.0 UNVERSIONED
erpnext 16.35.0 UNVERSIONED
```

## Configuration

### Environment

`.env` is created from `.env.example` on the first `make up`.

| Variable | Default | Description |
| --- | --- | --- |
| `HTTP_PUBLISH_PORT` | `8080` | Host port for the web frontend |
| `DB_PASSWORD` | `123` | MariaDB root password |
| `CUSTOM_IMAGE` | `frappe-demo` | Image name |
| `CUSTOM_TAG` | `16` | Image tag |

Site parameters are supplied per invocation:

```sh
make site SITE=mydemo.localhost ADMIN_PASSWORD=secret
```

### Applications

`apps.json` declares the Frappe apps compiled into the image. Frappe itself is always included.

```json
[{ "url": "https://github.com/frappe/erpnext", "branch": "version-16" }]
```

Apps are baked in at build time, so a change requires a rebuild and a new site:

```sh
make image && make destroy && make up && make site
```

```console
$ make apps
erpnext
frappe
```

## Repository layout

| Path | Purpose |
| --- | --- |
| `apps.json` | Frappe apps to compile into the image |
| `Makefile` | Build and lifecycle targets |
| `compose.demo.yaml` | Local overrides for the upstream `frappe_docker` compose files |
| `.env.example` | Default environment |
| `.build/` | Pinned `frappe_docker` checkout, created by `make` |

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Site unreachable after `make up` | Allow 60 seconds for initialisation, then inspect `make logs` |
| `Site demo.localhost already exists` | `make destroy && make up && make site` |
| `port is already allocated` | Change `HTTP_PUBLISH_PORT` in `.env`, then `make down && make up` |
| Image contents do not match `apps.json` | Confirm with `make apps` |
| A command hangs | `make down && make up` |

## License

[MIT](LICENSE)
