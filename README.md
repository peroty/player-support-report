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

## Notes / future ideas

- Pair specific patch to nearest previous TWID by date (instead of latest/latest).
- Add RSS polling scheduler (cron or APScheduler).
- Add full-text search (SQLite FTS5).
- Detect edits over time by diffing article snapshots.
