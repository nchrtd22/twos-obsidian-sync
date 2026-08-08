# twos_sync.py — Twos → Obsidian one-way import

A Python script that pulls your Twos ("NewTwos") data via the official API and
writes it into an Obsidian vault as properly-frontmattered Markdown notes.
Idempotent and incremental: re-run as often as you like; it only pulls things
changed since the last run and updates notes in place via a stable `twos_id`.

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
| URL on a permanent list | `<title>.md` — `type: clipping`, `source: <url>` | `<vault>/Things/` |
| Task (todo) on a permanent list | `- [ ]`/`- [x]` line in the list's compound note | `<vault>/Things/Twos - <ListName>.md` |
| Plain note on a permanent list | `- text` bullet in the list's compound note | `<vault>/Things/Twos - <ListName>.md` |
| Anything on a daily ("today") list | `## Twos` section (replaced wholesale each run) | `<vault>/Daily/YYYY-MM-DD.md` |

**Routing:** URLs are atomic (searchable, Bases-friendly); tasks and plain
notes stay grouped inside their list's compound note, mirroring how Twos
itself shows a list. This keeps the vault navigable (~250 new files instead
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
  - "[[Tech]]"           # tag-mapped via config, else [[Inbox]]
tags:
  - obsidian
source: https://...      # clippings only
---
```

## Setup

### 1. Get an API key

In Twos: Settings → Developer / API → generate a **scoped key** with:

- `read:lists`
- `read:things`
- `search` (optional)

The key looks like `twos_<keyId>_<secret>`. Keep it private.

### 2. Store the key

```sh
mkdir -p ~/.config/twos-sync
printf 'TWOS_API_KEY=twos_YOURKEY\n' > ~/.config/twos-sync/.env
chmod 600 ~/.config/twos-sync/.env
```

Or export it in your shell: `export TWOS_API_KEY=twos_YOURKEY`

The script never writes the key into the vault.

### 3. Create a tag map (optional but recommended)

```sh
cp twos-import-map.example.yaml ~/.config/twos-sync/twos-import-map.yaml
# edit with your tags and category names
```

Without a config file, the script uses built-in defaults (generic categories)
and routes everything unmapped to `[[Inbox]]`.

### 4. Dependencies

- Python 3.10+
- PyYAML (for the tag map): `pip install --user pyyaml`
  — or run via uv: `uv run --with pyyaml twos_sync.py ...`

### 5. Download the script

```sh
curl -O https://raw.githubusercontent.com/nchrtd22/twos-obsidian-sync/main/twos_sync.py
chmod +x twos_sync.py
```

## Running

```sh
# Preview only — no files written, no state change. Always do this first.
python3 twos_sync.py --vault /path/to/your/vault --dry-run

# Real run (incremental; pulls only things changed since last run)
python3 twos_sync.py --vault /path/to/your/vault

# Force a full re-import (ignore cursor)
python3 twos_sync.py --vault /path/to/your/vault --full

# Rebuild one list only (doesn't advance cursor)
python3 twos_sync.py --vault /path/to/your/vault --list <list-id>

# Built-in tests (no API calls)
python3 twos_sync.py --self-test
```

### Flags

| Flag | Default | Purpose |
|---|---|---|
| `--vault PATH` | Script's parent dir | Obsidian vault root |
| `--config PATH` | `$XDG_CONFIG_HOME/twos-sync/twos-import-map.yaml` | Tag → category map |
| `--log-file PATH` | `$XDG_DATA_HOME/twos-sync/twos-sync.log` | Log file location |
| `--dry-run` | off | Preview only; no writes, no state change |
| `--full` | off | Ignore cursor; fetch everything |
| `--list ID` | — | Rebuild one list only |
| `--verbose` | off | Log per-note-write details (DEBUG level) |
| `--self-test` | off | Run built-in tests and exit |

### Shell alias (optional)

```sh
alias twos-sync='python3 ~/path/to/twos_sync.py --vault ~/path/to/your/vault'
```

Then just: `twos-sync --dry-run`

### Log file in the vault

By default logs go to `~/.local/share/twos-sync/twos-sync.log` (XDG data dir,
not in your vault). If you prefer logs inside the vault:

```sh
python3 twos_sync.py --vault ~/my-vault --log-file ~/my-vault/_logs/twos-sync.log
```

## Scheduling (optional)

The script is a one-shot command; "regularly" needs an external scheduler.

### cron (every hour at :10)

```cron
10 * * * * /usr/bin/python3 /path/to/twos_sync.py --vault /path/to/vault >> ~/.local/share/twos-sync/sync.log 2>&1
```

### systemd user timer

`~/.config/systemd/user/twos-sync.service`:
```ini
[Unit]
Description=Twos -> Obsidian sync

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 %h/path/to/twos_sync.py --vault %h/path/to/vault
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

| Path | Purpose | XDG |
|---|---|---|
| `~/.config/twos-sync/.env` | API key | config |
| `~/.config/twos-sync/twos-import-map.yaml` | Tag → category map | config |
| `~/.local/share/twos-sync/state.json` | Cursor + `twos_id` → path map | data |
| `~/.local/share/twos-sync/twos-sync.log` | Log file (rotating, 1 MB) | data |

All paths respect `XDG_CONFIG_HOME` and `XDG_DATA_HOME` environment variables.
The script never writes secrets, state, or logs into your vault (unless you
explicitly override `--log-file`).

## Caveats

- **One-way only.** Edits made in Obsidian are *not* pushed back to Twos.
- **Deletions don't propagate.** If you delete a thing in Twos, the note stays
  in Obsidian. A `--reconcile` flag may come later.
- **Daily sections regenerate from `/today`.** If a thing moves out of a daily
  list, the next run touching that date will drop it from the section.
- **Rate limit: 1000 req/hr.** The script throttles itself (~8 calls/min) and
  backs off for 45–180s on a 429. A full first import is the heaviest run.
- **Archived lists are lazy-resolved.** `GET /lists` excludes archived lists,
  so the script calls `GET /lists/{id}` on first encounter and caches the
  result in `state.json` for future runs.
- **`Things/` is a staging folder.** Non-daily Twos imports land here to keep
  root navigable. Pre-existing files in `Things/` are left untouched.
- **Deleted files in Obsidian reappear** if the thing is later edited in Twos
  or on a `--full` run. Delete in Twos (the source of truth) to prevent this.

## Troubleshooting

- **`No API key`** — set `TWOS_API_KEY` env var or create `~/.config/twos-sync/.env`.
- **HTTP 401/403** — key invalid or missing the `read:lists` / `read:things` scopes.
- **HTTP 429** — rate-limited; the script backs off automatically. If it keeps
  happening, schedule less frequently.
- **PyYAML missing** — `pip install --user pyyaml`, or run via
  `uv run --with pyyaml twos_sync.py ...`.
- **Daily note not getting a `## Twos` section** — check the date is right
  (the script uses your system timezone to bucket things by day).
- **Config not found** — the script defaults to `~/.config/twos-sync/twos-import-map.yaml`.
  Use `--config /path/to/your.yaml` to point elsewhere.

## License

MIT — see [LICENSE](LICENSE).
