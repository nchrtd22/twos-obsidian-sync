#!/usr/bin/env python3
"""
twos_sync.py -- One-way import: Twos (NewTwos API) -> this Obsidian vault.

Writes notes with the vault frontmatter schema (created/categories/type/origin).
Atomic notes (clippings, tasks) are updated in place via twos_id.
Compound notes (daily `## Twos` sections, per-list notes) are regenerated.

API key:  env TWOS_API_KEY  or  $XDG_CONFIG_HOME/twos-sync/.env
State:    $XDG_DATA_HOME/twos-sync/state.json
Config:   $XDG_CONFIG_HOME/twos-sync/twos-import-map.yaml (--config to override)
Log:      $XDG_DATA_HOME/twos-sync/twos-sync.log (--log-file to override)

Usage:
  python3 twos_sync.py [--vault PATH] [--dry-run] [--full] [--list ID]
                       [--config PATH] [--log-file PATH] [--verbose] [--self-test]

See README.md for setup.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib import parse as urlparse
from urllib import request as urlreq
from urllib.error import HTTPError, URLError

API_BASE = "https://www.twosapp.com/api/v1"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "twos-sync"
STATE_DIR = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser() / "twos-sync"
ENV_PATH = CONFIG_DIR / ".env"
STATE_PATH = STATE_DIR / "state.json"
DEFAULT_LOG_PATH = STATE_DIR / "twos-sync.log"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "twos-import-map.yaml"
SECTION_HEADER = "## Twos"
DAILY_DIR = "Daily"
IMPORT_DIR = "Things"     # non-daily Twos imports land here (staging)
MAX_PAGES = 400
REQ_DELAY = 0.12           # seconds between API calls (limit ~17/min, we use ~8/min)
FN_MAX = 60

BAD_FN = re.compile(r'[/\\?%*:|"<>\x00-\x1f#]')

# Will be configured by setup_logging()
_logger = logging.getLogger("twos_sync")


# ------------------------------------------------------------- small helpers

def log(msg, level=logging.INFO):
    """Write to both stdout and the file logger."""
    _logger.log(level, msg)


def setup_logging(log_path, verbose=False):
    """Configure dual-output logging: stdout + rotating file."""
    log_path.parent.mkdir(parents=True, exist_ok=True)

    _logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    _logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    fh.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh.setFormatter(fmt)
    _logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.setFormatter(fmt)
    _logger.addHandler(sh)

    # Suppress lastResort handler so errors don't double-print
    _logger.propagate = False

    return _logger


def parse_iso(s):
    """ISO-8601 string (Z ok) -> aware datetime, or None."""
    if not s:
        return None
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def local_date(dt):
    return dt.astimezone().strftime("%Y-%m-%d") if dt else ""


def local_time(dt):
    return dt.astimezone().strftime("%H:%M") if dt else ""


def sanitize_filename(text, fallback):
    t = text or ""
    # Truncate at first ':' or '|' — common in page titles like "Site: Page"
    for sep in (":", "|"):
        if sep in t:
            t = t.split(sep, 1)[0]
    # Replace '/' with '-' instead of stripping (preserves readability: repo/path -> repo-path)
    t = BAD_FN.sub("", t.replace("/", "-"))
    t = re.sub(r"\s+", " ", t).strip().strip(".").strip()
    if len(t) > FN_MAX:
        t = t[:FN_MAX].rsplit(" ", 1)[0].strip()
    return t or fallback


def load_env_key(path, key_name):
    """Read a single KEY=value from an env-style file. Returns None if absent."""
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{key_name}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


# ----------------------------------------------------------------- API client

class Twos:
    def __init__(self, key):
        self.key = key
        self._n = 0

    def _get(self, path, params=None):
        url = API_BASE + path
        if params:
            qs = {k: v for k, v in params.items() if v is not None}
            if qs:
                url += "?" + urlparse.urlencode(qs)
        req = urlreq.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Accept": "application/json",
            },
        )
        for attempt in range(5):
            time.sleep(REQ_DELAY)
            try:
                self._n += 1
                with urlreq.urlopen(req, timeout=45) as r:
                    return json.loads(r.read().decode("utf-8"))
            except HTTPError as e:
                if e.code == 429 and attempt < 4:
                    wait = 45 * (attempt + 1)
                    log(f"Rate limited (429); sleeping {wait}s", logging.WARNING)
                    time.sleep(wait)
                    continue
                body = ""
                try:
                    body = e.read().decode("utf-8", "ignore")[:300]
                except Exception:
                    pass
                raise RuntimeError(f"HTTP {e.code} {e.reason} -- {body}") from e
            except URLError as e:
                if attempt < 4:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
        raise RuntimeError("repeatedly rate-limited; aborting")

    def _paged(self, path, key, **params):
        """Generic paginated fetch. Yields items from `key` until has_more is false."""
        out, page = [], 0
        while page < MAX_PAGES:
            d = self._get(path, {**params, "page": str(page)})
            out.extend(d.get(key, []))
            if not d.get("has_more"):
                break
            page += 1
        return out

    def lists_all(self):
        return self._paged("/lists", "lists")

    def things(self, **params):
        """Page through GET /things with optional since/sort/list_id/etc."""
        return self._paged("/things", "things", **params)

    def today(self, date_str):
        d = self._get("/today", {"date": date_str})
        return d.get("things", [])

    def list(self, list_id):
        """GET /lists/{id} -- works on archived lists too. Returns List dict or None."""
        try:
            d = self._get(f"/lists/{list_id}")
            return d.get("list") or d
        except RuntimeError:
            return None


# ------------------------------------------------------------- config loading

DEFAULT_CFG = {
    "categories": {
        "work, project, task": "[[Work]]",
        "travel, trip": "[[Travel]]",
        "tech, code, linux": "[[Tech]]",
        "personal, home, family": "[[Personal]]",
        "study, reading, book": "[[Studies]]",
    },
    "drop": ["chrome", "firefox", "email", "twos"],
    "default_category": "[[Inbox]]",
}


def load_config(config_path):
    if config_path.exists():
        try:
            import yaml
        except ImportError:
            log(f"NOTE: PyYAML missing and {config_path} present. "
                f"Install with: pip install --user pyyaml  (or uv run --with pyyaml)")
            log("Falling back to built-in defaults.")
            raw = DEFAULT_CFG
        else:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    else:
        log(f"NOTE: no config at {config_path}; using built-in defaults.")
        raw = DEFAULT_CFG

    cat_map = {}
    for key, cat in (raw.get("categories") or {}).items():
        for tg in str(key).split(","):
            tg = tg.strip().lower()
            if tg:
                cat_map[tg] = cat
    drop = {t.lower() for t in (raw.get("drop") or [])}
    default_cat = raw.get("default_category") or "[[Inbox]]"
    return {"cat_map": cat_map, "drop": drop, "default_cat": default_cat}


def map_tags(tags, cfg):
    """Return (categories list, tags list). Keeps unmapped tags too."""
    cats, out = [], []
    for t in tags or []:
        n = (t or "").strip().lstrip("#").strip().lower()
        if not n or n in cfg["drop"] or n.isdigit():
            continue
        if n not in out:
            out.append(n)
        c = cfg["cat_map"].get(n)
        if c and c not in cats:
            cats.append(c)
    if not cats:
        cats = [cfg["default_cat"]]
    return cats, out


# --------------------------------------------------------------- file writers

def _quote(value):
    """Quote a YAML scalar if it needs quoting."""
    s = str(value)
    if s.startswith("[[") or ":" in s or "#" in s or '"' in s:
        return f'"{s}"'
    return s


def frontmatter(props):
    """Serialize an ordered dict of props to a YAML frontmatter block."""
    lines = ["---"]
    for k, v in props.items():
        if v is None or v == []:
            continue
        if isinstance(v, bool):
            lines.append(f"{k}: {str(v).lower()}")
        elif isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {_quote(item)}")
        else:
            lines.append(f"{k}: {_quote(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _aggregate_tags(things, cfg):
    """Map all tags across a list of things, aggregating categories and tags."""
    cats, tags = [], set()
    for t in things:
        c, tg = map_tags(t.get("tags"), cfg)
        for c2 in c:
            if c2 not in cats:
                cats.append(c2)
        tags.update(tg)
    if not cats:
        cats = [cfg["default_cat"]]
    return cats, sorted(tags)


def _apply_formatting(text, t):
    """Apply bold, italic, underline formatting to text based on thing's formatting flags.
    Order: bold -> italic -> underline (innermost to outermost for proper nesting)."""
    if not text:
        return text
    # Apply in order: bold, then italic, then underline
    # This creates **_<u>text</u>_** for all three
    if t.get("bold"):
        text = f"**{text}**"
    if t.get("italic"):
        text = f"_{text}_"
    if t.get("underline"):
        text = f"<u>{text}</u>"
    return text


def _get_list_name(list_id, lists):
    """Get list name from lists dict, or return the ID if not found."""
    if not list_id:
        return ""
    list_info = lists.get(list_id, {})
    return list_info.get("title", list_id)


def render_clipping(t, lists, cfg):
    cats, tags = _aggregate_tags([t], cfg)
    text = (t.get("text") or "").strip()
    url = (t.get("url") or "").strip()
    title = text or url
    list_title = lists.get(t.get("list_id"), {}).get("title", "unknown")
    props = {
        "created": local_date(parse_iso(t.get("created"))),
        "type": "clipping",
        "origin": "twos",
        "twos_id": t.get("id"),
        "twos_list": list_title,
        "categories": cats,
        "tags": tags,
        "source": url,
    }
    # Add optional fields if present
    if t.get("list_ref"):
        props["twos_list_ref"] = t["list_ref"]
    if t.get("favorited"):
        props["favorited"] = True
    if t.get("clone_of"):
        props["twos_clone_of"] = t["clone_of"]
    if t.get("has_clones"):
        props["twos_has_clones"] = True
    
    body = [f"# {_apply_formatting(title, t)}"]
    if url:
        body.append("")
        body.append(f"{url}")
    for p in t.get("photos") or []:
        body.append("")
        body.append(f"![]({p})")
    # Add the long-form note if present
    if t.get("note"):
        body.append("")
        body.append("")
        body.append(t["note"].strip())
    return frontmatter(props) + "\n" + "\n".join(body) + "\n"


def render_list_note(title, things, list_created, cfg, lists=None):
    """Compound note for a permanent list. Renders ALL thing kinds (option c):
    todos as checkboxes, URLs as links, plain notes as bullets.
    
    Args:
        title: List title
        things: List of thing dicts to render
        list_created: ISO datetime string for list creation
        cfg: Config dict with cat_map, drop, default_cat
        lists: Optional dict of list_id -> list_info for resolving list_ref names
    """
    if lists is None:
        lists = {}
    cats, tags = _aggregate_tags(things, cfg)
    props = {
        "created": local_date(parse_iso(list_created)) if list_created else None,
        "type": "twos-list",
        "origin": "twos",
        "twos_list": title,
        "categories": cats,
    }
    body = [f"# Twos - {title}", ""]
    for t in sorted(things, key=lambda x: x.get("created", "")):
        text = (t.get("text") or "").strip()
        url = (t.get("url") or "").strip()
        # Apply formatting to text
        text = _apply_formatting(text, t)
        # Limit tabs to reasonable range (0-5 levels)
        indent = "  " * max(0, min(5, t.get("tabs") or 0))
        
        # Add favorited marker if present
        fav_marker = "⭐ " if t.get("favorited") else ""
        
        if t.get("type") == "todo":
            box = "x" if t.get("completed") else " "
            line = f"{indent}- [{box}] {fav_marker}{text}".rstrip()
            if url:
                line += f" {url}"
            if t.get("canceled"):
                line = f"~~{line}~~"
            body.append(line)
        elif t.get("header"):
            body.append("")
            body.append(f"## {fav_marker}{text}")
        elif t.get("subheader"):
            body.append("")
            body.append(f"### {fav_marker}{text}")
        elif t.get("quote"):
            body.append(f"{indent}> {fav_marker}{text}")
        elif t.get("code"):
            body.append(f"{indent}`{fav_marker}{text}`")
        elif url:
            label = text or url
            body.append(f"{indent}- {fav_marker}[{label}]({url})")
        elif text:
            body.append(f"{indent}- {fav_marker}{text}")
        # else: empty entry -> skip
        
        # Add photos if present
        for p in t.get("photos") or []:
            body.append(f"{indent}  ![{text or 'image'}]({p})")
        
        # Add note if present
        if t.get("note"):
            note_text = t["note"].strip()
            if note_text:
                body.append(f"{indent}  > {note_text}")
        
        # Add list_ref as wikilink if present
        if t.get("list_ref"):
            ref_name = _get_list_name(t["list_ref"], lists)
            if ref_name:
                body.append(f"{indent}  - [[{ref_name}]]")
        
        # Add clone information if present
        if t.get("clone_of"):
            body.append(f"{indent}  - *Cloned from: {t['clone_of']}*")
        if t.get("has_clones"):
            body.append(f"{indent}  - *Has clones*")
    return frontmatter(props) + "\n" + "\n".join(body) + "\n"


def render_daily_section(things, lists=None):
    """Body of the managed `## Twos` block for a daily note.
    
    Args:
        things: List of thing dicts to render
        lists: Optional dict of list_id -> list_info for resolving list_ref names
    """
    if lists is None:
        lists = {}
    lines = []
    for t in sorted(things, key=lambda x: x.get("created", "")):
        dt = parse_iso(t.get("created"))
        hhmm = local_time(dt) if dt else "??:??"
        text = (t.get("text") or "").strip()
        url = (t.get("url") or "").strip()
        # Apply formatting to text
        text = _apply_formatting(text, t)
        
        # Add favorited marker if present
        fav_marker = "⭐ " if t.get("favorited") else ""
        
        if t.get("type") == "todo":
            box = "x" if t.get("completed") else " "
            content = f"- [{box}] {hhmm} {fav_marker}{text}".rstrip()
            if url:
                content += f" {url}"
            if t.get("canceled"):
                content = f"~~{content}~~"
            lines.append(content)
        else:
            if url:
                label = text or url
                lines.append(f"- {hhmm} {fav_marker}[{label}]({url})")
            elif text:
                lines.append(f"- {hhmm} {fav_marker}{text}")
            # else: empty entry -> skip
        
        # Add photos if present
        for p in t.get("photos") or []:
            lines.append(f"  ![{text or 'image'}]({p})")
        
        # Add note if present
        if t.get("note"):
            note_text = t["note"].strip()
            if note_text:
                lines.append(f"  > {note_text}")
        
        # Add list_ref as wikilink if present
        if t.get("list_ref") and lists:
            ref_name = _get_list_name(t["list_ref"], lists)
            if ref_name:
                lines.append(f"  - [[{ref_name}]]")
    return "\n".join(lines) + "\n"


def upsert_twos_section(content, section_body):
    """Insert or replace the managed `## Twos` section.

    The managed block runs from `## Twos` to the next `## ` heading or EOF.
    Everything outside is preserved verbatim.
    """
    if not section_body.strip():
        # Nothing to show; leave existing content untouched.
        return content
    section = f"{SECTION_HEADER}\n\n{section_body.rstrip()}\n"
    lines = content.split("\n")
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == SECTION_HEADER:
            start = i
            break
    if start is None:
        return (content.rstrip() + "\n\n" + section).rstrip() + "\n"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    before = "\n".join(lines[:start]).rstrip()
    after = "\n".join(lines[end:]).strip("\n")
    out = before + "\n\n" + section
    if after:
        out += "\n" + after + "\n"
    return out


def new_daily_note(date_str):
    props = {
        "created": date_str,
        "type": "daily",
        "tags": ["daily"],
    }
    return frontmatter(props) + f"\n# {date_str}\n"


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------- main pipeline

def pick_atomic_kind(t):
    """Option (c): only URL-bearing non-todo/dash/number/bullet things become atomic clippings.
    Todos, dash, number, bullet fall through to the per-list compound note."""
    thing_type = t.get("type", "none")
    if thing_type in ("todo", "dash", "number", "bullet"):
        return None
    if t.get("url"):
        return "clipping"
    return None


def unique_path(vault, name, twos_id):
    """Resolve a filename under <vault>/Things/, avoiding collisions."""
    dest = vault / IMPORT_DIR
    dest.mkdir(parents=True, exist_ok=True)
    cand = dest / name
    if not cand.exists():
        return cand
    stem = Path(name).stem
    suffix = twos_id[:6] if twos_id else "x"
    cand2 = dest / f"{stem}-{suffix}.md"
    i = 2
    while cand2.exists():
        cand2 = dest / f"{stem}-{suffix}-{i}.md"
        i += 1
    return cand2


def resolve_list(lid, lists, extra_lists, tw, unresolved_lists):
    """Return List dict for lid (from maps, extra_lists, or GET /lists/{id}).
    Caches positive AND negative results. Returns a synthetic stub for
    null/empty ids and 404s so things are still imported, not skipped."""
    if not lid:
        if "unlisted" not in extra_lists:
            extra_lists["unlisted"] = {"id": "", "title": "Unlisted", "today": False, "archived": False}
        return extra_lists["unlisted"]
    if lid in lists:
        return lists[lid]
    if lid in extra_lists:
        return extra_lists[lid]
    li = tw.list(lid)
    if li is None:
        li = {"id": lid, "title": "Deleted list", "today": False, "archived": True}
        unresolved_lists.append(lid)
    extra_lists[lid] = li
    lists[lid] = li
    return li


def _now_iso():
    """Current UTC time in the same format the Twos API uses for timestamps."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _next_cursor(things, prev=None):
    """Next cursor: max updated (or created) across things, clamped to now.

    Twos can stamp things with a future `updated` (e.g. a thing moved to a
    future day). An unclamped cursor would leapfrog ahead of real time and
    blind every subsequent delta run until real time catches up.
    """
    cur = max((t.get("updated") or t.get("created") or "") for t in things) if things else (prev or None)
    now_iso = _now_iso()
    if cur and cur > now_iso:
        log(f"Cursor {cur} is in the future (future-dated thing); clamping to {now_iso}.", logging.WARNING)
        cur = now_iso
    return cur


