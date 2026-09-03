# Tech stack — hackathon-api

Python FastAPI service for the Tyneside Logistics hackathon. Sibling of [hackathon-site](https://github.com/Tyneside-Software/hackathon-site).

## At a glance

| Layer | Choice | Why |
|-------|--------|-----|
| Language | Python 3.12 | Matches the Cloud Run image |
| Framework | FastAPI `>=0.115,<0.117` | `/docs`, typing, CORS middleware |
| Server | Uvicorn `[standard]` `>=0.34,<0.36` | ASGI; `--reload` locally |
| Container | `python:3.12-slim` | `Dockerfile` in repo root |
| Hosting | Cloud Run `europe-west2` | Push to `main` deploys |
| Auth (now) | None (`--allow-unauthenticated`) | Hackathon demo |
| Persistence (now) | None | `/` and `/health` only; card 11 adds routes |

No Django, Flask, or database yet. Do not add Redis/Postgres unless a board card says so.

## Layout

```
hackathon-api/
  app/
    __init__.py
    main.py          # FastAPI app, CORS, routes
  Dockerfile
  requirements.txt
  README.md
  docs/
```

Entry point: `app.main:app`.

## Dependencies

From `requirements.txt`:

```
fastapi>=0.115.0,<0.117
uvicorn[standard]>=0.34.0,<0.36
```

Install in a venv. Do not commit `.venv`.

## HTTP surface

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | `{ service, docs, health, version }` |
| GET | `/health` | `{ ok, service, utc, version }` |
| GET | `/docs` | Swagger UI (FastAPI default) |
| GET | `/openapi.json` | OpenAPI schema |

`VERSION` in `app/main.py` (currently `0.1.0`) is returned on `/` and `/health` so deploys are identifiable.

Allowed methods on CORS: `GET`, `POST`, `OPTIONS`. New write routes should stay on `POST` (or add `PUT`/`PATCH` in CORS when you need them).

## CORS

Environment variable `CORS_ORIGINS` — comma-separated origins, no trailing slashes.

Default if unset:

```
http://127.0.0.1:5500,http://localhost:5500,https://michaelthomsoncc.github.io
```

Live site origin is **`https://hackathon.tyneside.software`**. Cloud Run must include that or the browser will block `fetch`. Also keep localhost for `start.ps1`.

`allow_credentials=False`. Headers: `Content-Type`, `Accept`. `max_age=600`.

## Config the site uses

The site reads `window.HACKATHON_API` from `hackathon-site/config.js`.

- Local: `http://127.0.0.1:8080`
- Live: the Cloud Run HTTPS URL

## Container

`Dockerfile`:

- Base `python:3.12-slim`
- `PORT` default `8080` (Cloud Run injects `PORT`)
- `CMD` runs Uvicorn on `0.0.0.0:${PORT}`

Do not listen on `127.0.0.1` in the container.

## What comes next (board)

- Card 11: persist routes (replace `localStorage`). In-memory is acceptable for the night if two requests to the same instance work — say so on the card. If the API is down, the map must still work.

Keep `/health` cheap and dependency-free so Cloud Run and the team can probe it.
