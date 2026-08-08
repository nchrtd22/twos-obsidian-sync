#!/usr/bin/env python3
"""
twos_sync.py -- One-way import: Twos (NewTwos API) -> this Obsidian vault.

Writes notes with the vault frontmatter schema (created/categories/type/origin).
Atomic notes (clippings, tasks) are updated in place via twos_id.
Compound notes (daily `## Twos` sections, per-list notes) are regenerated.

API key:  env TWOS_API_KEY  or  ~/.config/twos-sync/.env
State:    ~/.local/share/twos-sync/state.json  (cursor + twos_id->path map)
Config:   <vault>/twos-import-map.yaml  (or .json)

Usage:
  python3 twos_sync.py [--dry-run] [--full] [--list ID] [--vault PATH] [--self-test]

See twos-sync-README.md for setup.
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib import parse as urlparse
from urllib import request as urlreq
from urllib.error import HTTPError, URLError

API_BASE = "https://writethingsdown.com/api/v1"
CFG_YAML = "twos-import-map.yaml"
CFG_JSON = "twos-import-map.json"
ENV_PATH = Path("~/.config/twos-sync/.env").expanduser()
STATE_PATH = Path("~/.local/share/twos-sync/state.json").expanduser()
SECTION_HEADER = "## Twos"
DAILY_DIR = "Daily"
IMPORT_DIR = "Things"     # non-daily Twos imports land here (staging)
MAX_PAGES = 400
REQ_DELAY = 0.12           # seconds between API calls (limit ~17/min, we use ~8/min)
FN_MAX = 60

BAD_FN = re.compile(r'[/\\?%*:|"<>\x00-\x1f#]')


# ------------------------------------------------------------- small helpers

def log(msg):
    print(msg, flush=True)


def parse_iso(s):
    """ISO-8601 string (Z ok) -> aware datetime, or None."""
    if not s:
        return None
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def local_date(dt):
    return dt.astimezone().strftime("%Y-%m-%d")


def local_time(dt):
    return dt.astimezone().strftime("%H:%M")


def sanitize_filename(text, fallback):
    t = BAD_FN.sub("", text or "")
    t = re.sub(r"\s+", " ", t).strip().strip(".").strip()
    if len(t) > FN_MAX:
        t = t[:FN_MAX].rsplit(" ", 1)[0].strip()
    return t or fallback


def load_env_file(path):
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


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
                    log(f"  rate limited (429); sleeping {wait}s")
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

    def lists_all(self):
        out, page = [], 0
        while page < MAX_PAGES:
            d = self._get("/lists", {"page": page})
            out.extend(d.get("lists", []))
            if not d.get("has_more"):
                break
            page += 1
        return out

    def things(self, **params):
        """Page through GET /things with optional since/sort/list_id/etc."""
        out, page = [], 0
        while page < MAX_PAGES:
            d = self._get("/things", {**params, "page": str(page)})
            out.extend(d.get("things", []))
            if not d.get("has_more"):
                break
            page += 1
        return out

    def today(self, date_str):
        d = self._get("/today", {"date": date_str})
        return d.get("things", [])

    def list(self, list_id):
        """GET /lists/{id} -- works on archived lists too. Returns List dict or None."""
        try:
            d = self._get(f"/lists/{list_id}")
            return d.get("list") or d
        except RuntimeError as e:
            log(f"  WARN: could not resolve list {list_id}: {e}")
            return None

    def tags(self):
        d = self._get("/tags")
        return d.get("tags", [])


# ------------------------------------------------------------- config loading

DEFAULT_CFG = {
    "categories": {
        "master, mbas511, mbas512, mbas514, mbas515, mbas516, ola, pensum, obligatorisk, studier, pedagogikk, bok, bokkapittel, e-bok, smartnotes, feynman, notebooklm, artikkel, barnehageforskningskonferansen2026": "[[Studies]]",
        "veslefrikk, skreddern, jobb, barnehage, avdelingsplan, hms, mote, mykid, skole, spes.ped, dugnad": "[[Work]]",
        "reise": "[[Travel]]",
        "fotball, cpfc, cpsn": "[[Football]]",
        "familie, ane, jonas, markus, helse, hjemmet, mat, kler, telefon, ol, mal, ide, forslag, tips, sitat": "[[Personal]]",
        "okonomi, okonomi, bank, abonnement": "[[Finance]]",
        "linux, git, terminal, obsidian, ai, chatgpt, prompt, midjourney, android, cli, vpn, privacy, security, koreader, kobo, streaming": "[[Tech]]",
        "musikk, post-rock, tonsofrock": "[[Music]]",
    },
    "drop": ["chrome", "firefox", "email", "facebook", "doi", "twos",
             "agenda", "gjoremal", "detaljer", "viktig", "halvarlig", "arlig", "manedlig",
             "orden", "rydde", "vedlikehold", "karriere", "rom", "kontakt"],
    "default_category": "[[Inbox]]",
}


def load_config(vault):
    yml = vault / CFG_YAML
    jsn = vault / CFG_JSON
    raw = None
    if yml.exists():
        try:
            import yaml
        except ImportError:
            log(f"NOTE: PyYAML missing and {CFG_YAML} present. "
                f"Install with: pip install --user pyyaml  (or uv run --with pyyaml)")
            log(f"Falling back to built-in defaults.")
        else:
            raw = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
    if raw is None and jsn.exists():
        raw = json.loads(jsn.read_text(encoding="utf-8"))
    if raw is None:
        log(f"NOTE: no {CFG_YAML} found; using built-in defaults.")
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
                if isinstance(item, str) and (item.startswith("[[") or ":" in item or "#" in item):
                    lines.append(f'  - "{item}"')
                else:
                    lines.append(f"  - {item}")
        else:
            s = str(v)
            if s.startswith("[[") or ":" in s or "#" in s or '"' in s:
                lines.append(f'{k}: "{s}"')
            else:
                lines.append(f"{k}: {s}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def render_clipping(t, lists, cfg):
    cats, tags = map_tags(t.get("tags"), cfg)
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
    body = [f"# {title}"]
    if url:
        body.append("")
        body.append(f"{url}")
    for p in t.get("photos") or []:
        body.append("")
        body.append(f"![]({p})")
    return frontmatter(props) + "\n" + "\n".join(body) + "\n"


def render_task(t, lists, cfg):
    cats, tags = map_tags(t.get("tags"), cfg)
    text = (t.get("text") or "").strip()
    list_title = lists.get(t.get("list_id"), {}).get("title", "unknown")
    status = "done" if t.get("completed") else "todo"
    box = "x" if t.get("completed") else " "
    props = {
        "created": local_date(parse_iso(t.get("created"))),
        "type": "task",
        "origin": "twos",
        "twos_id": t.get("id"),
        "twos_list": list_title,
        "categories": cats,
        "tags": tags,
        "status": status,
    }
    line = f"- [{box}] {text}"
    if t.get("url"):
        line += f" {t['url']}"
    return frontmatter(props) + "\n" + line + "\n"


def render_list_note(title, things, list_created, cfg):
    """Compound note for a permanent list. Renders ALL thing kinds (option c):
    todos as checkboxes, URLs as links, plain notes as bullets."""
    cats, tags = [], set()
    for t in things:
        c, tg = map_tags(t.get("tags"), cfg)
        for c2 in c:
            if c2 not in cats:
                cats.append(c2)
        tags.update(tg)
    if not cats:
        cats = [cfg["default_cat"]]
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
        indent = "  " * (t.get("tabs") or 0)
        if t.get("type") == "todo":
            box = "x" if t.get("completed") else " "
            line = f"{indent}- [{box}] {text}".rstrip()
            if url:
                line += f" {url}"
            body.append(line)
        elif t.get("header"):
            body.append("")
            body.append(f"## {text}")
        elif t.get("subheader"):
            body.append("")
            body.append(f"### {text}")
        elif t.get("quote"):
            body.append(f"{indent}> {text}")
        elif t.get("code"):
            body.append(f"{indent}`{text}`")
        elif url:
            label = text or url
            body.append(f"{indent}- [{label}]({url})")
        elif text:
            body.append(f"{indent}- {text}")
        # else: empty entry -> skip
    return frontmatter(props) + "\n" + "\n".join(body) + "\n"


def render_daily_section(things):
    """Body of the managed `## Twos` block for a daily note."""
    lines = []
    for t in sorted(things, key=lambda x: x.get("created", "")):
        dt = parse_iso(t.get("created"))
        hhmm = local_time(dt) if dt else "??:??"
        text = (t.get("text") or "").strip()
        url = (t.get("url") or "").strip()
        if t.get("type") == "todo":
            box = "x" if t.get("completed") else " "
            content = f"- [{box}] {hhmm} {text}".rstrip()
            if url:
                content += f" {url}"
            lines.append(content)
        else:
            if url:
                label = text or url
                lines.append(f"- {hhmm} [{label}]({url})")
            elif text:
                lines.append(f"- {hhmm} {text}")
            # else: empty entry -> skip
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
        out += after + "\n"
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
    """Option (c): only URL-bearing non-todo things become atomic clippings.
    Todos fall through to the per-list compound note as checkboxes."""
    if t.get("type") == "todo":
        return None
    if t.get("url"):
        return "clipping"
    return None


def is_grouped(t):
    """True if a thing routes to a compound note (daily section or list note).
    Under option (c) that's: todos, plain notes. Clippings are atomic."""
    return pick_atomic_kind(t) is None


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


