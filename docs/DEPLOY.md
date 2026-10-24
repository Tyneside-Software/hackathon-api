# Deploy — hackathon-api

The live API is Docker Compose on the Linux box, not Cloud Run. The site nginx on that same box serves the static files the team edits. GitHub Pages publishes a second copy of the site.

Full procedure, including SSH: site wiki page `docs/linux.md` (hash `#linux`).

The SSH password is not in this repository. On Michael’s PC it is in:

`C:\Users\MichaelThomson\Desktop\hackathon-linux.md`

Do not copy that file into git.

## Local

From this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

Or, from the sibling site folder, `.\start.ps1` (site on port 5500 and this API on 8080).

- http://127.0.0.1:8080/health — `ok` and `version`
- http://127.0.0.1:8080/docs

`python -m unittest tests.test_board`

## Loop

1. Edit.
2. `git pull --rebase origin main`, commit, `git push origin main`.
3. Author used here: `Michael Thomson <michael.thomson@cloudconnectsoftware.com>`.
4. `origin` must be `Tyneside-Software/hackathon-api`. The box pulls that remote. The `personal` remote is not what it pulls.
5. From the sibling site folder run `.\scripts\box.ps1 update`. That SSHes as `michael` to `192.168.0.52` and runs `/usr/local/bin/update`. The password is read from the Desktop sheet.

Do not SSH to `100.65.94.5`. Tailscale SSH stops on a browser check. HTTP on that IP is fine: http://100.65.94.5:8080/health and http://100.65.94.5/board.html .

`/usr/local/bin/update` needs no sudo password. It switches to the `website` user, fast-forwards `/home/website/hackathon-api`, runs `docker compose up -d --build --force-recreate`, fast-forwards `/var/www/hackathon-site`, and polls `/health`.

SQLite is `/home/website/hackathon-api/data/hackathon.db`, mounted at `/data/hackathon.db`. The image copies `app/` in at build. Only `./data` is mounted. Do not delete that database, do not `docker compose down -v`, and do not re-seed. A pull without `--build` leaves the old code running. The update script always builds.

`/home/website` is mode 750. `michael` is not in the `docker` group. Root works with the password in the desktop file (`sudo -S`). Passwordless sudo is only `(website) NOPASSWD: /usr/local/bin/update`.

## What the container runs

`Dockerfile` base `python:3.14-slim`. `docker-entrypoint.sh` runs `uvicorn app.main:app` on port 8080 with one worker. Compose sets `DATABASE_URL=sqlite:////data/hackathon.db` and reads `.env` from the checkout on the box. That `.env` is not in git. Do not copy it into a repo.

`VERSION` is `app/config.py`.

## Cloud Run

Pushing `main` does not update the box. The old Cloud Run service `hackathon-api-git` in `europe-west2` used buildpacks (`Procfile`, `project.toml`, root `main.py`, Python 3.13), not this Dockerfile. That URL was not serving on 24 October 2026. `config.js` on the site does not point at it.

Leave the buildpacks files in place. Do not treat a green Cloud Build as a production deploy.
