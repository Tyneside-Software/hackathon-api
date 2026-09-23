#!/bin/sh
set -e
DB="/app/hackathon.db"
if [ -d "$DB" ]; then
  echo "hackathon.db is a directory. On the host, from hackathon-api:" >&2
  echo "  rm -rf hackathon.db && touch hackathon.db" >&2
  echo "then: docker compose up --build" >&2
  exit 1
fi
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}" --workers 1
