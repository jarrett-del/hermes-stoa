"""The desk's storage: one SQLite file holding deals, the people on them, the log, and an append-only
ledger of every change (what it replaced and what it left), which is what an undo reads.

Pure standard library and no Hermes imports, so it can be tested anywhere. ``Store`` takes a path
(or ``:memory:``) and opens a short-lived connection per operation: tool handlers, Slack button
clicks and the CLI all run on different threads and processes.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
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
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  email TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  linkedin TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  archived_at TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS contacts_email ON contacts (lower(email)) WHERE email != '' AND archived_at = '';
CREATE TABLE IF NOT EXISTS deal_contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id TEXT NOT NULL,
  contact_id INTEGER NOT NULL,
  role TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  archived_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS deal_contacts_deal ON deal_contacts (deal_id);
CREATE UNIQUE INDEX IF NOT EXISTS deal_contacts_pair ON deal_contacts (deal_id, contact_id) WHERE archived_at = '';
CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id TEXT NOT NULL,
  number TEXT NOT NULL DEFAULT '',
  amount_cents INTEGER NOT NULL DEFAULT 0,
  paid_cents INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'sent',
  issued_on TEXT NOT NULL DEFAULT '',
  due_on TEXT NOT NULL DEFAULT '',
  paid_on TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  archived_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS invoices_deal ON invoices (deal_id);
CREATE TABLE IF NOT EXISTS ledger (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  batch TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT '',
  entity TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  deal_id TEXT NOT NULL DEFAULT '',
  op TEXT NOT NULL,
  before TEXT NOT NULL DEFAULT '{}',
  after TEXT NOT NULL DEFAULT '{}',
  summary TEXT NOT NULL DEFAULT '',
  reverts INTEGER
);
CREATE INDEX IF NOT EXISTS ledger_deal ON ledger (deal_id, seq);
CREATE INDEX IF NOT EXISTS ledger_batch ON ledger (batch);
CREATE TRIGGER IF NOT EXISTS ledger_keeps_rows BEFORE DELETE ON ledger
BEGIN SELECT RAISE(ABORT, 'the ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_keeps_values BEFORE UPDATE ON ledger
BEGIN SELECT RAISE(ABORT, 'the ledger is append-only'); END;
CREATE TABLE IF NOT EXISTS orgs (
  key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  domain TEXT NOT NULL DEFAULT '',
  website TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS grps (
  key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  client TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',
  value_cents INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '',
  fields TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS grp_contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  grp_key TEXT NOT NULL,
  contact_id INTEGER NOT NULL,
  role TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  archived_at TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS grp_contacts_pair ON grp_contacts (grp_key, contact_id) WHERE archived_at = '';
CREATE TABLE IF NOT EXISTS declined (
  hash TEXT PRIMARY KEY,
  at TEXT NOT NULL,
  deal_id TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT ''
);
"""

# Columns added after the first release. A database made by an older version gains them when it is opened.
ADDED_COLUMNS = {
    "markets": {"detail": "TEXT NOT NULL DEFAULT '{}'"},
    "targets": {"detail": "TEXT NOT NULL DEFAULT '{}'"},
    "cards": {"session_key": "TEXT NOT NULL DEFAULT ''", "asked_by": "TEXT NOT NULL DEFAULT ''"},
    # the action a log line was part of (its batch on the ledger), and when it was taken back. `before` is what
    # 0.11 kept there; the ledger holds it now.
    "activities": {"before": "TEXT NOT NULL DEFAULT ''", "op": "TEXT NOT NULL DEFAULT ''", "undone": "TEXT NOT NULL DEFAULT ''"},
    # money as whole cents (`value` stays beside it for older versions reading the file), and a record taken off
    # the desk without being destroyed
    "deals": {"value_cents": "INTEGER NOT NULL DEFAULT 0", "archived_at": "TEXT NOT NULL DEFAULT ''",
              # when it is expected to close (for the forecast), and where the work stands once it is won
              "close_on": "TEXT NOT NULL DEFAULT ''", "delivery": "TEXT NOT NULL DEFAULT ''",
              # a few lines kept on the record itself, edited in place (the log is what happened; this is what to know)
              "notes": "TEXT NOT NULL DEFAULT ''"},
    "intakes": {"origin": "TEXT NOT NULL DEFAULT 'list'"},    # list: rows someone sent. proposal: changes Hermes suggests
}

EDITABLE = ("name", "company", "grp", "email", "linkedin", "value", "owner", "next_step", "next_due", "source", "close_on", "delivery",
            "notes")
PERSON = ("name", "email", "title", "role", "linkedin", "phone", "note")
CONTACT = ("name", "email", "title", "linkedin", "phone", "note")       # what a person is, on every record they are on
# A deal's values as the ledger keeps them: its columns, with money in cents and each custom field as fields.<key>.
TRACKED = ("name", "company", "grp", "email", "linkedin", "value_cents", "owner", "next_step", "next_due", "source",
           "stage", "status", "lost_reason", "snoozed_until", "stage_at", "close_on", "delivery", "notes")
# Put back by an undo only if nothing has touched them since; a difference there never stops the undo.
SOFT = ("stage_at", "snoozed_until", "lost_reason", "fields._prev_stage", "delivery")
INVOICE = ("number", "amount_cents", "paid_cents", "status", "issued_on", "due_on", "paid_on", "note")
INVOICE_STATUS = ("draft", "sent", "paid", "void")
UNDOABLE = "('insert', 'update', 'archive')"
STANDING = ("op IN " + UNDOABLE + " AND seq NOT IN (SELECT reverts FROM ledger WHERE reverts IS NOT NULL)")
QUIET_KINDS = "('snooze', 'card', 'undo')"      # bookkeeping: on the log, but not a touch and not something to take back
ORG_TAILS = {"inc", "llc", "ltd", "co", "corp", "corporation", "gmbh", "plc", "limited", "company", "sa", "ag", "bv", "pty"}
FREE_MAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com", "ymail.com",
             "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com", "pm.me", "gmx.com", "gmx.net",
             "fastmail.com", "hey.com", "zoho.com", "mail.com", "yandex.com", "qq.com", "163.com"}
CSV_COLUMNS = ("id", "name", "company", "grp", "email", "linkedin", "stage", "status", "value", "owner",
               "next_step", "next_due", "source", "lost_reason", "created_at", "updated_at", "stage_at", "close_on", "delivery")
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


def org_key(name: Any) -> str:
    """One key for a company however it is written: "Kestrel Labs, Inc." and "kestrel labs" are the same."""
    words = re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).split()
    while len(words) > 1 and words[-1] in ORG_TAILS:
        words.pop()
    return " ".join(words)


GROUP_STATUS = ("open", "hold", "done", "closed")       # a search, an account, a client engagement: where it stands


def group_key(name: Any) -> str:
    """One key for a group however it is typed: "Staff Backend · Northwind" and "staff backend - northwind" are the same."""
    return " ".join(re.sub(r"[\W_]+", " ", str(name or "").lower()).split())


def email_domain(address: Any) -> str:
    """The domain of an address when it says something about the company ("" for gmail and the like)."""
    domain = str(address or "").strip().lower().rpartition("@")[2]
    return "" if not domain or "." not in domain or domain in FREE_MAIL else domain


def to_cents(value: Any) -> int:
    """An amount as whole cents. Money is kept in integers so that sums are exact."""
    try:
        return int((Decimal(repr(_number(value))) * 100).to_integral_value(ROUND_HALF_UP))
    except (InvalidOperation, ValueError, OverflowError):
        return 0


def total(deals: Iterable[dict]) -> float:
    """The sum of the amounts on some deals, added up in cents."""
    return sum(int(deal.get("value_cents") or 0) for deal in deals) / 100


def flat(deal: Optional[dict]) -> dict:
    """A deal as the ledger keeps it: tracked columns, and each custom field as ``fields.<key>``."""
    if not deal:
        return {}
    out = {key: deal.get(key) for key in TRACKED}
    for key, value in (deal.get("fields") or {}).items():
        out["fields." + str(key)] = value
    return out


