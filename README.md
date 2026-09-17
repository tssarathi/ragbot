# ragbot

Frappe, ERPNext and Frappe Drive as a reproducible container stack.

## Quick start

Requires Docker Engine 23 or newer and 8 GB of free disk space.

```sh
make image   # compiles every application from source, several minutes
make up
make site
```

Sign in at <http://demo.localhost:8080> as `Administrator` / `admin`.
ERPNext is at `/app`, Drive at `/drive`.

`make help` lists the remaining targets. `make site` accepts `SITE=` and `ADMIN_PASSWORD=`.

## Configuration

| File | Purpose |
| --- | --- |
| `apps.json` | Applications compiled into the image |
| `.env` | Ports and passwords, generated from `.env.example` on first run |

Applications are baked into the image, so editing `apps.json` requires a rebuild and a
clean site: `make destroy`, then the three commands above.

## License

[MIT](LICENSE)
