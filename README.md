# hackathon-api

Python FastAPI backend for tonight’s hackathon. Hosted on **Google Cloud Run**.

**Repo:** https://github.com/Tyneside-Software/hackathon-api  
**Site:** https://github.com/Tyneside-Software/hackathon-site

To run **site + API** together, clone both as siblings and from the site folder run `.\start.ps1`.

## Local

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

- Health: http://127.0.0.1:8080/health  
- Docs: http://127.0.0.1:8080/docs  

## Cloud Run (London)

```powershell
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

gcloud run deploy hackathon-api `
  --source . `
  --region europe-west2 `
  --allow-unauthenticated `
  --set-env-vars "CORS_ORIGINS=https://michaelthomsoncc.github.io,http://127.0.0.1:5500,http://localhost:5500"
```

Paste the service URL into `hackathon-site/config.js` as `window.HACKATHON_API`.
