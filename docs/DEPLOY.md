# Deploy — hackathon-api

Push / merge to `main` deploys to **Cloud Run** in London (`europe-west2`). The site deploys separately (GitHub Pages).

## Local (required before you push)

From `hackathon-api`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

Or from the sibling site folder: `.\start.ps1`.

Check:

- http://127.0.0.1:8080/health — `ok` and `version`
- http://127.0.0.1:8080/docs

## Cloud Run (manual, if the trigger is not enough)

```powershell
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

gcloud run deploy hackathon-api `
  --source . `
  --region europe-west2 `
  --allow-unauthenticated `
  --set-env-vars "CORS_ORIGINS=https://hackathon.tyneside.software,http://127.0.0.1:5500,http://localhost:5500"
```

`--source .` uses the repo `Dockerfile` if Cloud Run is set to **Dockerfile**.  
GitHub continuous deploy is often set to **Google Cloud buildpacks** instead — that path **ignores the Dockerfile** and looks for `main:app` at the repo root. This repo now has both: `Dockerfile` *and* a root `main.py` + `Procfile` so either builder works.

If Cloud Build fails on `FROM python:…` / `toomanyrequests`, the Dockerfile already uses `mirror.gcr.io/library/python:3.12-slim` (Google's Hub cache).

After deploy:

1. Copy the service URL (`https://….run.app`).
2. Put it in `hackathon-site/config.js` as `window.HACKATHON_API` for the live site.
3. Confirm `GET {url}/health` from a browser on https://hackathon.tyneside.software (CORS).

## Environment

| Variable | Required | Meaning |
|----------|----------|---------|
| `PORT` | Cloud Run sets it | Listen port (Dockerfile default 8080) |
| `CORS_ORIGINS` | Yes in prod | Comma-separated allowed origins |

Do not enable `allow_credentials` unless you also lock origins tightly.

## Health for the platform

Cloud Run should use `GET /health`. It must not need a database.

## If Cloud Build fails

Typical causes for this repo:

| Symptom in Cloud Build | Cause | What we ship |
|------------------------|--------|----------------|
| `ModuleNotFoundError: No module named 'main'` or gunicorn/`uvicorn main:app` | Buildpacks default to a **root** `main.py`. Ours lived only in `app/main.py`. | Root `main.py` re-exports `app`. `Procfile` + `project.toml` set `uvicorn app.main:app`. |
| `toomanyrequests` / failed to pull `python:3.12-slim` | Docker Hub rate limit from Cloud Build IPs | Dockerfile `FROM mirror.gcr.io/library/python:3.12-slim` (only if the trigger uses Docker, not pack) |
| `invalid Python version specified: 3.12` against ubuntu2404 | GitHub → Cloud Run uses **buildpacks** (`gcr.io/k8s-skaffold/pack`). That OS only ships **3.13 and 3.14**. | `.python-version` and `GOOGLE_RUNTIME_VERSION` = `3.13` |

In the Cloud Run service → **Edit & deploy new revision** → **Build**: check whether it says Dockerfile or Buildpacks. Either should now work.

Confirm after a green build: `GET …/health` includes `"version": "0.1.1"` (or later) and `GET …/test_field` is 200.

## Rollback

Redeploy a previous revision in Cloud Run, or revert `main` and push. Keep `/health` and `/` stable so the site can detect a live API even when new resources fail.
