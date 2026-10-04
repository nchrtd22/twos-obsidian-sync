# AGENTS.md

Guide for AI agents working in this repository.

## Project Overview

Single-file Python script (`twos_sync.py`) that performs a one-way import from the Twos (NewTwos) API into an Obsidian vault as frontmattered Markdown notes. Idempotent and incremental — re-runs only pull things changed since the last run via a cursor.

No package structure, no build system, no CI. The entire codebase is one ~1000-line file plus a README and an example config.

## Commands

```sh
# Built-in tests (no API calls, no network) — run after every change
python3 twos_sync.py --self-test

# Dry run (preview, no writes, no state change)
python3 twos_sync.py --vault /path/to/vault --dry-run

# Real incremental run
python3 twos_sync.py --vault /path/to/vault

# Force full re-import (ignores cursor)
python3 twos_sync.py --vault /path/to/vault --full

# Rebuild a single list (does NOT advance cursor)
python3 twos_sync.py --vault /path/to/vault --list <list-id>
```

There is no test runner, linter, or formatter configured. The only test is `--self-test`, which runs inline assertions in `self_test()` and exits. Always run it after modifying the script.

## Dependencies

- Python 3.10+
- PyYAML (optional — only needed if a tag-map config file exists; falls back to `DEFAULT_CFG` if missing)
- Everything else is stdlib: `urllib`, `json`, `argparse`, `logging`, `re`, `pathlib`

No `requirements.txt`, `pyproject.toml`, or virtualenv. Run directly with `python3` or via `uv run --with pyyaml`.

## Architecture (top to bottom in twos_sync.py)

The file is organized in clearly delimited sections by comment banners:

1. **Helpers** (`log`, `setup_logging`, `parse_iso`, `local_date/time`, `sanitize_filename`, `load_env_key`) — small utilities. `sanitize_filename` has non-obvious logic: truncates at first `:` or `|`, replaces `/` with `-` (not stripped), caps at `FN_MAX` (60) chars breaking on last space.

2. **API client** (`class Twos`) — wraps the REST API at `https://www.twosapp.com/api/v1`. `_get` handles 429 retry with 45s×attempt backoff (5 attempts). `_paged` auto-paginates up to `MAX_PAGES` (400). Methods: `lists_all`, `things`, `today`, `list`.

3. **Config loading** (`load_config`, `map_tags`) — loads YAML tag→category map. `map_tags` returns `(categories, tags)` — categories are wikilink strings like `"[[Tech]]"`, tags are lowercased, `#`-stripped, deduped. Numeric-only tags and tags in `drop` are discarded. Unmapped tags still appear in `tags:` but default category is `[[Inbox]]`.

4. **File writers** (`frontmatter`, `render_clipping`, `render_list_note`, `render_daily_section`, `upsert_twos_section`, `new_daily_note`, `atomic_write`) — pure functions that produce Markdown content. `atomic_write` writes to `.tmp` then `replace()`s for crash safety. `upsert_twos_section` replaces the managed `## Twos` block in-place, preserving everything outside it.

5. **Main pipeline** (`pick_atomic_kind`, `unique_path`, `resolve_list`, `run`) — `run()` is the orchestrator: load state → fetch lists → fetch things (delta or full) → bucket into daily/permlist/atomic → write files → save state. `resolve_list` lazy-fetches archived lists via `GET /lists/{id}` and caches in `state["lists"]`.

6. **Self-test** (`self_test`) — inline assertion-based tests, no framework.

7. **Entry point** (`main`) — argparse + dispatch.

## Routing Logic (critical to understand)

`pick_atomic_kind(t)` decides whether a thing becomes an atomic note or stays in its list's compound note:

- Things of type `todo`, `dash`, `number`, `bullet` → **never atomic** (always grouped into compound list note)
- Things with a `url` field (and not the above types) → **atomic clipping** (own file in `Things/`)
- Everything else → grouped into compound list note

So a todo with a URL still goes into the list note, not its own file.

## Note Types and Destinations

| Twos entry | Destination file | `type:` frontmatter |
|---|---|---|
| URL-bearing non-todo | `<vault>/Things/<title>.md` | `clipping` |
| Task/note on permanent list | `<vault>/Things/Twos - <ListName>.md` (compound) | `twos-list` |
| Anything on a daily list | `## Twos` section in `<vault>/Daily/YYYY-MM-DD.md` | `daily` |

