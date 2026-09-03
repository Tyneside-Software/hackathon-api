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

`--source .` uses the repo `Dockerfile`.

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

## Rollback

Redeploy a previous revision in Cloud Run, or revert `main` and push. Keep `/health` and `/` stable so the site can detect a live API even when new resources fail.
