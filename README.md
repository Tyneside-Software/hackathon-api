# hackathon-api

Python **FastAPI** backend for the Tyneside Logistics hackathon. Hosted on **Google Cloud Run** (`europe-west2`). Push to `main` deploys.

**Repo:** https://github.com/Tyneside-Software/hackathon-api  
**Site:** https://github.com/Tyneside-Software/hackathon-site  
**Android:** https://github.com/Tyneside-Software/hackathon-android  
**Live site:** https://hackathon.tyneside.software  
**Live API:** https://hackathon-api-git-975511976696.europe-west2.run.app  

The picture of **all three** repos is the site wiki: [Architecture](https://hackathon.tyneside.software/docs/#architecture).

To run site + API together, clone both as siblings and from the **site** folder run `.\start.ps1`.

## Documentation

| Doc | Contents |
|-----|----------|
| [docs/STACK.md](docs/STACK.md) | FastAPI, routes, CORS, buildpacks vs Dockerfile |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Local, GitHub trigger, Cloud Build failures |
| Site wiki | https://hackathon.tyneside.software/docs/ |

## Stack (short)

- FastAPI + Uvicorn; `VERSION` **0.1.8** on `/` and `/health`
- Cloud Run from GitHub uses **buildpacks** (Python **3.13**, ubuntu2404) — not the Dockerfile
- Root `main.py` re-exports `app` for pack’s `main:app`
- CORS via `CORS_ORIGINS`
- `google-cloud-datastore` for fields, GPS, and User writes; `google-cloud-firestore` for location history and User reads — imported inside the handler, not at module top
- Accounts: `POST /register`, `POST /login`, `POST /token`, `GET /users/me` (bearer). GPS and map reads stay open. Set `JWT_SECRET_KEY` on Cloud Run.
- Buses: `GET /v1/buses` serves Firestore `BusCache/newcastle` (TTL 15s) plus a 10-minute trail per vehicle. Fetches bustimes.org only when a looking map tab finds the snapshot stale.

The site is static HTML + Alpine.js 3 + Leaflet. This API stays a JSON service those pages can `fetch`.

## Local

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

- http://127.0.0.1:8080/health  
- http://127.0.0.1:8080/test_field  
- http://127.0.0.1:8080/docs  

## Routes

| Method | Path | Returns |
|--------|------|---------|
| GET | `/` | service, docs, health, register, login, token, users_me, version |
| GET | `/health` | `ok`, service, utc, version |
| GET | `/test_field` | `ok`, key, value |
| POST | `/register` | Create a user |
| POST | `/login` | JSON login → bearer token |
| POST | `/token` | OAuth2 form login (Swagger Authorize) |
| GET | `/users/me` | Bearer required |
| POST | `/create_field` | Datastore write |
| GET | `/view_field/{key}` | Datastore read |
| POST | `/v1/locations` | GPS ping from the Android tracker |
| GET | `/v1/devices` | Last-known phones for the map |
| GET | `/v1/devices/{id}` | One phone |
| GET | `/v1/locations?device_id=` | Ping history (`source`: firestore / datastore / none) |
| GET | `/v1/buses` | Live buses + 10 min `trails`; bustimes.org only on a stale looking GET |

**Proven 11 September 2026:** emulator `POST /v1/locations` → HTTP 200 `stored=datastore`; `GET /v1/devices` listed `android-c55e59830b71ba38`. Live `/health` is **0.1.8** with `/v1/buses` trails.

Add new routes in `app/routers/`. Keep `/health` cheap.

## CORS

If you set `CORS_ORIGINS` on Cloud Run it **replaces** the code defaults. Production must include:

```
https://hackathon.tyneside.software,http://127.0.0.1:5500,http://localhost:5500
```

The site reads the base URL from `hackathon-site/config.js`.

## Cloud Run

See [docs/DEPLOY.md](docs/DEPLOY.md). The GitHub trigger is pack/buildpacks, not Docker.
