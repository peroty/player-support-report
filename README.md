# Destiny 2 TWID ↔ Patch Notes Tracker

A self-hosted Flask app that:

- Pulls Bungie News from RSS.
- Archives TWIDs and Destiny patch notes in SQLite.
- Extracts list-style change items.
- Compares latest patch notes vs latest TWID with fuzzy matching.
- Strikethroughs patch notes that were already teased in the TWID.
- Lets you search historical mentions (weapons, armor, systems).

## Why this helps your podcast workflow

You can run `/compare` after patch day and quickly identify:

- **Already teased** changes (crossed out) → skip in episode.
- **Net new** patch notes (bold rows) → focus your coverage.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open http://localhost:8000 then:

1. Click **Sync from RSS**.
2. Click **Compare latest**.
3. Optionally use `/search?q=your-term`.

## Deploy in homelab

You can run this behind nginx, Caddy, or Traefik with Gunicorn:

```bash
gunicorn -w 2 -b 0.0.0.0:8000 app:app
```

Persist `data.sqlite3` in a volume/bind mount.

## Docker quick start (recommended for homelab)

1. Copy env file:

```bash
cp .env.example .env
```

2. Make sure the external Docker network for Nginx Proxy Manager exists:

```bash
docker network create npm_proxy
```

3. Build and start:

```bash
docker compose up -d --build
```

4. Open `http://<docker-host>:8000` and run **Sync from RSS** once.

### Nginx Proxy Manager integration notes

- **Automatic pickup:** Nginx Proxy Manager does **not** auto-discover apps like Traefik. You still create a Proxy Host manually in NPM.
- This compose file attaches the app to an external `npm_proxy` network so NPM can route to it by container name.
- In NPM, set:
  - **Forward Hostname / IP:** `destiny-support-report`
  - **Forward Port:** `8000`
  - Then assign your domain + SSL cert in NPM as usual.

The persistent SQLite DB is stored on the host at `./data/data.sqlite3` via bind mount.

## Notes / future ideas

- Pair specific patch to nearest previous TWID by date (instead of latest/latest).
- Add RSS polling scheduler (cron or APScheduler).
- Add full-text search (SQLite FTS5).
- Detect edits over time by diffing article snapshots.