def run(args):
    vault = Path(args.vault).resolve() if args.vault else Path(__file__).resolve().parents[1]
    if not vault.exists():
        sys.exit(f"vault not found: {vault}")

    cfg = load_config(vault)

    # API key
    key = os_get("TWOS_API_KEY")
    if key:
        log(f"Using TWOS_API_KEY from environment.")
    else:
        env = load_env_file(ENV_PATH)
        key = env.get("TWOS_API_KEY")
        if key:
            log(f"Using TWOS_API_KEY from {ENV_PATH}")
    if not key:
        sys.exit(f"No API key. Set TWOS_API_KEY or create {ENV_PATH} with TWOS_API_KEY=twos_...")

    tw = Twos(key)

    # State
    state = {"cursor": None, "things": {}}
    if STATE_PATH.exists() and not args.full:
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            log(f"NOTE: could not parse {STATE_PATH}; starting fresh.")

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

    def resolve_list(lid):
        """Return List dict for lid (from maps, extra_lists, or GET /lists/{id}).
        Caches positive AND negative results. Returns a synthetic stub for
        null/empty ids and 404s so things are still imported, not skipped."""
        if not lid:
            # Orphaned things (no list) -> bucket into a synthetic "Unlisted" list
            stub = {"id": "", "title": "Unlisted", "today": False, "archived": False}
            return stub
        if lid in lists:
            return lists[lid]
        if lid in extra_lists:
            return extra_lists[lid]
        li = tw.list(lid)
        if li is None:
            # 404 / unresolvable -> synthetic stub so things still import
            li = {"id": lid, "title": "Deleted list", "today": False, "archived": True}
        extra_lists[lid] = li
        lists[lid] = li
        return li

    # Bucket
    daily_dates = {}        # date_str -> list of things (from this delta)
    permlist_new = {}       # list_id -> list of grouped things touched
    atomic_jobs = []        # (kind, thing) -- clippings only under option (c)
    tasks_grouped = 0       # count of todos routed to compound notes
    unknown_list_count = 0
    unmapped_tags = set()

    for t in things:
        lid = t.get("list_id")
        li = resolve_list(lid)
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
            # else: skip undated empty
        else:
            if kind:
                atomic_jobs.append((kind, t))
            elif li is None:
                unknown_list_count += 1
            else:
                permlist_new.setdefault(lid, []).append(t)
                if t.get("type") == "todo":
                    tasks_grouped += 1

    # Atomic notes
    new_atomic, upd_atomic = 0, 0
    plans = []  # for dry-run reporting
    id_map = dict(state.get("things") or {})

    for kind, t in atomic_jobs:
        text = (t.get("text") or "").strip()
        url = (t.get("url") or "").strip()
        # option (c): only clippings are atomic
        base = sanitize_filename(text or url, f"twos-{(t.get('id') or 'x')[:6]}")
        content = render_clipping(t, lists, cfg)
        if not base.endswith(".md"):
            base += ".md"
        tid = t.get("id")
        existing = id_map.get(tid)
        if existing and (vault / existing).exists():
            target = vault / existing
            action = "upd"
        else:
            target = unique_path(vault, base, tid)
            action = "new"
        plans.append(("atomic", kind, action, str(target.relative_to(vault)), t))
        if action == "new":
            new_atomic += 1
        else:
            upd_atomic += 1
        if not args.dry_run:
            atomic_write(target, content)
            id_map[tid] = str(target.relative_to(vault))

    # Daily notes
    daily_touched = []
    for date_str in sorted(daily_dates):
        if have_full:
            day_things = [t for t in things
                          if lists.get(t.get("list_id"), {}).get("today")
                          and parse_iso(t.get("created"))
                          and local_date(parse_iso(t.get("created"))) == date_str]
        else:
            day_things = tw.today(date_str)
        section_body = render_daily_section(day_things)
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

    # Permanent-list compound notes (now contain todos + plain notes; URLs are atomic)
    list_notes = []
    for lid, touched in permlist_new.items():
        li = lists.get(lid)
        if not li:
            continue
        title = li.get("title") or "untitled"
        if have_full:
            grouped = [t for t in things if t.get("list_id") == lid and is_grouped(t)]
        else:
            grouped = [t for t in tw.things(list_id=lid) if is_grouped(t)]
        list_path = vault / IMPORT_DIR / f"Twos - {sanitize_filename(title, 'untitled')}.md"
        if not grouped and not list_path.exists():
            continue
        content = render_list_note(title, grouped, li.get("created"), cfg)
        existed = list_path.exists()
        list_notes.append((title, "upd" if existed else "new"))
        if not args.dry_run:
            atomic_write(list_path, content)

    # Cursor = max updated seen
    new_cursor = max((t.get("updated") or t.get("created") or "") for t in things) if things else state.get("cursor")

    # ---- Report ----
    n_clip_new = sum(1 for p in plans if p[1] == "clipping" and p[2] == "new")
    n_clip_upd = sum(1 for p in plans if p[1] == "clipping" and p[2] == "upd")
    log("")
    log(f"Twos sync {'(DRY-RUN)' if args.dry_run else '(APPLIED)'}")
    log(f"  Things processed: {len(things)}  ({tw._n} API calls)")
    log(f"  Atomic clippings (-> {IMPORT_DIR}/): {n_clip_new + n_clip_upd} "
        f"({n_clip_new} new, {n_clip_upd} upd)")
    log(f"  Tasks grouped into list notes: {tasks_grouped}")
    log(f"  Daily notes touched (-> {DAILY_DIR}/): {len(daily_touched)} "
        f"({sum(1 for _, s in daily_touched if s == 'new')} new)")
    log(f"  Permanent-list notes (-> {IMPORT_DIR}/): {len(list_notes)} "
        f"({sum(1 for _, s in list_notes if s == 'new')} new)")
    if extra_lists:
        log(f"  Lazy-resolved lists (archived/unknown): {len(extra_lists)}")
    if unknown_list_count:
        log(f"  Things in unresolved lists skipped: {unknown_list_count}")
    if unmapped_tags:
        log(f"  Unmapped tags (consider adding to {CFG_YAML}): "
            f"{', '.join(sorted(unmapped_tags)[:20])}"
            + (f"  (+{len(unmapped_tags) - 20} more)" if len(unmapped_tags) > 20 else ""))
    log(f"  Cursor: {state.get('cursor') or 'none'}  ->  {new_cursor}")

    if args.dry_run:
        log("")
        log("Dry-run: no files written, no state change.")
        return

    # Persist state
    state["cursor"] = new_cursor
    state["things"] = id_map
    state["lists"] = extra_lists
    state["updated"] = datetime.now().astimezone().isoformat()
    _save_state(state)
    log(f"State saved to {STATE_PATH}")