def run(args):
    vault = Path(args.vault).resolve() if args.vault else Path(__file__).resolve().parents[1]
    if not vault.exists():
        sys.exit(f"vault not found: {vault}")

    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    log_path = Path(args.log_file) if args.log_file else DEFAULT_LOG_PATH

    setup_logging(log_path, verbose=args.verbose)
    log(f"## Sync started ({'dry-run' if args.dry_run else 'live'}, mode={'full' if args.full else 'delta'})")

    cfg = load_config(config_path)

    # API key
    key = os.environ.get("TWOS_API_KEY")
    if key:
        log(f"Using TWOS_API_KEY from environment.")
    else:
        key = load_env_key(ENV_PATH, "TWOS_API_KEY")
        if key:
            log(f"Using TWOS_API_KEY from {ENV_PATH}")
    if not key:
        sys.exit(f"No API key. Set TWOS_API_KEY or create {ENV_PATH} with TWOS_API_KEY=twos_...")

    tw = Twos(key)

    # State — always load (even in --full) to preserve the twos_id->path map for renames.
    # In --full mode we just null the cursor; in --list mode we keep everything and
    # skip cursor advancement at the end.
    state = {"cursor": None, "things": {}}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            log(f"NOTE: could not parse {STATE_PATH}; starting fresh.")

    # Sanitize a future-dated cursor on load so the fetch itself is unblinded
    # (state saved from this run forward will carry the clamped value).
    if state.get("cursor") and state["cursor"] > _now_iso():
        log(f"NOTE: state cursor {state['cursor']} is in the future; clamping to {_now_iso()}.", logging.WARNING)
        state["cursor"] = _now_iso()

    since = None if args.full else state.get("cursor")
    have_full = since is None and not args.list
    log(f"Mode: {'FULL' if since is None else 'delta'} "
        f"(cursor={since or 'none'}, have_full={have_full})")

    # Lists
    all_lists = tw.lists_all()
    lists = {l["id"]: l for l in all_lists}
    n_daily_lists = sum(1 for l in all_lists if l.get("today"))
    log(f"Lists: {len(all_lists)} ({n_daily_lists} daily, "
        f"{len(all_lists) - n_daily_lists} permanent)")

    # Things (delta or full)
    params = {}
    if since:
        params["since"] = since
        params["sort"] = "updated"
    if args.list:
        params["list_id"] = args.list
    things = tw.things(**params)
    log(f"Fetched {len(things)} things since last run ({tw._n} API calls so far).")

    if not things:
        log("Nothing new. Done.")
        if not args.dry_run:
            _save_state(state)
        return

    # Lazy-list resolution cache (unknown/archived lists) + restore from state
    extra_lists = dict(state.get("lists") or {})
    unresolved_lists = []

    # Bucket — also pre-group for O(1) lookups in full mode
    daily_dates = {}        # date_str -> list of things
    permlist_new = {}       # list_id -> list of things (grouped + clippings)
    atomic_jobs = []        # (kind, thing) -- clippings only
    tasks_grouped = 0
    unmapped_tags = set()

    for t in things:
        lid = t.get("list_id")
        li = resolve_list(lid, lists, extra_lists, tw, unresolved_lists)
        for tg in t.get("tags") or []:
            n = (tg or "").strip().lstrip("#").strip().lower()
            if n and not n.isdigit() and n not in cfg["drop"] and n not in cfg["cat_map"]:
                unmapped_tags.add(n)
        kind = pick_atomic_kind(t)
        if li and li.get("today"):
            dt = parse_iso(t.get("created"))
            if dt:
                daily_dates.setdefault(local_date(dt), []).append(t)
            elif kind:
                atomic_jobs.append((kind, t))
        else:
            if kind:
                atomic_jobs.append((kind, t))
                if li is not None:
                    permlist_new.setdefault(lid, []).append(t)
            elif li is not None:
                permlist_new.setdefault(lid, []).append(t)
                if t.get("type") == "todo":
                    tasks_grouped += 1

    # Atomic notes
    n_new, n_upd = 0, 0
    id_map = dict(state.get("things") or {})

    for kind, t in atomic_jobs:
        text = (t.get("text") or "").strip()
        url = (t.get("url") or "").strip()
        base = sanitize_filename(text or url, f"twos-{(t.get('id') or 'x')[:6]}")
        content = render_clipping(t, lists, cfg)
        if not base.endswith(".md"):
            base += ".md"
        tid = t.get("id")
        existing = id_map.get(tid)
        if existing and (vault / existing).exists():
            old_path = vault / existing
            desired = vault / IMPORT_DIR / base
            if old_path != desired and not desired.exists():
                if not args.dry_run:
                    old_path.rename(desired)
                    log(f"  renamed: {existing} -> {desired.relative_to(vault)}", logging.DEBUG)
                target = desired
            else:
                target = old_path
            action = "upd"
            n_upd += 1
        else:
            target = unique_path(vault, base, tid)
            action = "new"
            n_new += 1
        if not args.dry_run:
            atomic_write(target, content)
            id_map[tid] = str(target.relative_to(vault))
            log(f"  {'wrote' if action=='new' else 'updated'}: {target.relative_to(vault)}", logging.DEBUG)

    # Daily notes
    daily_touched = []
    for date_str in sorted(daily_dates):
        if have_full:
            day_things = daily_dates[date_str]
        else:
            day_things = tw.today(date_str)
        section_body = render_daily_section(day_things, lists)
        if not section_body.strip():
            continue
        path = vault / DAILY_DIR / f"{date_str}.md"
        existed = path.exists()
        if existed:
            content = path.read_text(encoding="utf-8")
        else:
            content = new_daily_note(date_str)
        new_content = upsert_twos_section(content, section_body)
        daily_touched.append((date_str, "upd" if existed else "new"))
        if not args.dry_run and new_content != content:
            atomic_write(path, new_content)
            log(f"  {'wrote' if not existed else 'updated'}: {path.relative_to(vault)}", logging.DEBUG)

    # Permanent-list compound notes (contain ALL things: todos, plain notes, URLs as links)
    list_notes = []
    for lid, touched in permlist_new.items():
        li = lists.get(lid)
        if not li:
            continue
        title = li.get("title") or "untitled"
        if have_full:
            all_in_list = permlist_new[lid]
        else:
            all_in_list = tw.things(list_id=lid)
        list_path = vault / IMPORT_DIR / f"Twos - {sanitize_filename(title, 'untitled')}.md"
        if not all_in_list and not list_path.exists():
            continue
        content = render_list_note(title, all_in_list, li.get("created"), cfg, lists)
        existed = list_path.exists()
        list_notes.append((title, "upd" if existed else "new"))
        if not args.dry_run:
            atomic_write(list_path, content)
            log(f"  {'wrote' if not existed else 'updated'}: {list_path.relative_to(vault)}", logging.DEBUG)

    # Cursor = max updated seen (skip in --list mode to avoid regression)
    if args.list:
        new_cursor = state.get("cursor")
    else:
        new_cursor = _next_cursor(things, state.get("cursor"))

    # ---- Report ----
    log(f"\nTwos sync {'(DRY-RUN)' if args.dry_run else '(APPLIED)'}")
    log(f"  Things processed: {len(things)}  ({tw._n} API calls)")
    log(f"  Atomic clippings (-> {IMPORT_DIR}/): {n_new + n_upd} ({n_new} new, {n_upd} upd)")
    log(f"  Tasks grouped into list notes: {tasks_grouped}")
    log(f"  Daily notes touched (-> {DAILY_DIR}/): {len(daily_touched)} "
        f"({sum(1 for _, s in daily_touched if s == 'new')} new)")
    log(f"  Permanent-list notes (-> {IMPORT_DIR}/): {len(list_notes)} "
        f"({sum(1 for _, s in list_notes if s == 'new')} new)")
    if extra_lists:
        log(f"  Lazy-resolved lists (archived/unknown): {len(extra_lists)}")
    if unresolved_lists:
        log(f"  Deleted/unresolvable lists (things imported as 'Deleted list'): {len(unresolved_lists)}", logging.WARNING)
    if unmapped_tags:
        log(f"  Unmapped tags (consider adding to {config_path}): "
            f"{', '.join(sorted(unmapped_tags)[:20])}"
            + (f"  (+{len(unmapped_tags) - 20} more)" if len(unmapped_tags) > 20 else ""),
            logging.WARNING)
    log(f"  Cursor: {state.get('cursor') or 'none'}  ->  {new_cursor}")

    if args.dry_run:
        log("Dry-run: no files written, no state change.")
        log("## Sync complete (dry-run)\n")
        return

    # Persist state (skip cursor advance in --list mode)
    state["cursor"] = new_cursor
    state["things"] = id_map
    state["lists"] = extra_lists
    state["updated"] = datetime.now().astimezone().isoformat()
    _save_state(state)
    log(f"State saved to {STATE_PATH}")
    log("## Sync complete\n")