def _plain(cents: int) -> str:
    """Cents as a plain amount for a log line: 1200000 -> 12,000 and 1999 -> 19.99 (the currency is the playbook's to say)."""
    whole, part = divmod(abs(int(cents)), 100)
    return ("-" if cents < 0 else "") + ("{:,}".format(whole) + (".%02d" % part if part else ""))


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
        self._ops = threading.local()       # the action in progress on this thread, if a caller opened one
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
            # Cents are what counts. `value` is written beside them, so an older version can read the file; where the
            # two disagree an older version wrote `value` last, and that is taken over.
            conn.execute("UPDATE deals SET value_cents = CAST(ROUND(value * 100) AS INTEGER) "
                         "WHERE value_cents != CAST(ROUND(value * 100) AS INTEGER)")
        self._adopt_people()

    def _adopt_people(self) -> None:
        """Version 0.11 kept the other people on a record in a table of their own, one row per record. They
        become contacts, one per person (by email), linked to each record they were on."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")         # two processes opening the file together: one does this, the other waits
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'people'").fetchone():
                return
            for row in conn.execute("SELECT * FROM people ORDER BY id").fetchall():
                person = dict(row)
                email = (person.get("email") or "").strip().lower()
                found = conn.execute("SELECT id FROM contacts WHERE lower(email) = ? AND archived_at = ''", (email,)).fetchone() if email else None
                if found is None:
                    contact_id = conn.execute(
                        "INSERT INTO contacts (name, email, title, linkedin, phone, note, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                        (person["name"], email, person.get("title") or "", person.get("linkedin") or "", person.get("phone") or "",
                         person.get("note") or "", person.get("created_at") or now(), person.get("updated_at") or now())).lastrowid
                else:
                    contact_id = found["id"]
                if not conn.execute("SELECT 1 FROM deal_contacts WHERE deal_id = ? AND contact_id = ? AND archived_at = ''",
                                    (person["deal_id"], contact_id)).fetchone():
                    conn.execute("INSERT INTO deal_contacts (deal_id, contact_id, role, created_at) VALUES (?,?,?,?)",
                                 (person["deal_id"], contact_id, person.get("role") or "", person.get("created_at") or now()))
            conn.execute("DROP TABLE people")

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

    # -- actions: what one click or one tool call wrote, so it can be taken back together ---------
    @contextmanager
    def operation(self):
        """Everything written inside is one action. Opened inside another, it joins that one."""
        if getattr(self._ops, "id", ""):
            yield self._ops.id
            return
        self._ops.id = uuid.uuid4().hex[:12]
        try:
            yield self._ops.id
        finally:
            self._ops.id = ""

    def _op(self) -> str:
        return getattr(self._ops, "id", "") or uuid.uuid4().hex[:12]

    @staticmethod
    def _entry(conn, batch: str, entity: str, entity_id: Any, deal_id: str, op: str, before: Optional[dict] = None,
               after: Optional[dict] = None, actor: str = "", summary: str = "", reverts: Optional[int] = None, at: str = "") -> int:
        """Append one line to the ledger: what changed, what it replaced and what it left. Lines are never
        changed or removed (the database refuses), so the ledger is the record of what happened."""
        return conn.execute(
            "INSERT INTO ledger (at, batch, actor, entity, entity_id, deal_id, op, before, after, summary, reverts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (at or now(), batch, actor, entity, str(entity_id), deal_id, op, json.dumps(before or {}, sort_keys=True, default=str),
             json.dumps(after or {}, sort_keys=True, default=str), (summary or "")[:300], reverts)).lastrowid

    def ledger(self, deal_id: str = "", limit: int = 50) -> list:
        """The ledger, newest first: for one record, or for the whole desk."""
        sql = "SELECT * FROM ledger" + (" WHERE deal_id = ?" if deal_id else "") + " ORDER BY seq DESC LIMIT ?"
        with self._conn() as conn:
            rows = conn.execute(sql, ([deal_id] if deal_id else []) + [int(limit)]).fetchall()
        out = []
        for row in rows:
            entry = dict(row)
            for key in ("before", "after"):
                try:
                    entry[key] = json.loads(entry[key] or "{}")
                except ValueError:
                    entry[key] = {}
            out.append(entry)
        return out

    # -- deals ----------------------------------------------------------------------------------
    @staticmethod
    def _deal(row) -> dict:
        deal = dict(row)
        try:
            deal["fields"] = json.loads(deal.get("fields") or "{}")
        except ValueError:
            deal["fields"] = {}
        deal["value_cents"] = int(deal.get("value_cents") or 0)
        deal["value"] = deal["value_cents"] / 100         # the amount, read from the cents
        return deal

    def get(self, deal_id: str, archived: bool = False) -> Optional[dict]:
        """A record by id. One taken off the desk is found only when ``archived`` asks for those."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM deals WHERE id = ? AND archived_at %s ''" % ("!=" if archived else "="), (deal_id,)).fetchone()
        return self._deal(row) if row else None

    def find(self, ref: str, archived: bool = False) -> Optional[dict]:
        """Resolve an id, a name, or a fragment of one. Raises ``Ambiguous`` on several matches."""
        ref = (ref or "").strip()
        if not ref:
            return None
        slug = slugify(ref)
        exact = self.get(ref, archived) or (self.get(slug, archived) if slug != "deal" or ref.lower() == "deal" else None)
        if exact:
            return exact
        here = "archived_at %s ''" % ("!=" if archived else "=")
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM deals WHERE " + here + " AND lower(name) = lower(?) ORDER BY status = 'open' DESC, updated_at DESC",
                                (ref,)).fetchall()
            if not rows:
                like = "%" + ref.lower().replace("%", "").replace("_", " ") + "%"
                rows = conn.execute(
                    "SELECT * FROM deals WHERE " + here + " AND (lower(name) LIKE ? OR lower(company) LIKE ? OR lower(email) = lower(?)) "
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
        stamp, batch = now(), self._op()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            deal_id, n = base, 1
            while conn.execute("SELECT 1 FROM deals WHERE id = ?", (deal_id,)).fetchone():
                n += 1
                suffix = "-%d" % n
                deal_id = base[:ID_MAX - len(suffix)] + suffix
            row = {k: values.get(k, "") for k in EDITABLE if k not in ("name", "value")}
            cents = to_cents(values.get("value"))
            conn.execute(
                "INSERT INTO deals (id, name, company, grp, email, linkedin, stage, value, value_cents, owner, next_step, next_due, "
                "source, fields, created_at, updated_at, stage_at, close_on, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (deal_id, name, row["company"] or "", row["grp"] or "", row["email"] or "", row["linkedin"] or "", stage,
                 cents / 100, cents, row["owner"] or "", row["next_step"] or "", row["next_due"] or "",
                 row["source"] or "", json.dumps(fields), stamp, stamp, stamp, row["close_on"] or "", str(row["notes"] or "")[:4000]))
            made = self._deal(conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone())
            self._entry(conn, batch, "deal", deal_id, deal_id, "insert", after=flat(made), actor=actor,
                        summary="Added at %s" % stage, at=stamp)
            conn.execute("INSERT INTO activities (deal_id, at, kind, actor, text, op) VALUES (?,?,?,?,?,?)",
                         (deal_id, stamp, "created", actor, "Added at %s" % stage, batch))
        return self.get(deal_id)

    def update(self, deal_id: str, *, actor: str = "", fields: Optional[dict] = None, **values: Any) -> dict:
        deal = self.get(deal_id)
        if deal is None:
            raise KeyError(deal_id)
        sets, params, changed, before, after = [], [], [], {}, {}
        for key in EDITABLE:
            if key in values and values[key] is not None:
                if key == "value":
                    cents = to_cents(values[key])
                    if cents != deal["value_cents"]:
                        sets += ["value = ?", "value_cents = ?"]
                        params += [cents / 100, cents]
                        changed.append(key)
                        before["value_cents"], after["value_cents"] = deal["value_cents"], cents
                    continue
                new = str(values[key]).strip()
                if new != deal[key]:
                    sets.append("%s = ?" % key)
                    params.append(new)
                    changed.append(key)
                    before[key], after[key] = deal[key], new
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
                for key in set(merged) | set(deal["fields"]):
                    if merged.get(key) != deal["fields"].get(key):
                        before["fields." + key], after["fields." + key] = deal["fields"].get(key), merged.get(key)
        if not sets:
            return deal
        stamp, batch = now(), self._op()
        with self._conn() as conn:
            conn.execute("UPDATE deals SET %s, updated_at = ? WHERE id = ?" % ", ".join(sets), params + [stamp, deal_id])
            self._entry(conn, batch, "deal", deal_id, deal_id, "update", before, after, actor, "Updated " + ", ".join(changed), at=stamp)
            conn.execute("INSERT INTO activities (deal_id, at, kind, actor, text, op) VALUES (?,?,?,?,?,?)",
                         (deal_id, stamp, "updated", actor, "Updated " + ", ".join(changed), batch))
        return self.get(deal_id)

    def move(self, deal_id: str, stage: str, *, status: str = "open", reason: str = "", actor: str = "",
             label: str = "", expect: Optional[str] = None, delivery: Optional[str] = None) -> Optional[dict]:
        """Change a record's stage and log it.

        With ``expect`` the change only happens if the record is still at that stage. The check and
        the change share one write transaction, so two clicks, two people or two processes acting
        on the same record cannot both move it. Returns ``None`` when someone else got there first.
        """
        stamp, batch = now(), self._op()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT stage, status, fields, lost_reason, snoozed_until, stage_at, delivery FROM deals "
                               "WHERE id = ? AND archived_at = ''", (deal_id,)).fetchone()
            if row is None:
                raise KeyError(deal_id)
            if expect is not None and row["stage"] != expect:
                return None
            fields = row["fields"]
            try:
                was = json.loads(fields or "{}")
            except ValueError:
                was = {}
            before = {"stage": row["stage"], "status": row["status"], "lost_reason": row["lost_reason"],
                      "snoozed_until": row["snoozed_until"], "stage_at": row["stage_at"], "fields._prev_stage": was.get("_prev_stage"),
                      "delivery": row["delivery"]}
            delivery = row["delivery"] if delivery is None else delivery
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
                         "updated_at = ?, delivery = ? WHERE id = ?",
                         (stage, status, reason if status == "lost" else "", fields, stamp, stamp, delivery, deal_id))
            try:
                kept = json.loads(fields or "{}").get("_prev_stage")
            except ValueError:
                kept = None
            after = {"stage": stage, "status": status, "lost_reason": reason if status == "lost" else "", "snoozed_until": "",
                     "stage_at": stamp, "fields._prev_stage": kept, "delivery": delivery}
            changed = [key for key in after if after[key] != before[key]]
            self._entry(conn, batch, "deal", deal_id, deal_id, "update", {key: before[key] for key in changed},
                        {key: after[key] for key in changed}, actor, text, at=stamp)
            conn.execute("INSERT INTO activities (deal_id, at, kind, actor, text, op) VALUES (?,?,?,?,?,?)",
                         (deal_id, stamp, "stage", actor, text, batch))
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
        stamp, batch = now(), self._op()
        with self._conn() as conn:
            row = conn.execute("SELECT snoozed_until FROM deals WHERE id = ? AND archived_at = ''", (deal_id,)).fetchone()
            if row is None:
                raise KeyError(deal_id)
            said = "Snoozed until %s" % until if until else "Snooze cleared"
            conn.execute("UPDATE deals SET snoozed_until = ?, updated_at = ? WHERE id = ?", (until, stamp, deal_id))
            if until != row["snoozed_until"]:
                self._entry(conn, batch, "deal", deal_id, deal_id, "update", {"snoozed_until": row["snoozed_until"]},
                            {"snoozed_until": until}, actor, said, at=stamp)
            conn.execute("INSERT INTO activities (deal_id, at, kind, actor, text, op) VALUES (?,?,?,?,?,?)",
                         (deal_id, stamp, "snooze", actor, said, batch))
        return self.get(deal_id)

    def delete(self, deal_id: str, actor: str = "") -> bool:
        """Destroy a record and what hangs off it. Only clearing out sample data does this: everything
        a person or the agent does takes a record off the desk by archiving it, which can be put right."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
            if row is None:
                return False
            self._entry(conn, self._op(), "deal", deal_id, deal_id, "delete", before=flat(self._deal(row)), actor=actor, summary="Removed")
            conn.execute("DELETE FROM deals WHERE id = ?", (deal_id,))
            conn.execute("DELETE FROM activities WHERE deal_id = ?", (deal_id,))
            conn.execute("DELETE FROM deal_contacts WHERE deal_id = ?", (deal_id,))
            conn.execute("DELETE FROM invoices WHERE deal_id = ?", (deal_id,))
            conn.execute("DELETE FROM cards WHERE deal_id = ?", (deal_id,))
            conn.execute("DELETE FROM meta WHERE k IN (?, ?)", ("pending:" + deal_id, "handoff:" + deal_id))
        return True

    def restore(self, deal_id: str, actor: str = "") -> Optional[dict]:
        """Put back a record that was taken off the desk (its add was undone)."""
        stamp = now()
        with self._conn() as conn:
            row = conn.execute("SELECT archived_at FROM deals WHERE id = ? AND archived_at != ''", (deal_id,)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE deals SET archived_at = '', updated_at = ? WHERE id = ?", (stamp, deal_id))
            self._entry(conn, self._op(), "deal", deal_id, deal_id, "restore", {"archived_at": row["archived_at"]}, {"archived_at": ""},
                        actor, "Put back on the desk", at=stamp)
            conn.execute("INSERT INTO activities (deal_id, at, kind, actor, text) VALUES (?,?,?,?,?)",
                         (deal_id, stamp, "updated", actor, "Put back on the desk"))
        return self.get(deal_id)

    def list(self, *, status: Optional[str] = "open", stage: str = "", grp: str = "", owner: str = "",
             query: str = "") -> list:
        where, params = ["archived_at = ''"], []
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
            return conn.execute("SELECT count(*) FROM deals WHERE archived_at = ''").fetchone()[0]

    # -- activity -------------------------------------------------------------------------------
    def log(self, deal_id: str, kind: str, text: str, *, actor: str = "", ref: str = "") -> dict:
        stamp, batch = now(), self._op()
        with self._conn() as conn:
            if not conn.execute("SELECT 1 FROM deals WHERE id = ? AND archived_at = ''", (deal_id,)).fetchone():
                raise KeyError(deal_id)
            made = conn.execute("INSERT INTO activities (deal_id, at, kind, actor, text, ref, op) VALUES (?,?,?,?,?,?,?)",
                                (deal_id, stamp, kind, actor, text, ref, batch)).lastrowid
            self._entry(conn, batch, "activity", made, deal_id, "insert", after={"kind": kind, "text": text[:1000], "ref": ref},
                        actor=actor, summary=text, at=stamp)
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
            row = conn.execute("SELECT max(at) FROM activities WHERE deal_id = ? AND undone = '' AND kind NOT IN " + QUIET_KINDS,
                               (deal_id,)).fetchone()
        return row[0] or ""

    def touches(self) -> dict:
        """``last_touch`` for every record in one query: ``{deal id: timestamp}``."""
        with self._conn() as conn:
            return {row[0]: row[1] or "" for row in conn.execute(
                "SELECT deal_id, max(at) FROM activities WHERE undone = '' AND kind NOT IN " + QUIET_KINDS + " GROUP BY deal_id")}

    # -- taking a change back --------------------------------------------------------------------
    # An undo reads the ledger. What one action wrote (a batch) is taken back together, by appending
    # the opposite entries; nothing on the ledger is altered. It is refused, whole, when a value it
    # would put back has been changed again since, so a later edit is never overwritten.
    def undoable(self, deal_id: str) -> Optional[dict]:
        """The last action on a record that still stands: ``{op, at, actor, text}`` (``op`` is its batch)."""
        with self._conn() as conn:
            last = conn.execute("SELECT batch FROM ledger WHERE deal_id = ? AND " + STANDING + " ORDER BY seq DESC LIMIT 1",
                                (deal_id,)).fetchone()
            if last is None:
                return None
            first = conn.execute("SELECT at, actor, summary FROM ledger WHERE deal_id = ? AND batch = ? AND " + STANDING +
                                 " ORDER BY seq LIMIT 1", (deal_id, last["batch"])).fetchone()
        return {"op": last["batch"], "at": first["at"], "actor": first["actor"], "text": first["summary"]}

    def undoables(self) -> dict:
        """``{deal_id: batch}``: for every record, its last action that still stands (one query, for a list)."""
        with self._conn() as conn:
            rows = conn.execute("SELECT deal_id, batch FROM ledger WHERE seq IN (SELECT max(seq) FROM ledger WHERE deal_id != '' AND "
                                + STANDING + " GROUP BY deal_id)").fetchall()
        return {row["deal_id"]: row["batch"] for row in rows}

    @staticmethod
    def _live(conn, entry) -> Optional[dict]:
        """The thing a ledger entry is about, as it is now, in the ledger's own terms."""
        if entry["entity"] == "deal":
            row = conn.execute("SELECT * FROM deals WHERE id = ? AND archived_at = ''", (entry["entity_id"],)).fetchone()
            return flat(Store._deal(row)) if row else None
        table = {"contact": "contacts", "link": "deal_contacts", "invoice": "invoices"}.get(entry["entity"])
        if table is None:
            return None
        row = conn.execute("SELECT * FROM %s WHERE id = ?" % table, (entry["entity_id"],)).fetchone()
        return dict(row) if row else None

    def undo(self, deal_id: str, op: str = "", actor: str = "") -> Optional[dict]:
        """Take back an action on a record: the last one that stands, or the one whose batch is ``op``.

        Returns ``None`` when there is nothing to take back. Otherwise ``{"text", "archived", "deal",
        "conflicts"}``: with ``conflicts`` (a list of ``{field, then, now}``) nothing was changed,
        because something the action set has been changed again since.
        """
        stamp, batch = now(), self._op()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            standing = conn.execute("SELECT * FROM ledger WHERE deal_id = ? AND " + STANDING + " ORDER BY seq DESC", (deal_id,)).fetchall()
            if not standing:
                return None
            target = op or standing[0]["batch"]
            entries = [dict(row) for row in standing if row["batch"] == target]
            if not entries:
                return None
            for entry in entries:
                entry["before"], entry["after"] = json.loads(entry["before"] or "{}"), json.loads(entry["after"] or "{}")
            text = entries[-1]["summary"]
            conflicts = []
            for entry in entries:
                if entry["entity"] == "deal" and entry["op"] == "insert":
                    if any(row["batch"] != target for row in standing):     # the record has been worked on since it was added
                        conflicts.append({"field": "record", "then": "just added", "now": "changed since"})
                elif entry["op"] == "update" and entry["entity"] in ("deal", "contact", "link", "invoice"):
                    live = self._live(conn, entry)
                    if live is None or (entry["entity"] != "deal" and live.get("archived_at")):
                        conflicts.append({"field": entry["entity"], "then": "there", "now": "gone"})
                        continue
                    for key, left in entry["after"].items():
                        if key not in SOFT and live.get(key) != left:
                            conflicts.append({"field": key, "then": left, "now": live.get(key)})
                elif entry["entity"] == "link" and entry["op"] == "archive":
                    live = self._live(conn, entry)
                    if live and conn.execute("SELECT 1 FROM deal_contacts WHERE deal_id = ? AND contact_id = ? AND archived_at = ''",
                                             (live["deal_id"], live["contact_id"])).fetchone():
                        conflicts.append({"field": "person", "then": "removed", "now": "on the record again"})
            if conflicts:
                return {"text": text, "archived": False, "deal": self._deal(conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()),
                        "conflicts": conflicts}
            archived = False
            for entry in entries:       # newest first
                kind, what = entry["entity"], entry["op"]
                before, after = {}, {}
                if kind == "deal" and what == "insert":
                    conn.execute("UPDATE deals SET archived_at = ?, updated_at = ? WHERE id = ?", (stamp, stamp, deal_id))
                    before, after, archived = {"archived_at": ""}, {"archived_at": stamp}, True
                elif kind == "deal" and what == "update":
                    row = self._deal(conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone())
                    live, fields, sets, params = flat(row), dict(row["fields"]), [], []
                    for key, was in entry["before"].items():
                        if key in SOFT and live.get(key) != entry["after"].get(key):
                            continue                                   # touched since: left as it is
                        before[key], after[key] = live.get(key), was
                        if key.startswith("fields."):
                            if was is None:
                                fields.pop(key[len("fields."):], None)
                            else:
                                fields[key[len("fields."):]] = was
                        elif key == "value_cents":
                            sets += ["value_cents = ?", "value = ?"]
                            params += [int(was or 0), int(was or 0) / 100]
                        elif key in TRACKED:
                            sets.append(key + " = ?")
                            params.append(was)
                    if fields != row["fields"]:
                        sets.append("fields = ?")
                        params.append(json.dumps(fields))
                    if sets:
                        conn.execute("UPDATE deals SET %s, updated_at = ? WHERE id = ?" % ", ".join(sets), params + [stamp, deal_id])
                elif kind == "activity":
                    conn.execute("UPDATE activities SET undone = ? WHERE id = ?", (stamp, entry["entity_id"]))
                    before, after = {"undone": ""}, {"undone": stamp}
                elif kind == "contact" and what == "insert":
                    if not conn.execute("SELECT 1 FROM deal_contacts WHERE contact_id = ? AND archived_at = '' AND deal_id != ?",
                                        (entry["entity_id"], deal_id)).fetchone():        # nobody else has them: off they go too
                        conn.execute("UPDATE contacts SET archived_at = ?, updated_at = ? WHERE id = ?", (stamp, stamp, entry["entity_id"]))
                        before, after = {"archived_at": ""}, {"archived_at": stamp}
                elif kind == "invoice" and what in ("insert", "archive"):
                    gone = stamp if what == "insert" else ""                 # an added one goes; a removed one comes back
                    conn.execute("UPDATE invoices SET archived_at = ?, updated_at = ? WHERE id = ?", (gone, stamp, entry["entity_id"]))
                    before, after = {"archived_at": "" if what == "insert" else entry["after"].get("archived_at")}, {"archived_at": gone}
                elif kind in ("contact", "link", "invoice") and what == "update":
                    table, keys = {"contact": ("contacts", CONTACT), "link": ("deal_contacts", ("role",)), "invoice": ("invoices", INVOICE)}[kind]
                    put = {key: was for key, was in entry["before"].items() if key in keys}
                    if put:
                        conn.execute("UPDATE %s SET %s WHERE id = ?" % (table, ", ".join(key + " = ?" for key in put)),
                                     list(put.values()) + [entry["entity_id"]])
                        before, after = {key: entry["after"].get(key) for key in put}, put
                elif kind == "link" and what == "insert":
                    conn.execute("UPDATE deal_contacts SET archived_at = ? WHERE id = ?", (stamp, entry["entity_id"]))
                    before, after = {"archived_at": ""}, {"archived_at": stamp}
                elif kind == "link" and what == "archive":
                    conn.execute("UPDATE deal_contacts SET archived_at = '' WHERE id = ?", (entry["entity_id"],))
                    before, after = {"archived_at": entry["after"].get("archived_at")}, {"archived_at": ""}
                self._entry(conn, batch, kind, entry["entity_id"], deal_id, "undo", before, after, actor,
                            "Took back: " + entry["summary"], reverts=entry["seq"], at=stamp)
            conn.execute("UPDATE activities SET undone = ? WHERE deal_id = ? AND op = ? AND undone = ''", (stamp, deal_id, target))
            conn.execute("INSERT INTO activities (deal_id, at, kind, actor, text, op) VALUES (?,?,?,?,?,?)",
                         (deal_id, stamp, "undo", actor, "Took back: " + text, batch))
        return {"text": text, "archived": archived, "deal": None if archived else self.get(deal_id), "conflicts": []}

    # -- the other people on a record ---------------------------------------------------------------
    # A person is kept once (a contact) and linked to each record they are on, with their part in
    # that record on the link. Change their title or email and it changes everywhere they appear.
    PEOPLE = ("SELECT c.id AS id, l.id AS link, l.deal_id AS deal_id, c.name AS name, c.email AS email, c.title AS title, "
              "l.role AS role, c.linkedin AS linkedin, c.phone AS phone, c.note AS note, l.created_at AS created_at, "
              "c.updated_at AS updated_at FROM deal_contacts l JOIN contacts c ON c.id = l.contact_id "
              "WHERE l.archived_at = '' AND c.archived_at = ''")

    def people(self, deal_id: str = "") -> list:
        """The other people on one record, or every person-on-a-record there is. (A record's own name
        and email are its first person.) ``id`` is the person, the same on every record they are on."""
        with self._conn() as conn:
            if deal_id:
                rows = conn.execute(self.PEOPLE + " AND l.deal_id = ? ORDER BY l.id", (deal_id,)).fetchall()
            else:
                rows = conn.execute(self.PEOPLE + " ORDER BY l.id").fetchall()
        return [dict(row) for row in rows]

    def person(self, person_id: Any, deal_id: str = "") -> Optional[dict]:
        """One person: as they are on ``deal_id``, or on the first record they are on."""
        with self._conn() as conn:
            row = conn.execute(self.PEOPLE + " AND c.id = ?" + (" AND l.deal_id = ?" if deal_id else "") + " ORDER BY l.id LIMIT 1",
                               [person_id] + ([deal_id] if deal_id else [])).fetchone()
        return dict(row) if row else None

    def person_records(self, person_id: Any) -> list:
        """The ids of the records a person is on."""
        with self._conn() as conn:
            return [row[0] for row in conn.execute("SELECT deal_id FROM deal_contacts WHERE contact_id = ? AND archived_at = '' ORDER BY id",
                                                   (person_id,)).fetchall()]

    def person_add(self, deal_id: str, name: str, *, actor: str = "", **values: Any) -> dict:
        """Put someone on a record. Someone already known by that email is linked, not entered twice."""
        name = " ".join(str(name or "").split())[:120]
        if not name:
            raise ValueError("a name is required")
        clean = {key: " ".join(str(values.get(key) or "").split())[:300] for key in PERSON if key != "name"}
        clean["email"] = clean["email"].lower()
        stamp, batch = now(), self._op()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM deals WHERE id = ? AND archived_at = ''", (deal_id,)).fetchone():
                raise KeyError(deal_id)
            contact_id, name, said = self._contact(conn, batch, name, clean, deal_id, actor, stamp)
            if not conn.execute("SELECT 1 FROM deal_contacts WHERE deal_id = ? AND contact_id = ? AND archived_at = ''", (deal_id, contact_id)).fetchone():
                link = conn.execute("INSERT INTO deal_contacts (deal_id, contact_id, role, created_at) VALUES (?,?,?,?)",
                                    (deal_id, contact_id, clean["role"], stamp)).lastrowid
                self._entry(conn, batch, "link", link, deal_id, "insert", after={"contact_id": contact_id, "role": clean["role"]}, actor=actor,
                            summary=said, at=stamp)
                conn.execute("INSERT INTO activities (deal_id, at, kind, actor, text, op) VALUES (?,?,?,?,?,?)",
                             (deal_id, stamp, "person", actor, said, batch))
                conn.execute("UPDATE deals SET updated_at = ? WHERE id = ?", (stamp, deal_id))
        return self.person(contact_id, deal_id)

    def _contact(self, conn, batch: str, name: str, clean: dict, deal_id: str, actor: str, stamp: str) -> tuple:
        """The person these details describe: someone already known by that email (blanks filled in), or a new
        contact. ``(id, name as kept, what to say)``. Runs inside the caller's transaction."""
        known = conn.execute("SELECT * FROM contacts WHERE lower(email) = ? AND archived_at = ''", (clean["email"],)).fetchone() \
            if clean["email"] else None
        said = "Added %s%s" % (name if known is None else known["name"],
                               " (%s)" % (clean["role"] or clean["title"]) if clean["role"] or clean["title"] else "")
        if known is None:
            contact_id = conn.execute(
                "INSERT INTO contacts (name, email, title, linkedin, phone, note, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (name, clean["email"], clean["title"], clean["linkedin"], clean["phone"], clean["note"], stamp, stamp)).lastrowid
            self._entry(conn, batch, "contact", contact_id, deal_id, "insert", after=dict({key: clean[key] for key in CONTACT if key != "name"}, name=name),
                        actor=actor, summary=said, at=stamp)
            return contact_id, name, said
        fill = {key: clean[key] for key in CONTACT if key != "name" and clean[key] and not known[key]}     # only what was blank
        if fill:
            conn.execute("UPDATE contacts SET %s, updated_at = ? WHERE id = ?" % ", ".join(key + " = ?" for key in fill),
                         list(fill.values()) + [stamp, known["id"]])
            self._entry(conn, batch, "contact", known["id"], deal_id, "update", {key: known[key] for key in fill}, fill, actor,
                        "Updated %s: %s" % (known["name"], ", ".join(fill)), at=stamp)
        return known["id"], known["name"], said

    def person_update(self, person_id: Any, *, actor: str = "", deal_id: str = "", **values: Any) -> Optional[dict]:
        """Change a person. Their name, email, title and the rest change on every record they are on;
        ``role`` is their part in ``deal_id`` only."""
        person = self.person(person_id, deal_id)
        if person is None:
            return None
        deal_id = person["deal_id"]
        new = {key: " ".join(str(values[key]).split())[:300] for key in PERSON if values.get(key) is not None}
        if "email" in new:
            new["email"] = new["email"].lower()
        new = {key: value for key, value in new.items() if value != person[key] and (key != "name" or value)}
        if not new:
            return person
        stamp, batch = now(), self._op()
        said = "Updated %s: %s" % (person["name"], ", ".join(new))
        with self._conn() as conn:
            about = {key: value for key, value in new.items() if key in CONTACT}
            if about:
                try:
                    conn.execute("UPDATE contacts SET %s, updated_at = ? WHERE id = ?" % ", ".join(key + " = ?" for key in about),
                                 list(about.values()) + [stamp, person_id])
                except sqlite3.IntegrityError:
                    raise ValueError("that email already belongs to someone else on the desk")
                self._entry(conn, batch, "contact", person_id, deal_id, "update", {key: person[key] for key in about}, about, actor, said, at=stamp)
            if "role" in new:
                conn.execute("UPDATE deal_contacts SET role = ? WHERE id = ?", (new["role"], person["link"]))
                self._entry(conn, batch, "link", person["link"], deal_id, "update", {"role": person["role"]}, {"role": new["role"]}, actor, said, at=stamp)
            conn.execute("INSERT INTO activities (deal_id, at, kind, actor, text, op) VALUES (?,?,?,?,?,?)",
                         (deal_id, stamp, "person", actor, said, batch))
        return self.person(person_id, deal_id)

    def person_remove(self, person_id: Any, *, actor: str = "", deal_id: str = "") -> bool:
        """Take a person off a record. They stay on any other record they are on."""
        person = self.person(person_id, deal_id)
        if person is None:
            return False
        stamp, batch = now(), self._op()
        with self._conn() as conn:
            conn.execute("UPDATE deal_contacts SET archived_at = ? WHERE id = ?", (stamp, person["link"]))
            self._entry(conn, batch, "link", person["link"], person["deal_id"], "archive", {"archived_at": ""}, {"archived_at": stamp}, actor,
                        "Removed %s" % person["name"], at=stamp)
            conn.execute("INSERT INTO activities (deal_id, at, kind, actor, text, op) VALUES (?,?,?,?,?,?)",
                         (person["deal_id"], stamp, "person", actor, "Removed %s" % person["name"], batch))
        return True

    # -- invoices: what has been billed on a record, and what has come in ----------------------------
    # Stoa does not send invoices or move money. It keeps the account of them, in whole cents, so the
    # desk can say what is owed, what is late, and what was won but never billed.
    @staticmethod
    def _invoice(row) -> dict:
        invoice = dict(row)
        amount, paid = int(invoice["amount_cents"] or 0), int(invoice["paid_cents"] or 0)
        invoice["amount"], invoice["paid"], invoice["owed"] = amount / 100, paid / 100, 0.0
        invoice["owed_cents"], invoice["days_overdue"] = 0, 0
        if invoice["status"] in ("void", "draft"):
            invoice["state"] = invoice["status"]
        elif invoice["status"] == "paid" or (amount and paid >= amount):
            invoice["state"] = "paid"
        else:
            invoice["owed_cents"] = max(0, amount - paid)
            invoice["owed"] = invoice["owed_cents"] / 100
            late = invoice["due_on"] and invoice["due_on"] < today()
            invoice["state"] = "overdue" if late else "partial" if paid else "sent"
            if late:
                invoice["days_overdue"] = (dt.date.today() - dt.date.fromisoformat(invoice["due_on"])).days
        return invoice

    def invoices(self, deal_id: str = "") -> list:
        """The invoices on one record, or every invoice on the desk, oldest first. Removed ones are left out."""
        sql = ("SELECT i.* FROM invoices i JOIN deals d ON d.id = i.deal_id WHERE i.archived_at = '' AND d.archived_at = ''"
               + (" AND i.deal_id = ?" if deal_id else "") + " ORDER BY i.id")
        with self._conn() as conn:
            return [self._invoice(row) for row in conn.execute(sql, [deal_id] if deal_id else []).fetchall()]

    def invoice(self, invoice_id: Any) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM invoices WHERE id = ? AND archived_at = ''", (invoice_id,)).fetchone()
        return self._invoice(row) if row else None

    def invoice_numbered(self, number: Any) -> list:
        """Every invoice on the desk with this number (failing that, this id). Removed ones are left out."""
        want = str(number or "").strip().lower().lstrip("#")
        if not want:
            return []
        listed = self.invoices()
        return ([i for i in listed if i["number"] and i["number"].lower().lstrip("#") == want]
                or [i for i in listed if str(i["id"]) == want])

    def invoice_last(self, deal_id: str, invoice_id: Any = None) -> Optional[dict]:
        """The last action that still stands on a record's invoices, or on one of them: ``{op, invoice_id, text}``."""
        one = invoice_id not in (None, "")
        with self._conn() as conn:
            row = conn.execute("SELECT batch, entity_id, summary FROM ledger WHERE deal_id = ? AND entity = 'invoice' AND " + STANDING
                               + (" AND entity_id = ?" if one else "") + " ORDER BY seq DESC LIMIT 1",
                               (deal_id, str(invoice_id)) if one else (deal_id,)).fetchone()
        return {"op": row["batch"], "invoice_id": row["entity_id"], "text": row["summary"]} if row else None

    @staticmethod
    def _named(invoice: dict) -> str:
        return "Invoice %s" % invoice["number"] if invoice.get("number") else "Invoice"

    def invoice_add(self, deal_id: str, amount: Any, *, actor: str = "", number: str = "", due_on: str = "", issued_on: str = "",
                    status: str = "sent", note: str = "") -> dict:
        cents = to_cents(amount)
        if cents <= 0:
            raise ValueError("an invoice needs an amount above zero")
        for name, value in (("due", due_on), ("issued", issued_on)):
            if value and not ISO_DATE.match(value):
                raise ValueError("the %s date must be YYYY-MM-DD" % name)
        status = status if status in ("draft", "sent") else "sent"
        issued_on = issued_on or (today() if status == "sent" else "")
        number, note = " ".join(str(number or "").split())[:40], str(note or "").strip()[:500]
        stamp, batch = now(), self._op()
        with self._conn() as conn:
            if not conn.execute("SELECT 1 FROM deals WHERE id = ? AND archived_at = ''", (deal_id,)).fetchone():
                raise KeyError(deal_id)
            made = conn.execute("INSERT INTO invoices (deal_id, number, amount_cents, status, issued_on, due_on, note, created_at, updated_at) "
                                "VALUES (?,?,?,?,?,?,?,?,?)", (deal_id, number, cents, status, issued_on, due_on, note, stamp, stamp)).lastrowid
            said = "%s for %s%s%s" % ("Invoice %s" % number if number else "Invoice", _plain(cents), " (draft)" if status == "draft" else "",
                                      ", due %s" % due_on if due_on else "")
            self._entry(conn, batch, "invoice", made, deal_id, "insert",
                        after={"number": number, "amount_cents": cents, "status": status, "issued_on": issued_on, "due_on": due_on},
                        actor=actor, summary=said, at=stamp)
            conn.execute("INSERT INTO activities (deal_id, at, kind, actor, text, op) VALUES (?,?,?,?,?,?)",
                         (deal_id, stamp, "invoice", actor, said, batch))
            conn.execute("UPDATE deals SET updated_at = ? WHERE id = ?", (stamp, deal_id))
        return self.invoice(made)

    def invoice_update(self, invoice_id: Any, *, actor: str = "", said: str = "", **values: Any) -> Optional[dict]:
        """Change an invoice. ``amount`` and ``paid`` are amounts; the rest are its own columns."""
        invoice = self.invoice(invoice_id)
        if invoice is None:
            return None
        new: dict = {}
        if values.get("amount") is not None:
            new["amount_cents"] = to_cents(values["amount"])
            if new["amount_cents"] <= 0:
                raise ValueError("an invoice needs an amount above zero")
        if values.get("paid") is not None:
            new["paid_cents"] = max(0, to_cents(values["paid"]))
        for key in ("number", "status", "issued_on", "due_on", "paid_on", "note"):
            if values.get(key) is not None:
                new[key] = str(values[key]).strip()[:500 if key == "note" else 40]
        if "status" in new and new["status"] not in INVOICE_STATUS:
            raise ValueError("status is one of: %s" % ", ".join(INVOICE_STATUS))
        for key in ("issued_on", "due_on", "paid_on"):
            if new.get(key) and not ISO_DATE.match(new[key]):
                raise ValueError("dates must be YYYY-MM-DD")
        new = {key: value for key, value in new.items() if value != invoice[key]}
        if not new:
            return invoice
        stamp, batch = now(), self._op()
        said = said or "%s updated: %s" % (self._named(invoice), ", ".join(key.replace("_cents", "").replace("_on", " date") for key in new))
        with self._conn() as conn:
            conn.execute("UPDATE invoices SET %s, updated_at = ? WHERE id = ?" % ", ".join(key + " = ?" for key in new),
                         list(new.values()) + [stamp, invoice_id])
            self._entry(conn, batch, "invoice", invoice_id, invoice["deal_id"], "update", {key: invoice[key] for key in new}, new, actor, said, at=stamp)
            conn.execute("INSERT INTO activities (deal_id, at, kind, actor, text, op) VALUES (?,?,?,?,?,?)",
                         (invoice["deal_id"], stamp, "invoice", actor, said, batch))
            conn.execute("UPDATE deals SET updated_at = ? WHERE id = ?", (stamp, invoice["deal_id"]))
        return self.invoice(invoice_id)

    def invoice_pay(self, invoice_id: Any, amount: Any = None, *, on: str = "", actor: str = "") -> Optional[dict]:
        """Record money that came in: the amount given, or whatever was still owed. Paid in full closes it."""
        invoice = self.invoice(invoice_id)
        if invoice is None:
            return None
        if invoice["status"] == "void":
            raise ValueError("that invoice was voided")
        came = to_cents(amount) if amount not in (None, "") else max(0, invoice["amount_cents"] - invoice["paid_cents"])
        if came <= 0:
            raise ValueError("nothing is owed on that invoice" if amount in (None, "") else "a payment needs an amount above zero")
        paid = invoice["paid_cents"] + came
        full = paid >= invoice["amount_cents"]
        said = "%s: %s received%s" % (self._named(invoice), _plain(came), ", paid in full" if full else ", %s still owed" % _plain(invoice["amount_cents"] - paid))
        return self.invoice_update(invoice_id, actor=actor, said=said, paid=paid / 100, status="paid" if full else "sent",
                                   paid_on=(on or today()) if full else None)

    def collected(self, month: str) -> int:
        """Cents received during a month (``YYYY-MM``): every recorded payment that still stands, read off the ledger."""
        with self._conn() as conn:
            live = {str(row["id"]) for row in conn.execute(
                "SELECT i.id FROM invoices i JOIN deals d ON d.id = i.deal_id WHERE i.archived_at = '' AND d.archived_at = ''").fetchall()}
            rows = conn.execute("SELECT at, entity_id, before, after FROM ledger WHERE entity = 'invoice' AND op = 'update' AND " + STANDING).fetchall()
        total = 0
        for row in rows:
            when = parse_ts(row["at"])
            if not when or when.astimezone().date().isoformat()[:7] != month or row["entity_id"] not in live:
                continue
            try:
                before, after = json.loads(row["before"] or "{}"), json.loads(row["after"] or "{}")
            except ValueError:
                continue
            if "paid_cents" in after:
                total += int(after["paid_cents"] or 0) - int(before.get("paid_cents") or 0)
        return total

    def invoice_remove(self, invoice_id: Any, *, actor: str = "") -> bool:
        """Take an invoice off the record (entered by mistake). It is archived, and can be put back by an undo."""
        invoice = self.invoice(invoice_id)
        if invoice is None:
            return False
        stamp, batch = now(), self._op()
        with self._conn() as conn:
            conn.execute("UPDATE invoices SET archived_at = ?, updated_at = ? WHERE id = ?", (stamp, stamp, invoice_id))
            self._entry(conn, batch, "invoice", invoice_id, invoice["deal_id"], "archive", {"archived_at": ""}, {"archived_at": stamp}, actor,
                        "%s removed" % self._named(invoice), at=stamp)
            conn.execute("INSERT INTO activities (deal_id, at, kind, actor, text, op) VALUES (?,?,?,?,?,?)",
                         (invoice["deal_id"], stamp, "invoice", actor, "%s removed" % self._named(invoice), batch))
        return True

    # -- companies: a few facts kept per company, whatever records it has ---------------------------
    def org_get(self, name: Any) -> Optional[dict]:
        key = org_key(name)
        if not key:
            return None
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM orgs WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def org_put(self, name: Any, **values: Any) -> Optional[dict]:
        key = org_key(name)
        if not key:
            return None
        clean = {}
        for k in ("domain", "website", "note"):
            if values.get(k) is not None:
                clean[k] = str(values[k]).strip()[:4000] if k == "note" else " ".join(str(values[k]).split()).lower()[:200]
        with self._conn() as conn:
            conn.execute("INSERT INTO orgs (key, name, updated_at) VALUES (?,?,?) ON CONFLICT (key) DO NOTHING",
                         (key, " ".join(str(name).split())[:200], now()))
            was = dict(conn.execute("SELECT * FROM orgs WHERE key = ?", (key,)).fetchone())
            clean = {k: v for k, v in clean.items() if v != was[k]}
            if clean:
                conn.execute("UPDATE orgs SET %s, updated_at = ? WHERE key = ?" % ", ".join(k + " = ?" for k in clean),
                             list(clean.values()) + [now(), key])
                self._entry(conn, self._op(), "company", key, "", "update", {k: was[k] for k in clean}, clean, str(values.get("actor") or ""),
                            "Updated %s: %s" % (was["name"], ", ".join(clean)))
        return self.org_get(name)

    def orgs(self) -> list:
        with self._conn() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM orgs").fetchall()]

    # -- groups: a search, an account, a client engagement. Records name their group in `grp`; what is known
    #    about the group itself (who the client is, where it stands, its terms, the people on the client's side)
    #    is kept here under the same name, however it is typed. A group nobody has said anything about has no row.
    @staticmethod
    def _grp(row) -> dict:
        group = dict(row)
        try:
            group["fields"] = json.loads(group.get("fields") or "{}")
        except ValueError:
            group["fields"] = {}
        group["value"] = int(group.pop("value_cents") or 0) / 100
        return group

    def grp_get(self, name: Any) -> Optional[dict]:
        key = group_key(name)
        if not key:
            return None
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM grps WHERE key = ?", (key,)).fetchone()
        return self._grp(row) if row else None

    def grps(self) -> list:
        with self._conn() as conn:
            return [self._grp(row) for row in conn.execute("SELECT * FROM grps ORDER BY name COLLATE NOCASE").fetchall()]

    def grp_put(self, name: Any, *, actor: str = "", **values: Any) -> Optional[dict]:
        """Say something about a group: ``client``, ``status``, ``value`` (what one record in it is worth, unless the
        record says otherwise), ``note``, ``fields`` (merged; an empty value clears one). The row is made on first use."""
        key = group_key(name)
        if not key:
            return None
        clean: dict = {}
        if values.get("client") is not None:
            clean["client"] = " ".join(str(values["client"]).split())[:200]
        if values.get("status") is not None:
            status = str(values["status"]).strip().lower()
            if status not in GROUP_STATUS:
                raise ValueError("status is one of: %s" % ", ".join(GROUP_STATUS))
            clean["status"] = status
        if values.get("value") is not None:
            clean["value_cents"] = max(0, to_cents(values["value"]))
        if values.get("note") is not None:
            clean["note"] = str(values["note"]).strip()[:4000]
        stamp = now()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            made = conn.execute("INSERT INTO grps (key, name, created_at, updated_at) VALUES (?,?,?,?) ON CONFLICT (key) DO NOTHING",
                                (key, " ".join(str(name).split())[:200], stamp, stamp)).rowcount == 1
            was = dict(conn.execute("SELECT * FROM grps WHERE key = ?", (key,)).fetchone())
            if isinstance(values.get("fields"), dict):
                fields = json.loads(was["fields"] or "{}")
                for field, value in values["fields"].items():
                    text = " ".join(str(value if value is not None else "").split())[:300]
                    if text:
                        fields[str(field)[:60]] = text
                    else:
                        fields.pop(str(field)[:60], None)
                clean["fields"] = json.dumps(fields, sort_keys=True)
            clean = {k: v for k, v in clean.items() if v != was[k]}
            if clean:
                conn.execute("UPDATE grps SET %s, updated_at = ? WHERE key = ?" % ", ".join(k + " = ?" for k in clean),
                             list(clean.values()) + [stamp, key])
            if clean or made:
                self._entry(conn, self._op(), "group", key, "", "insert" if made else "update", {k: was[k] for k in clean}, clean, actor,
                            ("Opened %s" % was["name"]) if made and not clean else "Updated %s: %s" % (
                                was["name"], ", ".join(k.replace("value_cents", "value") for k in clean)), at=stamp)
        return self.grp_get(name)

    def grp_rename(self, old: Any, new: Any, *, actor: str = "") -> int:
        """Give a group another name: on every record in it, and on what is kept about it. Returns how many records."""
        was, key, name = group_key(old), group_key(new), " ".join(str(new or "").split())[:200]
        if not was or not key:
            raise ValueError("a group has a name")
        with self._conn() as conn:
            ids = [row["id"] for row in conn.execute("SELECT id, grp FROM deals WHERE archived_at = ''").fetchall() if group_key(row["grp"]) == was]
            if key != was and conn.execute("SELECT 1 FROM grps WHERE key = ?", (key,)).fetchone():
                raise ValueError("there is already one called %s" % name)
        with self.operation():
            for deal_id in ids:
                self.update(deal_id, actor=actor, grp=name)
            stamp = now()
            with self._conn() as conn:
                row = conn.execute("SELECT * FROM grps WHERE key = ?", (was,)).fetchone()
                if row is not None:
                    conn.execute("UPDATE grps SET key = ?, name = ?, updated_at = ? WHERE key = ?", (key, name, stamp, was))
                    conn.execute("UPDATE grp_contacts SET grp_key = ? WHERE grp_key = ?", (key, was))
                    self._entry(conn, self._op(), "group", key, "", "update", {"name": row["name"]}, {"name": name}, actor,
                                "Renamed %s to %s" % (row["name"], name), at=stamp)
        return len(ids)

    GRP_PEOPLE = ("SELECT c.id AS id, l.id AS link, l.grp_key AS grp_key, c.name AS name, c.email AS email, c.title AS title, "
                  "l.role AS role, c.linkedin AS linkedin, c.phone AS phone, c.note AS note FROM grp_contacts l "
                  "JOIN contacts c ON c.id = l.contact_id WHERE l.archived_at = '' AND c.archived_at = ''")

    def grp_people(self, name: Any = None) -> list:
        """The people on a group's side (the hiring manager on a search, the sponsor at an account), or all of them."""
        with self._conn() as conn:
            if name is None:
                rows = conn.execute(self.GRP_PEOPLE + " ORDER BY l.id").fetchall()
            else:
                rows = conn.execute(self.GRP_PEOPLE + " AND l.grp_key = ? ORDER BY l.id", (group_key(name),)).fetchall()
        return [dict(row) for row in rows]

    def grp_person_add(self, group: Any, name: str, *, actor: str = "", **values: Any) -> dict:
        """Put someone on a group. Someone already known by that email is linked, not entered twice."""
        key, name = group_key(group), " ".join(str(name or "").split())[:120]
        if not key:
            raise ValueError("say which group")
        if not name:
            raise ValueError("a name is required")
        clean = {k: " ".join(str(values.get(k) or "").split())[:300] for k in PERSON if k != "name"}
        clean["email"] = clean["email"].lower()
        self.grp_put(group, actor=actor)                    # the group has a row from here on
        stamp, batch = now(), self._op()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            contact_id, name, said = self._contact(conn, batch, name, clean, "", actor, stamp)
            if not conn.execute("SELECT 1 FROM grp_contacts WHERE grp_key = ? AND contact_id = ? AND archived_at = ''", (key, contact_id)).fetchone():
                link = conn.execute("INSERT INTO grp_contacts (grp_key, contact_id, role, created_at) VALUES (?,?,?,?)",
                                    (key, contact_id, clean["role"], stamp)).lastrowid
                self._entry(conn, batch, "group_link", link, "", "insert", after={"contact_id": contact_id, "role": clean["role"], "group": key},
                            actor=actor, summary=said, at=stamp)
        return next(person for person in self.grp_people(group) if person["id"] == contact_id)

    def grp_person_update(self, group: Any, person_id: Any, *, actor: str = "", **values: Any) -> Optional[dict]:
        """Change someone on a group. What they are (name, email, title...) changes wherever they appear; ``role`` is
        their part in this group only."""
        person = next((p for p in self.grp_people(group) if str(p["id"]) == str(person_id)), None)
        if person is None:
            return None
        new = {key: " ".join(str(values[key]).split())[:300] for key in PERSON if values.get(key) is not None}
        if "email" in new:
            new["email"] = new["email"].lower()
        new = {key: value for key, value in new.items() if value != person[key] and (key != "name" or value)}
        if not new:
            return person
        stamp, batch = now(), self._op()
        said = "Updated %s: %s" % (person["name"], ", ".join(new))
        with self._conn() as conn:
            about = {key: value for key, value in new.items() if key in CONTACT}
            if about:
                try:
                    conn.execute("UPDATE contacts SET %s, updated_at = ? WHERE id = ?" % ", ".join(key + " = ?" for key in about),
                                 list(about.values()) + [stamp, person["id"]])
                except sqlite3.IntegrityError:
                    raise ValueError("that email already belongs to someone else on the desk")
                self._entry(conn, batch, "contact", person["id"], "", "update", {key: person[key] for key in about}, about, actor, said, at=stamp)
            if "role" in new:
                conn.execute("UPDATE grp_contacts SET role = ? WHERE id = ?", (new["role"], person["link"]))
                self._entry(conn, batch, "group_link", person["link"], "", "update", {"role": person["role"]}, {"role": new["role"]}, actor, said, at=stamp)
        return next(p for p in self.grp_people(group) if p["id"] == person["id"])

    def grp_person_remove(self, group: Any, person_id: Any, *, actor: str = "") -> bool:
        key = group_key(group)
        person = next((p for p in self.grp_people(group) if str(p["id"]) == str(person_id)), None)
        if person is None:
            return False
        stamp = now()
        with self._conn() as conn:
            conn.execute("UPDATE grp_contacts SET archived_at = ? WHERE id = ?", (stamp, person["link"]))
            self._entry(conn, self._op(), "group_link", person["link"], "", "archive", {"archived_at": ""}, {"archived_at": stamp}, actor,
                        "Removed %s" % person["name"], at=stamp)
        return True

    # -- a copy of the file each day, beside it --------------------------------------------------------
    def snapshot(self, folder: Any, keep: int = 14) -> str:
        """Copy the file as it stands into ``folder`` as ``desk-YYYY-MM-DD.db``, once a day (a copy made earlier
        today is left alone), and keep the newest ``keep``. Returns the path written, or "" when nothing was."""
        if self._memory is not None:
            return ""
        folder, stem = Path(folder), Path(self.path).stem      # desk-2026-11-12.db, and pipe-p2-2026-11-12.db for a second pipeline
        target = folder / ("%s-%s.db" % (stem, today()))
        if target.exists():
            return ""
        with self._conn() as conn:
            if not conn.execute("SELECT (SELECT count(*) FROM deals) + (SELECT count(*) FROM ledger)").fetchone()[0]:
                return ""                               # nothing has ever been put on this desk
        folder.mkdir(parents=True, exist_ok=True)
        part = folder / (target.name + ".part")
        source, copy = sqlite3.connect(self.path, timeout=15), sqlite3.connect(str(part))
        try:
            source.backup(copy)
        finally:
            copy.close()
            source.close()
        os.replace(str(part), str(target))
        for old in sorted(folder.glob(stem + "-????-??-??.db"))[:-max(1, int(keep))]:
            old.unlink()
        return str(target)

    # -- what was declined, so it is not proposed again ----------------------------------------------
    def decline(self, digest: str, deal_id: str = "", summary: str = "") -> None:
        with self._conn() as conn:
            conn.execute("INSERT INTO declined (hash, at, deal_id, summary) VALUES (?,?,?,?) ON CONFLICT (hash) DO UPDATE SET at = excluded.at",
                         (digest, now(), deal_id, summary[:300]))

    def declined(self, max_age_days: float = 90) -> dict:
        """``{hash: when}`` for what was declined and is still remembered."""
        with self._conn() as conn:
            rows = conn.execute("SELECT hash, at FROM declined").fetchall()
        return {row["hash"]: row["at"] for row in rows if (days_since(row["at"]) or 0) <= max_age_days}

    def declined_list(self, limit: int = 50) -> list:
        with self._conn() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM declined ORDER BY at DESC LIMIT ?", (int(limit),)).fetchall()]

    def declined_clear(self) -> int:
        with self._conn() as conn:
            return conn.execute("DELETE FROM declined").rowcount

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

    def card_known(self, channel: str, ts: str) -> bool:
        """Whether a message in Slack (a card, a review, a map) was posted from this file's pipeline."""
        with self._conn() as conn:
            return conn.execute("SELECT 1 FROM cards WHERE channel = ? AND ts = ?", (channel, ts)).fetchone() is not None

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

    def intake_put(self, intake_id: str, source: str, rows: list, origin: str = "list") -> None:
        with self._conn() as conn:
            conn.execute("INSERT INTO intakes (id, at, source, status, rows, origin) VALUES (?,?,?,?,?,?)",
                         (intake_id, now(), source, "open", json.dumps(rows), origin if origin in ("list", "proposal") else "list"))

    def intake_get(self, intake_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM intakes WHERE id = ?", (intake_id,)).fetchone()
        return self._intake(row) if row else None

    def intake_list(self, status: str = "open", limit: int = 10) -> list:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM intakes WHERE status = ? ORDER BY at DESC, rowid DESC LIMIT ?",
                                (status, int(limit))).fetchall()
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
            return {"open": one("SELECT count(*) FROM deals WHERE status = 'open' AND archived_at = ''"),
                    "won": one("SELECT count(*) FROM deals WHERE status = 'won' AND archived_at = ''"),
                    "lost": one("SELECT count(*) FROM deals WHERE status = 'lost' AND archived_at = ''"),
                    "archived": one("SELECT count(*) FROM deals WHERE archived_at != ''"),
                    "ledger": one("SELECT count(*) FROM ledger"),
                    "samples": one("SELECT count(*) FROM deals WHERE source = ? AND archived_at = ''", sample_source) if sample_source else 0,
                    "activities": one("SELECT count(*) FROM activities"),
                    "from_sweeps": one("SELECT count(*) FROM activities WHERE ref != ''"),
                    "maps": one("SELECT count(*) FROM markets"),
                    "invoices": one("SELECT count(*) FROM invoices WHERE archived_at = ''"),
                    "people": one("SELECT count(*) FROM deal_contacts l JOIN contacts c ON c.id = l.contact_id "
                                  "WHERE l.archived_at = '' AND c.archived_at = ''"),
                    "declined": one("SELECT count(*) FROM declined"),
                    "lists_waiting": one("SELECT count(*) FROM intakes WHERE status = 'open'")}

    # -- CSV ------------------------------------------------------------------------------------
    def export_rows(self, field_keys: Iterable[str] = (), groups: Optional[set] = None) -> tuple:
        """The pipeline as a header and rows. ``groups`` (a set of group keys) narrows it to the records in them."""
        field_keys = list(field_keys)
        header = list(CSV_COLUMNS) + field_keys
        rows = []
        for deal in sorted(self.list(status="all"), key=lambda d: (d["grp"], d["name"])):
            if groups is not None and group_key(deal["grp"]) not in groups:
                continue
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
