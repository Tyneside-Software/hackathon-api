"""Cloud Run / buildpack entrypoint.

Google's Python buildpack defaults to `uvicorn main:app` (or gunicorn
`main:app`). The FastAPI app lives in `app/main.py`; this re-export keeps
source deploys working without a custom GOOGLE_ENTRYPOINT.
"""

from app.main import app

__all__ = ["app"]