Daily sections are **replaced wholesale** each run. Compound list notes are **fully regenerated**. Only atomic clippings are updated in place (via `twos_id` → path map in state).

## State Management

`state.json` at `$XDG_DATA_HOME/twos-sync/state.json` (default `~/.local/share/twos-sync/`):

```json
{
  "cursor": "<ISO timestamp of last updated thing seen>",
  "things": {"<twos_id>": "<relative/path/in/vault>"},
  "lists": {"<list_id>": {"id":..., "title":..., "today":..., "archived":...}},
  "updated": "<ISO timestamp of last run>"
}
```

- `cursor` enables incremental fetch (`?since=<cursor>&sort=updated`)
- `things` map enables in-place updates and renames of atomic clippings
- `lists` caches archived/deleted list metadata to avoid repeated `GET /lists/{id}` calls
- `--full` nulls the cursor but **preserves** the `things` map (for rename tracking)
- `--list` mode skips cursor advancement entirely

## Gotchas

- **`_aggregate_tags` must return a `list`, not a `set`.** Returning a set breaks YAML frontmatter (renders as `set([...])`). This was a fixed bug — the self-test checks for it explicitly.

- **Daily section replacement is destructive within the section.** `upsert_twos_section` replaces everything between `## Twos` and the next `## ` heading (or EOF). If a thing moves off a daily list, the next run drops it. User content outside the section is preserved.

- **Archived lists are invisible to `GET /lists`.** The script must call `GET /lists/{id}` individually on first encounter. `resolve_list` caches both positive and negative (404 → synthetic "Deleted list" stub) results so things are still imported, never skipped.

- **`--vault` defaults to the script's parent directory**, not the current directory. This is `Path(__file__).resolve().parents[1]` — intentional for the single-file distribution model where the script sits inside the vault.

- **Deleted files in Obsidian reappear** if the thing is edited in Twos or on a `--full` run. Twos is the source of truth; delete there to prevent re-import.

- **Rate limiting is self-imposed.** `REQ_DELAY = 0.12s` between calls (~8/min, well under the 1000/hr limit). On 429, backs off 45s × attempt number. A full first import is the heaviest run.

- **Frontmatter quoting:** `_quote()` wraps values in double quotes if they contain `:`, `#`, `"`, or start with `[[`. Wikilink categories like `[[Tech]]` are always quoted.

- **Formatting pipeline:** `_apply_formatting` applies bold → italic → underline in that order (innermost to outermost). Combined bold+italic produces `_**text**_`, not `**_text_**`.

- **`Things/` is a staging folder.** Pre-existing files there are left untouched, but Twos-generated files may be created alongside them.

- **Config is optional.** If no YAML file exists, `DEFAULT_CFG` (hardcoded in the script) is used. If PyYAML is missing but a config file exists, it falls back to defaults with a warning.

- **XDG paths are respected.** `XDG_CONFIG_HOME` and `XDG_DATA_HOME` env vars override the default `~/.config/` and `~/.local/share/` prefixes.

## Conventions

- No external dependencies beyond stdlib + optional PyYAML. Keep it that way.
- Pure functions for rendering (`render_clipping`, `render_list_note`, `render_daily_section`) — easy to test in `self_test()`.
- All file writes go through `atomic_write` (tmp + rename).
- Logging via the module-level `_logger`, dual-output to stdout + rotating file (1MB, 1 backup). `log()` helper writes to both.
- Frontmatter is hand-serialized (not via a YAML library) to control ordering and quoting. Props are an ordered dict; `None` and empty list values are omitted.
- Thing dicts from the API use string fields (`"text"`, `"url"`, `"type"`, `"created"`, `"updated"`, `"id"`, `"list_id"`, `"tags"`, `"completed"`, `"canceled"`, `"bold"`, `"italic"`, `"underline"`, `"favorited"`, `"photos"`, `"note"`, `"list_ref"`, `"clone_of"`, `"has_clones"`, `"header"`, `"subheader"`, `"quote"`, `"code"`, `"tabs"`).

## Adding Tests

Tests live in `self_test()` near the bottom of the file. No framework — just `assert` statements and `print`. Add new assertions there, then run `python3 twos_sync.py --self-test`. The function returns 0 on success and raises `AssertionError` on failure.
