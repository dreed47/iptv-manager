"""
m3u_browser_index.py — fast, indexed search backend for the /m3u_browser page.

Builds a per-provider SQLite index (a plain table for group/prefix filtering, plus an
FTS5 virtual table for full-text name search) from the raw fetched M3U file, so
/m3u_browser_data can answer search/filter/paginate requests with indexed SQL queries
instead of re-parsing the entire (200K+ entry) file on every request.

Mirrors the fingerprint/rebuild-on-change caching pattern used for the Xtream catalog in
xtream_server_routes.py (_compute_fingerprint / get_xtream_cache / _build_cache), but
keyed on a single file per provider, with a per-item build lock instead of one global lock.
"""
import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass

import config

logger = logging.getLogger(__name__)

_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    tvg_name TEXT NOT NULL DEFAULT '',
    group_title TEXT NOT NULL DEFAULT '',
    prefix TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_channels_group_id  ON channels(group_title, id);
CREATE INDEX IF NOT EXISTS idx_channels_prefix_id ON channels(prefix, id);

CREATE VIRTUAL TABLE IF NOT EXISTS channels_fts USING fts5(
    name, tvg_name, content='channels', content_rowid='id'
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

MIN_PREFIX_COUNT = 5  # only treat a detected prefix as a real provider tag if it appears this often
_BATCH_SIZE = 2000
_BUILD_RETRY_COOLDOWN = 60.0


@dataclass
class BrowserIndex:
    fingerprint: tuple
    db_path: str
    groups: list
    prefixes: list
    row_count: int


_cache: dict[int, BrowserIndex] = {}
_locks: dict[int, asyncio.Lock] = {}
_build_failures: dict[int, float] = {}


def _get_lock(item_id: int) -> asyncio.Lock:
    lock = _locks.get(item_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[item_id] = lock
    return lock


def _compute_fingerprint(item_id: int) -> tuple:
    path = os.path.join(config.M3U_DIR, f"xtream_playlist_{item_id}.m3u")
    try:
        st = os.stat(path)
        return (path, st.st_mtime, st.st_size)
    except FileNotFoundError:
        return (path, 0, 0)


@contextmanager
def _index_conn(db_path: str):
    # timeout=30: retry internally on a transient lock (e.g. a rebuild's rename racing a
    # read) instead of immediately raising "database is locked" (sqlite3's default is 5s).
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _parse_m3u(file_path: str):
    """Yield (display_name, tvg_name, group_title, prefix, url) for every channel entry.

    Parsing/prefix-detection logic is copied verbatim from what m3u_browser_data used to
    do inline, so indexed results are identical to the old per-request scan."""
    prev_extinf: str | None = None
    first_line_checked = False
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not first_line_checked:
                first_line_checked = True
                if line.strip() == "#EXTM3U":
                    continue

            if line.startswith("#EXTINF"):
                prev_extinf = line
                continue

            if prev_extinf is None:
                continue

            extinf = prev_extinf
            url = line.strip()
            prev_extinf = None

            attrs = {}
            display_name = ""
            if "," in extinf:
                attr_part, display_name = extinf.split(",", 1)
                for k, v in re.findall(r'(\S+?)="([^"]*)"', attr_part):
                    attrs[k.lower()] = v
            display_name = display_name.strip()
            tvg_name = attrs.get("tvg-name", "").strip()
            group_title = attrs.get("group-title", "").strip()

            # Detect provider prefix  e.g. "SLING: ESPN" -> "SLING:", "EN - BBC" -> "EN -"
            src = tvg_name or display_name
            ch_prefix = ""
            if ":" in src:
                candidate = src.split(":")[0].strip()
                if 1 < len(candidate) <= 15 and not any(c.isdigit() for c in candidate):
                    ch_prefix = candidate + ":"
            elif " - " in src:
                candidate = src.split(" - ")[0].strip()
                if 1 < len(candidate) <= 6:
                    ch_prefix = candidate + " -"

            yield (display_name, tvg_name, group_title, ch_prefix, url)


def _build_index(item_id: int, fingerprint: tuple) -> BrowserIndex:
    file_path = os.path.join(config.M3U_DIR, f"xtream_playlist_{item_id}.m3u")
    db_path = os.path.join(config.M3U_DIR, f"m3u_browser_index_{item_id}.db")
    tmp_path = db_path + ".tmp"
    for stray in (tmp_path, tmp_path + "-wal", tmp_path + "-shm"):
        try:
            os.remove(stray)
        except FileNotFoundError:
            pass

    start = time.time()
    groups: set = set()
    prefix_counts: dict = {}
    row_count = 0

    conn = sqlite3.connect(tmp_path, check_same_thread=False, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_INDEX_SCHEMA)

        batch = []
        for display_name, tvg_name, group_title, ch_prefix, url in _parse_m3u(file_path):
            row_count += 1
            if group_title:
                groups.add(group_title)
            if ch_prefix:
                prefix_counts[ch_prefix] = prefix_counts.get(ch_prefix, 0) + 1
            batch.append((row_count, display_name, tvg_name, group_title, ch_prefix, url))
            if len(batch) >= _BATCH_SIZE:
                conn.executemany(
                    "INSERT INTO channels (id, name, tvg_name, group_title, prefix, url) VALUES (?,?,?,?,?,?)",
                    batch,
                )
                batch = []
        if batch:
            conn.executemany(
                "INSERT INTO channels (id, name, tvg_name, group_title, prefix, url) VALUES (?,?,?,?,?,?)",
                batch,
            )

        # Only real, recurring provider prefixes count — clear one-off channel-name colons.
        valid_prefixes = sorted(p for p, c in prefix_counts.items() if c >= MIN_PREFIX_COUNT)
        invalid_prefixes = [p for p in prefix_counts if p not in valid_prefixes]
        if invalid_prefixes:
            placeholders = ",".join("?" for _ in invalid_prefixes)
            conn.execute(f"UPDATE channels SET prefix='' WHERE prefix IN ({placeholders})", invalid_prefixes)

        conn.execute("INSERT INTO channels_fts(channels_fts) VALUES('rebuild')")

        groups_sorted = sorted(groups)
        for key, value in (
            ("fingerprint", json.dumps(list(fingerprint))),
            ("groups", json.dumps(groups_sorted)),
            ("prefixes", json.dumps(valid_prefixes)),
            ("row_count", json.dumps(row_count)),
        ):
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

        conn.execute("ANALYZE")
        conn.commit()

        # Fold the WAL back into the main file and drop the WAL/SHM sidecars before the
        # atomic rename below — otherwise a rename of just the .tmp file could leave
        # recently-written rows stranded in an orphaned .tmp-wal file.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        conn.close()

    os.replace(tmp_path, db_path)
    # The file being replaced may itself have been opened by readers in WAL mode
    # (_index_conn always sets journal_mode=WAL), leaving -wal/-shm sidecars for the
    # *previous* generation of this path. Those are now stale — our new file was already
    # checkpointed to DELETE mode above and carries no WAL of its own — but SQLite gets
    # confused ("database is locked") if it finds a leftover -wal next to a swapped-in
    # main file that doesn't match it. Clear them so the first reader starts clean.
    for stale in (db_path + "-wal", db_path + "-shm"):
        try:
            os.remove(stale)
        except FileNotFoundError:
            pass
    logger.info(
        f"M3U browser index built for item {item_id}: {row_count} rows, "
        f"{len(groups_sorted)} groups, {len(valid_prefixes)} prefixes in {time.time() - start:.1f}s"
    )

    return BrowserIndex(
        fingerprint=fingerprint,
        db_path=db_path,
        groups=groups_sorted,
        prefixes=valid_prefixes,
        row_count=row_count,
    )


async def get_browser_index(item_id: int) -> BrowserIndex:
    """Return the current search index for this provider, building/rebuilding it first
    if the underlying M3U file is new or has changed since the last build."""
    fingerprint = _compute_fingerprint(item_id)
    lock = _get_lock(item_id)
    async with lock:
        cached = _cache.get(item_id)
        if cached and cached.fingerprint == fingerprint and os.path.exists(cached.db_path):
            return cached

        last_fail = _build_failures.get(item_id, 0.0)
        if time.time() - last_fail < _BUILD_RETRY_COOLDOWN:
            if cached:
                logger.warning(
                    f"M3U browser index build cooldown active for item {item_id} — returning stale index"
                )
                return cached
            raise RuntimeError("Search index build failed recently — try again shortly")

        try:
            new_index = await asyncio.to_thread(_build_index, item_id, fingerprint)
        except Exception as exc:
            _build_failures[item_id] = time.time()
            logger.error(f"M3U browser index build failed for item {item_id}: {exc}", exc_info=True)
            if cached:
                logger.warning(f"M3U browser index: returning stale index for item {item_id} after build failure")
                return cached
            raise

        _build_failures.pop(item_id, None)
        _cache[item_id] = new_index
        return new_index


def _tokenize_for_fts(search: str) -> list:
    return re.findall(r"\w+", search or "", re.UNICODE)


def _fts_match_expr(tokens: list) -> str:
    # Quote every token so raw user input can never be parsed as FTS5 query syntax
    # (unescaped ", *, -, AND/OR/NOT all have special meaning to MATCH).
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def query_channels(db_path: str, search: str, group: str, prefix: str, page: int, per_page: int):
    """Return (channels, total) for the given filters — indexed SQL, no file re-scan."""
    tokens = _tokenize_for_fts(search)
    group_f = (group or "").strip()
    prefix_f = (prefix or "").strip()
    offset = (page - 1) * per_page

    with _index_conn(db_path) as conn:
        if tokens:
            # Note: the FTS5 table must be referenced by its real name in MATCH/bm25(),
            # not a table alias (SQLite raises "no such column" otherwise) — confirmed
            # directly against this sqlite build, not assumed from docs.
            match_expr = _fts_match_expr(tokens)
            where = ["channels_fts MATCH ?"]
            params: list = [match_expr]
            if group_f:
                where.append("c.group_title = ?")
                params.append(group_f)
            if prefix_f:
                where.append("c.prefix = ?")
                params.append(prefix_f)
            where_sql = " AND ".join(where)

            total = conn.execute(
                f"SELECT COUNT(*) FROM channels_fts JOIN channels c ON c.id = channels_fts.rowid WHERE {where_sql}",
                params,
            ).fetchone()[0]

            rows = conn.execute(
                f"""SELECT c.id, c.name, c.tvg_name, c.group_title, c.prefix, c.url
                    FROM channels_fts JOIN channels c ON c.id = channels_fts.rowid
                    WHERE {where_sql}
                    ORDER BY bm25(channels_fts) LIMIT ? OFFSET ?""",
                params + [per_page, offset],
            ).fetchall()
        else:
            where = []
            params = []
            if group_f:
                where.append("group_title = ?")
                params.append(group_f)
            if prefix_f:
                where.append("prefix = ?")
                params.append(prefix_f)
            where_sql = (" WHERE " + " AND ".join(where)) if where else ""

            total = conn.execute(f"SELECT COUNT(*) FROM channels{where_sql}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT id, name, tvg_name, group_title, prefix, url FROM channels{where_sql} "
                f"ORDER BY id LIMIT ? OFFSET ?",
                params + [per_page, offset],
            ).fetchall()

    channels = [
        {
            "name": r["name"],
            "tvg_name": r["tvg_name"],
            "group": r["group_title"],
            "prefix": r["prefix"],
            "url": r["url"],
        }
        for r in rows
    ]
    return channels, total
