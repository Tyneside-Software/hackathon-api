# hackathon-api

Python **FastAPI** backend for the Tyneside Logistics hackathon. Hosted on **Google Cloud Run** (`europe-west2`). Push to `main` deploys.

**Repo:** https://github.com/Tyneside-Software/hackathon-api  
**Site:** https://github.com/Tyneside-Software/hackathon-site  
**Live site:** https://hackathon.tyneside.software  
**Live API:** https://hackathon-api-git-975511976696.europe-west2.run.app  

The picture of **both** repos is the site wiki: [Architecture](https://hackathon.tyneside.software/docs/#architecture).

To run site + API together, clone both as siblings and from the **site** folder run `.\start.ps1`.

## Documentation

| Doc | Contents |
|-----|----------|
| [docs/STACK.md](docs/STACK.md) | FastAPI, routes, CORS, buildpacks vs Dockerfile |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Local, GitHub trigger, Cloud Build failures |
| Site wiki | https://hackathon.tyneside.software/docs/ |

## Stack (short)

- FastAPI + Uvicorn; `VERSION` **0.1.4** on `/` and `/health`
- Cloud Run from GitHub uses **buildpacks** (Python **3.13**, ubuntu2404) — not the Dockerfile
- Root `main.py` re-exports `app` for pack’s `main:app`
- CORS via `CORS_ORIGINS`
- `google-cloud-datastore` only for `POST /create_field`

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
| GET | `/` | service, docs, health, test_field, version |
| GET | `/health` | `ok`, service, utc, version |
| GET | `/test_field` | `ok`, key, value |
| POST | `/create_field` | Datastore write |
| POST | `/v1/locations` | GPS ping from the Android tracker |
| GET | `/v1/devices` | Last-known phones for the map |
| GET | `/v1/devices/{id}` | One phone |

Add new routes in `app/main.py`. Keep `/health` cheap.

## CORS

If you set `CORS_ORIGINS` on Cloud Run it **replaces** the code defaults. Production must include:

```
https://hackathon.tyneside.software,http://127.0.0.1:5500,http://localhost:5500
```

The site reads the base URL from `hackathon-site/config.js`.

## Cloud Run

See [docs/DEPLOY.md](docs/DEPLOY.md). The GitHub trigger is pack/buildpacks, not Docker.
