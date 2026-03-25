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

Open http://localhost:7777 then:

1. Click **Sync from RSS**.
2. Click **Compare latest**.
3. Optionally use `/search?q=your-term`.

## Deploy in homelab

You can run this behind nginx, Caddy, or Traefik with Gunicorn:

```bash
gunicorn -w 2 -b 0.0.0.0:7777 app:app
```

Persist `data.sqlite3` in a volume/bind mount.

## Docker quick start (recommended for homelab)

1. Copy env file:

```bash
cp .env.example .env
```

2. Build and start:

```bash
docker compose up -d --build
```

3. Open `http://<docker-host>:7777` and run **Sync from RSS** once.

The persistent SQLite DB is stored on the host at `./data/data.sqlite3` via bind mount.
Application logs are persisted under `./data/logs/app.log`.

## Troubleshooting sync

- Use the in-app **Logs** page (`/logs`) to inspect `INFO`, `WARNING`, and `ERROR` events.
- After clicking **Sync from RSS**, the home page now displays imported / skipped / failed counts.

## Resolve merge conflicts automatically (accept all)

If you want to accept every conflict without manual review:

```bash
# keep your current branch version for every conflicted file
scripts/resolve_conflicts.sh ours

# OR keep incoming branch ("theirs") for every conflicted file
scripts/resolve_conflicts.sh theirs
```

Then finish the merge as normal:

```bash
git commit
```

## Notes / future ideas

- Pair specific patch to nearest previous TWID by date (instead of latest/latest).
- Add RSS polling scheduler (cron or APScheduler).
- Add full-text search (SQLite FTS5).
- Detect edits over time by diffing article snapshots.
