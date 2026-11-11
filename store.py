"""The desk's ledger: one SQLite file holding deals, their activity log, posted cards and caches.

Pure standard library and no Hermes imports, so it can be tested anywhere. ``Store`` takes a path
(or ``:memory:``) and opens a short-lived connection per operation: tool handlers, Slack button
clicks and the CLI all run on different threads and processes.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import threading
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS deals (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  company TEXT NOT NULL DEFAULT '',
  grp TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',
  linkedin TEXT NOT NULL DEFAULT '',
  stage TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  value REAL NOT NULL DEFAULT 0,
  owner TEXT NOT NULL DEFAULT '',
  next_step TEXT NOT NULL DEFAULT '',
  next_due TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  lost_reason TEXT NOT NULL DEFAULT '',
  snoozed_until TEXT NOT NULL DEFAULT '',
  fields TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  stage_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id TEXT NOT NULL,
  at TEXT NOT NULL,
  kind TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  ref TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS activities_deal ON activities (deal_id, at);
CREATE TABLE IF NOT EXISTS cards (
  channel TEXT NOT NULL,
  ts TEXT NOT NULL,
  deal_id TEXT NOT NULL,
  session_key TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (channel, ts)
);
CREATE TABLE IF NOT EXISTS cache (
  key TEXT PRIMARY KEY,
  at TEXT NOT NULL,
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notes_seen (
  id TEXT PRIMARY KEY,
  at TEXT NOT NULL,
  deal_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS markets (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  brief TEXT NOT NULL DEFAULT '',
  segments TEXT NOT NULL DEFAULT '[]',
  detail TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS targets (
  id TEXT PRIMARY KEY,
  market_id TEXT NOT NULL,
  company TEXT NOT NULL,
  domain TEXT NOT NULL DEFAULT '',
  segment TEXT NOT NULL DEFAULT '',
  fit INTEGER NOT NULL DEFAULT 3,
  size TEXT NOT NULL DEFAULT '',
  location TEXT NOT NULL DEFAULT '',
  why TEXT NOT NULL DEFAULT '',
  signal TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'new',
  deal_id TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS targets_market ON targets (market_id, segment);
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
"""

# Columns added after the first release. A database made by an older version gains them when it is opened.
ADDED_COLUMNS = {
    "markets": {"detail": "TEXT NOT NULL DEFAULT '{}'"},
    "targets": {"detail": "TEXT NOT NULL DEFAULT '{}'"},
    "cards": {"session_key": "TEXT NOT NULL DEFAULT ''"},
}

EDITABLE = ("name", "company", "grp", "email", "linkedin", "value", "owner", "next_step", "next_due", "source")
CSV_COLUMNS = ("id", "name", "company", "grp", "email", "linkedin", "stage", "status", "value", "owner",
               "next_step", "next_due", "source", "lost_reason", "created_at", "updated_at", "stage_at")
ID_MAX = 48  # Slack caps an overflow option's value at 75 characters; ids travel in those values


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return dt.date.today().isoformat()


def parse_ts(value: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def days_since(value: str) -> Optional[float]:
    when = parse_ts(value)
    if when is None:
        return None
    return (dt.datetime.now(dt.timezone.utc) - when).total_seconds() / 86400.0


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:ID_MAX].strip("-") or "deal"


