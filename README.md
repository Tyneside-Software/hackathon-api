# hackathon-api

Python **FastAPI** backend for the Tyneside Logistics hackathon. Hosted on **Google Cloud Run** (`europe-west2`). Push to `main` deploys.

**Repo:** https://github.com/Tyneside-Software/hackathon-api  
**Site:** https://github.com/Tyneside-Software/hackathon-site  
**Live site:** https://hackathon.tyneside.software  

To run **site + API** together, clone both as siblings and from the site folder run `.\start.ps1`.

## Documentation

| Doc | Contents |
|-----|----------|
| [docs/STACK.md](docs/STACK.md) | FastAPI, Uvicorn, Docker, CORS, routes |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Local, Cloud Run, env vars, health |
| Site JS layer | Alpine.js — [hackathon-site/docs/JAVASCRIPT.md](https://github.com/Tyneside-Software/hackathon-site/blob/main/docs/JAVASCRIPT.md) |

## Tech stack (short)

- Python 3.13 on Cloud Run buildpacks (ubuntu2404; 3.12 is not available there), FastAPI, Uvicorn
- `Dockerfile` (Cloud Build) **and** root `main.py` + `Procfile` (buildpacks from GitHub)
- Cloud Run
- CORS via `CORS_ORIGINS`
- `VERSION` on `/` and `/health` (now `0.1.0`)
- No database yet

The site is static HTML + **Alpine.js 3** + Leaflet. This API must stay a boring JSON service those pages can `fetch`.

## Local

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

- Health: http://127.0.0.1:8080/health  
- Docs: http://127.0.0.1:8080/docs  
- Root: http://127.0.0.1:8080/

## Routes

| Method | Path | Returns |
|--------|------|---------|
| GET | `/` | service, docs, health, version |
| GET | `/health` | `ok`, service, utc, version |

New routes: add them in `app/main.py`, keep `/health` free of extra dependencies, and extend CORS methods if you need more than GET/POST.

## CORS

Set `CORS_ORIGINS` to a comma-separated list. Production must include:

```
https://hackathon.tyneside.software,http://127.0.0.1:5500,http://localhost:5500
```

The site reads the API base from `hackathon-site/config.js` (`window.HACKATHON_API`).

## Cloud Run

See [docs/DEPLOY.md](docs/DEPLOY.md). Short form:

```powershell
gcloud run deploy hackathon-api `
  --source . `
  --region europe-west2 `
  --allow-unauthenticated `
  --set-env-vars "CORS_ORIGINS=https://hackathon.tyneside.software,http://127.0.0.1:5500,http://localhost:5500"
```

Paste the service URL into the site `config.js` for Pages.