def _save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------- self-test

def self_test():
    print("Running self-test...")

    # parse_iso / local
    dt = parse_iso("2026-08-01T14:49:00.000Z")
    assert dt is not None and dt.year == 2026 and dt.hour == 14, "parse_iso"
    assert re.match(r"\d{4}-\d{2}-\d{2}", local_date(dt)), "local_date"
    assert re.match(r"\d{2}:\d{2}", local_time(dt)), "local_time"

    # sanitize
    assert sanitize_filename("", "fb") == "fb", "sanitize empty"
    assert sanitize_filename("#tag in name", "fb") == "tag in name", "sanitize hash"
    assert sanitize_filename("site/repo-name: A description", "fb") == "site-repo-name", \
        f"sanitize slash/colon: {sanitize_filename('site/repo-name: A description', 'fb')}"
    assert sanitize_filename("site/page|extra", "fb") == "site-page", "sanitize pipe"
    long = "x" * 200
    assert len(sanitize_filename(long, "fb")) <= FN_MAX + 5, "sanitize trunc"

    # map_tags
    cfg = {
        "cat_map": {"study": "[[Studies]]", "travel": "[[Travel]]"},
        "drop": ["chrome"],
        "default_cat": "[[Inbox]]",
    }
    cats, tags = map_tags(["#newtag", "chrome", "123", "random"], cfg)
    assert cats == ["[[Inbox]]"], f"cats default: {cats}"
    assert "newtag" in tags and "random" in tags and "chrome" not in tags and "123" not in tags, \
        f"tags filter: {tags}"
    cats2, _ = map_tags(["study", "travel"], cfg)
    assert cats2 == ["[[Studies]]", "[[Travel]]"], f"multi-cat: {cats2}"

    # _aggregate_tags must return a list (not a set) so frontmatter renders it as YAML list, not a set repr
    _, agg_tags = _aggregate_tags([
        {"tags": ["tag1", "tag2"]},
        {"tags": ["tag1", "tag3"]},
    ], cfg)
    assert isinstance(agg_tags, list), f"aggregate tags should be list, got {type(agg_tags)}"
    assert agg_tags == ["tag1", "tag2", "tag3"], f"aggregate tags: {agg_tags}"

    # upsert_twos_section
    base = "---\ncreated: 2026-08-01\ntype: daily\n---\n\n# 2026-08-01\n\nSome journal.\n"
    out = upsert_twos_section(base, "- 09:00 Hello\n")
    assert "## Twos" in out and "- 09:00 Hello" in out and "Some journal." in out, "insert"
    out2 = upsert_twos_section(out, "- 10:00 Bye\n")
    assert "- 09:00 Hello" not in out2 and "- 10:00 Bye" in out2, "replace"
    assert out2.count("## Twos") == 1, "single section"
    # A following heading must be separated from the section body by a blank line
    with_after = upsert_twos_section("# Day\n\n## Twos\n\n- 09:00 old\n\n## Journal\n\nText.\n",
                                      "- 10:00 new\n")
    assert "\n\n## Journal\n" in with_after and "- 10:00 new\n##" not in with_after, "blank line after section"

    # render_daily_section
    things = [
        {"type": "todo", "completed": True, "text": "Call dad", "created": "2026-08-01T12:58:00Z"},
        {"type": "note", "text": "Hello", "url": "https://example.com", "created": "2026-08-01T11:00:00Z"},
        {"type": "todo", "completed": False, "text": "Important", "bold": True, "created": "2026-08-01T10:00:00Z"},
        {"type": "todo", "completed": True, "text": "Canceled", "canceled": True, "created": "2026-08-01T09:00:00Z"},
        {"type": "note", "text": "With note", "note": "Extended note here", "created": "2026-08-01T08:00:00Z"},
        {"type": "note", "text": "Favorited", "favorited": True, "created": "2026-08-01T07:00:00Z"},
        {"type": "note", "text": "With photo", "photos": ["https://example.com/photo.jpg"], "created": "2026-08-01T06:00:00Z"},
    ]
    lists = {"ref123": {"title": "Reference List"}}
    things_with_ref = things + [
        {"type": "note", "text": "With ref", "list_ref": "ref123", "created": "2026-08-01T05:00:00Z"}
    ]
    body = render_daily_section(things_with_ref, lists)
    assert "- [x]" in body and "[Hello](https://example.com)" in body, "daily render"
    assert "**Important**" in body, "daily bold formatting"
    assert "~~- [x]" in body and "Canceled~~" in body, "daily canceled strikethrough"
    assert "> Extended note here" in body, "daily note field included"
    assert "⭐ Favorited" in body, "daily favorited marker"
    assert "![With photo](https://example.com/photo.jpg)" in body, "daily photo included"
    assert "[[Reference List]]" in body, "daily list_ref wikilink"

    # frontmatter quoting
    fm = frontmatter({"categories": ["[[Studies]]"], "source": "https://example.com/p", "type": "clipping"})
    assert '"[[Studies]]"' in fm and '"https://example.com/p"' in fm, "fm quoting"
    
    # render_clipping with note field
    clipping = {
        "id": "test123",
        "text": "Test Article",
        "url": "https://example.com/article",
        "note": "This is a long-form note about the article",
        "type": "note",
        "created": "2026-08-01T10:00:00Z",
        "list_id": "list456",
        "tags": ["tech"],
    }
    lists = {"list456": {"title": "Tech"}}
    clipping_content = render_clipping(clipping, lists, cfg)
    assert "# Test Article" in clipping_content, "clipping title"
    assert "https://example.com/article" in clipping_content, "clipping url"
    assert "This is a long-form note about the article" in clipping_content, "clipping note field"
    
    # _apply_formatting helper
    assert _apply_formatting("hello", {"bold": True}) == "**hello**", "bold formatting"
    assert _apply_formatting("hello", {"italic": True}) == "_hello_", "italic formatting"
    assert _apply_formatting("hello", {"underline": True}) == "<u>hello</u>", "underline formatting"
    # Note: formatting is applied in order bold->italic->underline, so combined creates nested markdown
    assert _apply_formatting("hello", {"bold": True, "italic": True}) == "_**hello**_", "combined bold+italic"
    assert _apply_formatting("hello", {"bold": True, "underline": True}) == "<u>**hello**</u>", "combined bold+underline"
    assert _apply_formatting("", {"bold": True}) == "", "empty text formatting"
    assert _apply_formatting("hello", {}) == "hello", "no formatting"

    # load_config: missing config file -> built-in defaults
    cfg_dflt = load_config(Path("/nonexistent/twos-import-map.yaml"))
    assert cfg_dflt["default_cat"] == "[[Inbox]]" and cfg_dflt["cat_map"]["work"] == "[[Work]]", \
        "load_config defaults"

    # load_config: config file present but PyYAML missing -> must fall back to
    # built-in defaults, NOT crash (regression: raw was never assigned)
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tf:
        tf.write('categories:\n  zzz: "[[ZZZ]]"\n')
        tmp_cfg = Path(tf.name)
    yaml_mod = sys.modules.get("yaml")
    sys.modules["yaml"] = None  # forces `import yaml` to raise ImportError
    try:
        cfg_noyaml = load_config(tmp_cfg)
    finally:
        if yaml_mod is None:
            sys.modules.pop("yaml", None)
        else:
            sys.modules["yaml"] = yaml_mod
        tmp_cfg.unlink(missing_ok=True)
    assert cfg_noyaml["default_cat"] == "[[Inbox]]" and "zzz" not in cfg_noyaml["cat_map"], \
        "load_config falls back to defaults when PyYAML missing"

    # _next_cursor: max updated/created, clamped to now (Twos can stamp things
    # with a future `updated`, which used to blind subsequent delta runs)
    assert _next_cursor([], "2026-01-01T00:00:00.000Z") == "2026-01-01T00:00:00.000Z", "cursor: prev passthrough"
    assert _next_cursor([{"updated": "2026-01-02T00:00:00.000Z"},
                         {"created": "2026-01-03T00:00:00.000Z"}]) == "2026-01-03T00:00:00.000Z", \
        "cursor: max updated/created"
    clamped = _next_cursor([{"updated": "2099-01-01T00:00:00.000Z"}])
    dt_clamped = parse_iso(clamped)
    assert dt_clamped and abs((dt_clamped - datetime.now(timezone.utc)).total_seconds()) < 60, \
        f"cursor clamped to now: {clamped}"

    # option (c) routing: todos/dash/number/bullet are NOT atomic; only url non-todos are clippings
    assert pick_atomic_kind({"type": "todo", "url": "https://example.com"}) is None, "todo not atomic"
    assert pick_atomic_kind({"type": "dash", "url": "https://example.com"}) is None, "dash not atomic"
    assert pick_atomic_kind({"type": "number", "url": "https://example.com"}) is None, "number not atomic"
    assert pick_atomic_kind({"type": "bullet", "url": "https://example.com"}) is None, "bullet not atomic"
    assert pick_atomic_kind({"type": "note", "url": "https://example.com"}) == "clipping", "url->clipping"
    assert pick_atomic_kind({"type": "none", "text": "hello"}) is None, "plain not atomic"
    assert pick_atomic_kind({"type": "none", "url": "https://example.com"}) == "clipping", "none+url->clipping"

    # render_list_note renders todos as checkboxes + urls as links + formatting + notes
    list_things = [
        {"type": "todo", "completed": False, "text": "Read articles", "created": "2026-01-01T08:00:00Z", "tags": ["study"]},
        {"type": "todo", "completed": True, "text": "Send ticket", "created": "2026-01-02T08:00:00Z"},
        {"type": "note", "text": "Check this", "url": "https://obsidian.md", "created": "2026-01-03T08:00:00Z"},
        {"type": "note", "text": "Plain text", "created": "2026-01-04T08:00:00Z"},
        {"type": "todo", "completed": False, "text": "Important task", "bold": True, "created": "2026-01-05T08:00:00Z"},
        {"type": "todo", "completed": True, "text": "Canceled task", "canceled": True, "created": "2026-01-06T08:00:00Z"},
        {"type": "note", "text": "With note", "note": "This is a long note", "created": "2026-01-07T08:00:00Z"},
    ]
    note = render_list_note("Project A", list_things, "2026-01-01T00:00:00Z", cfg)
    assert "- [ ] Read articles" in note, "list: open task"
    assert "- [x] Send ticket" in note, "list: done task"
    assert "[Check this](https://obsidian.md)" in note, "list: url link"
    assert "- Plain text" in note, "list: plain bullet"
    assert "[[Studies]]" in note, "list: category from tags"
    assert "**Important task**" in note, "list: bold formatting"
    assert "~~- [x] Canceled task~~" in note, "list: canceled strikethrough"
    assert "> This is a long note" in note, "list: note field included"
    
    # Test favorited, photos, list_ref, clone info in list notes
    lists_for_test = {"list1": {"title": "Main List"}, "ref1": {"title": "Ref List"}}
    extended_things = [
        {"type": "todo", "completed": False, "text": "Starred task", "favorited": True, "created": "2026-01-01T08:00:00Z"},
        {"type": "note", "text": "With image", "photos": ["https://example.com/img.jpg"], "created": "2026-01-02T08:00:00Z"},
        {"type": "note", "text": "With reference", "list_ref": "ref1", "created": "2026-01-03T08:00:00Z"},
        {"type": "note", "text": "Cloned item", "clone_of": "orig123", "created": "2026-01-04T08:00:00Z"},
        {"type": "note", "text": "Has clones", "has_clones": True, "created": "2026-01-05T08:00:00Z"},
    ]
    extended_note = render_list_note("Extended", extended_things, "2026-01-01T00:00:00Z", cfg, lists_for_test)
    assert "⭐ Starred task" in extended_note, "list: favorited marker"
    assert "![With image](https://example.com/img.jpg)" in extended_note, "list: photo included"
    assert "[[Ref List]]" in extended_note, "list: list_ref wikilink"
    assert "*Cloned from: orig123*" in extended_note, "list: clone_of info"
    assert "*Has clones*" in extended_note, "list: has_clones info"

    print("Self-test PASSED.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="One-way Twos -> Obsidian import.")
    ap.add_argument("--dry-run", action="store_true", help="Compute and report; write nothing.")
    ap.add_argument("--full", action="store_true", help="Ignore cursor; fetch everything.")
    ap.add_argument("--list", metavar="ID", help="Restrict to one list id (rebuild only that).")
    ap.add_argument("--vault", metavar="PATH", help="Vault root (default: script's parent).")
    ap.add_argument("--config", metavar="PATH", help=f"Tag map YAML (default: {DEFAULT_CONFIG_PATH}).")
    ap.add_argument("--log-file", metavar="PATH", help=f"Log file (default: {DEFAULT_LOG_PATH}).")
    ap.add_argument("--verbose", action="store_true", help="Log per-note-write details.")
    ap.add_argument("--version", action="version", version="%(prog)s 1.0.0")
    ap.add_argument("--self-test", action="store_true", help="Run built-in tests and exit.")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        sys.exit(0)
    try:
        run(args)
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
    except RuntimeError as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