class Ambiguous(Exception):
    """A reference matched more than one deal; ``matches`` holds the candidates."""

    def __init__(self, ref: str, matches: list):
        super().__init__("'%s' matches %d records" % (ref, len(matches)))
        self.matches = matches


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        self._memory = None
        self._lock = threading.RLock()
        if self.path == ":memory:":  # one shared connection, for tests
            self._memory = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory.row_factory = sqlite3.Row
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            for table, columns in ADDED_COLUMNS.items():
                have = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
                for column, declaration in columns.items():
                    if column not in have:
                        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, declaration))

    # -- connection -----------------------------------------------------------------------------
    class _Session:
        def __init__(self, store: "Store"):
            self.store = store
            self.conn = None

        def __enter__(self):
            self.store._lock.acquire()
            if self.store._memory is not None:
                self.conn = self.store._memory
            else:
                self.conn = sqlite3.connect(self.store.path, timeout=15)
                self.conn.row_factory = sqlite3.Row
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA busy_timeout=15000")
            return self.conn

        def __exit__(self, exc_type, exc, tb):
            try:
                if exc_type is None:
                    self.conn.commit()
                else:
                    self.conn.rollback()
                if self.store._memory is None:
                    self.conn.close()
            finally:
                self.store._lock.release()
            return False

    def _conn(self) -> "Store._Session":
        return Store._Session(self)

    # -- deals ----------------------------------------------------------------------------------
    @staticmethod
    def _deal(row) -> dict:
        deal = dict(row)
        try:
            deal["fields"] = json.loads(deal.get("fields") or "{}")
        except ValueError:
            deal["fields"] = {}
        return deal

    def get(self, deal_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
        return self._deal(row) if row else None

    def find(self, ref: str) -> Optional[dict]:
        """Resolve an id, a name, or a fragment of one. Raises ``Ambiguous`` on several matches."""
        ref = (ref or "").strip()
        if not ref:
            return None
        exact = self.get(ref) or self.get(slugify(ref))
        if exact:
            return exact
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM deals WHERE lower(name) = lower(?) ORDER BY status = 'open' DESC, updated_at DESC",
                                (ref,)).fetchall()
            if not rows:
                like = "%" + ref.lower().replace("%", "").replace("_", " ") + "%"
                rows = conn.execute(
                    "SELECT * FROM deals WHERE lower(name) LIKE ? OR lower(company) LIKE ? OR lower(email) = lower(?) "
                    "ORDER BY status = 'open' DESC, updated_at DESC", (like, like, ref)).fetchall()
        deals = [self._deal(r) for r in rows]
        if not deals:
            return None
        open_deals = [d for d in deals if d["status"] == "open"]
        if len(deals) == 1:
            return deals[0]
        if len(open_deals) == 1:
            return open_deals[0]
        raise Ambiguous(ref, deals[:8])

    def add(self, name: str, stage: str, *, actor: str = "", **values: Any) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("a name is required")
        fields = dict(values.pop("fields", None) or {})
        base = slugify(name + ("-" + values["grp"] if values.get("grp") else ""))
        stamp = now()
        with self._conn() as conn:
            deal_id, n = base, 1
            while conn.execute("SELECT 1 FROM deals WHERE id = ?", (deal_id,)).fetchone():
                n += 1
                suffix = "-%d" % n
                deal_id = base[:ID_MAX - len(suffix)] + suffix
            row = {k: values.get(k, "") for k in EDITABLE if k not in ("name", "value")}
            conn.execute(
                "INSERT INTO deals (id, name, company, grp, email, linkedin, stage, value, owner, next_step, next_due, "
                "source, fields, created_at, updated_at, stage_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (deal_id, name, row["company"] or "", row["grp"] or "", row["email"] or "", row["linkedin"] or "", stage,
                 _number(values.get("value")), row["owner"] or "", row["next_step"] or "", row["next_due"] or "",
                 row["source"] or "", json.dumps(fields), stamp, stamp, stamp))
            conn.execute("INSERT INTO activities (deal_id, at, kind, actor, text) VALUES (?,?,?,?,?)",
                         (deal_id, stamp, "created", actor, "Added at %s" % stage))
        return self.get(deal_id)

    def update(self, deal_id: str, *, actor: str = "", fields: Optional[dict] = None, **values: Any) -> dict:
        deal = self.get(deal_id)
        if deal is None:
            raise KeyError(deal_id)
        sets, params, changed = [], [], []
        for key in EDITABLE:
            if key in values and values[key] is not None:
                new = _number(values[key]) if key == "value" else str(values[key]).strip()
                if new != deal[key]:
                    sets.append("%s = ?" % key)
                    params.append(new)
                    changed.append(key)
        if fields:
            merged = dict(deal["fields"])
            for key, val in fields.items():
                if val in (None, ""):
                    merged.pop(key, None)
                else:
                    merged[key] = val
            if merged != deal["fields"]:
                sets.append("fields = ?")
                params.append(json.dumps(merged))
                changed.append("fields")
        if not sets:
            return deal
        stamp = now()
        with self._conn() as conn:
            conn.execute("UPDATE deals SET %s, updated_at = ? WHERE id = ?" % ", ".join(sets), params + [stamp, deal_id])
            conn.execute("INSERT INTO activities (deal_id, at, kind, actor, text) VALUES (?,?,?,?,?)",
                         (deal_id, stamp, "updated", actor, "Updated " + ", ".join(changed)))
        return self.get(deal_id)

    def move(self, deal_id: str, stage: str, *, status: str = "open", reason: str = "", actor: str = "",
             label: str = "") -> dict:
        deal = self.get(deal_id)
        if deal is None:
            raise KeyError(deal_id)
        stamp = now()
        text = "%s → %s" % (deal["stage"], label or stage)
        if reason:
            text += " (%s)" % reason
        with self._conn() as conn:
            conn.execute("UPDATE deals SET stage = ?, status = ?, lost_reason = ?, snoozed_until = '', stage_at = ?, "
                         "updated_at = ? WHERE id = ?", (stage, status, reason if status == "lost" else "", stamp, stamp, deal_id))
            conn.execute("INSERT INTO activities (deal_id, at, kind, actor, text) VALUES (?,?,?,?,?)",
                         (deal_id, stamp, "stage", actor, text))
        return self.get(deal_id)

    def snooze(self, deal_id: str, days: int, *, actor: str = "") -> dict:
        until = (dt.date.today() + dt.timedelta(days=max(0, int(days)))).isoformat() if days else ""
        stamp = now()
        with self._conn() as conn:
            if not conn.execute("SELECT 1 FROM deals WHERE id = ?", (deal_id,)).fetchone():
                raise KeyError(deal_id)
            conn.execute("UPDATE deals SET snoozed_until = ?, updated_at = ? WHERE id = ?", (until, stamp, deal_id))
            conn.execute("INSERT INTO activities (deal_id, at, kind, actor, text) VALUES (?,?,?,?,?)",
                         (deal_id, stamp, "snooze", actor, "Snoozed until %s" % until if until else "Snooze cleared"))
        return self.get(deal_id)

    def delete(self, deal_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM deals WHERE id = ?", (deal_id,))
            conn.execute("DELETE FROM activities WHERE deal_id = ?", (deal_id,))
            conn.execute("DELETE FROM cards WHERE deal_id = ?", (deal_id,))
        return cur.rowcount > 0

    def list(self, *, status: Optional[str] = "open", stage: str = "", grp: str = "", owner: str = "",
             query: str = "") -> list:
        where, params = [], []
        if status and status != "all":
            where.append("status = ?")
            params.append(status)
        if stage:
            where.append("stage = ?")
            params.append(stage)
        if grp:
            where.append("lower(grp) LIKE ?")
            params.append("%" + grp.lower() + "%")
        if owner:
            where.append("lower(owner) LIKE ?")
            params.append("%" + owner.lower() + "%")
        if query:
            where.append("(lower(name) LIKE ? OR lower(company) LIKE ? OR lower(grp) LIKE ? OR lower(next_step) LIKE ?)")
            params += ["%" + query.lower() + "%"] * 4
        sql = "SELECT * FROM deals" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY updated_at DESC"
        with self._conn() as conn:
            return [self._deal(r) for r in conn.execute(sql, params).fetchall()]

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT count(*) FROM deals").fetchone()[0]

    # -- activity -------------------------------------------------------------------------------
    def log(self, deal_id: str, kind: str, text: str, *, actor: str = "", ref: str = "") -> dict:
        stamp = now()
        with self._conn() as conn:
            if not conn.execute("SELECT 1 FROM deals WHERE id = ?", (deal_id,)).fetchone():
                raise KeyError(deal_id)
            conn.execute("INSERT INTO activities (deal_id, at, kind, actor, text, ref) VALUES (?,?,?,?,?,?)",
                         (deal_id, stamp, kind, actor, text, ref))
            conn.execute("UPDATE deals SET updated_at = ? WHERE id = ?", (stamp, deal_id))
        return {"deal_id": deal_id, "at": stamp, "kind": kind, "actor": actor, "text": text, "ref": ref}

    def activities(self, deal_id: str = "", limit: int = 10, since: str = "") -> list:
        where, params = [], []
        if deal_id:
            where.append("deal_id = ?")
            params.append(deal_id)
        if since:
            where.append("at >= ?")
            params.append(since)
        sql = ("SELECT * FROM activities" + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY at DESC, id DESC LIMIT ?")
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, params + [int(limit)]).fetchall()]

    def last_touch(self, deal_id: str) -> str:
        """Latest thing a person or the agent did on the deal (bookkeeping rows don't count)."""
        with self._conn() as conn:
            row = conn.execute("SELECT max(at) FROM activities WHERE deal_id = ? AND kind NOT IN ('snooze', 'card')",
                               (deal_id,)).fetchone()
        return row[0] or ""

    # -- cards ----------------------------------------------------------------------------------
    def remember_card(self, deal_id: str, channel: str, ts: str, session_key: str = "") -> None:
        """Record where a card lives. ``session_key`` is the conversation it was posted from, which is
        where a later button click sends the agent's writing task; an empty one keeps what is stored."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO cards (channel, ts, deal_id, session_key) VALUES (?,?,?,?) "
                "ON CONFLICT (channel, ts) DO UPDATE SET deal_id = excluded.deal_id, session_key = "
                "CASE WHEN excluded.session_key != '' THEN excluded.session_key ELSE cards.session_key END",
                (channel, ts, deal_id, session_key))

    def card_session(self, channel: str, ts: str) -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT session_key FROM cards WHERE channel = ? AND ts = ?", (channel, ts)).fetchone()
        return row[0] if row else ""

    def cards(self, deal_id: str) -> list:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT channel, ts FROM cards WHERE deal_id = ? ORDER BY ts DESC",
                                                  (deal_id,)).fetchall()]

    def forget_card(self, channel: str, ts: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM cards WHERE channel = ? AND ts = ?", (channel, ts))

    # -- cache, notes, meta ---------------------------------------------------------------------
    def cache_get(self, key: str, max_age_days: float = 90) -> Optional[Any]:
        with self._conn() as conn:
            row = conn.execute("SELECT at, data FROM cache WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        age = days_since(row["at"])
        if age is None or age > max_age_days:
            return None
        try:
            return json.loads(row["data"])
        except ValueError:
            return None

    def cache_put(self, key: str, data: Any) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO cache (key, at, data) VALUES (?,?,?)", (key, now(), json.dumps(data)))

    def note_seen(self, note_id: str) -> bool:
        with self._conn() as conn:
            return conn.execute("SELECT 1 FROM notes_seen WHERE id = ?", (note_id,)).fetchone() is not None

    def mark_note(self, note_id: str, deal_id: str = "") -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO notes_seen (id, at, deal_id) VALUES (?,?,?)", (note_id, now(), deal_id))

    def meta_get(self, key: str, default: str = "") -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row[0] if row else default

    def meta_set(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?,?)", (key, str(value)))

    def meta_pop(self, key: str) -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
            conn.execute("DELETE FROM meta WHERE k = ?", (key,))
        return row[0] if row else ""

    def bump(self, key: str, by: int = 1) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
            value = (int(row[0]) if row and str(row[0]).isdigit() else 0) + by
            conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?,?)", (key, str(value)))
        return value

    # -- CSV ------------------------------------------------------------------------------------
    def export_rows(self, field_keys: Iterable[str] = ()) -> tuple:
        field_keys = list(field_keys)
        header = list(CSV_COLUMNS) + field_keys
        rows = []
        for deal in sorted(self.list(status="all"), key=lambda d: (d["grp"], d["name"])):
            rows.append([deal.get(c, "") for c in CSV_COLUMNS] + [deal["fields"].get(k, "") for k in field_keys])
        return header, rows


def _number(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^0-9.kKmM-]", "", str(value))
    mult = 1.0
    if text[-1:] in "kK":
        mult, text = 1e3, text[:-1]
    elif text[-1:] in "mM":
        mult, text = 1e6, text[:-1]
    try:
        return float(text) * mult
    except ValueError:
        return 0.0
