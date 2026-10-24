# hackathon-api

Python **FastAPI** backend for the Tyneside Logistics hackathon. The live process is Docker Compose on the Linux box. Push to `main`, then `/usr/local/bin/update` over SSH. See [docs/DEPLOY.md](docs/DEPLOY.md).

**Repo:** https://github.com/Tyneside-Software/hackathon-api  
**Site:** https://github.com/Tyneside-Software/hackathon-site  
**Android:** https://github.com/Tyneside-Software/hackathon-android  
**Live site (tailnet):** http://100.65.94.5/  
**Live API:** http://100.65.94.5:8080/health  
**Pages:** https://hackathon.tyneside.software (static only; it cannot call the HTTP API)

SSH and the password: site doc `docs/linux.md`. The password is not in git. On Michael’s PC it is `C:\Users\MichaelThomson\Desktop\hackathon-linux.md`.

The picture of **all three** repos is the site wiki: [Architecture](https://hackathon.tyneside.software/docs/#architecture). Deploy steps: [Linux box](https://hackathon.tyneside.software/docs/#linux).

To run site + API together, clone both as siblings and from the **site** folder run `.\start.ps1`.

## Documentation

| Doc | Contents |
|-----|----------|
| [docs/STACK.md](docs/STACK.md) | FastAPI, routes, CORS, buildpacks vs Dockerfile |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Local run, SSH, `/usr/local/bin/update` |
| Site wiki | https://hackathon.tyneside.software/docs/ |

## Stack (short)

- FastAPI + Uvicorn; `VERSION` is `app/config.py`. `/health` on the box must report that string after `hackathon-site/scripts/box.ps1 update`
- Live host is Docker (`python:3.14-slim`, `compose.yaml`). Buildpacks files remain for the old Cloud Run trigger and are not the deploy
- SQLite for the board, users, GPS, fields, and the bus cache. Firestore is switched off
- Accounts: `POST /register`, `POST /login`, `POST /token`, `GET /users/me` (bearer). GPS and map reads stay open. Set `JWT_SECRET_KEY` in the box `.env` or tokens die on restart
- Buses: `GET /v1/buses` serves the SQLite cache (TTL 15s) plus a 10-minute trail. Fetches bustimes.org only when that cache is stale

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
- Katie Admin: `POST /katie/admin/login` and `POST /katie/admin/verify`

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
| POST | `/create_field` | SQLite write |
| GET | `/view_field/{key}` | SQLite read |
| POST | `/v1/locations` | GPS ping from the Android tracker |
| GET | `/v1/devices` | Last-known phones for the map |
| GET | `/v1/devices/{id}` | One phone |
| GET | `/v1/locations?device_id=` | Ping history (`source`: sqlite) |
| GET | `/v1/buses` | Live buses + 10 min `trails`; bustimes.org only on a stale looking GET |
| GET | `/v1/board` | Kanban in SQLite. Empty database imports `app/board_seed.json` once |
| POST | `/v1/board/cards/{id}/move` | Move a card. `reason` is optional, including a move back |
| GET | `/v1/board/export` | Cards for `update_board.py pull` on the site. Older event rows are included so a pull does not drop them. New edits do not add events. |
| POST | `/katie/admin/login` | Katie shop Admin password check → session token |
| POST | `/katie/admin/verify` | Check a Katie Admin session token |

Live `/health` on the box reports the `VERSION` in `app/config.py`. The committed Android `API_BASE` still names the old Cloud Run host. That APK does not call the box until it is pointed there.

Add new routes in `app/routers/`. Keep `/health` cheap.

## CORS

`CORS_ORIGINS` is merged with a fixed tail. The tail always includes `https://hackathon.tyneside.software`, `http://127.0.0.1:5500`, `http://localhost:5500`, and `http://100.65.94.5`.

The site reads the base URL from `hackathon-site/config.js` (`http://100.65.94.5:8080`).

## Deploy

See [docs/DEPLOY.md](docs/DEPLOY.md). Push `origin/main`, then `/usr/local/bin/update` on the box.
