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
  asked_by TEXT NOT NULL DEFAULT '',
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
CREATE TABLE IF NOT EXISTS intakes (
  id TEXT PRIMARY KEY,
  at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',
  rows TEXT NOT NULL DEFAULT '[]',
  result TEXT NOT NULL DEFAULT '{}'
);
"""

# Columns added after the first release. A database made by an older version gains them when it is opened.
ADDED_COLUMNS = {
    "markets": {"detail": "TEXT NOT NULL DEFAULT '{}'"},
    "targets": {"detail": "TEXT NOT NULL DEFAULT '{}'"},
    "cards": {"session_key": "TEXT NOT NULL DEFAULT ''", "asked_by": "TEXT NOT NULL DEFAULT ''"},
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
    """An id from a name. Letters and digits of any script are kept, so two names never share one by accident."""
    slug = re.sub(r"[\W_]+", "-", (text or "").lower()).strip("-")
    return slug[:ID_MAX].strip("-") or "deal"


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_due(value: Any, base: Optional[dt.date] = None) -> Optional[str]:
    """A due date as ``YYYY-MM-DD``, from the ways people write one.

    ``""`` for nothing, ``None`` when it cannot be read. Understands ISO dates, 12/01/2026, "Dec 1",
    "1 December 2026", today, tomorrow, a weekday name, "+3d", "in 2 weeks" and "next week".
    """
    text = " ".join(str(value if value is not None else "").replace(",", " ").lower().split()).rstrip(".")
    if text in ("", "none", "null", "clear", "-", "n/a"):
        return ""
    base = base or dt.date.today()

    def make(year: int, month: int, day: int) -> Optional[str]:
        try:
            return dt.date(year, month, day).isoformat()
        except ValueError:
            return None

    def this_or_next_year(month: int, day: int) -> Optional[str]:
        found = make(base.year, month, day)
        if found and (base - dt.date.fromisoformat(found)).days > 60:   # "Jan 5" said in December means next January
            return make(base.year + 1, month, day)
        return found

    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[t ].*)?$", text)
    if match:
        return make(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if text in ("today", "now", "eod"):
        return base.isoformat()
    if text in ("tomorrow", "tmrw"):
        return (base + dt.timedelta(days=1)).isoformat()
    if text == "next week":
        return (base + dt.timedelta(days=7)).isoformat()
    match = re.match(r"^(?:in |\+)?(\d{1,3}) ?(d|day|days|w|wk|wks|week|weeks)$", text)
    if match:
        span = int(match.group(1)) * (7 if match.group(2).startswith("w") else 1)
        return (base + dt.timedelta(days=span)).isoformat()
    word = re.sub(r"^(?:on|by|this|next) ", "", text)
    for index, name in enumerate(WEEKDAYS):
        if word in (name, name[:3]) or (name == "thursday" and word in ("thur", "thurs")) or (name == "tuesday" and word == "tues"):
            ahead = (index - base.weekday()) % 7 or 7
            return (base + dt.timedelta(days=ahead)).isoformat()
    match = re.match(r"^(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2}|\d{4}))?$", text)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        if month > 12 >= day:       # written day first
            month, day = day, month
        if match.group(3):
            year = int(match.group(3))
            return make(year + 2000 if year < 100 else year, month, day)
        return this_or_next_year(month, day)
    words = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", word).split()
    month = next((i + 1 for w in words for i, name in enumerate(MONTHS) if w in (name, name[:3], name[:4])), 0)
    numbers = [int(w) for w in words if w.isdigit()]
    if month and numbers and len(words) <= 3:
        day = next((n for n in numbers if n <= 31), 0)
        year = next((n for n in numbers if n > 31), 0)
        if day:
            return make(year, month, day) if year else this_or_next_year(month, day)
    return None


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
            if self._memory is None:
                conn.execute("PRAGMA journal_mode=WAL")     # kept in the file: every later connection gets it
            conn.executescript(SCHEMA)
            for table, columns in ADDED_COLUMNS.items():
                have = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
                for column, declaration in columns.items():
                    if column not in have:
                        try:
                            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, declaration))
                        except sqlite3.OperationalError as exc:     # another process added it a moment ago
                            if "duplicate column" not in str(exc).lower():
                                raise

    # -- connection -----------------------------------------------------------------------------
    class _Session:
        def __init__(self, store: "Store"):
            self.store = store
            self.conn = None

        def __enter__(self):
            self.store._lock.acquire()
            try:
                if self.store._memory is not None:
                    self.conn = self.store._memory
                else:
                    self.conn = sqlite3.connect(self.store.path, timeout=15)    # waits up to 15 s on a busy file
                    self.conn.row_factory = sqlite3.Row
            except BaseException:       # never leave the lock held: every other thread would wait forever
                self.store._lock.release()
                raise
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
        slug = slugify(ref)
        exact = self.get(ref) or (self.get(slug) if slug != "deal" or ref.lower() == "deal" else None)
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
            conn.execute("BEGIN IMMEDIATE")
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
             label: str = "", expect: Optional[str] = None) -> Optional[dict]:
        """Change a record's stage and log it.

        With ``expect`` the change only happens if the record is still at that stage. The check and
        the change share one write transaction, so two clicks, two people or two processes acting
        on the same record cannot both move it. Returns ``None`` when someone else got there first.
        """
        stamp = now()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT stage, status, fields FROM deals WHERE id = ?", (deal_id,)).fetchone()
            if row is None:
                raise KeyError(deal_id)
            if expect is not None and row["stage"] != expect:
                return None
            fields = row["fields"]
            if status != "open" and row["status"] == "open":    # remember where it was, for a later reopen
                try:
                    data = json.loads(fields or "{}")
                except ValueError:
                    data = {}
                data["_prev_stage"] = row["stage"]
                fields = json.dumps(data)
            text = "%s \u2192 %s" % (row["stage"], label or stage)
            if reason:
                text += " (%s)" % reason
            conn.execute("UPDATE deals SET stage = ?, status = ?, lost_reason = ?, snoozed_until = '', fields = ?, stage_at = ?, "
                         "updated_at = ? WHERE id = ?", (stage, status, reason if status == "lost" else "", fields, stamp, stamp, deal_id))
            conn.execute("INSERT INTO activities (deal_id, at, kind, actor, text) VALUES (?,?,?,?,?)",
                         (deal_id, stamp, "stage", actor, text))
        return self.get(deal_id)

    def snooze(self, deal_id: str, days: int = 0, *, until: str = "", actor: str = "") -> dict:
        """Put a record aside for ``days``, or until the date ``until`` (YYYY-MM-DD). Neither clears it."""
        if until and not ISO_DATE.match(until):
            raise ValueError("until must be YYYY-MM-DD")
        if not until:
            days = max(0, min(int(days), 3650))
            until = (dt.date.today() + dt.timedelta(days=days)).isoformat() if days else ""
        elif until <= today():
            until = ""
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
            conn.execute("DELETE FROM meta WHERE k IN (?, ?)", ("pending:" + deal_id, "handoff:" + deal_id))
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

    def logged(self, deal_id: str, ref: str) -> bool:
        """Whether something from outside (an email, a message, a call) is already on this record's log."""
        if not ref:
            return False
        with self._conn() as conn:
            return conn.execute("SELECT 1 FROM activities WHERE deal_id = ? AND ref = ? LIMIT 1",
                                (deal_id, ref)).fetchone() is not None

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

    def touches(self) -> dict:
        """``last_touch`` for every record in one query: ``{deal id: timestamp}``."""
        with self._conn() as conn:
            return {row[0]: row[1] or "" for row in conn.execute(
                "SELECT deal_id, max(at) FROM activities WHERE kind NOT IN ('snooze', 'card') GROUP BY deal_id")}

    # -- cards ----------------------------------------------------------------------------------
    def remember_card(self, deal_id: str, channel: str, ts: str, session_key: str = "", asked_by: str = "") -> None:
        """Record where a card lives. ``session_key`` is the conversation it was posted from, which is
        where a later button click sends the agent's writing task; ``asked_by`` is the Slack user it
        was posted for, who may press its buttons. An empty one keeps what is stored."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO cards (channel, ts, deal_id, session_key, asked_by) VALUES (?,?,?,?,?) "
                "ON CONFLICT (channel, ts) DO UPDATE SET deal_id = excluded.deal_id, session_key = "
                "CASE WHEN excluded.session_key != '' THEN excluded.session_key ELSE cards.session_key END, asked_by = "
                "CASE WHEN excluded.asked_by != '' THEN excluded.asked_by ELSE cards.asked_by END",
                (channel, ts, deal_id, session_key, asked_by))

    def card_asker(self, channel: str, ts: str) -> str:
        """The Slack user a card, a review or a map was posted for; '' when nobody is on record."""
        with self._conn() as conn:
            row = conn.execute("SELECT asked_by FROM cards WHERE channel = ? AND ts = ?", (channel, ts)).fetchone()
        return row[0] if row else ""

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

    def meta_prefix(self, prefix: str) -> dict:
        """Every stored value whose key starts with ``prefix``, keyed by the rest of the key."""
        with self._conn() as conn:
            return {row[0][len(prefix):]: row[1] for row in conn.execute(
                "SELECT k, v FROM meta WHERE substr(k, 1, ?) = ?", (len(prefix), prefix))}

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
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
            value = (int(row[0]) if row and str(row[0]).isdigit() else 0) + by
            conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?,?)", (key, str(value)))
        return value

    # -- intakes: lists waiting for someone to approve them -------------------------------------
    @staticmethod
    def _intake(row) -> dict:
        intake = dict(row)
        for key, empty in (("rows", []), ("result", {})):
            try:
                intake[key] = json.loads(intake.get(key) or "null") or empty
            except ValueError:
                intake[key] = empty
        return intake

    def intake_put(self, intake_id: str, source: str, rows: list) -> None:
        with self._conn() as conn:
            conn.execute("INSERT INTO intakes (id, at, source, status, rows) VALUES (?,?,?,?,?)",
                         (intake_id, now(), source, "open", json.dumps(rows)))

    def intake_get(self, intake_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM intakes WHERE id = ?", (intake_id,)).fetchone()
        return self._intake(row) if row else None

    def intake_list(self, status: str = "open", limit: int = 10) -> list:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM intakes WHERE status = ? ORDER BY at DESC LIMIT ?", (status, int(limit))).fetchall()
        return [self._intake(row) for row in rows]

    def intake_claim(self, intake_id: str) -> bool:
        """Take an open intake for applying. Only one caller ever gets it, so two clicks apply it once."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("UPDATE intakes SET status = 'applying' WHERE id = ? AND status = 'open'", (intake_id,))
        return cur.rowcount == 1

    def intake_finish(self, intake_id: str, status: str, result: Optional[dict] = None, rows: Optional[list] = None) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE intakes SET status = ?, result = ? WHERE id = ?", (status, json.dumps(result or {}), intake_id))
            if rows is not None:
                conn.execute("UPDATE intakes SET rows = ? WHERE id = ?", (json.dumps(rows), intake_id))

    def tally(self, sample_source: str = "") -> dict:
        """How much is in the ledger, for the Setup tab."""
        with self._conn() as conn:
            one = lambda sql, *params: conn.execute(sql, params).fetchone()[0]      # noqa: E731
            return {"open": one("SELECT count(*) FROM deals WHERE status = 'open'"),
                    "won": one("SELECT count(*) FROM deals WHERE status = 'won'"),
                    "lost": one("SELECT count(*) FROM deals WHERE status = 'lost'"),
                    "samples": one("SELECT count(*) FROM deals WHERE source = ?", sample_source) if sample_source else 0,
                    "activities": one("SELECT count(*) FROM activities"),
                    "from_sweeps": one("SELECT count(*) FROM activities WHERE ref != ''"),
                    "maps": one("SELECT count(*) FROM markets"),
                    "lists_waiting": one("SELECT count(*) FROM intakes WHERE status = 'open'")}

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
