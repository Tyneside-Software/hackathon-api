# Deploy — hackathon-api

Push / merge to **`main`** deploys to Cloud Run in London (`europe-west2`). The site deploys separately (GitHub Pages).

## Local (before you push)

From `hackathon-api`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

Or from the sibling site folder: `.\start.ps1`.

- http://127.0.0.1:8080/health — `ok` and `version`
- http://127.0.0.1:8080/test_field
- http://127.0.0.1:8080/docs

## How GitHub deploy actually builds

The connected service **hackathon-api-git** uses Cloud Build step `gcr.io/k8s-skaffold/pack` (buildpacks) on **ubuntu2404**. That OS only has Python **3.13 and 3.14**. It does **not** use the Dockerfile.

That is why the repo has:

- Root `main.py` (pack defaults to `main:app`)
- `Procfile` + `project.toml` entrypoint
- `.python-version` = **3.13** (a `3.12` pin fails the build)

After a green build, `GET /health` should report `"version": "0.1.3"` or later, and `GET /test_field` should be 200.

## Manual deploy (if the trigger is not enough)

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

`--source .` uses the **Dockerfile** if that service is set to Docker. The GitHub trigger uses pack instead.

## Environment

| Variable | Required | Meaning |
|----------|----------|---------|
| `PORT` | Cloud Run sets it | Listen port |
| `CORS_ORIGINS` | Yes in prod | Comma-separated origins; **replaces** code defaults if set |

Do not enable `allow_credentials` unless origins are locked tightly.

## If Cloud Build fails

| Log line | Cause | Fix in repo |
|----------|--------|-------------|
| `gcr.io/k8s-skaffold/pack` then `invalid Python version specified: 3.12` | ubuntu2404 has no 3.12 | `.python-version` / `GOOGLE_RUNTIME_VERSION` = `3.13` |
| `No module named 'main'` / `uvicorn main:app` | Pack looks at repo-root `main.py` | Root `main.py` re-exports `app` |
| `toomanyrequests` pulling `python:3.12-slim` | Docker Hub rate limit | Only applies if the trigger uses **Docker**; Dockerfile already uses `mirror.gcr.io` |

Health for the platform: `GET /health`. It must not need Datastore.

## Rollback

Redeploy a previous Cloud Run revision, or revert `main` and push.
