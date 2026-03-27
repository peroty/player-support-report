# Destiny 2 TWID ↔ Patch Notes Tracker

A self-hosted Flask app that:

- Pulls Bungie News from RSS.
- Resolves RSS item links (including relative `/7/en/...` links) to full Bungie URLs.
- Fetches each linked article page and stores the full HTML locally.
- Archives TWIDs and Destiny patch notes in SQLite.
- Keeps a versioned snapshot every time RSS sync runs.
- Extracts list-style change items.
- Compares latest patch notes vs latest TWID with fuzzy matching.
- Strikethroughs patch notes that were already teased in the TWID.
- Lets you search full text mentions, including quoted phrases.
- Stores and displays parsed markdown-style body text for each article (headings/lists/paragraphs; media omitted).
- Detects tease sections more flexibly (e.g., Sandbox/Balance/Class/Changes headings, not just exact \"Patch Preview\").

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
3. Optionally use `/search?q=warlock,titan,"sweet business"`.
4. Use **View parsed text** links (or `/article/<id>`) to read full TWID/Patch text in markdown-like format.

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

2. Build and start:

```bash
docker compose up -d --build
```

3. Open `http://<docker-host>:7777` and run **Sync from RSS** once.
   (container listens on 8000, host exposes 7777)

The persistent SQLite DB is stored on the host at `./data/data.sqlite3` via bind mount.
Application logs are persisted under `./data/logs/app.log`.

## Fast update (accept incoming remote changes without manual conflict review)

If you want your local checkout to exactly match `origin/main` and skip merge-conflict review:

```bash
cd /home/peroty/docker/player-support-report
git fetch origin
git reset --hard origin/main
docker compose up -d --build
```

This discards any uncommitted local changes in that repo directory.

### If you also want to refresh a PR branch and force it to latest `origin/main`

If your local PR branch (for example `work`) does not exist yet, recreate it from `origin/main` and push:

```bash
cd /home/peroty/docker/player-support-report
git fetch origin
git checkout -B work origin/main
git push --force-with-lease origin work
```

This is useful when you want a clean branch state with no manual conflict resolution.

## Troubleshooting sync

- Use the in-app **Logs** page (`/logs`) to inspect `INFO`, `WARNING`, and `ERROR` events.
- After clicking **Sync from RSS**, the home page now displays imported / unchanged / skipped / failed counts.
- `skipped` is normally duplicates seen across paginated RSS pages (same slug) or entries missing links.
- Use each article’s **Versions** link to inspect stored snapshot history and change markers.
- The sync pipeline follows RSS pagination via `atom:link rel=\"next\"` for up to `RSS_MAX_PAGES`.
- If RSS fetch fails, check `/logs` for HTTP status/body errors from Bungie and verify `BUNGIE_RSS_URL`.

### Docker healthcheck troubleshooting

If Docker reports `unhealthy`, run:

```bash
docker ps
docker logs player-support-report
docker inspect --format='{{json .State.Health}}' player-support-report
```

The container healthcheck calls `http://127.0.0.1:8000/healthz` from inside the container.

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
