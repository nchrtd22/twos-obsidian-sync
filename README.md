# twos_sync.py — Twos → Obsidian one-way import

A Python script that pulls your Twos ("NewTwos") data via the official API and
writes it into this vault as properly-frontmattered Markdown notes. Idempotent
and incremental: re-run as often as you like; it only pulls things changed
since the last run and updates notes in place via a stable `twos_id`.

> [!warning] Disclaimer
> This script writes, creates, and renames files in your Obsidian vault. It is
> provided **as-is, without warranty**, and you run it at your own risk. Always
> run `--dry-run` first to preview what will happen, and **back up your vault**
> before the first real run. The author is not responsible for data loss,
> duplicated files, or any other damage.

> [!note] Built with AI
> This script was written through collaboration with an AI assistant. The code has been tested against a real Twos account and Obsidian
> vault, but review it before relying on it.

## What it produces

| Twos entry | Lands as | Destination |
|---|---|---|
| URL on a permanent list | `<title>.md` — `type: clipping`, `source: <url>` | `Things/` |
| Task (todo) on a permanent list | `- [ ]`/`- [x]` line in the list's compound note | `Things/Twos - <ListName>.md` |
| Plain note on a permanent list | `- text` bullet in the list's compound note | `Things/Twos - <ListName>.md` |
| Anything on a daily ("today") list | `## Twos` section (replaced wholesale each run) | `Daily/YYYY-MM-DD.md` |

**Routing choice (option c):** URLs are atomic (searchable, Bases-friendly);
tasks and plain notes stay grouped inside their list's compound note, mirroring
how Twos itself shows a list. This keeps root navigable (~250 new files instead
of ~950) while preserving the part you'll actually search for later.

Every imported note carries:

```yaml
---
created: 2026-03-07
type: clipping           # or twos-list / daily
origin: twos
twos_id: "<stable id>"   # the dedup key (atomic notes only)
twos_list: Tech          # provenance
categories:
  - "[[Tech]]"           # tag-mapped via twos-import-map.yaml, else [[Inbox]]
tags:
  - obsidian
source: https://...      # clippings only
---
```

### Categories (9)

`[[Studies]]` `[[Work]]` `[[Travel]]` `[[Football]]` `[[Personal]]`
`[[Finance]]` `[[Tech]]` `[[Music]]` `[[Inbox]]` (default for unmapped)

## One-time setup

### 1. Get an API key

In Twos: Settings → Developer / API → generate a **scoped key** with these scopes:

- `read:lists`
- `read:things`
- `search` (optional, for future use)

The key looks like `twos_<keyId>_<secret>`. Keep it private.

### 2. Store the key (outside the vault)

```sh
mkdir -p ~/.config/twos-sync
printf 'TWOS_API_KEY=twos_YOURKEY\n' > ~/.config/twos-sync/.env
chmod 600 ~/.config/twos-sync/.env
```

Or export it in your shell:

```sh
export TWOS_API_KEY=twos_YOURKEY
```

The script never writes the key into the vault.

### 3. Dependencies

- Python 3.10+
- PyYAML (for the tag map): `pip install --user pyyaml`
  — or run via uv with it auto-provided: `uv run --with pyyaml _templater_scripts/twos_sync.py ...`

## Running

From the vault root:

```sh
# Preview only — no files written, no state change. Always do this first.
python3 _templater_scripts/twos_sync.py --dry-run

# Real run (incremental; pulls only things changed since last run)
python3 _templater_scripts/twos_sync.py

# Force a full re-import (ignore cursor)
python3 _templater_scripts/twos_sync.py --full

# Rebuild one list only
python3 _templater_scripts/twos_sync.py --list <list-id>

# Built-in tests (no API calls)
python3 _templater_scripts/twos_sync.py --self-test
```

### Dry-run output (example)

```
Mode: FULL (cursor=none, have_full=True)
Lists: 633 (386 daily, 247 permanent)
Fetched 5264 things since last run (134 API calls so far).

Twos sync (DRY-RUN)
  Things processed: 5264  (134 API calls)
  Atomic clippings (-> Things/): 77 (77 new, 0 upd)
  Tasks grouped into list notes: 770
  Daily notes touched (-> Daily/): 375 (20 new)
  Permanent-list notes (-> Things/): 196 (196 new)
  Lazy-resolved lists (archived/unknown): 15
  Unmapped tags (consider adding to twos-import-map.yaml): booking, billett, cup, handball  (+16 more)
  Cursor: none  ->  2026-08-08T04:55:03.668Z

Dry-run: no files written, no state change.
```

### After a dry run

- **Unmapped tags** — add any you care about to `twos-import-map.yaml` so they
  route to the right category. Unmapped tags still appear in each note's
  `tags:` list; they just don't set a `categories:` value.
- **Counts look sane** — run for real (drop `--dry-run`).

## Scheduling (optional)

The script is a one-shot command; "regularly" needs an external scheduler.

### cron (every hour at :10)

```cron
10 * * * * /usr/bin/python3 /path/to/vault/_templater_scripts/twos_sync.py >> ~/.local/share/twos-sync/sync.log 2>&1
```

### systemd user timer

`~/.config/systemd/user/twos-sync.service`:
```ini
[Unit]
Description=Twos -> Obsidian sync

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 %h/Dokumenter/obsidian-vault/_templater_scripts/twos_sync.py
```

`~/.config/systemd/user/twos-sync.timer`:
```ini
[Unit]
Description=Run Twos sync hourly

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now twos-sync.timer
journalctl --user -u twos-sync -f
```

## Where things live

| Path | Purpose | In vault? |
|---|---|---|
| `_templater_scripts/twos_sync.py` | The script | Yes (versioned with vault) |
| `twos-import-map.yaml` | Tag → category map | Yes (edit freely) |
| `~/.config/twos-sync/.env` | API key | **No** (secret, never syncs) |
| `~/.local/share/twos-sync/state.json` | Cursor + `twos_id` → path map | **No** (state) |

## Caveats (v1)

- **One-way only.** Edits made in Obsidian are *not* pushed back to Twos.
- **Deletions don't propagate.** If you delete a thing in Twos, the note stays
  in Obsidian. A `--reconcile` flag may come later.
- **Daily sections regenerate from `/today`.** If a thing moves out of a daily
  list, the next run touching that date will drop it from the section.
- **Rate limit: 1000 req/hr.** The script throttles itself (~8 calls/min) and
  backs off for 45–180s on a 429. A full first import is the heaviest run.
- **`page` + `since` interaction** in the API isn't explicitly documented; the
  script trusts it and caps at 400 pages as a safety net.
- **Archived lists are lazy-resolved.** `GET /lists` excludes archived lists,
  so the script calls `GET /lists/{id}` on first encounter and caches the
  result in `state.json` for future runs. This recovers things in archived
  lists that would otherwise be skipped.
- **`Things/` is a staging folder.** Non-daily Twos imports land here to keep
  root navigable. Pre-existing files in `Things/` are left untouched (the
  script only writes files it owns via `twos_id`).

## Troubleshooting

- **`No API key`** — set `TWOS_API_KEY` env var or create `~/.config/twos-sync/.env`.
- **HTTP 401/403** — key invalid or missing the `read:lists` / `read:things` scopes.
- **HTTP 429** — rate-limited; the script backs off automatically. If it keeps
  happening, schedule less frequently or run `--full` only when needed.
- **PyYAML missing** — `pip install --user pyyaml`, or run via
  `uv run --with pyyaml _templater_scripts/twos_sync.py ...`.
- **Daily note not getting a `## Twos` section** — check the date is right
  (the script uses your system timezone to bucket things by day).
