# Tech stack — hackathon-api

Python FastAPI for the Tyneside Logistics hackathon. Sibling of [hackathon-site](https://github.com/Tyneside-Software/hackathon-site).

The **human-facing** picture of both repos is the site wiki: [Architecture](https://hackathon.tyneside.software/docs/#architecture).

## At a glance

| Layer | Choice | Notes |
|-------|--------|--------|
| Language | Python **3.13** on Cloud Run | Buildpacks / ubuntu2404. Laptop may be 3.12 or 3.14. |
| Framework | FastAPI `>=0.115,<0.117` | `/docs`, CORS middleware |
| Server | Uvicorn `[standard]` | ASGI; `--reload` locally |
| Datastore | `google-cloud-datastore` | Fields + Device/LocationPing + User writes; imported inside the handler |
| Firestore | `google-cloud-firestore` | LocationPing history + User reads |
| Host | Cloud Run `europe-west2` | Push `main` → GitHub trigger |
| Builder | **Buildpacks** (`pack`) | GitHub CD **ignores** the Dockerfile |
| Auth | JWT on `/users/me` only | Cloud Run still `--allow-unauthenticated`. `POST /register` `/login` `/token`. Set `JWT_SECRET_KEY`. |

Keep `/health` and `/test_field` free of Datastore. Do not add Redis or Postgres unless a board card says so.

## Layout

```
hackathon-api/
  app/main.py         FastAPI app, CORS, VERSION, routes
  main.py             Re-export `app` for pack’s `main:app`
  Procfile            uvicorn app.main:app --port $PORT
  project.toml        GOOGLE_RUNTIME_VERSION=3.13 + entrypoint
  .python-version     3.13
  Dockerfile          Docker-trigger only (mirror.gcr.io python 3.13-slim)
  requirements.txt
  docs/
```

ASGI object: `app.main:app` (and `main:app` via the root re-export).

## Dependencies

```
fastapi>=0.115.0,<0.117
uvicorn[standard]>=0.34.0,<0.36
google-cloud-datastore
google-cloud-firestore
```

Install in a venv. Do not commit `.venv`.

## HTTP

| Method | Path | Body |
|--------|------|------|
| GET | `/` | `service`, `docs`, `health`, `test_field`, `version` |
| GET | `/health` | `ok`, `service`, `utc`, `version` |
| GET | `/test_field` | `ok`, `key`, `value` |
| POST | `/create_field` | Datastore entity (needs GCP credentials) |
| GET | `/view_field/{key}` | Datastore read |
| POST | `/v1/locations` | Phone GPS ping |
| GET | `/v1/devices` | Last-known phones |
| GET | `/v1/devices/{id}` | One phone |
| GET | `/v1/locations?device_id=` | Ping history (`source` firestore or datastore) |
| GET | `/docs` | Swagger |
| GET | `/openapi.json` | OpenAPI |

`VERSION` is in `app/main.py` (currently **0.1.5**). CORS methods: `GET`, `POST`, `DELETE`, `OPTIONS`.

Emulator tracker posted a live ping on 11 September 2026 (`stored=datastore`).

## CORS

`CORS_ORIGINS` — comma-separated, no trailing slashes. If the env var is **set on Cloud Run**, it **replaces** the code defaults. Production must include:

```
https://hackathon.tyneside.software
http://127.0.0.1:5500
http://localhost:5500
```

## Site config

The browser reads `window.HACKATHON_API` from `hackathon-site/config.js` (Cloud Run URL in git). Local override: `http://127.0.0.1:8080`.

## Dockerfile (optional path)

Used only if the Cloud Run trigger is Docker, not pack.

- Base: `mirror.gcr.io/library/python:3.13-slim` (Hub cache; avoids `toomanyrequests`)
- `PORT` default 8080; Cloud Run injects `PORT`
- `CMD` Uvicorn on `0.0.0.0:${PORT}`

Do not bind to `127.0.0.1` in the container.

## Board follow-on

Card 11: persist routes. In-memory is acceptable for the night if two requests hit the same instance — say so on the card. If the API is down, the map must still work.