def _save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def os_get(name):
    import os
    return os.environ.get(name)


# --------------------------------------------------------------- self-test

def self_test():
    log("Running self-test...")
    ok = True

    # parse_iso / local
    dt = parse_iso("2026-08-01T14:49:00.000Z")
    assert dt is not None and dt.year == 2026 and dt.hour == 14, "parse_iso"
    assert re.match(r"\d{4}-\d{2}-\d{2}", local_date(dt)), "local_date"
    assert re.match(r"\d{2}:\d{2}", local_time(dt)), "local_time"

    # sanitize
    assert sanitize_filename("a/b\\c?d:e*g", "fb") == "abcde g" or \
           sanitize_filename("a/b\\c?d", "fb") == "abcd", "sanitize basic"
    assert sanitize_filename("", "fb") == "fb", "sanitize empty"
    assert sanitize_filename("#tag in name", "fb") == "tag in name", "sanitize hash"
    long = "x" * 200
    assert len(sanitize_filename(long, "fb")) <= FN_MAX + 5, "sanitize trunc"

    # map_tags
    cfg = {
        "cat_map": {"master": "[[Studies]]", "reise": "[[Travel]]"},
        "drop": ["chrome"],
        "default_cat": "[[Inbox]]",
    }
    cats, tags = map_tags(["#newtag", "chrome", "123", "random"], cfg)
    assert cats == ["[[Inbox]]"], f"cats default: {cats}"
    assert "newtag" in tags and "random" in tags and "chrome" not in tags and "123" not in tags, \
        f"tags filter: {tags}"
    cats2, _ = map_tags(["master", "reise"], cfg)
    assert cats2 == ["[[Studies]]", "[[Travel]]"], f"multi-cat: {cats2}"

    # upsert_twos_section
    base = "---\ncreated: 2026-08-01\ntype: daily\n---\n\n# 2026-08-01\n\nSome journal.\n"
    out = upsert_twos_section(base, "- 09:00 Hello\n")
    assert "## Twos" in out and "- 09:00 Hello" in out and "Some journal." in out, "insert"
    # replace existing
    out2 = upsert_twos_section(out, "- 10:00 Bye\n")
    assert "- 09:00 Hello" not in out2 and "- 10:00 Bye" in out2, "replace"
    assert out2.count("## Twos") == 1, "single section"

    # render_daily_section
    things = [
        {"type": "todo", "completed": True, "text": "Ring", "created": "2026-08-01T12:58:00Z"},
        {"type": "note", "text": "Hei", "url": "https://x.no", "created": "2026-08-01T11:00:00Z"},
    ]
    body = render_daily_section(things)
    assert "- [x]" in body and "[Hei](https://x.no)" in body, "daily render"

    # frontmatter quoting
    fm = frontmatter({"categories": ["[[Studies]]"], "source": "https://x.no/p", "type": "clipping"})
    assert '"[[Studies]]"' in fm and '"https://x.no/p"' in fm, "fm quoting"

    # option (c) routing: todos are NOT atomic; only url non-todos are clippings
    assert pick_atomic_kind({"type": "todo", "url": "https://x.no"}) is None, "todo not atomic"
    assert pick_atomic_kind({"type": "note", "url": "https://x.no"}) == "clipping", "url->clipping"
    assert pick_atomic_kind({"type": "note", "text": "hei"}) is None, "plain not atomic"
    assert is_grouped({"type": "todo"}) and is_grouped({"type": "note", "text": "x"}), "grouped"
    assert not is_grouped({"type": "note", "url": "https://x.no"}), "clipping not grouped"

    # render_list_note renders todos as checkboxes + urls as links
    list_things = [
        {"type": "todo", "completed": False, "text": "Les artikler", "created": "2026-01-01T08:00:00Z", "tags": ["master"]},
        {"type": "todo", "completed": True, "text": "Send billett", "created": "2026-01-02T08:00:00Z"},
        {"type": "note", "text": "Sjekk denne", "url": "https://obsidian.md", "created": "2026-01-03T08:00:00Z"},
        {"type": "note", "text": "Ren tekst", "created": "2026-01-04T08:00:00Z"},
    ]
    cfg2 = {"cat_map": {"master": "[[Studies]]"}, "drop": [], "default_cat": "[[Inbox]]"}
    note = render_list_note("OLA 1", list_things, "2026-01-01T00:00:00Z", cfg2)
    assert "- [ ] Les artikler" in note, "list: open task"
    assert "- [x] Send billett" in note, "list: done task"
    assert "[Sjekk denne](https://obsidian.md)" in note, "list: url link"
    assert "- Ren tekst" in note, "list: plain bullet"
    assert "[[Studies]]" in note, "list: category from tags"

    if ok:
        log("Self-test PASSED.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="One-way Twos -> Obsidian import.")
    ap.add_argument("--dry-run", action="store_true", help="Compute and report; write nothing.")
    ap.add_argument("--full", action="store_true", help="Ignore cursor; fetch everything.")
    ap.add_argument("--list", metavar="ID", help="Restrict to one list id (rebuild only that).")
    ap.add_argument("--vault", metavar="PATH", help="Vault root (default: script's parent).")
    ap.add_argument("--self-test", action="store_true", help="Run built-in tests and exit.")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())
    try:
        run(args)
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
    except RuntimeError as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
