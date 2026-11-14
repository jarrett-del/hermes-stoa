"""The desk: everything the tools, the Slack buttons, the slash command and the CLI can do.

No Hermes imports here. ``__init__.register`` hands in the few host services the desk needs
(settings, secrets, the data directory), which keeps this module testable on its own.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import logging
import contextvars
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Optional

from . import cards, demo, sources
from . import store as st
from .intake import Intake
from .market import Markets
from .playbook import DEFAULT, MOST_STAGES, SHAPES, Library, Playbook, PlaybookError

log = logging.getLogger("hermes.plugins.stoa")

PDL_KEY = "PDL_API_KEY"
GRANOLA_KEY = "GRANOLA_API_KEY"
LOG_KINDS = ("note", "call", "email", "meeting", "task")
HANDOFF_FRESH = 90  # seconds a "Drafting..." line is believed without news (a restart loses the watcher that clears it)
DESKTOP_FRESH = 90  # seconds since the Desktop app or Webapp half last checked in for it to count as open
TOUR_MARKER = "tour.json"       # in the data folder while sample data is on show; it names the example and its file
TOUR_FILE = re.compile(r"^tour-[0-9a-f]{8}\.db$")
# What a scheduled job may change by itself. Nobody is watching a job, so the desk decides this, not
# the prompt: anything a level does not allow is held as a proposal for a person to approve.
AUTONOMY = {
    "ask": {"label": "Ask me first", "allows": ("log",),
            "summary": "A scheduled job may log what it finds. Every other change waits for your approval."},
    "routine": {"label": "Handle the routine", "allows": ("log", "update", "done", "person"),
                "summary": "A scheduled job may also set next steps, fill in facts and add people to a record. "
                           "Stage changes, new records and closing wait for your approval."},
    "full": {"label": "Run it", "allows": ("log", "update", "done", "person", "add", "move", "close", "reopen", "snooze", "invoice", "group"),
             "summary": "A scheduled job may make any change itself. Every change is on the log and can be taken back."},
}
# Which pipeline the call in hand is about ("" = the first one). Set for the length of one tool call, one page request
# or one button click, so everything beneath reads and writes that pipeline's file with that pipeline's playbook.
PIPELINE: contextvars.ContextVar = contextvars.ContextVar("stoa_pipeline", default="")
MAIN = "main"                       # the first pipeline: desk.db, on the playbook in the plugin's settings
PIPELINES_FILE = "pipelines.json"   # the others, and what the first is called
MOST_PIPELINES = 6
NOTES_MAX = 4000                    # a record's notes: a few lines to know, not a document
TOUR_NOTE = ("The desk is showing sample data (a tour): every person and company on it is made up, and the "
             "team's own records are out of sight until the tour ends.")


def _int(value: Any, default: int, low: int = 0, high: int = 10000) -> int:
    """A whole number from a tool argument, whatever was actually passed, kept within sensible bounds."""
    try:
        number = int(float(value)) if isinstance(value, str) else int(value)
    except (TypeError, ValueError, OverflowError):
        number = default
    return max(low, min(number, high))


def _due(args: dict) -> tuple:
    """``(args with next_due as YYYY-MM-DD, error)``. A due date that cannot be read is an error, not a
    guess; an empty one means "no date", which is how a date is taken off."""
    for key in ("next_due", "close_on"):        # the same for when the record is expected to close
        if args.get(key) is None:
            continue
        if not str(args[key]).strip():
            args = dict(args, **{key: ""})
            continue
        due = st.parse_due(args[key])
        if due is None:
            return args, {"ok": False, "error": "%s should be a date such as %s (tomorrow, friday, +3d and 12/01/2026 "
                                                "also work); got '%s'. Nothing was changed."
                                                % (key, (dt.date.today() + dt.timedelta(days=3)).isoformat(), str(args[key])[:40])}
        args = dict(args, **{key: due})
    return args, None


class _Blank(dict):
    def __missing__(self, key):
        return ""


class Desk:
    def __init__(self, *, data_dir: Callable[[], Path], get_config: Callable, set_config: Callable,
                 get_secret: Callable[[str], Optional[str]], actor: Optional[Callable[[], str]] = None,
                 http: Callable = sources.http_json):
        self._data_dir = data_dir
        self._get_config = get_config
        self._set_config = set_config
        self._get_secret = get_secret
        self._actor = actor or (lambda: "")
        self.http = http
        self._stores: dict = {}
        self._copied: dict = {}                 # per file, the day its daily copy was last seen to
        self._pipes_seen: tuple = (None, {})    # pipelines.json as last read: ((path, modified), what it said)
        self._pipes_lock = threading.Lock()
        self._tour_seen: tuple = (None, {})     # the tour marker as last read: ((path, modified), what it said)
        self.can_configure = True          # False where settings cannot be saved (the page's own stand-in desk)
        self.home: Optional[Path] = None   # the Hermes profile this desk belongs to, when Hermes built it
        self.slack = None  # set by __init__ when the Slack half is wired
        self.announce: Callable[[], None] = lambda: None   # tells an open Desktop app or Webapp that the desk changed
        self.desktop_seen = 0.0     # when the Desktop/Webapp half last checked in (it does so while it is open)
        self.markets = Markets(self)
        self.intake = Intake(self)

    # -- host services --------------------------------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return Path(self._data_dir())

    def _store_at(self, name: str) -> st.Store:
        path = str(self.data_dir / name)
        if path not in self._stores:
            self._stores[path] = st.Store(path)
        return self._stores[path]

    @property
    def store(self) -> st.Store:
        """The ledger in use: the sample one during a tour, otherwise the team's own."""
        tour = self.tour()
        return self._store_at(tour["file"]) if tour else self.own_store

    @property
    def own_store(self) -> st.Store:
        """The team's own ledger, whatever is on show. Once a day a copy of it is put beside it (``backups/``,
        the last fourteen kept): the first time it is opened that day, so the copy is the desk as yesterday left it."""
        name, day = self.own_file(), st.today()
        store = self._store_at(name)
        if self._copied.get(name) != day:
            self._copied[name] = day
            try:
                store.snapshot(self.data_dir / "backups")
            except Exception as exc:
                log.warning("stoa: could not keep today's copy of the desk: %s", exc)
        return store

    def backups(self) -> dict:
        """The daily copies there are of the pipeline in hand: how many, the newest, and where."""
        folder, stem = self.data_dir / "backups", Path(self.own_file()).stem
        found = sorted(path.name for path in folder.glob(stem + "-????-??-??.db")) if folder.is_dir() else []
        return {"folder": str(folder), "count": len(found), "newest": found[-1][len(stem) + 1:len(stem) + 11] if found else ""}

    # -- pipelines: several books on one desk, each with its own file and its own playbook --------------
    # The first is desk.db on the playbook in the plugin's settings, as it always was; a desk that never adds
    # another has no pipelines.json and behaves exactly as before. The others are listed in that file.
    def _pipes(self) -> dict:
        path = self.data_dir / PIPELINES_FILE
        try:
            key = (str(path), path.stat().st_mtime_ns)
        except OSError:
            self._pipes_seen = (None, {})
            return {}
        if self._pipes_seen[0] != key:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            self._pipes_seen = (key, data if isinstance(data, dict) else {})
        return self._pipes_seen[1]

    def _pipes_write(self, data: dict) -> None:
        folder = self.data_dir
        folder.mkdir(parents=True, exist_ok=True)
        partial = folder / (".%s.%d.part" % (PIPELINES_FILE, os.getpid()))
        partial.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(partial, folder / PIPELINES_FILE)
        self._pipes_seen = (None, {})

    def pipelines(self) -> list:
        """Every pipeline on the desk, the first one first: ``{id, name, playbook, title, file, first}``."""
        data = self._pipes()
        first = self.library.load_or_default(str(self.setting("playbook", DEFAULT)))
        out = [{"id": MAIN, "name": str(data.get("name") or "").strip() or first.title, "playbook": first.name, "title": first.title,
                "file": "desk.db", "first": True}]
        for entry in data.get("more") or []:
            if isinstance(entry, dict) and re.fullmatch(r"p[0-9]{1,3}", str(entry.get("id") or "")) and not entry.get("removed"):
                playbook = self.library.load_or_default(str(entry.get("playbook") or DEFAULT))
                out.append({"id": entry["id"], "name": str(entry.get("name") or "").strip() or playbook.title, "playbook": playbook.name,
                            "title": playbook.title, "file": "pipe-%s.db" % entry["id"], "first": False})
        return out

    def _pipe(self) -> dict:
        """The entry in pipelines.json for the pipeline the call in hand is about; ``{}`` for the first (and for an
        id that is not, or is no longer, a pipeline). This is on the path of every read and write: it stays cheap."""
        want = PIPELINE.get()
        if not want or want == MAIN:
            return {}
        for entry in self._pipes().get("more") or []:
            if isinstance(entry, dict) and entry.get("id") == want and not entry.get("removed") and re.fullmatch(r"p[0-9]{1,3}", want):
                return entry
        return {}

    def current(self) -> dict:
        """The pipeline the call in hand is about, as ``pipelines()`` lists it: the one chosen for it, else the first."""
        listed, want = self.pipelines(), self._pipe().get("id") or MAIN
        return next((entry for entry in listed if entry["id"] == want), listed[0])

    def own_file(self) -> str:
        entry = self._pipe()
        return "pipe-%s.db" % entry["id"] if entry else "desk.db"

    @contextmanager
    def on(self, pipeline: str):
        """Do what is inside for one pipeline (its id; "" or "main" is the first)."""
        token = PIPELINE.set(pipeline or "")
        try:
            yield
        finally:
            PIPELINE.reset(token)

    def pipeline_pick(self, ref: Any) -> tuple:
        """``(id, error)`` for a pipeline by its name, a clear part of it, or its id."""
        listed, want = self.pipelines(), " ".join(str(ref or "").lower().split())
        if not want or want in (MAIN, "first", "default"):
            return MAIN, None
        exact = [entry for entry in listed if want in (entry["id"], entry["name"].lower())]
        near = exact or [entry for entry in listed if want in entry["name"].lower()]
        if len(near) == 1:
            return near[0]["id"], None
        return "", {"ok": False, "error": ("'%s' matches several pipelines; use one of these names" if near else "no pipeline is called '%s'") % str(ref)[:60],
                    "pipelines": [entry["name"] for entry in listed]}

    def pipeline_add(self, name: Any, playbook: Any) -> dict:
        """Open another pipeline: a name, and the playbook it runs on. It starts empty, in a file of its own."""
        name = " ".join(str(name or "").split())[:60]
        if not name:
            return {"ok": False, "error": "a pipeline needs a name (My practice, Client work)"}
        try:
            chosen = self.library.load(str(playbook or "").strip() or DEFAULT)
        except (PlaybookError, OSError, KeyError) as exc:
            return {"ok": False, "error": "no playbook called '%s': %s" % (str(playbook)[:60], exc), "playbooks": [p["name"] for p in self.library.catalog()]}
        with self._pipes_lock:
            listed = self.pipelines()
            if any(entry["name"].lower() == name.lower() for entry in listed):
                return {"ok": False, "error": "there is already a pipeline called %s" % name, "pipelines": [entry["name"] for entry in listed]}
            if len(listed) >= MOST_PIPELINES:
                return {"ok": False, "error": "a desk holds at most %d pipelines" % MOST_PIPELINES}
            data = dict(self._pipes())
            more = [entry for entry in data.get("more") or [] if isinstance(entry, dict)]
            number = max([1] + [int(str(entry.get("id") or "p1")[1:]) for entry in more if re.fullmatch(r"p[0-9]{1,3}", str(entry.get("id") or ""))]) + 1
            made = {"id": "p%d" % number, "name": name, "playbook": chosen.name, "added": st.now()}
            data["more"] = more + [made]
            self._pipes_write(data)
        with self.on(made["id"]):
            self.own_store                              # its file exists from now on
        return {"ok": True, "added": {"id": made["id"], "name": name, "playbook": chosen.name, "title": chosen.title},
                "pipelines": [entry["name"] for entry in self.pipelines()]}

    def pipeline_rename(self, ref: Any, name: Any) -> dict:
        found, error = self.pipeline_pick(ref)
        if error:
            return error
        name = " ".join(str(name or "").split())[:60]
        if not name:
            return {"ok": False, "error": "say what to call it"}
        with self._pipes_lock:
            if any(entry["name"].lower() == name.lower() and entry["id"] != found for entry in self.pipelines()):
                return {"ok": False, "error": "there is already a pipeline called %s" % name}
            data = dict(self._pipes())
            if found == MAIN:
                data["name"] = name
            else:
                data["more"] = [dict(entry, name=name) if isinstance(entry, dict) and entry.get("id") == found else entry for entry in data.get("more") or []]
            self._pipes_write(data)
        return {"ok": True, "renamed": found, "pipelines": [entry["name"] for entry in self.pipelines()]}

    def pipeline_remove(self, ref: Any) -> dict:
        """Take a pipeline off the desk. Only an empty one, and never the first; its file is left where it is."""
        found, error = self.pipeline_pick(ref)
        if error:
            return error
        if found == MAIN:
            return {"ok": False, "error": "the first pipeline stays; rename it or change its playbook instead"}
        with self.on(found):
            holds = self.own_store.count()
        if holds:
            return {"ok": False, "error": "that pipeline still has %d record%s in it; it is only removed when it is empty" % (holds, "" if holds == 1 else "s")}
        with self._pipes_lock:
            data = dict(self._pipes())
            data["more"] = [dict(entry, removed=st.now()) if isinstance(entry, dict) and entry.get("id") == found else entry for entry in data.get("more") or []]
            self._pipes_write(data)
        return {"ok": True, "removed": found, "pipelines": [entry["name"] for entry in self.pipelines()]}

    # -- the tour: sample data, in a file of its own ----------------------------------------------
    def tour(self) -> dict:
        """``{"example", "file", "since"}`` while the desk is showing sample data, else ``{}``.

        The marker is a small file in the plugin's data folder, so every process that serves this
        profile (the gateway, the dashboard, a terminal) agrees on it. While it is there the team's
        own ``desk.db`` is not read or written by anything but the Setup tab's counts.
        """
        marker = self.data_dir / TOUR_MARKER
        try:
            key = (str(marker), marker.stat().st_mtime_ns)
        except OSError:
            self._tour_seen = (None, {})
            return {}
        if self._tour_seen[0] != key:
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            good = isinstance(data, dict) and data.get("example") in demo.EXAMPLES and TOUR_FILE.match(str(data.get("file") or ""))
            self._tour_seen = (key, {"example": data["example"], "file": data["file"],
                                     "since": str(data.get("since") or "")} if good else {})
        return self._tour_seen[1]

    def tour_start(self, which: str = "") -> dict:
        """Show a worked example in place of the team's own desk. Every tour starts from a fresh copy."""
        which = str(which or "").strip().lower() or demo.which_for(self.own_playbook())
        if which not in demo.EXAMPLES:
            return {"ok": False, "error": "no example named '%s'" % which[:40], "examples": sorted(demo.EXAMPLES)}
        self.tour_end()
        folder = self.data_dir
        folder.mkdir(parents=True, exist_ok=True)
        partial = folder / (".%s.%d.part" % (TOUR_MARKER, os.getpid()))
        partial.write_text(json.dumps({"example": which, "file": "tour-%s.db" % os.urandom(4).hex(), "since": st.now()}),
                           encoding="utf-8")
        os.replace(partial, folder / TOUR_MARKER)
        self._tour_seen = (None, {})
        added = demo.load(self, which)
        return {"ok": True, "example": which, "title": demo.EXAMPLES[which]["title"], "added": added,
                "summary": demo.summary(self, which)}

    def tour_end(self) -> bool:
        """Back to the team's own desk; the sample file is thrown away. Returns whether a tour was on."""
        folder = self.data_dir
        was = bool(self.tour())
        try:
            (folder / TOUR_MARKER).unlink()
        except OSError:
            pass
        self._tour_seen = (None, {})
        for path in folder.glob("tour-*.db*"):
            if not TOUR_FILE.match(path.name.split(".db")[0] + ".db"):
                continue
            self._stores.pop(str(path), None)
            try:
                path.unlink()
            except OSError:
                pass
        return was

    def stray_samples(self) -> int:
        """Sample records sitting in the team's own ledger: versions before 0.10 loaded the examples there."""
        return self.own_store.tally(demo.SOURCE)["samples"]

    def tidy_samples(self) -> dict:
        """Take those sample records (and the sample maps) out of the team's own ledger."""
        if self.tour():
            return {"ok": False, "error": "Leave the tour first; then the sample records in your own desk can be removed."}
        return {"ok": True, "removed": demo.clear(self)}

    @property
    def library(self) -> Library:
        return Library(self.data_dir / "playbooks")

    def setting(self, key: str, default: Any = None) -> Any:
        try:
            value = self._get_config(key, default)
        except Exception:
            return default
        return default if value in (None, "") else value

    def secret(self, name: str) -> str:
        try:
            return (self._get_secret(name) or "").strip()
        except Exception:
            return ""

    def own_playbook(self) -> Playbook:
        """The playbook the team chose for the pipeline in hand: the ``playbook`` setting for the first one, what
        was said when it was opened (or since) for another."""
        entry = self._pipe()
        return self.library.load_or_default(str(entry.get("playbook") or DEFAULT) if entry else str(self.setting("playbook", DEFAULT)))

    def playbook(self) -> Playbook:
        """The playbook in use: the example's own during a tour, otherwise the team's."""
        tour = self.tour()
        if tour:
            return self.library.load_or_default(demo.EXAMPLES[tour["example"]]["playbook"])
        return self.own_playbook()

    def actor(self) -> str:
        try:
            return self._actor() or "agent"
        except Exception:
            return "agent"

    def allowed(self, session: Optional[dict]) -> bool:
        """Whether the desk is available in this conversation (the ``channels`` setting)."""
        allow = self.setting("channels", []) or []
        if isinstance(allow, str):
            allow = [c.strip() for c in allow.split(",") if c.strip()]
        session = session or {}
        if session.get("platform") == "cron" and self.tour():
            return False        # scheduled jobs wait while sample data is on show: they are for the team's own records
        if not allow:
            return True
        if session.get("platform", "") in ("", "cli", "tui", "desktop", "local"):
            return True
        if session.get("platform") == "cron":        # scheduled jobs have no chat: they are opted in by name
            return "cron" in allow
        return session.get("chat_id", "") in allow

    def slack_users(self) -> set:
        """Slack user ids who may press the buttons on any card (the ``slack_users`` setting). With
        nobody listed, only the person a card was posted for may press its buttons."""
        listed = self.setting("slack_users", []) or []
        if isinstance(listed, str):
            listed = listed.replace(",", " ").split()
        return {str(item).strip() for item in listed if str(item).strip()}

    # -- files the desk may touch on a model's say-so ------------------------------------------------
    def import_roots(self) -> list:
        """The folders a CSV may be read from when the path arrives in a tool call: the plugin's own
        ``imports`` folder, the folder named in the ``import_dir`` setting, and the working directory
        when that is a project folder (not the home directory or anything above it)."""
        roots = [self.data_dir / "imports"]
        configured = str(self.setting("import_dir", "") or "").strip()
        if configured:
            roots.append(Path(configured).expanduser())
        try:
            here, home = Path.cwd().resolve(), Path.home().resolve()
            if here != home and here not in home.parents:
                roots.append(here)
        except (OSError, RuntimeError):
            pass
        out = []
        for root in roots:
            try:
                resolved = root.resolve()
            except (OSError, RuntimeError):
                continue
            if resolved not in out:
                out.append(resolved)
        return out

    def csv_path(self, given: Any, trusted: bool = False) -> tuple:
        """``(path, None)`` for a CSV the desk may read, or ``(None, error)``. Only ``.csv`` and ``.tsv``.
        A path a person typed at a terminal is ``trusted``; one that arrives in a tool call has to sit
        inside one of the import folders once every symlink in it is resolved."""
        raw = str(given or "").strip()
        if not raw:
            return None, {"ok": False, "error": "this needs the path of a CSV file"}
        path = Path(raw).expanduser()
        if path.suffix.lower() not in (".csv", ".tsv"):
            return None, {"ok": False, "error": "only a .csv or .tsv file can be read from a path. For anything else (a PDF, a "
                                                "spreadsheet, an image), read it yourself and pass what you find as rows."}
        roots = [] if trusted else self.import_roots()
        if not trusted and not path.is_absolute():       # a bare file name: look for it in the import folders
            found = [root / path for root in roots if (root / path).is_file()]
            path = found[0] if found else roots[0] / path
        try:
            path = path.resolve()
        except (OSError, RuntimeError):
            return None, {"ok": False, "error": "that path cannot be read"}
        if not trusted and not any(root in path.parents for root in roots):
            try:
                (self.data_dir / "imports").mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            return None, {"ok": False, "error": "that file is outside the folders the desk reads from by path (%s). Put the CSV in "
                                                "one of them, or read it yourself and pass its rows." % ", ".join(str(r) for r in roots)}
        if not path.is_file():
            return None, {"ok": False, "error": "no file at %s" % path}
        return path, None

    def export_path(self, name: Any, default: str, suffix: str = ".csv") -> Path:
        """A file in the plugin's ``exports`` folder and nowhere else. ``name`` is a file name; anything
        that reads as a path, or that would land outside the folder, raises ``ValueError``."""
        folder = self.data_dir / "exports"
        folder.mkdir(parents=True, exist_ok=True)
        folder = folder.resolve()
        name = str(name or default).strip()
        pathlike = (not name or name.startswith(".") or ".." in name or len(name) > 120 or Path(name).is_absolute()
                    or any(mark in name for mark in ("/", "\\", "\x00", os.sep) + ((os.altsep,) if os.altsep else ())))
        if pathlike:
            raise ValueError("export takes a file name, not a path: the file is always written to %s" % folder)
        if not name.lower().endswith(suffix):
            name += suffix
        path = (folder / name).resolve()
        if path.parent != folder:
            raise ValueError("export takes a file name, not a path: the file is always written to %s" % folder)
        return path

    def desktop_open(self, session: Optional[dict] = None) -> bool:
        """Whether this conversation is in the Hermes Desktop app or Webapp with Stoa's half loaded,
        where a card or a map can be drawn live in the conversation instead of pasted as text."""
        return (session or {}).get("platform") in ("local", "desktop") and time.time() - self.desktop_seen <= DESKTOP_FRESH

    def can_enrich(self, playbook: Optional[Playbook] = None) -> bool:
        """Whether a record can be enriched: the key is set and the playbook has not switched it off."""
        return bool(self.secret(PDL_KEY)) and (playbook or self.playbook()).enrich != "none" and not self.tour()

    # -- shared helpers -------------------------------------------------------------------------
    def _resolve(self, ref: Any) -> tuple:
        """``(deal, error_dict)`` for an id or name."""
        ref = str(ref or "").strip()
        if not ref:
            return None, {"ok": False, "error": "say which record: pass `deal` (its id or name)"}
        try:
            deal = self.store.find(ref)
        except st.Ambiguous as exc:
            return None, {"ok": False, "error": "'%s' matches several records; use one of these ids" % ref,
                          "matches": [{"id": d["id"], "name": d["name"], "company": d["company"], "group": d["grp"],
                                       "stage": d["stage"], "status": d["status"]} for d in exc.matches]}
        if deal is None:
            return None, {"ok": False, "error": "no record matches '%s'" % ref}
        return deal, None

    def view(self, deal: dict, playbook: Optional[Playbook] = None, activities: int = 0,
             touches: Optional[dict] = None, groups: Optional[dict] = None) -> dict:
        """A deal as the model sees it: empty values dropped, playbook words applied.

        ``touches`` is ``store.touches()`` and ``groups`` is ``{key: row}`` of ``store.grps()`` when the caller is
        showing many records at once.
        """
        playbook = playbook or self.playbook()
        touch = touches.get(deal["id"], "") if touches is not None else self.store.last_touch(deal["id"])
        out = {"id": deal["id"], "name": deal["name"], "company": deal["company"], "group": deal["grp"],
               "stage": deal["stage"], "stage_label": playbook.label(deal["stage"]), "status": deal["status"],
               "value": deal["value"], "owner": deal["owner"], "next_step": deal["next_step"], "next_due": deal["next_due"],
               "email": deal["email"], "phone": deal.get("phone") or "", "linkedin": deal["linkedin"], "source": deal["source"],
               "lost_reason": deal["lost_reason"], "snoozed_until": deal["snoozed_until"], "close_on": deal.get("close_on") or "",
               "delivery": deal.get("delivery") or "" if deal["status"] == "won" else "",
               "delivery_label": playbook.delivery_label(deal.get("delivery") or "") if deal["status"] == "won" else "",
               "fields": {k: v for k, v in deal["fields"].items() if not k.startswith("_")}, "notes": deal.get("notes") or "",
               "last_touch": touch, "stale": cards.is_stale(playbook, deal, touch), "due": cards.is_due(deal)}
        if deal["grp"]:
            group = (groups.get(st.group_key(deal["grp"])) if groups is not None else self.store.grp_get(deal["grp"])) or {}
            out["client"] = group.get("client") or ""
            out["group_status"] = self.GROUP_LABELS[group["status"]] if group.get("status") not in (None, "open") else ""
        out = {k: v for k, v in out.items() if v not in ("", None, 0, 0.0, {}, False)}
        if activities:
            out["activity"] = [{"at": a["at"], "kind": a["kind"], "text": a["text"], "by": a["actor"]}
                               for a in self.store.activities(deal["id"], limit=activities)]
        return out

    def text_card(self, deal: dict) -> str:
        return cards.text_card(self.playbook(), deal, self.store.activities(deal["id"], limit=3),
                               self.store.last_touch(deal["id"]))

    def blocks(self, deal: dict) -> list:
        return cards.card_blocks(self.playbook(), deal, self.store.activities(deal["id"], limit=2),
                                 self.store.last_touch(deal["id"]), can_enrich=self.can_enrich(),
                                 draft=self.handoff(deal["id"]))

    def _changed(self, deal: dict) -> None:
        """A record changed: bring any card already posted for it up to date."""
        if self.slack is not None:
            try:
                self.slack.refresh(deal["id"])
            except Exception:
                log.debug("stoa: card refresh failed", exc_info=True)

    # -- gtm_pipeline ---------------------------------------------------------------------------
    def pipeline(self, args: dict) -> dict:
        playbook = self.playbook()
        view = str(args.get("view") or "board").lower()
        if view == "brief":
            return self.brief()
        if view == "today":
            return self.today()
        if view == "cash":
            return self.cash(limit=15)
        if view == "activity":
            days = _int(args.get("days") or 7, 7, 1, 3650)
            since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            acts = self.store.activities(limit=_int(args.get("limit") or 40, 40, 1, 500), since=since)
            return {"ok": True, "view": "activity", "days": days, "activity": [
                {"deal": a["deal_id"], "at": a["at"], "kind": a["kind"], "text": a["text"], "by": a["actor"]} for a in acts]}
        stage = playbook.resolve_stage(str(args.get("stage") or "")) or ""
        status = str(args.get("status") or "open").lower()
        deals = self.store.list(status=status, stage=stage, grp=str(args.get("group") or ""),
                                owner=str(args.get("owner") or ""), query=str(args.get("query") or ""))
        touches = self.store.touches()
        if view == "stale":
            deals = [d for d in deals if cards.is_stale(playbook, d, touches.get(d["id"], ""))]
        elif view == "due":
            deals = sorted([d for d in deals if cards.is_due(d)], key=cards.due_date)
        limit = _int(args.get("limit") or 60, 60, 1, 500)
        out = {"ok": True, "view": view, "playbook": playbook.name, "count": len(deals),
               "open_value": st.total(d for d in deals if d["status"] == "open")}
        if view == "board":
            out["board"] = cards.board_text(playbook, deals)
            out["by_stage"] = self._by_stage(playbook, deals)
        out["deals"] = [self.view(d, playbook, touches=touches) for d in deals[:limit]]
        if len(deals) > limit:
            out["truncated"] = len(deals) - limit
        if not self.store.count():
            out["hint"] = ("The desk is empty. Add records with gtm_deal(action='add'), import a CSV with "
                           "gtm_export(action='import'), or load sample data with `hermes stoa demo`.")
        return out

    @staticmethod
    def _by_stage(playbook: Playbook, deals: list) -> list:
        rows = []
        for stage in playbook.stages:
            here = [d for d in deals if d["stage"] == stage["id"] and d["status"] == "open"]
            rows.append({"stage": stage["id"], "label": stage["label"], "count": len(here), "value": st.total(here)})
        return rows

    def brief(self) -> dict:
        """What needs a person today. The morning-brief automation is built on this."""
        playbook = self.playbook()
        open_deals = self.store.list(status="open")
        week_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        stale, due = [], []
        touches = self.store.touches()
        for deal in open_deals:
            touch = touches.get(deal["id"], "")
            if cards.is_due(deal):
                due.append(self.view(deal, playbook, touches=touches))
            elif cards.is_stale(playbook, deal, touch):
                row = self.view(deal, playbook, touches=touches)
                row["quiet_days"] = int(st.days_since(touch or deal["updated_at"]) or 0)
                stale.append(row)
        closed = self.store.list(status="all")
        won = [d for d in closed if d["status"] == "won" and d["stage_at"] >= week_ago]
        lost = [d for d in closed if d["status"] == "lost" and d["stage_at"] >= week_ago]
        moves = [a for a in self.store.activities(limit=200, since=week_ago) if a["kind"] == "stage"]
        notes = self.notes({"action": "inbox"})
        return {
            "ok": True, "view": "brief", "playbook": playbook.name, "date": st.today(),
            "terms": playbook.terms,
            "open": len(open_deals), "open_value": st.total(open_deals),
            "by_stage": self._by_stage(playbook, open_deals),
            "due": sorted(due, key=lambda d: d.get("next_due", ""))[:15],
            "stale": sorted(stale, key=lambda d: -d["quiet_days"])[:15],
            "this_week": {"new": len([d for d in open_deals if d["created_at"] >= week_ago]), "stage_moves": len(moves),
                          "won": [{"name": d["name"], "value": d["value"]} for d in won],
                          "lost": [{"name": d["name"], "reason": d["lost_reason"]} for d in lost]},
            "notes_waiting": len(notes.get("notes") or []),
        }

    def today(self, full: bool = True) -> dict:
        """What wants a person today, most pressing first: next steps overdue and due, lists and drafts
        waiting on them, records gone quiet, and new ones with nothing planned. A snoozed record is on
        none of the lists until its date. ``full`` gives each record as the model sees it; without it
        a row is the record's id and the fact that put it on the list (the page has the rest)."""
        playbook = self.playbook()
        date, day = st.today(), dt.date.today()
        week_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        soon = (day + dt.timedelta(days=7)).isoformat()
        touches, pending = self.store.touches(), self.store.meta_prefix("pending:")
        lists = {key: [] for key in ("overdue", "due_today", "quiet", "new", "coming", "snoozed", "drafts")}
        by_id = {}
        for deal in self.store.list(status="open"):
            by_id[deal["id"]] = deal
            due, touch = cards.due_date(deal), touches.get(deal["id"], "")
            if pending.get(deal["id"]):
                lists["drafts"].append({"id": deal["id"]})
            if cards.snoozed(deal):
                lists["snoozed"].append({"id": deal["id"], "until": deal["snoozed_until"]})
            elif due and due < date:
                lists["overdue"].append({"id": deal["id"], "days": (day - dt.date.fromisoformat(due)).days})
            elif due == date:
                lists["due_today"].append({"id": deal["id"]})
            elif cards.is_stale(playbook, deal, touch):
                lists["quiet"].append({"id": deal["id"], "days": int(st.days_since(touch or deal["updated_at"]) or 0),
                                       "expected": playbook.stale_days(deal["stage"])})
            elif not deal["next_step"].strip() and deal["created_at"] >= week_ago:
                lists["new"].append({"id": deal["id"], "days": int(st.days_since(deal["created_at"]) or 0)})
            elif due and due <= soon:
                lists["coming"].append({"id": deal["id"], "due": due})
        lists["overdue"].sort(key=lambda row: -row["days"])
        lists["quiet"].sort(key=lambda row: -row["days"])
        lists["new"].sort(key=lambda row: row["days"])
        lists["coming"].sort(key=lambda row: row["due"])
        lists["snoozed"].sort(key=lambda row: row["until"])
        approvals = self.intake.queue()
        # Money: invoices past their date, and (once the desk keeps invoices at all) recent wins with nothing billed.
        everyone = {deal["id"]: deal for deal in self.store.list(status="all")}
        every_invoice = self.store.invoices()
        invoices = [i for i in every_invoice if i["deal_id"] in everyone]
        clients = {st.client_ref(org["name"]): org["name"] for org in self.store.orgs()}
        lists["invoices_overdue"] = sorted(
            [{"id": i["deal_id"], "invoice": i["id"], "number": i["number"], "owed": i["owed"], "days": i["days_overdue"], "due": i["due_on"]}
             for i in invoices if i["state"] == "overdue" and not cards.snoozed(everyone[i["deal_id"]])], key=lambda row: -row["days"])
        billed: dict = {}
        for invoice in invoices:
            if invoice["state"] not in ("void", "draft"):
                billed[invoice["deal_id"]] = billed.get(invoice["deal_id"], 0) + invoice["amount_cents"]
        recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lists["to_invoice"] = [{"id": deal["id"], "amount": deal["value"], "days": int(st.days_since(deal["stage_at"]) or 0)}
                               for deal in everyone.values()
                               if invoices and playbook.billing != "none" and deal["status"] == "won" and deal["value_cents"] > 0 and not billed.get(deal["id"])
                               and deal["stage_at"] >= recent and not cards.snoozed(deal)]
        by_id = dict(everyone, **by_id)
        to_bill = self.to_bill()
        client_late = sorted([{"client": clients[i["deal_id"]], "invoice": i["id"], "number": i["number"], "owed": i["owed"], "days": i["days_overdue"],
                               "period": i.get("period") or ""} for i in every_invoice if i["deal_id"] in clients and i["state"] == "overdue"],
                             key=lambda row: -row["days"])
        counts = dict({key: len(rows) for key, rows in lists.items()}, approvals=len(approvals), to_bill=len(to_bill), client_overdue=len(client_late))
        needs = sum(counts[key] for key in ("overdue", "due_today", "quiet", "new", "drafts", "approvals", "invoices_overdue", "to_invoice",
                                            "to_bill", "client_overdue"))
        if full:
            lists = {key: [dict(self.view(by_id[row["id"]], playbook, touches=touches), **{k: v for k, v in row.items() if k != "id"})
                           for row in rows[:15]] for key, rows in lists.items()}
        out = dict(lists, ok=True, view="today", date=date, playbook=playbook.name, approvals=approvals, counts=counts, needs=needs,
                   to_bill=to_bill, client_overdue=client_late)
        if full:
            out["terms"] = playbook.terms
            if self.tour():
                out["note"] = TOUR_NOTE
        return out

    def today_text(self) -> str:
        data, playbook = self.today(), self.playbook()
        lines = ["Today · %s · %s" % (data["date"], playbook.title)]
        if self.tour():
            lines.append("Sample data: nothing here is real.")
        counts = data["counts"]

        def section(title: str, key: str, row) -> None:
            if data[key]:
                lines.extend(["", "%s (%d)" % (title, counts[key])] + ["  " + row(item) for item in data[key]])

        section("Overdue", "overdue", lambda d: "%s — %s (%dd overdue)" % (d["name"], d.get("next_step", "next step"), d["days"]))
        section("Due today", "due_today", lambda d: "%s — %s" % (d["name"], d.get("next_step", "next step")))
        if data["approvals"]:
            lines += ["", "Waiting for your approval (%d)" % counts["approvals"]] + [
                "  " + (self.intake.headline(self.store.intake_get(item["intake"])) if item.get("list")
                        else self.intake._line(item)[len("%d. " % item["n"]):]) for item in data["approvals"]]
        section("Writing waiting to be picked up", "drafts", lambda d: "%s — %s" % (d["name"], d["stage_label"]))
        money = lambda value: cards.money(value, playbook.currency) or "0"       # noqa: E731
        section("Invoices overdue", "invoices_overdue", lambda d: "%s — %s%s, %dd late" % (
            d["name"], money(d["owed"]), " (#%s)" % d["number"] if d["number"] else "", d["days"]))
        section("%s, not invoiced" % playbook.won["label"], "to_invoice", lambda d: "%s — %s, %dd ago" % (d["name"], money(d["amount"]), d["days"]))
        section("To bill", "to_bill", lambda d: "%s — %s: %s%s" % (d["bill_to"] if d["bill_to"] == d["client"] else "%s (to %s)" % (d["client"], d["bill_to"]),
                                                                  d["label"], cards.exact(d["amount"], playbook.currency), " for %s h" % int(d["hours"]) if d.get("hours") else ""))
        section("Client invoices overdue", "client_overdue", lambda d: "%s — %s%s, %dd late" % (
            d["client"], money(d["owed"]), " (#%s)" % d["number"] if d["number"] else "", d["days"]))
        section("Gone quiet", "quiet", lambda d: "%s — %s, quiet %dd (a nudge is due after %dd)"
                % (d["name"], d["stage_label"], d["days"], d["expected"]))
        section("New, nothing planned yet", "new", lambda d: "%s — %s" % (d["name"], d.get("company") or d["stage_label"]))
        section("Coming up this week", "coming", lambda d: "%s — %s (%s)" % (d["name"], d.get("next_step", ""), d["due"]))
        if counts["snoozed"]:
            lines += ["", "%d snoozed" % counts["snoozed"]]
        if not data["needs"]:
            lines.append("Nothing needs you today.")
        return "\n".join(lines)

    def setup(self) -> dict:
        """What is set up, what is not, and the rules in force, for the Setup tab. A key or a token is
        reported as set or not set; its value never leaves the server."""
        from . import blueprints
        problems: list = []

        def read(what: str, fn: Callable, fallback: Any) -> Any:
            """One thing Setup reports, read by itself: this is the tab people open when something is
            wrong, so a part that cannot be read is named on it and logged, and the rest still shows."""
            try:
                return fn()
            except Exception as exc:
                log.warning("stoa: setup could not read %s: %s", what, exc, exc_info=True)
                problems.append("%s: %s: %s" % (what, type(exc).__name__, str(exc)[:200]))
                return fallback

        tour = read("the tour", self.tour, {})
        own = self.own_playbook()
        playbook = read("the playbook on show", self.playbook, own)
        empty = dict.fromkeys(("open", "won", "lost", "archived", "ledger", "samples", "activities", "from_sweeps", "maps", "people",
                               "declined", "lists_waiting"), 0)
        tally = dict(empty, **read("what is on the desk", lambda: self.own_store.tally(demo.SOURCE), {}))
        real = tally["open"] + tally["won"] + tally["lost"] - tally["samples"]
        chosen = read("the playbook setting", lambda: self.setting("playbook", None) is not None, False)
        slack = read("Slack", lambda: None if self.slack is None else bool(getattr(self.slack, "available", False)), None)
        pdl = read("the lookup key", lambda: bool(self.secret(PDL_KEY)), False)
        granola = read("the Granola key", lambda: bool(self.secret(GRANOLA_KEY)), False)
        notes_dir = read("the notes folder", lambda: str(self.setting("notes_dir", "") or ""), "")
        cap = read("the lookup cap", lambda: _int(self.setting("pdl_monthly_cap", 50), 50, 0, 10 ** 6), 50)
        used = read("lookups used", lambda: _int(self.own_store.meta_get("pdl_calls:" + st.today()[:7], "0"), 0, 0, 10 ** 9), 0)
        allow = read("the chats setting", lambda: self.setting("channels", []) or [], [])
        if isinstance(allow, str):
            allow = [c.strip() for c in allow.split(",") if c.strip()]
        allow = [str(c) for c in allow] if isinstance(allow, (list, tuple, set)) else []
        chats = [c for c in allow if c != "cron"]
        users = read("who may press Slack buttons", lambda: sorted(self.slack_users()), [])
        roots = read("the folders it may read", lambda: [str(root) for root in self.import_roots()], [])
        catalog = read("the playbooks", self.library.catalog, [])
        level = read("the autonomy setting", self.autonomy, "ask")
        terms = own.terms
        size = read("the size of the file", lambda: (self.data_dir / self.own_file()).stat().st_size, 0)
        checklist = [
            {"id": "playbook", "done": chosen or real > 0, "title": "Set up your pipeline: what it is for, and its stages",
             "detail": "You are on %s: %s." % (own.title, " → ".join(s["label"] for s in own.stages) + " → " + own.won["label"])},
            {"id": "records", "done": real > 0, "title": "Put your first %s on the desk" % terms["deals"],
             "detail": ("%d on the desk." % real) if real else "Add one by hand, tell Hermes about one, or drop a CSV on the page."},
            {"id": "slack", "done": bool(slack), "title": "Connect Slack",
             "detail": "Connected: cards with buttons, and !gtm in any chat the desk is allowed in." if slack
             else "Once Hermes is in your Slack, records post there as cards whose buttons move the pipeline."},
            {"id": "sweep", "done": tally["from_sweeps"] > 0, "title": "Have Hermes log email, calls and Slack for you",
             "detail": ("%d entries logged from your inbox, calls and Slack so far." % tally["from_sweeps"]) if tally["from_sweeps"]
             else "Type /blueprint in a chat and switch on “%s”. It logs what it finds and asks before any stage change."
             % blueprints.titled("activity-sweep")},
            {"id": "notes", "done": granola or bool(notes_dir), "title": "Point it at your meeting notes",
             "detail": "Connected." if granola or notes_dir else "Set a notes folder or a Granola key in the plugin's settings, and call notes "
                                                               "are filed on the right record with the next step they imply."},
        ]
        connections = [
            {"id": "slack", "label": "Slack", "on": slack,
             "detail": "Cards with buttons, market maps and list reviews post here." if slack
             else "Not visible from this page." if slack is None else "Not connected for this agent."},
            {"id": "lookup", "label": "People Data Labs lookups", "on": pdl and own.enrich != "none",
             "detail": ("%d of %d lookups used this month." % (used, cap) if cap else "%d lookups used this month; no cap." % used) if pdl and own.enrich != "none"
             else "Switched off in the %s playbook." % own.name if pdl else "No key set (PDL_API_KEY). Optional."},
            {"id": "granola", "label": "Granola meeting notes", "on": granola,
             "detail": "Key set." if granola else "No key set (GRANOLA_API_KEY). Optional."},
            {"id": "notes", "label": "Meeting notes folder", "on": bool(notes_dir), "detail": notes_dir or "Not set (notes_dir). Optional."},
            {"id": "google", "label": "Gmail, Calendar and Sheets", "on": None,
             "detail": "Stoa uses Hermes's own Google tools when they are connected; it holds no Google access itself."},
        ]
        rules = [
            {"label": "Where the desk answers",
             "value": "Only in these chats: %s (and the Desktop app, Webapp and terminal)." % ", ".join(chats) if chats
             else "In every chat this agent is in."},
            {"label": "Who may press a Slack button",
             "value": "These Slack members: %s." % ", ".join(users) if users else "Only the person a card was posted for."},
            {"label": "What a scheduled job may change by itself", "own": True,     # own: the page gives it a box of its own
             "value": "%s. %s" % (AUTONOMY[level]["label"], AUTONOMY[level]["summary"])},
            {"label": "What you declined", "own": True,
             "value": ("%d declined proposals are remembered for 90 days and not made again." % tally["declined"])
             if tally["declined"] else "A proposal you decline is remembered for 90 days and not made again."},
            {"label": "Lists (a CSV, a Sheet, pasted rows)", "value": "Held as a review. Nothing is written until you apply."},
            {"label": "Scheduled jobs",
             "value": "Paused while sample data is on show." if tour
             else "Cannot use the desk: add cron to the channels setting." if allow and "cron" not in allow
             else "May use the desk."},
            {"label": "Files it may read", "value": "CSV and TSV only, from: %s." % ", ".join(roots)},
            {"label": "Files it writes", "value": "Exports and saved maps, in %s." % (self.data_dir / "exports")},
            {"label": "Paid lookups", "value": ("At most %d a month." % cap if cap else "No monthly cap.") if pdl else "None: no lookup key is set."},
        ]
        return {
            "ok": True, "version": _version(), "problems": problems,
            "tour": {"example": tour["example"], "title": demo.EXAMPLES[tour["example"]]["title"], "since": tour["since"]} if tour else None,
            "examples": [{"id": key, "title": example["title"], "playbook": example["playbook"]} for key, example in demo.EXAMPLES.items()],
            "playbook": {"name": own.name, "title": own.title, "door": own.door, "summary": own.summary, "origin": own.origin,
                         "shape": own.shape, "shape_title": next((shape["title"] for shape in SHAPES if shape["id"] == own.shape), ""),
                         "chosen": chosen, "shown": playbook.name},
            "playbooks": [dict(entry, active=entry["name"] == own.name) for entry in catalog],
            "can_switch": bool(self.can_configure),
            "autonomy": {"level": level, "levels": [{"id": key, "label": entry["label"], "summary": entry["summary"]}
                                                    for key, entry in AUTONOMY.items()]},
            "declined": tally["declined"],
            "checklist": checklist, "connections": connections, "rules": rules,
            "automations": [{"name": bp["name"], "title": bp["title"], "description": bp["description"]} for bp in blueprints.BLUEPRINTS],
            "data": {"path": str(self.data_dir / self.own_file()), "size": size, "records": real, "open": tally["open"], "won": tally["won"],
                     "lost": tally["lost"], "samples": tally["samples"], "activities": tally["activities"], "maps": tally["maps"],
                     "people": tally["people"], "ledger": tally["ledger"], "archived": tally["archived"],
                     "lists_waiting": tally["lists_waiting"], "exports": str(self.data_dir / "exports"),
                     "backups": read("the daily copies", self.backups, {"folder": "", "count": 0, "newest": ""})},
            "playbook_sets": {
                "words": "%s, %s" % (terms["deal"], terms["group"]),
                "stages": [s["label"] for s in own.stages] + [own.won["label"]],
                "quiet": [{"stage": s["label"], "days": own.stale_days(s["id"])} for s in own.stages],
                "lost_reasons": own.lost["reasons"], "fields": [f["label"] for f in own.fields],
                "drafts": [own.label(key) for key, text in (own.drafts.get("on_enter") or {}).items() if text],
                "lookup": own.enrich},
        }

    def flow(self, deals: Optional[list] = None, playbook: Optional[Playbook] = None) -> dict:
        """How records have moved through the playbook, for a Sankey diagram.

        A record is counted as having reached every stage up to the furthest one it got to: its
        current stage if open, every stage if won, the stage it was closed from if lost.
        """
        playbook = playbook or self.playbook()
        stages = playbook.stages
        count = len(stages)
        reached, here, lost = [0] * count, [0] * count, [0] * count
        value = [0.0] * count
        won = from_map = 0
        won_value = 0          # money is added up in cents and turned into an amount at the end
        value = [0] * count
        for deal in (deals if deals is not None else self.store.list(status="all")):
            if deal["status"] == "open":
                far = playbook.position(deal["stage"])
                if far < 0:
                    continue
                here[far] += 1
                value[far] += deal["value_cents"]
            elif deal["status"] == "won":
                far = count - 1
                won += 1
                won_value += deal["value_cents"]
            else:
                far = max(0, playbook.position(str(deal["fields"].get("_prev_stage") or "")))
                lost[far] += 1
            if deal["source"].startswith("market map"):
                from_map += 1
            for index in range(far + 1):
                reached[index] += 1
        return {"stages": [{"id": s["id"], "label": s["label"], "reached": reached[i], "here": here[i], "lost": lost[i],
                            "value": value[i] / 100} for i, s in enumerate(stages)],
                "won": won, "won_value": won_value / 100, "won_label": playbook.won["label"], "lost": sum(lost),
                "lost_label": playbook.lost["label"], "from_map": from_map, "total": reached[0] if reached else 0}

    # -- gtm_deal -------------------------------------------------------------------------------
    def deal(self, args: dict, actor: str = "") -> dict:
        """``actor`` names who is doing this when it is not the agent's own tool call (a form on the page).
        Whatever one call changes is one action on the log, taken back as one."""
        action = str(args.get("action") or "").lower()
        if action == "undo":
            return self.undo(args.get("deal"), actor=actor)
        if action == "restore":
            return self.restore(args.get("deal"), actor=actor)
        with self.store.operation():
            return self._deal(args, actor)

    def undo(self, ref: Any, op: str = "", actor: str = "") -> dict:
        """Take back the last action on a record (and the one before it, if asked again), or the action
        ``op``. Nothing is taken back when a value it set has been changed again since: the answer says
        which, so a later edit is never overwritten."""
        deal, error = self._resolve(ref)
        if error:
            return error
        with self.store.operation():
            result = self.store.undo(deal["id"], op=str(op or ""), actor=actor or self.actor())
        if result is None:
            return {"ok": False, "error": "Nothing to take back on %s: that has been taken back already, or it was done before "
                                          "changes were kept on the ledger." % deal["name"]}
        if result["conflicts"]:
            said = self._conflicts(result["conflicts"])
            return {"ok": False, "undid": "", "conflicts": said,
                    "error": "Not taken back, because it would overwrite a later change on %s: %s. Nothing was changed."
                             % (deal["name"], "; ".join(said))}
        if result["deal"]:
            self._changed(result["deal"])
        return {"ok": True, "action": "undo", "undid": result["text"], "removed": result["archived"], "name": deal["name"],
                "deal": self.view(result["deal"], activities=3) if result["deal"] else None,
                "note": "%s is off the desk, not destroyed: gtm_deal(action=\"restore\") puts it back." % deal["name"]
                if result["archived"] else ""}

    def reason_asked(self, args: dict) -> Optional[dict]:
        """What to answer an agent that is closing a record as lost with a reason in its own words: the playbook's
        reasons, to pick from. ``None`` when the reason is one of them (however it was put), starts with "Other", or
        none was given. Buttons and the page offer the list themselves; this is for free text."""
        action = str((args or {}).get("action") or "").lower()
        said = " ".join(str((args or {}).get("reason") or "").split())
        if not said or action not in ("close", "move"):
            return None
        playbook = self.playbook()
        if action == "close" and str(args.get("outcome") or "").lower() != "lost":
            return None
        if action == "move" and playbook.resolve_stage(str(args.get("stage") or "")) != playbook.lost["id"]:
            return None
        if playbook.reason(said) in playbook.lost["reasons"] or said.lower().startswith("other"):
            return None
        return {"ok": False, "reasons": playbook.lost["reasons"],
                "error": "Give the reason in this playbook's words, so losses can be counted: pass `reason` as one of %s, or as "
                         "'Other: <a few words>' when none fits. Nothing was changed." % ", ".join(playbook.lost["reasons"])}

    def _conflicts(self, conflicts: list) -> list:
        """Each conflict as a line a person can read: what the action left, and what is there now."""
        playbook = self.playbook()
        labels = {"value_cents": playbook.terms["value"], "grp": playbook.terms["group"].capitalize(), "company": playbook.terms["company"],
                  "owner": playbook.terms["owner"].capitalize(), "next_step": "Next step", "next_due": "Due", "stage": "Stage",
                  "status": "Status", "name": "Name", "email": "Email", "linkedin": "LinkedIn", "role": "Role", "title": "Title"}
        labels.update({"fields." + field["key"]: field["label"] for field in playbook.fields})

        def shown(key: str, value: Any) -> str:
            if key == "value_cents":
                return cards.money(int(value or 0) / 100, playbook.currency) or "nothing"
            if key == "stage":
                return playbook.label(str(value or ""))
            return str(value) if value not in (None, "") else "nothing"

        out = []
        for item in conflicts:
            key = item["field"]
            if key == "record":
                out.append("it has been worked on since it was added (take those changes back first)")
            elif key in ("deal", "contact", "link", "person"):
                out.append("the %s it changed is %s" % ("person" if key != "deal" else "record", item["now"]))
            else:
                out.append("%s is now %s (that action left it at %s)" % (labels.get(key, key.replace("fields.", "")), shown(key, item["now"]),
                                                                        shown(key, item["then"])))
        return out

    def restore(self, ref: Any, actor: str = "") -> dict:
        """Put back a record that was taken off the desk when its add was undone."""
        ref = str(ref or "").strip()
        if not ref:
            return {"ok": False, "error": "say which record: pass `deal` (its id or name)"}
        try:
            gone = self.store.find(ref, archived=True)
        except st.Ambiguous as exc:
            return {"ok": False, "error": "'%s' matches several records that were taken off the desk; use one of these ids" % ref,
                    "matches": [{"id": d["id"], "name": d["name"], "company": d["company"]} for d in exc.matches]}
        if gone is None:
            return {"ok": False, "error": "nothing named '%s' has been taken off the desk" % ref[:60]}
        deal = self.store.restore(gone["id"], actor=actor or self.actor())
        self._changed(deal)
        return {"ok": True, "action": "restore", "deal": self.view(deal, activities=3)}

    # -- what a scheduled job may do by itself ----------------------------------------------------
    def autonomy(self) -> str:
        level = self.own_store.meta_get("autonomy", "ask")
        return level if level in AUTONOMY else "ask"

    def set_autonomy(self, level: Any) -> dict:
        """Set by a person, on the Setup tab or at a terminal. There is no tool for it: the agent cannot raise its own."""
        level = str(level or "").strip().lower()
        if level not in AUTONOMY:
            return {"ok": False, "error": "autonomy is one of: %s" % ", ".join(AUTONOMY)}
        self.own_store.meta_set("autonomy", level)
        return {"ok": True, "autonomy": level, "label": AUTONOMY[level]["label"], "summary": AUTONOMY[level]["summary"]}

    def guard(self, tool: str, args: dict, session: Optional[dict]) -> Optional[dict]:
        """The answer to give a scheduled job in place of running a tool, or ``None`` to run it.

        Reading and logging always run. A change the autonomy setting does not allow is held as a
        proposal on the Today tab when a proposal can carry it, and refused when it cannot.
        """
        if (session or {}).get("platform") != "cron":
            return None
        allows = AUTONOMY[self.autonomy()]["allows"]
        action = str((args or {}).get("action") or "").lower()
        refused = {"ok": False, "error": "A scheduled job cannot do that by itself: the desk's autonomy setting (%s) keeps it for "
                                         "the person. Say what you would have done in your reply instead."
                                         % AUTONOMY[self.autonomy()]["label"]}
        if tool == "gtm_deal":
            action = action or "get"
            if action == "get" or action in allows:
                return None
            if action in ("undo", "restore"):
                return refused
            return self.hold(action, args or {})
        unheld = {"ok": False, "error": "A scheduled job cannot change that by itself: the desk's autonomy setting (%s) keeps it for the "
                                        "person, and nothing has been left for them to approve. Say in your reply what you would "
                                        "change and why, so they can do it." % AUTONOMY[self.autonomy()]["label"]}
        if tool == "gtm_group" and action in ("set", "rename") and "group" not in allows:
            return unheld
        if tool == "gtm_contact" and action in ("add", "update", "remove") and (args or {}).get("group") and not (args or {}).get("deal"):
            return None if "group" in allows else unheld        # someone on a group's side: no proposal can carry that yet
        if tool == "gtm_contact" and action in ("add", "update", "remove") and "person" not in allows:
            return self.hold("person", args or {}) if action == "add" else refused
        if tool == "gtm_contact" and action == "company" and "update" not in allows and (
                args.get("note") is not None or args.get("domain") is not None):
            return refused
        if tool == "gtm_invoice" and action not in ("", "list") and "invoice" not in allows:
            return self.hold("invoice_" + action, args or {}) if action in ("add", "pay") else refused
        if tool == "gtm_playbook" and action in ("add_pipeline", "rename_pipeline"):
            return unheld
        if tool == "gtm_intake" and action in ("apply", "discard"):
            return {"ok": False, "error": "A scheduled job cannot approve or discard what is waiting: that is the person's call, "
                                          "on the Today tab. Leave it there."}
        if tool == "gtm_export" and action == "import" and "add" not in allows:
            return {"ok": False, "error": "A scheduled job cannot import records by itself. Propose the rows with gtm_intake and "
                                          "the person will approve them."}
        if tool == "gtm_market" and action == "track" and "add" not in allows:
            return {"ok": False, "error": "A scheduled job cannot put a company in the pipeline by itself. Add it to the map and "
                                          "say which ones you would call first; the person adds them with one press."}
        if tool == "gtm_playbook" and action in ("save", "use"):
            return refused
        return None

    def hold(self, action: str, args: dict) -> dict:
        """Turn a change a scheduled job asked for into a proposal waiting for a person."""
        playbook = self.playbook()
        row: dict = {"why": args.get("why") or "", "evidence": args.get("evidence") or []}
        if action == "add":
            row.update({key: args.get(key) for key in ("name", "company", "group", "stage", "value", "owner", "email", "linkedin",
                                                      "source", "next_step", "next_due", "fields", "note") if args.get(key) not in (None, "")})
        else:
            if action == "invoice_pay":         # a payment may come with only the invoice's number
                deal, error = self._invoice_record(args.get("deal"), str(args.get("invoice") or "").strip())
            else:
                deal, error = self._resolve(args.get("deal"))
            if error:
                return error
            row["record"] = deal["id"]
            if action == "move":
                target = str(args.get("stage") or "next")
                stage = playbook.next_stage(deal["stage"]) if target.lower() == "next" else playbook.resolve_stage(target)
                if not stage:
                    return {"ok": False, "error": "unknown stage '%s'" % target,
                            "stages": playbook.stage_ids() + [playbook.won["id"], playbook.lost["id"]]}
                row.update(stage=stage, lost_reason=str(args.get("reason") or ""))
            elif action == "close":
                outcome = str(args.get("outcome") or "").lower()
                if outcome not in ("won", "lost"):
                    return {"ok": False, "error": "close needs outcome: won or lost"}
                row.update(stage=playbook.won["id"] if outcome == "won" else playbook.lost["id"], lost_reason=str(args.get("reason") or ""))
            elif action == "update":
                row.update({key: args.get(key) for key in ("name", "company", "group", "email", "linkedin", "value", "owner",
                                                          "next_step", "next_due", "fields") if args.get(key) not in (None, "")})
            elif action == "done":
                step = deal["next_step"].strip()
                row.update(note=str(args.get("text") or "").strip() or ("Done: %s" % step if step else "Done."),
                           next_step=args.get("next_step") or "", next_due=args.get("next_due") or "")
            elif action == "person":
                row["person"] = {key: args.get(key) for key in ("name", "email", "title", "role", "linkedin", "phone")}
                row["person"]["name"] = args.get("name") or args.get("person") or ""
            elif action == "invoice_add":
                row["invoice"] = {key: args.get(key) for key in ("amount", "number", "due", "note") if args.get(key) not in (None, "")}
            elif action == "invoice_pay":
                row["payment"] = {key: args.get(key) for key in ("invoice", "amount", "on") if args.get(key) not in (None, "")}
            else:
                return {"ok": False, "error": "A scheduled job cannot %s a record. Say in your reply that you would, and why."
                                              % action}
        out = self.intake.propose({"rows": [row], "source": "Hermes, on a schedule", "kind": "proposal"}, {"platform": "cron"})
        if not out.get("ok"):
            return out
        waiting = (out.get("counts") or {}).get("new", 0) + (out.get("counts") or {}).get("update", 0)
        return {"ok": True, "held": bool(waiting), "id": out.get("id") if waiting else "",
                "note": out.get("note") if not waiting else
                "Held for approval, not done: the desk's autonomy setting (%s) keeps this kind of change for the person, who "
                "approves or declines it on the Today tab. Do not try another way. Carry on, and mention in one line that it "
                "is waiting." % AUTONOMY[self.autonomy()]["label"]}

    def _deal(self, args: dict, actor: str = "") -> dict:
        action = str(args.get("action") or "get").lower()
        playbook = self.playbook()
        actor = actor or self.actor()
        args, error = _due(args)
        if error and action in ("add", "update", "log", "done"):
            return error
        if action == "update" and args.get("delivery") is not None:       # where the work stands, once it is won
            deal, missing = self._resolve(args.get("deal"))
            if missing:
                return missing
            want = str(args["delivery"]).strip().lower()
            ids = [item["id"] for item in playbook.delivery]
            if deal["status"] != "won":
                return {"ok": False, "error": "%s is not %s yet, so there is no delivery stage to set." % (deal["name"], playbook.won["label"].lower())}
            if want == "next":
                at = ids.index(deal["delivery"]) if deal["delivery"] in ids else -1
                want = ids[min(at + 1, len(ids) - 1)] if ids else ""
            found = next((item["id"] for item in playbook.delivery if want in (item["id"], item["label"].lower())), None)
            if found is None:
                return {"ok": False, "error": "unknown delivery stage '%s'" % want[:40], "delivery_stages": ids}
            args = dict(args, delivery=found)
        if action == "add":
            return self._add(args, playbook, actor)
        deal, error = self._resolve(args.get("deal"))
        if error:
            return error
        if action == "get":
            out = {"ok": True, "deal": self.view(deal, playbook, activities=_int(args.get("activities") or 12, 12, 0, 100)),
                   "card": self.text_card(deal)}
            pending = self.store.meta_pop("pending:" + deal["id"])
            if pending:  # a button asked for some writing; this read is the agent picking it up
                out["pending_task"] = pending
                if self.handoff(deal["id"]) != "drafting":      # it was waiting for a reply: the card can stop saying so
                    self.clear_handoff(deal["id"])
                    self._changed(deal)
            return out
        if action == "update":
            values = {k: args.get(a) for k, a in (("name", "name"), ("company", "company"), ("grp", "group"), ("email", "email"),
                                                  ("linkedin", "linkedin"), ("value", "value"), ("owner", "owner"),
                                                  ("next_step", "next_step"), ("next_due", "next_due"), ("source", "source"),
                                                  ("close_on", "close_on"), ("delivery", "delivery"), ("notes", "notes"), ("phone", "phone"))}
            if values.get("notes") is not None:
                values["notes"] = str(values["notes"]).strip()[:NOTES_MAX]
            fields = args.get("fields") if isinstance(args.get("fields"), dict) else None
            if values.get("grp"):
                values["grp"] = self._group_spelling(str(values["grp"]).strip())
            deal = self.store.update(deal["id"], actor=actor, fields=fields, **values)
        elif action == "move":
            target = str(args.get("stage") or "next")
            stage = playbook.next_stage(deal["stage"]) if target.lower() == "next" else playbook.resolve_stage(target)
            if not stage:
                return {"ok": False, "error": "unknown stage '%s'" % target,
                        "stages": playbook.stage_ids() + [playbook.won["id"], playbook.lost["id"]]}
            moved = self._move(deal, stage, playbook, actor, str(args.get("reason") or ""))
            if moved is None:
                return self._raced(deal["id"], playbook, action)
            deal = moved
        elif action == "close":
            outcome = str(args.get("outcome") or "").lower()
            if outcome not in ("won", "lost"):
                return {"ok": False, "error": "close needs outcome: won or lost"}
            stage = playbook.won["id"] if outcome == "won" else playbook.lost["id"]
            moved = self._move(deal, stage, playbook, actor, str(args.get("reason") or ""))
            if moved is None:
                return self._raced(deal["id"], playbook, action)
            deal = moved
        elif action == "reopen":
            moved = self._reopen(deal, playbook, actor, str(args.get("stage") or ""))
            if moved is None:
                return self._raced(deal["id"], playbook, action)
            deal = moved
        elif action == "log":
            text = str(args.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "log needs text"}
            kind = str(args.get("kind") or "note").lower()
            ref = str(args.get("ref") or "").strip()[:200]
            if ref and self.store.logged(deal["id"], ref):      # the same email or message, met again by a later sweep
                return {"ok": True, "action": "log", "already": True, "deal": self.view(deal, playbook, activities=3)}
            self.store.log(deal["id"], kind if kind in LOG_KINDS else "note", text[:4000], actor=actor, ref=ref)
            follow = {k: args.get(k) for k in ("next_step", "next_due") if args.get(k) is not None}
            if follow:
                self.store.update(deal["id"], actor=actor, **follow)
            deal = self.store.get(deal["id"])
        elif action == "done":
            step = deal["next_step"].strip()
            text = str(args.get("text") or "").strip() or ("Done: %s" % step if step else "")
            if not text:
                return {"ok": False, "error": "%s has no next step to mark done. Use log to record what happened." % deal["name"]}
            self.store.log(deal["id"], "task", text[:4000], actor=actor)
            self.store.update(deal["id"], actor=actor, next_step=str(args.get("next_step") or ""), next_due=args.get("next_due") or "")
            deal = self.store.get(deal["id"])
        elif action == "snooze":
            until = ""
            if args.get("until") not in (None, ""):
                until = st.parse_due(args["until"]) or ""
                if not until or until <= st.today():
                    return {"ok": False, "error": "until should be a date after today such as %s (friday and +10d also work); "
                                                  "got '%s'. Nothing was changed."
                                                  % ((dt.date.today() + dt.timedelta(days=7)).isoformat(), str(args["until"])[:40])}
            deal = self.store.snooze(deal["id"], _int(args.get("days") if args.get("days") is not None else 3, 3, 0, 365),
                                     until=until, actor=actor)
        else:
            return {"ok": False, "error": "unknown action '%s'" % action,
                    "actions": ["get", "add", "update", "move", "close", "reopen", "log", "done", "snooze", "undo", "restore"]}
        self._changed(deal)
        return {"ok": True, "action": action, "deal": self.view(deal, playbook, activities=3)}

    def _add(self, args: dict, playbook: Playbook, actor: str) -> dict:
        name = str(args.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "add needs a name"}
        group = self._group_spelling(str(args.get("group") or "").strip())
        if not args.get("force"):
            for existing in self.store.list(status="open", query=name):
                if existing["name"].lower() == name.lower() and existing["grp"].lower() == group.lower():
                    return {"ok": True, "exists": True, "deal": self.view(existing, playbook),
                            "note": "Already on the desk; nothing was added. Pass force=true to add a second record."}
        stage = playbook.resolve_stage(str(args.get("stage") or "")) or playbook.first_stage()
        if playbook.status_of(stage) != "open":
            return {"ok": False, "error": "add a record at an open stage, then close it"}
        value = args.get("value")
        if value in (None, "") and group:
            value = (self.store.grp_get(group) or {}).get("value") or None      # what one record in this group is worth
        deal = self.store.add(
            name, stage, actor=actor, company=str(args.get("company") or ""), grp=group,
            email=str(args.get("email") or ""), linkedin=str(args.get("linkedin") or ""), value=value,
            owner=str(args.get("owner") or ""), next_step=str(args.get("next_step") or ""),
            next_due=str(args.get("next_due") or ""), source=str(args.get("source") or ""), close_on=str(args.get("close_on") or ""),
            notes=str(args.get("notes") or "").strip()[:NOTES_MAX], phone=str(args.get("phone") or "").strip()[:60], fields=args.get("fields") if isinstance(args.get("fields"), dict) else None)
        if args.get("note"):
            self.store.log(deal["id"], "note", str(args["note"])[:4000], actor=actor)
        return {"ok": True, "action": "add", "deal": self.view(self.store.get(deal["id"]), playbook)}

    def _move(self, deal: dict, stage: str, playbook: Playbook, actor: str, reason: str = "") -> Optional[dict]:
        """Move a record from the stage it was read at. ``None`` means someone else moved it first."""
        status = playbook.status_of(stage)
        # Won: the work begins, at the playbook's first delivery stage. Anything else: there is no delivery to speak of.
        delivery = (playbook.delivery[0]["id"] if playbook.delivery else "") if status == "won" else ""
        if status == "lost":
            reason = playbook.reason(reason)      # "Timing" however it was put, where the playbook has the word for it
        return self.store.move(deal["id"], stage, status=status, reason=reason, actor=actor,
                               label=playbook.label(stage), expect=deal["stage"], delivery=delivery)

    def _reopen(self, deal: dict, playbook: Playbook, actor: str, stage: str = "") -> Optional[dict]:
        target = playbook.resolve_stage(stage) or deal["fields"].get("_prev_stage") or playbook.first_stage()
        if playbook.status_of(target) != "open" or playbook.position(target) < 0:
            target = playbook.first_stage()
        return self.store.move(deal["id"], target, status="open", actor=actor, label=playbook.label(target), expect=deal["stage"], delivery="")

    def _raced(self, deal_id: str, playbook: Playbook, action: str) -> dict:
        """Someone else changed the stage between our read and our write: say where it stands now."""
        return {"ok": True, "action": action, "changed": False, "deal": self.view(self.store.get(deal_id), playbook, activities=3),
                "note": "Someone else moved this record a moment ago, so nothing was changed. This is where it stands now."}

    # -- button clicks --------------------------------------------------------------------------
    def click(self, verb: str, deal_id: str, extra: str = "", actor: str = "") -> dict:
        """The deterministic half of a Slack button click.

        Returns ``{"deal", "followup", "notice"}``: ``followup`` is an instruction for the agent
        (the writing half), ``notice`` a short line for the person who clicked.
        """
        with self.store.operation():
            return self._click(verb, deal_id, extra, actor)

    def _click(self, verb: str, deal_id: str, extra: str = "", actor: str = "") -> dict:
        deal = self.store.get(deal_id)
        if deal is None:
            return {"deal": None, "notice": "That record is no longer on the desk."}
        playbook = self.playbook()
        out = {"deal": deal, "followup": "", "notice": ""}
        if verb == "advance":
            if deal["status"] != "open" or (extra and extra != deal["stage"]):
                out["notice"] = "Already moved to %s; the card is now up to date." % playbook.label(deal["stage"])
                return out
            nxt = playbook.next_stage(deal["stage"])
            if not nxt:
                out["notice"] = "This stage is not in the current playbook; ask me to move it instead."
                return out
            moved = self._move(deal, nxt, playbook, actor)
            if moved is None:      # another click got there first
                out["deal"] = self.store.get(deal_id)
                out["notice"] = "Already moved to %s; the card is now up to date." % playbook.label(out["deal"]["stage"])
                return out
            out["deal"] = moved
            task = playbook.draft_prompt("enter", nxt)
            if task:
                out["followup"] = self._followup(
                    playbook, moved, actor, task,
                    "It moved from %s to %s. That change is already saved, so do not move it again."
                    % (playbook.label(deal["stage"]), playbook.label(nxt)))
        elif verb == "lost":
            if deal["status"] != "open":
                out["notice"] = "Already closed; the card is now up to date."
                return out
            reasons = playbook.lost["reasons"]
            reason = reasons[int(extra)] if extra.isdigit() and int(extra) < len(reasons) else ""
            moved = self._move(deal, playbook.lost["id"], playbook, actor, reason)
            if moved is None:
                out["deal"] = self.store.get(deal_id)
                out["notice"] = "Already moved to %s; the card is now up to date." % playbook.label(out["deal"]["stage"])
                return out
            out["deal"] = moved
            task = playbook.draft_prompt("lost")
            if task:
                out["followup"] = self._followup(
                    playbook, moved, actor, task,
                    "It was marked %s (%s). That change is already saved." % (playbook.lost["label"], reason or "no reason given"),
                    reason=reason)
        elif verb == "draft":
            task = playbook.draft_prompt("follow_up") or "Draft a short follow-up to {name}."
            out["followup"] = self._followup(playbook, deal, actor, task, "Nothing about the record changed.")
        elif verb == "reopen":
            if deal["status"] == "open":
                out["notice"] = "Already open; the card is now up to date."
                return out
            out["deal"] = self._reopen(deal, playbook, actor) or self.store.get(deal_id)
        elif verb in ("snooze3", "snooze7"):
            out["deal"] = self.store.snooze(deal_id, 3 if verb == "snooze3" else 7, actor=actor)
            out["notice"] = "Snoozed until %s." % out["deal"]["snoozed_until"]
        elif verb == "enrich":
            result = self.enrich({"deal": deal_id})
            out["deal"] = self.store.get(deal_id)
            out["notice"] = result.get("summary") or result.get("error") or "No match found."
        return out

    def park_followup(self, deal_id: str, followup: str) -> None:
        """Keep a writing task on the record; the next gtm_deal(get) hands it over and clears it."""
        self.store.meta_set("pending:" + deal_id, followup)

    # Where a requested draft stands, for the line on the card: "drafting" once the agent has been
    # handed the task, "waiting" when it is parked until someone replies, nothing otherwise.
    @staticmethod
    def _task_token(followup: str) -> str:
        return hashlib.sha1(followup.encode("utf-8")).hexdigest()[:12]

    def _handoff_raw(self, deal_id: str) -> tuple:
        state, _, rest = self.store.meta_get("handoff:" + deal_id).partition("|")
        stamp, _, token = rest.partition("|")
        try:
            age = time.time() - float(stamp)
        except ValueError:
            age = float("inf")
        return state, age, token

    def set_handoff(self, deal_id: str, state: str, followup: str) -> None:
        self.store.meta_set("handoff:" + deal_id, "%s|%d|%s" % (state, time.time(), self._task_token(followup)))

    def clear_handoff(self, deal_id: str, followup: str = "") -> None:
        """Forget the state. With ``followup``, only if it is still that task's (a later click may own it now)."""
        if not followup or self._handoff_raw(deal_id)[2] == self._task_token(followup):
            self.store.meta_pop("handoff:" + deal_id)

    def handoff(self, deal_id: str) -> str:
        state, age, _token = self._handoff_raw(deal_id)
        if state == "drafting" and age <= HANDOFF_FRESH:
            return "drafting"
        return "waiting" if self.store.meta_get("pending:" + deal_id) else ""

    def handoff_same(self, deal_id: str, followup: str) -> bool:
        """True while this very task is already being drafted: a second press of the same button."""
        state, age, token = self._handoff_raw(deal_id)
        return state == "drafting" and age <= HANDOFF_FRESH and token == self._task_token(followup)

    def _followup(self, playbook: Playbook, deal: dict, actor: str, task: str, happened: str, reason: str = "") -> str:
        words = _Blank(name=deal["name"], company=deal["company"] or "their company", group=deal["grp"] or "this",
                       stage=playbook.label(deal["stage"]), reason=reason or "not recorded", owner=deal["owner"],
                       value=cards.money(deal["value"], playbook.currency),
                       client=((self.store.grp_get(deal["grp"]) or {}).get("client") if deal["grp"] else "") or "the client")
        try:
            task = " ".join(task.split()).format_map(words)
        except (ValueError, IndexError):
            task = " ".join(task.split())
        where = " in the %s %s" % (deal["grp"], playbook.terms["group"]) if deal["grp"] else ""
        return ("[Stoa desk] %s used a button on the card for %s (%s%s). %s\n\n"
                "Your task: %s\n\n"
                "First call gtm_deal(action=\"get\", deal=\"%s\") to read the record and its activity log. "
                "Reply here with the draft only. Do not send anything and do not change the record."
                % (actor or "Someone", deal["name"], playbook.terms["deal"], where, happened, task, deal["id"]))

    # -- gtm_market -----------------------------------------------------------------------------
    def market(self, args: dict, session: Optional[dict] = None) -> dict:
        return self.markets.run(args, session)

    # -- gtm_invoice, and the cash picture ------------------------------------------------------------
    @staticmethod
    def _bill(invoice: dict, name: str = "") -> dict:
        """An invoice as the page and the model see it."""
        out = {key: invoice[key] for key in ("id", "number", "amount", "paid", "owed", "state", "issued_on", "due_on", "paid_on",
                                             "days_overdue", "note")}
        out["deal"] = invoice["deal_id"]
        if name:
            out["name"] = name
        return out

    def money(self, deal: dict, invoices: Optional[list] = None) -> dict:
        """What has been billed and paid on one record, and what is left."""
        bills = [i for i in (self.store.invoices(deal["id"]) if invoices is None else invoices) if i["state"] not in ("void", "draft")]
        billed, paid = sum(i["amount_cents"] for i in bills), sum(i["paid_cents"] for i in bills)
        return {"invoiced": billed / 100, "paid": paid / 100, "owed": sum(i["owed_cents"] for i in bills) / 100,
                "overdue": sum(i["owed_cents"] for i in bills if i["state"] == "overdue") / 100,
                "uninvoiced": max(0, deal["value_cents"] - billed) / 100 if deal["status"] == "won" and self.playbook().billing != "none" else 0.0}

    def _due_on(self, value: Any) -> tuple:
        """``(YYYY-MM-DD, error)`` for an invoice's due date: a date, "net 30", or nothing (thirty days from today)."""
        text = str(value or "").strip().lower()
        if not text:
            return (dt.date.today() + dt.timedelta(days=30)).isoformat(), None
        net = re.fullmatch(r"net\s*(\d{1,3})(\s*days?)?", text)
        due = (dt.date.today() + dt.timedelta(days=int(net.group(1)))).isoformat() if net else st.parse_due(text)
        if due is None:
            return "", {"ok": False, "error": "due should be a date such as %s, or 'net 30'; got '%s'. Nothing was changed."
                                              % ((dt.date.today() + dt.timedelta(days=30)).isoformat(), text[:40])}
        return due, None

    def _which_invoice(self, deal: dict, ref: Any) -> tuple:
        """``(invoice, error)``: the one named by id or number, or the only one still open."""
        listed, want = self.store.invoices(deal["id"]), str(ref or "").strip().lower().lstrip("#")
        if want:
            found = [i for i in listed if str(i["id"]) == want or (i["number"] and i["number"].lower().lstrip("#") == want)]
        else:
            found = [i for i in listed if i["state"] in ("sent", "partial", "overdue", "draft")]
        if len(found) == 1:
            return found[0], None
        return None, {"ok": False, "error": ("no invoice '%s' on %s" % (want, deal["name"])) if want and not found
                      else "say which invoice on %s: pass `invoice` (its number or id)" % deal["name"],
                      "invoices": [self._bill(i) for i in listed]}

    def invoice(self, args: dict, actor: str = "") -> dict:
        action = str(args.get("action") or "list").lower()
        actor = actor or self.actor()
        if args.get("client") and not args.get("deal"):
            return self.client_invoice(args, actor)
        named_first = str(args.get("invoice") or "").strip()
        if named_first and not args.get("deal") and action not in ("list", "add"):
            homes = self.store.invoice_numbered(named_first)
            if homes and all(st.is_client(i["deal_id"]) for i in homes) and len({i["deal_id"] for i in homes}) == 1:
                org = next((o for o in self.store.orgs() if st.client_ref(o["name"]) == homes[0]["deal_id"]), None)
                if org:                     # "invoice 1043 was paid", and 1043 went to a client: it is that client's
                    return self.client_invoice(dict(args, client=org["name"]), actor)
        if action == "list" and not args.get("deal"):
            cash = self.cash()
            return {"ok": True, "action": "list", "invoices": cash["invoices"], "strip": cash["strip"]}
        named = str(args.get("invoice") or "").strip()
        deal, error = self._invoice_record(args.get("deal"), named if action not in ("list", "add") else "")
        if error:
            return error
        if action == "list":
            return {"ok": True, "action": "list", "deal": deal["id"], "name": deal["name"], "money": self.money(deal),
                    "invoices": [self._bill(i) for i in self.store.invoices(deal["id"])]}
        if action == "undo":
            return self._invoice_undo(deal, named, actor)
        try:
            with self.store.operation():
                if action == "add":
                    amount = args.get("amount")
                    if amount in (None, ""):                         # nothing said: whatever of the record's value is not billed yet
                        amount = self.money(deal)["uninvoiced"] if deal["status"] == "won" else deal["value"] - self.money(deal)["invoiced"]
                    due, error = self._due_on(args.get("due"))
                    if error:
                        return error
                    draft = str(args.get("status") or "").lower() == "draft"
                    made = self.store.invoice_add(deal["id"], amount, actor=actor, number=str(args.get("number") or ""), due_on=due,
                                                  status="draft" if draft else "sent", note=str(args.get("note") or ""))
                else:
                    found, error = self._which_invoice(deal, args.get("invoice"))
                    if error:
                        return error
                    if action == "pay":
                        on = st.parse_due(args["on"]) if args.get("on") else ""
                        made = self.store.invoice_pay(found["id"], args.get("amount"), on=on or "", actor=actor)
                    elif action == "send":
                        due = None
                        if args.get("due"):
                            due, error = self._due_on(args.get("due"))
                            if error:
                                return error
                        made = self.store.invoice_update(found["id"], actor=actor, status="sent", issued_on=st.today(), due_on=due,
                                                         said="%s sent" % self.store._named(found))
                    elif action == "void":
                        made = self.store.invoice_update(found["id"], actor=actor, status="void", said="%s voided" % self.store._named(found))
                    elif action == "update":
                        due = None
                        if args.get("due") not in (None, ""):
                            due, error = self._due_on(args.get("due"))
                            if error:
                                return error
                        made = self.store.invoice_update(found["id"], actor=actor, amount=args.get("amount"), number=args.get("number"),
                                                         due_on=due, note=args.get("note"))
                    elif action == "remove":
                        self.store.invoice_remove(found["id"], actor=actor)
                        made = None
                    else:
                        return {"ok": False, "error": "unknown action '%s'" % action,
                                "actions": ["list", "add", "send", "pay", "void", "update", "remove", "undo"]}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        deal = self.store.get(deal["id"])
        self._changed(deal)
        return {"ok": True, "action": action, "deal": deal["id"], "name": deal["name"], "invoice": self._bill(made) if made else None,
                "money": self.money(deal), "invoices": [self._bill(i) for i in self.store.invoices(deal["id"])]}

    def _invoice_record(self, ref: Any, named: str = "") -> tuple:
        """``(deal, error)`` for an invoice action: the record named, or, with none named, the one the invoice
        number belongs to ("invoice 1043 was paid"). A record and an invoice that do not go together are an
        error that says where the invoice is, so a payment is never put on the wrong one."""
        ref = str(ref or "").strip()
        homes = self.store.invoice_numbered(named) if named else []
        listed = [self._bill(i, (self.store.get(i["deal_id"]) or {}).get("name", "")) for i in homes]
        if not ref and homes:
            if len({i["deal_id"] for i in homes}) > 1:
                return None, {"ok": False, "invoices": listed,
                              "error": "more than one record has an invoice '%s'; pass `deal` as well. Nothing was changed." % named[:40]}
            ref = homes[0]["deal_id"]
        if not ref:
            return None, {"ok": False, "error": "say which record (`deal`: its id or name) or which invoice (`invoice`: its number)"}
        deal, error = self._resolve(ref)
        if error:
            return None, error
        if homes and all(i["deal_id"] != deal["id"] for i in homes):
            where = [self.store.get(i["deal_id"]) for i in homes]
            return None, {"ok": False, "invoices": listed,
                          "error": "invoice %s is not on %s; it is on %s. Pass that record as `deal`, or leave `deal` out. Nothing was changed."
                                   % (named[:40], deal["name"], " and ".join("%s (%s)" % (d["name"], d["grp"] or d["company"]) for d in where if d))}
        return deal, None

    def _invoice_undo(self, deal: dict, named: str, actor: str) -> dict:
        """Take back the last thing done to an invoice: a payment entered by mistake, a void, an edit, or the
        invoice itself. With no invoice named, the last thing done to any of the record's invoices."""
        target = None
        if named:
            want = named.lower().lstrip("#")
            target = next((i for i in self.store.invoices(deal["id"])
                           if str(i["id"]) == want or (i["number"] and i["number"].lower().lstrip("#") == want)), None)
        last = self.store.invoice_last(deal["id"], target["id"] if target else None)
        if last is not None and named and target is None and named.lower().lstrip("#") not in last["text"].lower():
            last = None                         # a removed invoice is found by what the ledger says of it, or not at all
        if last is None:
            return {"ok": False, "error": "Nothing to take back on %s: that has been taken back already, or nothing has been done to it."
                                          % ("invoice %s" % named[:40] if named else "the invoices on %s" % deal["name"]),
                    "invoices": [self._bill(i) for i in self.store.invoices(deal["id"])]}
        out = self.undo(deal["id"], op=last["op"], actor=actor)
        if out.get("ok"):
            fresh = self.store.get(deal["id"]) or deal
            out.update(money=self.money(fresh), invoices=[self._bill(i) for i in self.store.invoices(deal["id"])])
        return out

    # -- billing a client by the month: hourly contract work, or a retainer ----------------------------------
    # Some work is billed to a client, not on a record: a contract recruiter's hours for a portfolio company,
    # billed to the fund each month; a fractional lead's retainer. The terms are kept on the client (the company
    # a search is for, or an account), and each month that has ended and is not billed yet is waiting on Today.
    BILL_KINDS = ("hourly", "retainer")

    @staticmethod
    def _month(value: Any) -> str:
        """``YYYY-MM`` for a month written as 2026-10, "October 2026", "Oct 2026" or "october" (this year); "" if not one."""
        text = " ".join(str(value or "").split()).lower()
        if not text:
            return ""
        if re.fullmatch(r"\d{4}-\d{2}", text):
            return text if 1 <= int(text[5:]) <= 12 else ""
        names = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
        found = re.fullmatch(r"([a-z]+)\.?(?:\s+(\d{4}))?", text)
        if not found:
            return ""
        month = next((index + 1 for index, name in enumerate(names) if name.startswith(found.group(1)) and len(found.group(1)) >= 3), 0)
        if not month:
            return ""
        return "%s-%02d" % (found.group(2) or dt.date.today().year, month)

    @staticmethod
    def _month_label(period: str) -> str:
        try:
            return dt.date(int(period[:4]), int(period[5:7]), 1).strftime("%B %Y")
        except (ValueError, TypeError):
            return period

    @staticmethod
    def _weekdays(period: str) -> int:
        first = dt.date(int(period[:4]), int(period[5:7]), 1)
        after = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        return sum(1 for n in range((after - first).days) if (first + dt.timedelta(days=n)).weekday() < 5)

    def _clean_terms(self, raw: Any) -> dict:
        """Terms as they are kept, checked: ``{kind, rate, hours_week, bill_to, start, end, due_days, skipped}``.
        Raises ValueError with what is wrong."""
        raw = raw if isinstance(raw, dict) else {}
        kind = str(raw.get("kind") or "").strip().lower()
        if kind in ("hour", "hours", "contract", "time"):
            kind = "hourly"
        if kind in ("monthly", "month", "fixed"):
            kind = "retainer"
        if kind not in self.BILL_KINDS:
            raise ValueError("kind is hourly (a rate per hour, hours a week) or retainer (an amount each month)")
        rate = st.to_cents(raw.get("rate"))
        if rate <= 0:
            raise ValueError("an hourly rate above zero" if kind == "hourly" else "a monthly amount above zero")
        out = {"kind": kind, "rate": rate / 100, "bill_to": " ".join(str(raw.get("bill_to") or "").split())[:120]}
        if kind == "hourly":
            try:
                hours = float(raw.get("hours_week") if raw.get("hours_week") not in (None, "") else 40)
            except (TypeError, ValueError):
                raise ValueError("hours a week is a number, such as 40")
            if not 0 < hours <= 80:
                raise ValueError("hours a week is between 1 and 80")
            out["hours_week"] = int(hours) if hours == int(hours) else round(hours, 2)
        for key in ("start", "end"):
            month = self._month(raw.get(key))
            if raw.get(key) not in (None, "") and not month:
                raise ValueError("%s is a month, such as 2026-10" % key)
            out[key] = month
        if not out["start"]:
            out["start"] = dt.date.today().isoformat()[:7]
        if out["end"] and out["end"] < out["start"]:
            raise ValueError("the end month is before the start month")
        try:
            out["due_days"] = max(0, min(120, int(raw.get("due_days") if raw.get("due_days") not in (None, "") else 30)))
        except (TypeError, ValueError):
            raise ValueError("due_days is a number of days, such as 30")
        out["skipped"] = sorted({self._month(p) for p in (raw.get("skipped") or []) if self._month(p)})
        return out

    def terms_of(self, name: Any) -> dict:
        """The billing terms kept on a client ({} when there are none)."""
        org = self.store.org_get(name) or {}
        try:
            raw = json.loads(org.get("terms") or "{}")
        except ValueError:
            return {}
        try:
            return self._clean_terms(raw) if raw.get("kind") else {}
        except ValueError:
            return {}

    def _client_name(self, name: Any) -> str:
        """The client as it is written on the desk: a group's client, an account, or a company with terms."""
        key = st.org_key(name)
        if not key:
            return ""
        for group in self.store.grps():
            if group.get("client") and st.org_key(group["client"]) == key:
                return group["client"]
        for group in self.groups():
            if st.org_key(group["name"]) == key:
                return group["name"]
        org = self.store.org_get(name)
        return org["name"] if org else " ".join(str(name).split())[:200]

    def _suggest_hours(self, terms: dict, period: str) -> float:
        hours = self._weekdays(period) * float(terms.get("hours_week") or 40) / 5
        return float(round(hours))

    def to_bill(self, name: Any = None) -> list:
        """Months that have ended and are not billed yet, for every client with terms (or one): what each would come to.
        A month already billed (a voided invoice does not count) or skipped is not asked for again; at most six back."""
        last = (dt.date.today().replace(day=1) - dt.timedelta(days=1)).isoformat()[:7]
        out = []
        names = [name] if name is not None else [org["name"] for org in self.store.orgs() if (org.get("terms") or "{}") != "{}"]
        for client in names:
            terms = self.terms_of(client)
            if not terms:
                continue
            billed = {i["period"] for i in self.store.invoices(st.client_ref(client)) if i.get("period") and i["state"] != "void"}
            end = min(last, terms["end"]) if terms["end"] else last
            months, cursor = [], terms["start"]
            while cursor <= end and len(months) < 240:
                if cursor not in billed and cursor not in terms["skipped"]:
                    months.append(cursor)
                year, month = int(cursor[:4]), int(cursor[5:]) + 1
                cursor = "%d-%02d" % (year + (month > 12), month - 12 if month > 12 else month)
            display = self._client_name(client)
            for period in months[-6:]:
                hours = self._suggest_hours(terms, period) if terms["kind"] == "hourly" else None
                amount = round(hours * terms["rate"], 2) if hours is not None else terms["rate"]
                out.append({"client": display, "period": period, "label": self._month_label(period), "kind": terms["kind"],
                            "hours": hours, "rate": terms["rate"], "amount": amount, "bill_to": terms["bill_to"] or display})
        return sorted(out, key=lambda row: (row["period"], row["client"].lower()))

    def client_billing(self, name: Any) -> dict:
        """What a client is billed: its terms, its invoices (newest first), the months waiting to be billed, and totals."""
        display = self._client_name(name)
        ref = st.client_ref(display)
        invoices = self.store.invoices(ref) if ref else []
        live = [i for i in invoices if i["state"] not in ("void", "draft")]
        return {"client": display, "terms": self.terms_of(display), "to_bill": self.to_bill(display) if ref else [],
                "invoices": [dict(self._bill(i), period=i.get("period") or "", period_label=self._month_label(i["period"]) if i.get("period") else "")
                             for i in reversed(invoices)],
                "billed": sum(i["amount_cents"] for i in live) / 100, "paid": sum(i["paid_cents"] for i in live) / 100,
                "owed": sum(i["owed_cents"] for i in live) / 100, "overdue": sum(i["owed_cents"] for i in live if i["state"] == "overdue") / 100}

    def client_terms(self, name: Any, terms: Any, actor: str = "") -> dict:
        """Set (or, with an empty kind, clear) how a client is billed."""
        display = self._client_name(name)
        if not display:
            return {"ok": False, "error": "say which client"}
        if not isinstance(terms, dict) or not str(terms.get("kind") or "").strip():
            self.store.org_put(display, terms={}, actor=actor)
            return dict(self.client_billing(display), ok=True, note="%s has no billing terms now." % display)
        before = self.terms_of(display)
        merged = dict(before, **{k: v for k, v in terms.items() if v is not None})
        try:
            clean = self._clean_terms(merged)
        except ValueError as exc:
            return {"ok": False, "error": str(exc) + ". Nothing was changed."}
        self.store.org_put(display, terms=clean, actor=actor)
        return dict(self.client_billing(display), ok=True)

    def bill_client(self, name: Any, period: Any, hours: Any = None, amount: Any = None, number: Any = "", due: Any = None,
                    actor: str = "", draft: bool = False) -> dict:
        """Record the invoice for one month of a client's terms: the hours given (or the month's weekdays at the terms'
        hours a week) times the rate, or the retainer. Stoa keeps the record; it does not send the invoice."""
        display = self._client_name(name)
        terms = self.terms_of(display)
        month = self._month(period)
        if not month:
            return {"ok": False, "error": "say which month, such as 2026-10 or October 2026"}
        if not terms and amount in (None, ""):
            return {"ok": False, "error": "%s has no billing terms; set them (gtm_invoice action='terms'), or give the amount" % (display or "That client")}
        ref = st.client_ref(display)
        if any(i.get("period") == month and i["state"] != "void" for i in self.store.invoices(ref)):
            return {"ok": False, "error": "%s is already billed to %s. Void that invoice first if it was wrong. Nothing was changed."
                                          % (self._month_label(month), display)}
        bits = []                       # the month is the invoice's own period: the note says what it was for
        if amount in (None, ""):
            if terms["kind"] == "hourly":
                try:
                    used = float(hours) if hours not in (None, "") else self._suggest_hours(terms, month)
                except (TypeError, ValueError):
                    return {"ok": False, "error": "hours is a number, such as 160"}
                if not 0 < used <= 400:
                    return {"ok": False, "error": "hours for the month is between 1 and 400"}
                amount = round(used * terms["rate"], 2)
                bits.append("%s h at %s" % (int(used) if used == int(used) else used, cards.money(terms["rate"], self.playbook().currency) or terms["rate"]))
            else:
                amount = terms["rate"]
                bits.append("retainer")
        if terms.get("bill_to") and st.org_key(terms["bill_to"]) != st.org_key(display):
            bits.append("to %s" % terms["bill_to"])
        note = " · ".join(bits)
        due_on, error = self._due_on(due if due not in (None, "") else ("net %d" % terms.get("due_days", 30) if terms else None))
        if error:
            return error
        try:
            with self.store.operation():
                made = self.store.invoice_add(ref, amount, actor=actor or self.actor(), number=str(number or ""), due_on=due_on,
                                              status="draft" if draft else "sent", note=note, period=month)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        self.announce()
        return dict(self.client_billing(display), ok=True, invoice=self._bill(made),
                    note="Recorded. Stoa keeps the account; send the invoice itself the way you always do.")

    def skip_month(self, name: Any, period: Any, skip: bool = True) -> dict:
        """A month not to bill (time off, a pause), or, with ``skip=False``, one to bill after all."""
        display = self._client_name(name)
        terms, month = self.terms_of(display), self._month(period)
        if not terms or not month:
            return {"ok": False, "error": "say which client with billing terms, and which month"}
        skipped = set(terms["skipped"])
        skipped = skipped | {month} if skip else skipped - {month}
        self.store.org_put(display, terms=dict(terms, skipped=sorted(skipped)))
        self.announce()
        return dict(self.client_billing(display), ok=True)

    def client_invoice(self, args: dict, actor: str = "") -> dict:
        """gtm_invoice with ``client``: terms | add | list | pay | send | void | update | remove | undo | skip."""
        action = str(args.get("action") or "list").lower()
        actor = actor or self.actor()
        display = self._client_name(args.get("client"))
        if not display:
            return {"ok": False, "error": "say which client (`client`: the company a search is for, or an account)"}
        if action == "list":
            return dict(self.client_billing(display), ok=True)
        if action == "terms":
            terms = args.get("terms") if isinstance(args.get("terms"), dict) else {
                key: args.get(key) for key in ("kind", "rate", "hours_week", "bill_to", "start", "end", "due_days") if args.get(key) not in (None, "")}
            return self.client_terms(display, terms, actor)
        if action == "add":
            return self.bill_client(display, args.get("period"), args.get("hours"), args.get("amount"), args.get("number") or "",
                                    args.get("due"), actor, draft=str(args.get("status") or "").lower() == "draft")
        if action in ("skip", "unskip"):
            return self.skip_month(display, args.get("period"), action == "skip")
        ref = st.client_ref(display)
        listed, want = self.store.invoices(ref), str(args.get("invoice") or "").strip().lower().lstrip("#")
        if action == "undo":
            target = next((i for i in listed if want and (str(i["id"]) == want or (i["number"] or "").lower().lstrip("#") == want)), None)
            last = self.store.invoice_last(ref, target["id"] if target else None)
            if last is None:
                return {"ok": False, "error": "Nothing to take back on %s's invoices." % display}
            with self.store.operation():
                result = self.store.undo(ref, op=last["op"], actor=actor)
            if not result:
                return {"ok": False, "error": "Nothing to take back on %s's invoices." % display}
            if result["conflicts"]:
                return {"ok": False, "error": "Not taken back, because it would overwrite a later change: %s. Nothing was changed."
                                              % "; ".join(self._conflicts(result["conflicts"]))}
            self.announce()
            return dict(self.client_billing(display), ok=True, undid=result["text"])
        period = self._month(args.get("period"))
        found = [i for i in listed if (want and (str(i["id"]) == want or (i["number"] or "").lower().lstrip("#") == want))
                 or (not want and period and i.get("period") == period)]
        if not want and not period:
            found = [i for i in listed if i["state"] in ("sent", "partial", "overdue", "draft")]
        if len(found) != 1:
            return {"ok": False, "error": "say which of %s's invoices (`invoice`: its number, or `period`: its month)" % display,
                    "invoices": [self._bill(i) for i in listed]}
        invoice = found[0]
        try:
            with self.store.operation():
                if action == "pay":
                    on = st.parse_due(args["on"]) if args.get("on") else ""
                    self.store.invoice_pay(invoice["id"], args.get("amount"), on=on or "", actor=actor)
                elif action == "send":
                    self.store.invoice_update(invoice["id"], actor=actor, status="sent", issued_on=st.today(), said="%s sent" % self.store._named(invoice))
                elif action == "void":
                    self.store.invoice_update(invoice["id"], actor=actor, status="void", said="%s voided" % self.store._named(invoice))
                elif action == "update":
                    due = None
                    if args.get("due") not in (None, ""):
                        due, error = self._due_on(args.get("due"))
                        if error:
                            return error
                    self.store.invoice_update(invoice["id"], actor=actor, amount=args.get("amount"), number=args.get("number"),
                                              due_on=due, note=args.get("note"))
                elif action == "remove":
                    self.store.invoice_remove(invoice["id"], actor=actor)
                else:
                    return {"ok": False, "error": "unknown action '%s'" % action,
                            "actions": ["list", "terms", "add", "skip", "unskip", "pay", "send", "void", "update", "remove", "undo"]}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        self.announce()
        return dict(self.client_billing(display), ok=True, action=action)

    def clients(self) -> list:
        """Every client the desk works for: the companies its searches are for, and any company with billing terms.
        For the page's list of clients and the agent's overview."""
        playbook, touches = self.playbook(), self.store.touches()
        groups = {row["key"]: row for row in self.store.grps()}
        deals = self.store.list(status="all")
        out: dict = {}
        for group in groups.values():
            if not group.get("client"):
                continue
            key = st.org_key(group["client"])
            entry = out.setdefault(key, {"name": group["client"], "key": key, "groups": [], "open": 0, "open_value": 0.0, "won": 0,
                                         "last_touch": ""})
            entry["groups"].append({"name": group["name"], "status": group.get("status") or "open"})
        for org in self.store.orgs():
            if (org.get("terms") or "{}") != "{}" and st.org_key(org["name"]) not in out:
                out[st.org_key(org["name"])] = {"name": org["name"], "key": st.org_key(org["name"]), "groups": [], "open": 0,
                                                 "open_value": 0.0, "won": 0, "last_touch": ""}
        by_group = {}
        for key, entry in out.items():
            for group in entry["groups"]:
                by_group[st.group_key(group["name"])] = key
        for deal in deals:
            owner = by_group.get(st.group_key(deal["grp"]))
            if not owner:
                continue
            entry = out[owner]
            if deal["status"] == "open":
                entry["open"] += 1
                entry["open_value"] += deal["value"]
            elif deal["status"] == "won":
                entry["won"] += 1
            entry["last_touch"] = max(entry["last_touch"], touches.get(deal["id"], ""))
        rows = []
        for entry in out.values():
            billing = self.client_billing(entry["name"])
            terms = billing["terms"]
            rows.append(dict(entry, open_groups=len([g for g in entry["groups"] if g["status"] in ("open", "hold")]),
                             terms=terms, owed=billing["owed"], overdue=billing["overdue"], to_bill=len(billing["to_bill"])))
        return sorted(rows, key=lambda row: (-row["open"], row["name"].lower()))

    def client_view(self, name: Any) -> dict:
        """One client: what is known about it, the searches it has (and where each stands), the records in them, the people
        on its side, how it is billed, and what happened lately."""
        display = self._client_name(name)
        key = st.org_key(display)
        if not key:
            return {"ok": False, "error": "say which client"}
        playbook, touches = self.playbook(), self.store.touches()
        mine = [row for row in self.store.grps() if row.get("client") and st.org_key(row["client"]) == key]
        if not mine and not self.terms_of(display) and not self.store.org_get(display):
            return {"ok": False, "error": "no client on the desk is called '%s'" % str(name)[:60],
                    "clients": [row["name"] for row in self.clients()][:20]}
        keys = {row["key"] for row in mine}
        deals = [deal for deal in self.store.list(status="all") if st.group_key(deal["grp"]) in keys]
        names = self._group_names(self.store.list(status="all"))
        searches = []
        for row in mine:
            here = [deal for deal in deals if st.group_key(deal["grp"]) == row["key"]]
            open_here = [deal for deal in here if deal["status"] == "open"]
            searches.append({"name": names.get(row["key"], row["name"]), "status": row.get("status") or "open",
                             "status_label": self.GROUP_LABELS.get(row.get("status") or "open", "Open"),
                             "open": len(open_here), "open_value": st.total(open_here), "won": len([d for d in here if d["status"] == "won"]),
                             "facts": [{"label": field["label"], "value": (row.get("fields") or {}).get(field["key"], "")}
                                       for field in playbook.group_fields if (row.get("fields") or {}).get(field["key"])],
                             "last_touch": max([touches.get(d["id"], "") for d in here] or [""])})
        searches.sort(key=lambda row: (row["status"] not in ("open", "hold"), -row["open"], row["name"].lower()))
        people, seen = [], set()
        for row in mine:
            for person in self.store.grp_people(row["name"]):
                if person["id"] not in seen:
                    seen.add(person["id"])
                    people.append(self._side(person, on="group", group=names.get(row["key"], row["name"])))
        for deal in deals:
            for person in self.store.people(deal["id"]):
                if person["id"] not in seen:
                    seen.add(person["id"])
                    people.append(self._side(person, on="record", record=deal["id"], record_name=deal["name"]))
        org = self.store.org_get(display) or {}
        ids = {deal["id"]: deal["name"] for deal in deals}
        recent = [a for a in self.store.activities(limit=400) if a["deal_id"] in ids][:20]
        open_deals, won = [d for d in deals if d["status"] == "open"], [d for d in deals if d["status"] == "won"]
        return {"ok": True, "client": {"name": display, "key": key, "domain": org.get("domain") or "", "note": org.get("note") or ""},
                "searches": searches, "open": len(open_deals), "open_value": st.total(open_deals), "won": len(won), "won_value": st.total(won),
                "lost": len([d for d in deals if d["status"] == "lost"]),
                "last_touch": max([touches.get(d["id"], "") for d in deals] or [""]),
                "records": [self._brief(deal, playbook) for deal in sorted(deals, key=lambda d: (d["status"] != "open", -playbook.position(d["stage"]), d["name"].lower()))],
                "people": people[:60], "billing": self.client_billing(display),
                "activity": [{"deal": a["deal_id"], "name": ids[a["deal_id"]], "at": a["at"], "kind": a["kind"], "text": a["text"], "by": a["actor"]}
                             for a in recent]}

    def odds(self, playbook: Playbook, deals: list) -> dict:
        """How likely a record at each stage is to be won, as a percentage, and what that rests on:
        this desk's own history once enough records have closed, else the playbook's figures, else
        an even climb through the stages."""
        stages = playbook.stages
        closed = [d for d in deals if d["status"] in ("won", "lost")]
        even = {stage["id"]: round(100 * (index + 1) / (len(stages) + 1)) for index, stage in enumerate(stages)}
        stated = {stage["id"]: stage["win_probability"] for stage in stages if stage.get("win_probability") is not None}
        if len(closed) >= 12:
            reached, won = [0] * len(stages), [0] * len(stages)
            for deal in closed:
                far = len(stages) - 1 if deal["status"] == "won" else max(0, playbook.position(str(deal["fields"].get("_prev_stage") or "")))
                for index in range(far + 1):
                    reached[index] += 1
                    won[index] += deal["status"] == "won"
            if all(count >= 3 for count in reached):
                return {"basis": "history", "closed": len(closed),
                        "by_stage": {stage["id"]: round(100 * won[index] / reached[index]) for index, stage in enumerate(stages)}}
        if stated:
            return {"basis": "playbook", "closed": len(closed), "by_stage": dict(even, **stated)}
        return {"basis": "even", "closed": len(closed), "by_stage": even}

    def cash(self, limit: int = 40) -> dict:
        """The money side of the desk: what was won, billed, collected and is late; what the open
        pipeline is likely to bring in and when; and where the won work stands."""
        playbook, today = self.playbook(), dt.date.today()
        deals = self.store.list(status="all")
        names = {deal["id"]: deal for deal in deals}
        clients = {st.client_ref(org["name"]): org["name"] for org in self.store.orgs()}
        invoices = [i for i in self.store.invoices() if i["deal_id"] in names or i["deal_id"] in clients]
        waiting = self.to_bill()
        month = today.isoformat()[:7]
        nxt = (today.replace(day=1) + dt.timedelta(days=32)).isoformat()[:7]
        local_month = lambda stamp: (st.parse_ts(stamp).astimezone().date().isoformat()[:7] if st.parse_ts(stamp) else "")      # noqa: E731
        won = [d for d in deals if d["status"] == "won"]
        won_now = [d for d in won if local_month(d["stage_at"]) == month]
        per: dict = {}
        for invoice in invoices:
            per.setdefault(invoice["deal_id"], []).append(invoice)
        owing = [i for i in invoices if i["state"] in ("sent", "partial", "overdue")]
        late = [i for i in owing if i["state"] == "overdue"]
        unbilled = [(d, self.money(d, per.get(d["id"], []))["uninvoiced"]) for d in won] if playbook.billing != "none" else []
        unbilled = [(d, amount) for d, amount in unbilled if amount > 0]
        strip = {
            "won_month": {"count": len(won_now), "value": st.total(won_now)},
            "owed": {"count": len(owing), "value": sum(i["owed_cents"] for i in owing) / 100},
            "collected_month": {"value": self.store.collected(month) / 100},
            "overdue": {"count": len(late), "value": sum(i["owed_cents"] for i in late) / 100},
            "uninvoiced": {"count": len(unbilled), "value": round(sum(amount for _, amount in unbilled), 2)},
            "to_bill": {"count": len(waiting), "value": round(sum(row["amount"] for row in waiting), 2)},
        }
        odds = self.odds(playbook, deals)
        buckets = {key: {"key": key, "label": label, "count": 0, "cents": 0, "weighted": 0.0} for key, label in (
            ("past", "Past its date"), ("this", today.strftime("%B")), ("next", dt.date.fromisoformat(nxt + "-01").strftime("%B")),
            ("later", "Later"), ("none", "No date set"))}
        for deal in deals:
            if deal["status"] != "open" or playbook.position(deal["stage"]) < 0:
                continue
            close = deal.get("close_on") or ""
            key = "none" if not close else "past" if close < today.isoformat() else "this" if close[:7] == month \
                else "next" if close[:7] == nxt else "later"
            bucket = buckets[key]
            bucket["count"] += 1
            bucket["cents"] += deal["value_cents"]
            bucket["weighted"] += deal["value_cents"] * odds["by_stage"].get(deal["stage"], 0) / 100
        forecast = [{"key": b["key"], "label": b["label"], "count": b["count"], "value": b["cents"] / 100, "weighted": round(b["weighted"] / 100, 2)}
                    for b in buckets.values()]
        lane = []
        for deal in sorted(won, key=lambda d: d["stage_at"], reverse=True)[:limit]:
            money = self.money(deal, per.get(deal["id"], []))
            ids = [item["id"] for item in playbook.delivery]
            lane.append({"id": deal["id"], "name": deal["name"], "company": deal["company"], "value": deal["value"],
                         "delivery": deal["delivery"] if deal["delivery"] in ids else (ids[0] if ids else ""), "won_at": deal["stage_at"],
                         "invoiced": money["invoiced"], "paid": money["paid"], "owed": money["owed"], "overdue": money["overdue"],
                         "uninvoiced": money["uninvoiced"]})
        order = {"overdue": 0, "partial": 1, "sent": 1, "draft": 2}
        listed = sorted([i for i in invoices if i["state"] in order], key=lambda i: (order[i["state"]], i["due_on"] or "9999", i["id"]))
        settled = sorted([i for i in invoices if i["state"] == "paid"], key=lambda i: i["paid_on"] or i["updated_at"], reverse=True)[:5]
        return {"ok": True, "view": "cash", "currency": playbook.currency, "strip": strip,
                "forecast": {"basis": odds["basis"], "closed": odds["closed"], "buckets": forecast,
                             "value": sum(b["value"] for b in forecast), "weighted": round(sum(b["weighted"] for b in forecast), 2),
                             "odds": [{"stage": s["id"], "label": s["label"], "odds": odds["by_stage"].get(s["id"], 0)} for s in playbook.stages]},
                "delivery": {"stages": playbook.delivery, "records": lane},
                "to_bill": waiting, "billing": playbook.billing,
                "invoices": [dict(self._bill(i, names[i["deal_id"]]["name"] if i["deal_id"] in names else clients[i["deal_id"]]),
                                  client=clients.get(i["deal_id"], ""), period=i.get("period") or "") for i in listed + settled]}

    def cash_text(self) -> str:
        cash, playbook = self.cash(), self.playbook()
        money = lambda value: cards.money(value, playbook.currency) or "0"       # noqa: E731
        strip, lines = cash["strip"], ["Cash · %s" % playbook.title]
        if self.tour():
            lines.append("Sample data: nothing here is real.")
        lines += ["%s this month: %s (%d)" % (playbook.won["label"], money(strip["won_month"]["value"]), strip["won_month"]["count"]),
                  "Collected this month: %s" % money(strip["collected_month"]["value"]),
                  "Invoiced and owed: %s (%d)" % (money(strip["owed"]["value"]), strip["owed"]["count"]),
                  "Overdue: %s (%d)" % (money(strip["overdue"]["value"]), strip["overdue"]["count"]),
                  "%s but not invoiced: %s (%d)" % (playbook.won["label"], money(strip["uninvoiced"]["value"]), strip["uninvoiced"]["count"])]
        if strip.get("to_bill", {}).get("count"):
            exact = lambda value: cards.exact(value, playbook.currency)      # noqa: E731
            lines.append("To bill (months ended): %s (%d)" % (exact(strip["to_bill"]["value"]), strip["to_bill"]["count"]))
            lines += ["  %s, %s: %s%s" % (row["client"], row["label"], exact(row["amount"]), " for %d h" % row["hours"] if row.get("hours") else "")
                      for row in cash["to_bill"][:8]]
        lines += ["", "Forecast (weighted by %s)" % {"history": "how often each stage has gone on to close here",
                                                     "playbook": "the playbook's odds for each stage", "even": "an even climb through the stages"}[cash["forecast"]["basis"]]]
        lines += ["  %s: %s of %s (%d)" % (b["label"], money(b["weighted"]), money(b["value"]), b["count"]) for b in cash["forecast"]["buckets"] if b["count"]]
        late = [i for i in cash["invoices"] if i["state"] == "overdue"]
        if late:
            lines += ["", "Overdue invoices"] + ["  %s — %s%s, %dd late" % (i["name"], money(i["owed"]), " (#%s)" % i["number"] if i["number"] else "",
                                                                            i["days_overdue"]) for i in late[:10]]
        return "\n".join(lines)

    # -- gtm_contact: the other people on a record, and what is known about a company ---------------
    def people(self, deal: dict) -> list:
        """Everyone on a record: the record's own person first, then the others."""
        first = {"primary": True, "name": deal["name"], "email": deal["email"], "linkedin": deal["linkedin"], "phone": deal.get("phone") or "",
                 "title": str((deal.get("fields") or {}).get("title") or ""), "role": ""}
        return [first] + [dict({key: person[key] for key in ("id", "name", "email", "title", "role", "linkedin", "phone", "note")},
                               primary=False, also_on=len(self.store.person_records(person["id"])) - 1)
                          for person in self.store.people(deal["id"])]

    def _brief(self, deal: dict, playbook: Playbook) -> dict:
        return {"id": deal["id"], "name": deal["name"], "company": deal["company"], "group": deal["grp"], "status": deal["status"],
                "stage": deal["stage"], "stage_label": playbook.label(deal["stage"]), "value": deal["value"],
                "next_step": deal["next_step"], "next_due": deal["next_due"]}

    def contact(self, args: dict, actor: str = "") -> dict:
        action = str(args.get("action") or "find").lower()
        actor = actor or self.actor()
        if action == "match":
            return self.match(args.get("emails") or args.get("email"))
        if action == "find":
            return self.find_people(str(args.get("query") or args.get("name") or ""))
        if action == "company":
            return self.company(args.get("company") or args.get("query"), note=args.get("note"), domain=args.get("domain"))
        if args.get("group") and not args.get("deal") and action in ("people", "add", "update", "remove"):
            return self._group_contact(action, args, actor)
        deal, error = self._resolve(args.get("deal"))
        if error:
            return error
        if action == "people":
            return {"ok": True, "deal": deal["id"], "name": deal["name"], "people": self.people(deal)}
        if action == "add":
            name = str(args.get("name") or "").strip()
            if not name:
                return {"ok": False, "error": "add needs the person's name"}
            email = str(args.get("email") or "").strip().lower()
            for known in self.people(deal):
                if (email and (known["email"] or "").lower() == email) or known["name"].lower() == name.lower():
                    return {"ok": True, "exists": True, "deal": deal["id"], "people": self.people(deal)}
            with self.store.operation():
                self.store.person_add(deal["id"], name, actor=actor, **{key: args.get(key) for key in st.PERSON if key != "name"})
        elif action in ("update", "remove"):
            want, listed = str(args.get("person") or args.get("name") or "").strip().lower(), self.store.people(deal["id"])
            found = [p for p in listed if str(p["id"]) == want or p["name"].lower() == want or (p["email"] and p["email"].lower() == want)]
            if len(found) != 1:
                return {"ok": False, "error": "say which person on %s: pass `person` (a name, an email or an id)" % deal["name"],
                        "people": self.people(deal)}
            if action == "remove":
                self.store.person_remove(found[0]["id"], actor=actor, deal_id=deal["id"])
            else:
                values = {key: args.get(key) for key in st.PERSON if key != "name" and args.get(key) is not None}
                if args.get("new_name"):
                    values["name"] = args["new_name"]
                try:
                    with self.store.operation():
                        self.store.person_update(found[0]["id"], actor=actor, deal_id=deal["id"], **values)
                except ValueError as exc:
                    return {"ok": False, "error": str(exc)}
        else:
            return {"ok": False, "error": "unknown action '%s'" % action,
                    "actions": ["find", "match", "people", "add", "update", "remove", "company"]}
        deal = self.store.get(deal["id"])
        self._changed(deal)
        return {"ok": True, "action": action, "deal": deal["id"], "name": deal["name"], "people": self.people(deal)}

    def _addresses(self) -> tuple:
        """``(by address, by company domain, records by id)`` across every record and everyone on it."""
        deals = self.store.list(status="all")
        by_id = {deal["id"]: deal for deal in deals}
        exact: dict = {}
        domains: dict = {}

        def note(address: str, deal: dict, who: str) -> None:
            address = (address or "").strip().lower()
            if not address:
                return
            exact.setdefault(address, []).append((deal, who))
            domain = st.email_domain(address)
            if domain and deal["company"]:
                domains.setdefault(domain, {})[st.org_key(deal["company"])] = deal["company"]

        for deal in deals:
            note(deal["email"], deal, deal["name"])
        for person in self.store.people():
            if person["deal_id"] in by_id:
                note(person["email"], by_id[person["deal_id"]], person["name"])
        for org in self.store.orgs():
            if org["domain"]:
                domains.setdefault(org["domain"].lower(), {})[org["key"]] = org["name"]
        return exact, domains, by_id

    def match(self, emails: Any) -> dict:
        """Whose address is this? For each one: the records it belongs to, or, failing that, the
        company its domain belongs to and that company's open records."""
        if isinstance(emails, str):
            emails = re.split(r"[,;\s]+", emails)
        if not isinstance(emails, list) or not emails:
            return {"ok": False, "error": "match needs emails: a list of addresses"}
        playbook = self.playbook()
        exact, domains, by_id = self._addresses()
        names, rows = self._group_names(list(by_id.values())), {row["key"]: row for row in self.store.grps()}
        sides = {}
        for person in self.store.grp_people():
            if person["email"] and person["grp_key"] in names:
                sides.setdefault(person["email"].lower(), {"who": person["name"], "key": person["grp_key"], "group": names[person["grp_key"]],
                                                            "client": (rows.get(person["grp_key"]) or {}).get("client") or ""})
        out, seen = [], set()
        for raw in emails[:60]:
            found = re.search(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", str(raw or ""))
            address = found.group(0).lower() if found else ""
            if not address or address in seen:
                continue
            seen.add(address)
            hits = sorted(exact.get(address) or [], key=lambda hit: hit[0]["status"] != "open")
            side = sides.get(address)
            if side and not hits:           # on a group's side (a hiring manager, a sponsor): the records it could be about
                inside = [deal for deal in by_id.values() if st.group_key(deal["grp"]) == side["key"]]
                inside.sort(key=lambda deal: (deal["status"] != "open", deal["name"].lower()))
                out.append({"email": address, "known": True, "who": side["who"], "group": side["group"], "client": side["client"],
                            "records": [self._brief(deal, playbook) for deal in inside[:8]],
                            "note": "On the client's side of %s, not a record. Log what they say on the record it is about." % side["group"]})
                continue
            if hits:
                out.append({"email": address, "known": True, "who": hits[0][1], "records": [self._brief(deal, playbook) for deal, _ in hits[:5]]})
                continue
            companies = domains.get(st.email_domain(address)) or {}
            records = [deal for deal in by_id.values() if st.org_key(deal["company"]) in companies]
            records.sort(key=lambda deal: (deal["status"] != "open", deal["name"].lower()))
            entry = {"email": address, "known": False, "records": [self._brief(deal, playbook) for deal in records[:5]]}
            if companies:
                entry["company"] = sorted(companies.values())[0]
                entry["note"] = ("Not on the desk, but this address is at %s. If the message is about one of these records, log it "
                                 "there and propose adding the sender as a person on it." % entry["company"])
            out.append(entry)
        return {"ok": True, "matches": out}

    def find_people(self, query: str) -> dict:
        query = " ".join(query.lower().split())
        if not query:
            return {"ok": False, "error": "find needs query: a name, an email, a title or a company"}
        playbook, found = self.playbook(), []
        for deal in self.store.list(status="all"):
            for person in self.people(deal):
                text = (" ".join(str(person.get(key) or "") for key in ("name", "email", "title", "role")) + " " + deal["company"]
                        + " " + deal["grp"]).lower()
                if query in text:
                    # The record's company is its own person's. Someone else on the record is said to be there only when
                    # their address agrees: a hiring manager on a candidate's record works for the client, not the candidate's employer.
                    there = person["primary"] or (st.email_domain(person.get("email")) and
                                                  st.email_domain(person.get("email")) == st.email_domain(deal["email"]))
                    found.append(dict({key: person.get(key) or "" for key in ("name", "email", "title", "role")},
                                      primary=person["primary"], company=deal["company"] if there else "", record=self._brief(deal, playbook)))
        names, rows = self._group_names(), {row["key"]: row for row in self.store.grps()}
        for person in self.store.grp_people():
            group, client = names.get(person["grp_key"]) or "", (rows.get(person["grp_key"]) or {}).get("client") or ""
            text = " ".join([str(person.get(key) or "") for key in ("name", "email", "title", "role")] + [group, client]).lower()
            if group and query in text:
                found.append(dict({key: person.get(key) or "" for key in ("name", "email", "title", "role")}, primary=False,
                                  company=client, group=group, record=None))
        found.sort(key=lambda person: ((person["record"] or {"status": "open"})["status"] != "open", person["name"].lower()))
        return {"ok": True, "count": len(found), "people": found[:25]}

    def companies(self) -> list:
        """Every company on the desk, with what is open there: for the page's list of companies."""
        touches, rows = self.store.touches(), {}
        on: dict = {}
        for person in self.store.people():
            on.setdefault(person["deal_id"], set()).add(person["id"])
        who: dict = {}      # per company, everyone there once: a record's own person, and each contact however many records they are on
        cents: dict = {}
        for deal in self.store.list(status="all"):
            key = st.org_key(deal["company"])
            if not key:
                continue
            row = rows.setdefault(key, {"key": key, "name": deal["company"], "open": 0, "open_value": 0.0, "won": 0, "won_value": 0.0,
                                        "lost": 0, "people": 0, "last_touch": ""})
            row[deal["status"]] += 1
            if deal["status"] != "lost":
                cents[(key, deal["status"])] = cents.get((key, deal["status"]), 0) + deal["value_cents"]
            who.setdefault(key, set()).update({("record", deal["id"])} | {("person", person) for person in on.get(deal["id"], ())})
            row["last_touch"] = max(row["last_touch"], touches.get(deal["id"], "") or deal["updated_at"])
        for key, row in rows.items():
            row["people"] = len(who.get(key, ()))
            row["open_value"], row["won_value"] = cents.get((key, "open"), 0) / 100, cents.get((key, "won"), 0) / 100
        return sorted(rows.values(), key=lambda row: (-row["open_value"], -row["open"], row["name"].lower()))

    def company(self, name: Any, note: Any = None, domain: Any = None) -> dict:
        """One company: its records, everyone known there, what is open and won, and the notes kept on it."""
        key = st.org_key(name)
        if not key:
            return {"ok": False, "error": "say which company"}
        deals = self.store.list(status="all")
        here = [deal for deal in deals if st.org_key(deal["company"]) == key]
        mine = [group for group in self.store.grps() if group["client"] and st.org_key(group["client"]) == key]
        if not here and mine:
            return self._client(key, mine, deals, note, domain)
        if not here:
            near = {st.org_key(deal["company"]): deal["company"] for deal in deals if key in st.org_key(deal["company"])}
            if len(near) == 1:      # "Kestrel" for "Kestrel Labs": only one company it could be
                return self.company(next(iter(near.values())), note=note, domain=domain)
            out = {"ok": False, "error": "no company on the desk matches '%s'" % str(name)[:60], "companies": sorted(near.values())[:12]}
            # A playbook may keep the client in the group, not the company ("Staff Backend · Northwind", where the
            # company is the candidate's employer): then the records there, and the other people on them, are the answer.
            grouped = [deal for deal in deals if " %s " % key in " %s " % st.org_key(deal["grp"])]
            if grouped:
                playbook, people, seen = self.playbook(), [], set()
                grouped.sort(key=lambda d: (d["status"] != "open", d["name"].lower()))
                for deal in grouped:
                    for person in self.people(deal):
                        if person["primary"] or person["id"] in seen:
                            continue
                        seen.add(person["id"])
                        people.append(dict({k: person.get(k) or "" for k in ("name", "email", "title", "role")},
                                           record=deal["id"], record_name=deal["name"]))
                out.update(groups=sorted({deal["grp"] for deal in grouped}), records=[self._brief(deal, playbook) for deal in grouped[:25]],
                           people=people[:25],
                           note="No company is called that, but it is named in the %s of %d record%s: those records, and the other "
                                "people on them, are listed here." % (playbook.terms["group"], len(grouped), "" if len(grouped) == 1 else "s"))
            return out
        display = max((deal["company"] for deal in here), key=lambda text: sum(d["company"] == text for d in here))
        if note is not None or domain is not None:
            self.store.org_put(display, note=note, domain=domain)
        org = self.store.org_get(display) or {}
        playbook, touches = self.playbook(), self.store.touches()
        people, seen = [], {}
        for deal in sorted(here, key=lambda d: (d["status"] != "open", d["name"].lower())):
            for person in self.people(deal):
                if not person["primary"] and person["id"] in seen:          # the same person, on another record here
                    seen[person["id"]]["record_name"] += ", " + deal["name"]
                    continue
                entry = dict({k: person.get(k) or "" for k in ("name", "email", "title", "role")}, primary=person["primary"],
                             record=deal["id"], record_name=deal["name"])
                if not person["primary"]:
                    seen[person["id"]] = entry
                people.append(entry)
        names = self._group_names(deals)
        for group in mine:                  # and whoever is on the side of a group it is the client of
            for person in self.store.grp_people(group["name"]):
                if person["id"] not in seen:
                    seen[person["id"]] = dict({k: person.get(k) or "" for k in ("name", "email", "title", "role")}, primary=False,
                                              record="", record_name=names.get(group["key"], group["name"]), group=names.get(group["key"], group["name"]))
                    people.append(seen[person["id"]])
        guesses: dict = {}
        for person in people:
            found = st.email_domain(person["email"])
            if found:
                guesses[found] = guesses.get(found, 0) + 1
        open_here = [deal for deal in here if deal["status"] == "open"]
        won = [deal for deal in here if deal["status"] == "won"]
        ids = {deal["id"]: deal["name"] for deal in here}
        recent = [a for a in self.store.activities(limit=300) if a["deal_id"] in ids][:8]
        return {"ok": True,
                "company": {"name": display, "key": key, "domain": org.get("domain") or (max(guesses, key=guesses.get) if guesses else ""),
                            "note": org.get("note") or "", "website": org.get("website") or ""},
                "open": len(open_here), "open_value": st.total(open_here),
                "won": len(won), "won_value": st.total(won),
                "lost": len([deal for deal in here if deal["status"] == "lost"]),
                "last_touch": max([touches.get(deal["id"], "") for deal in here] or [""]),
                "records": [self._brief(deal, playbook) for deal in sorted(here, key=lambda d: (d["status"] != "open", -d["value"]))],
                "people": people[:60],
                "activity": [{"deal": a["deal_id"], "name": ids[a["deal_id"]], "at": a["at"], "kind": a["kind"], "text": a["text"],
                              "by": a["actor"]} for a in recent]}

    def _client(self, key: str, mine: list, deals: list, note: Any, domain: Any) -> dict:
        """A company that is no record's own but is the client of one or more groups: what is open for it, and
        who is on its side."""
        display = mine[0]["client"]
        if note is not None or domain is not None:
            self.store.org_put(display, note=note, domain=domain)
        org = self.store.org_get(display) or {}
        playbook, touches, names = self.playbook(), self.store.touches(), self._group_names(deals)
        keys = {group["key"] for group in mine}
        here = [deal for deal in deals if st.group_key(deal["grp"]) in keys]
        people, seen = [], set()

        def add(person: dict, **where: Any) -> None:
            if person["id"] not in seen:
                seen.add(person["id"])
                people.append(dict({k: person.get(k) or "" for k in ("name", "email", "title", "role")}, primary=False, **where))

        for group in mine:
            for person in self.store.grp_people(group["name"]):
                add(person, record="", record_name=names.get(group["key"], group["name"]), group=names.get(group["key"], group["name"]))
        for deal in here:                   # someone else put on one of its records is on the client's side too
            for person in self.store.people(deal["id"]):
                add(person, record=deal["id"], record_name=deal["name"], group=deal["grp"])
        guesses: dict = {}
        for person in people:
            found = st.email_domain(person["email"])
            if found:
                guesses[found] = guesses.get(found, 0) + 1
        open_here = [deal for deal in here if deal["status"] == "open"]
        won = [deal for deal in here if deal["status"] == "won"]
        ids = {deal["id"]: deal["name"] for deal in here}
        recent = [a for a in self.store.activities(limit=300) if a["deal_id"] in ids][:8]
        return {"ok": True, "via": "group", "groups": sorted(names.get(group["key"], group["name"]) for group in mine),
                "company": {"name": display, "key": key, "domain": org.get("domain") or (max(guesses, key=guesses.get) if guesses else ""),
                            "note": org.get("note") or "", "website": org.get("website") or ""},
                "open": len(open_here), "open_value": st.total(open_here), "won": len(won), "won_value": st.total(won),
                "lost": len([deal for deal in here if deal["status"] == "lost"]),
                "last_touch": max([touches.get(deal["id"], "") for deal in here] or [""]),
                "records": [self._brief(deal, playbook) for deal in sorted(here, key=lambda d: (d["status"] != "open", -d["value"]))],
                "people": people[:60],
                "activity": [{"deal": a["deal_id"], "name": ids[a["deal_id"]], "at": a["at"], "kind": a["kind"], "text": a["text"],
                              "by": a["actor"]} for a in recent]}

    # -- groups: a search, an account, a client engagement -----------------------------------------
    # A record names its group; what is known about the group itself (who the client is, where it stands, its
    # terms, who is on the client's side) is kept under the same name. A group nobody has described is still a
    # group: it has its records, and nothing else yet.
    GROUP_LABELS = {"open": "Open", "hold": "On hold", "done": "Done", "closed": "Closed"}
    GROUP_WORDS = {"open": "open", "active": "open", "live": "open", "hold": "hold", "on hold": "hold", "on-hold": "hold", "paused": "hold",
                   "done": "done", "filled": "done", "won": "done", "complete": "done", "completed": "done",
                   "closed": "closed", "cancelled": "closed", "canceled": "closed", "lost": "closed"}

    def _group_names(self, deals: Optional[list] = None) -> dict:
        """``{key: name}`` for every group there is: named on a record (in the spelling most of them use), or
        opened by itself."""
        spellings: dict = {}
        for deal in (deals if deals is not None else self.store.list(status="all")):
            key = st.group_key(deal["grp"])
            if key:
                found = spellings.setdefault(key, {})
                found[deal["grp"]] = found.get(deal["grp"], 0) + 1
        names = {key: max(sorted(found), key=found.get) for key, found in spellings.items()}
        for row in self.store.grps():
            names[row["key"]] = row["name"]             # once someone has opened or described it, that is its name
        return names

    def _group_spelling(self, group: str) -> str:
        """A group's name as the desk already has it, so one group typed two ways does not become two."""
        key = st.group_key(group)
        return self._group_names().get(key, group) if key else group

    def _resolve_group(self, ref: Any, names: Optional[dict] = None) -> tuple:
        """``(name, error)`` for a group by its name, or by a part of it that only one group has."""
        term, key = self.playbook().terms["group"], st.group_key(ref)
        if not key:
            return "", {"ok": False, "error": "say which %s: pass `group` (its name)" % term}
        names = names if names is not None else self._group_names()
        if key in names:
            return names[key], None
        near = sorted(name for found, name in names.items() if " %s " % key in " %s " % found)
        if len(near) == 1:
            return near[0], None
        if near:
            return "", {"ok": False, "error": "'%s' matches several; use one of these names" % str(ref)[:60], "groups": near[:20]}
        return "", {"ok": False, "error": "no %s on the desk matches '%s'" % (term, str(ref)[:60]), "groups": sorted(names.values())[:20]}

    def groups(self) -> list:
        """Every group, with who the client is, where it stands and what is in it."""
        deals, touches = self.store.list(status="all"), self.store.touches()
        names, rows = self._group_names(deals), {row["key"]: row for row in self.store.grps()}
        inside: dict = {}
        for deal in deals:
            key = st.group_key(deal["grp"])
            if key:
                inside.setdefault(key, []).append(deal)
        sides: dict = {}                    # per group, everyone on the client's side once: put on the group, or on one of its records
        for person in self.store.grp_people():
            sides.setdefault(person["grp_key"], set()).add(person["id"])
        where = {deal["id"]: st.group_key(deal["grp"]) for deal in deals}
        for person in self.store.people():
            if where.get(person["deal_id"]):
                sides.setdefault(where[person["deal_id"]], set()).add(person["id"])
        people = {key: len(found) for key, found in sides.items()}
        out = []
        for key, name in names.items():
            row, here = rows.get(key) or {}, inside.get(key, [])
            open_here, won = [d for d in here if d["status"] == "open"], [d for d in here if d["status"] == "won"]
            status = row.get("status") or "open"
            out.append({"name": name, "key": key, "client": row.get("client") or "", "status": status, "status_label": self.GROUP_LABELS[status],
                        "value": row.get("value") or 0, "note": row.get("note") or "", "fields": row.get("fields") or {},
                        "records": len(here), "open": len(open_here), "open_value": st.total(open_here), "won": len(won),
                        "won_value": st.total(won), "lost": len([d for d in here if d["status"] == "lost"]), "people": people.get(key, 0),
                        "last_touch": max([touches.get(d["id"], "") or d["updated_at"] for d in here] or [row.get("updated_at") or ""])})
        order = {"open": 0, "hold": 1, "done": 2, "closed": 3}
        out.sort(key=lambda group: (order[group["status"]], -group["open_value"], group["name"].lower()))
        return out

    @staticmethod
    def _side(person: dict, **where: Any) -> dict:
        return dict({key: person.get(key) or "" for key in ("id", "name", "email", "title", "role", "linkedin", "phone")}, **where)

    def group_view(self, ref: Any) -> dict:
        """One group: what is known about it, the records in it, and who is on the client's side."""
        name, error = self._resolve_group(ref)
        if error:
            return error
        playbook, key, touches = self.playbook(), st.group_key(name), self.store.touches()
        row = self.store.grp_get(name) or {}
        deals = [deal for deal in self.store.list(status="all") if st.group_key(deal["grp"]) == key]
        people = [self._side(person, on="group") for person in self.store.grp_people(name)]
        seen = {person["id"] for person in people}
        for deal in deals:                  # the other people on its records: on the client's side as well, kept where they were put
            for person in self.store.people(deal["id"]):
                if person["id"] not in seen:
                    seen.add(person["id"])
                    people.append(self._side(person, on="record", record=deal["id"], record_name=deal["name"]))
        company = None
        if playbook.group_is_company():
            # An account or a client is a company: what is kept on the company (its email domain, its notes) and every
            # person on its records belong on the same page, so there is one place to look.
            names = [deal["company"] for deal in deals if deal["company"]]
            display = max(names, key=names.count) if names else name
            org = self.store.org_get(display) or {}
            seen_domains = [st.email_domain(deal["email"]) for deal in deals if st.email_domain(deal["email"])]
            company = {"name": display, "domain": org.get("domain") or (max(seen_domains, key=seen_domains.count) if seen_domains else ""),
                       "domain_known": bool(org.get("domain")), "note": org.get("note") or ""}
            for deal in deals:
                if deal["status"] == "lost":
                    continue
                own = self.people(deal)[0]
                people.append(dict(self._side(own, on="record", record=deal["id"], record_name=deal["name"]), primary=True, id="record:" + deal["id"]))
        status = row.get("status") or "open"
        open_here, won = [d for d in deals if d["status"] == "open"], [d for d in deals if d["status"] == "won"]
        ids = {deal["id"]: deal["name"] for deal in deals}
        recent = [a for a in self.store.activities(limit=400) if a["deal_id"] in ids][:20]
        return {"ok": True,
                "group": {"name": name, "key": key, "client": row.get("client") or "", "status": status, "status_label": self.GROUP_LABELS[status],
                          "value": row.get("value") or 0, "note": row.get("note") or "", "fields": row.get("fields") or {},
                          "facts": [{"key": field["key"], "label": field["label"], "value": (row.get("fields") or {}).get(field["key"], "")}
                                    for field in playbook.group_fields]},
                "open": len(open_here), "open_value": st.total(open_here), "won": len(won), "won_value": st.total(won),
                "lost": len([d for d in deals if d["status"] == "lost"]),
                "last_touch": max([touches.get(d["id"], "") for d in deals] or [""]),
                "records": [self._brief(deal, playbook) for deal in
                            sorted(deals, key=lambda d: (d["status"] != "open", -playbook.position(d["stage"]), d["name"].lower()))],
                "people": people[:60], "company": company, "billing": self.client_billing(name) if company is not None else None,
                "activity": [{"deal": a["deal_id"], "name": ids[a["deal_id"]], "at": a["at"], "kind": a["kind"], "text": a["text"],
                              "by": a["actor"]} for a in recent]}

    def group(self, args: dict, actor: str = "") -> dict:
        """gtm_group: list | get | set | rename."""
        action, actor = str(args.get("action") or "list").lower(), actor or self.actor()
        playbook = self.playbook()
        term = playbook.terms["group"]
        if action == "list":
            listed = self.groups()
            return {"ok": True, "called": term, "count": len(listed),
                    "groups": [{k: v for k, v in group.items() if k in ("name", "status") or v not in ("", 0, 0.0, {}, None)}
                               for group in listed[:80]]}
        ref = args.get("group") or args.get("name")
        if action == "get":
            return self.group_view(ref)
        if action == "set":
            key, names = st.group_key(ref), self._group_names()
            if not key:
                return {"ok": False, "error": "set needs `group`: the name of the %s" % term}
            created = False
            if key in names:
                name = names[key]
            elif args.get("new"):
                name, created = " ".join(str(ref).split())[:200], True
            else:
                name, error = self._resolve_group(ref, names)
                if error and "several" in error["error"]:
                    return dict(error, note="Pass new=true to open a %s with exactly this name." % term)
                if error:                               # none like it: this opens it
                    name, created = " ".join(str(ref).split())[:200], True
            status = None
            if args.get("status") not in (None, ""):
                status = self.GROUP_WORDS.get(" ".join(str(args["status"]).lower().split()))
                if status is None:
                    return {"ok": False, "error": "status is one of: open, on hold, done, closed. Nothing was changed."}
            fields, ignored = None, []
            if isinstance(args.get("fields"), dict):
                by_key = {field["key"]: field["key"] for field in playbook.group_fields}
                by_label = {field["label"].lower(): field["key"] for field in playbook.group_fields}
                fields = {}
                for field, value in args["fields"].items():
                    found = by_key.get(str(field)) or by_label.get(" ".join(str(field).lower().split()))
                    if found:
                        fields[found] = value
                    else:
                        ignored.append(str(field)[:60])
            try:
                with self.store.operation():
                    self.store.grp_put(name, actor=actor, client=args.get("client"), status=status, value=args.get("value"),
                                       note=args.get("note"), fields=fields)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            out = self.group_view(name)
            out.update(action="set", created=created)
            if ignored:
                out["ignored"] = ignored
                out["note"] = "Not kept: %s. On a %s this playbook keeps: %s." % (
                    ", ".join(ignored), term, ", ".join(field["label"] for field in playbook.group_fields) or "no extra facts")
            return out
        if action == "rename":
            name, error = self._resolve_group(ref)
            if error:
                return error
            new = " ".join(str(args.get("to") or "").split())
            if not new:
                return {"ok": False, "error": "rename needs `to`: the new name"}
            try:
                moved = self.store.grp_rename(name, new, actor=actor)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            out = self.group_view(new)
            out.update(action="rename", renamed_on=moved)
            return out
        return {"ok": False, "error": "unknown action '%s'" % action, "actions": ["list", "get", "set", "rename"]}

    def _group_contact(self, action: str, args: dict, actor: str) -> dict:
        """The people on a group's side, through gtm_contact: people | add | update | remove with `group`."""
        name, error = self._resolve_group(args.get("group"))
        if error:
            return error

        def out(**more: Any) -> dict:
            return dict({"ok": True, "action": action, "group": name,
                         "people": [self._side(person, on="group") for person in self.store.grp_people(name)]}, **more)

        listed = self.store.grp_people(name)
        if action == "people":
            return out()
        if action == "add":
            who, email = str(args.get("name") or "").strip(), str(args.get("email") or "").strip().lower()
            if not who:
                return {"ok": False, "error": "add needs the person's name"}
            for known in listed:
                if (email and (known["email"] or "").lower() == email) or known["name"].lower() == who.lower():
                    return out(exists=True)
            with self.store.operation():
                self.store.grp_person_add(name, who, actor=actor, **{key: args.get(key) for key in st.PERSON if key != "name"})
            return out()
        want = str(args.get("person") or args.get("name") or "").strip().lower()
        found = [p for p in listed if str(p["id"]) == want or p["name"].lower() == want or (p["email"] and p["email"].lower() == want)]
        if len(found) != 1:
            return dict(out(), ok=False, error="say which person on %s: pass `person` (a name, an email or an id)" % name)
        if action == "remove":
            self.store.grp_person_remove(name, found[0]["id"], actor=actor)
        else:
            values = {key: args.get(key) for key in st.PERSON if key != "name" and args.get(key) is not None}
            if args.get("new_name"):
                values["name"] = args["new_name"]
            try:
                with self.store.operation():
                    self.store.grp_person_update(name, found[0]["id"], actor=actor, **values)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
        return out()

    # -- gtm_card -------------------------------------------------------------------------------
    def card(self, args: dict, session: Optional[dict] = None) -> dict:
        deal, error = self._resolve(args.get("deal"))
        if error:
            return error
        session = session or {}
        channel = str(args.get("channel") or "").strip()
        if not channel and session.get("platform") == "slack":
            channel = session.get("chat_id") or ""
        text = self.text_card(deal)
        if not channel and self.desktop_open(session):
            return {"ok": True, "posted": False, "directive": "::stoa-card{id=\"%s\"}" % deal["id"],
                    "note": "Show the card by writing that directive as a paragraph by itself, exactly as given. "
                            "The app draws the live card with its buttons there. Do not also paste the record as text."}
        if self.slack is None or not self.slack.available or not channel:
            why = ("Slack is not connected in this session" if self.slack is None or not self.slack.available
                   else "no Slack channel was given")
            return {"ok": True, "posted": False, "card": text,
                    "note": "%s, so here is the card as text. Show it to the user as is." % why}
        # A card posted into the conversation that asked for it goes in that conversation's thread
        # and remembers the session, so a later button click can hand the writing back to it.
        here = session.get("platform") == "slack" and channel == session.get("chat_id")
        thread = session.get("thread_id") or "" if here and not args.get("top_level") else ""
        asker = session.get("user_id") or "" if session.get("platform") == "slack" else ""     # the person it is posted for
        result = self.slack.post_card(channel, deal["id"], thread, session.get("session_key") or "" if here else "", asker)
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error") or "Slack did not accept the card", "card": text}
        return {"ok": True, "posted": True, "channel": result.get("channel", channel), "ts": result.get("ts", ""),
                "note": "The card is posted with its buttons. Do not repeat its contents; a one-line confirmation is enough."}

    # -- gtm_enrich -----------------------------------------------------------------------------
    def enrich(self, args: dict) -> dict:
        if self.tour():
            return {"ok": False, "error": "Lookups are switched off on the sample desk, so no credits are spent on "
                                          "made-up people. Leave the tour to look up real records."}
        key = self.secret(PDL_KEY)
        if not key:
            return {"ok": False, "error": "People Data Labs is not set up. Add PDL_API_KEY in the plugin's settings "
                                          "(or `hermes config` secrets) to turn on enrichment."}
        playbook = self.playbook()
        if playbook.enrich == "none":
            return {"ok": False, "error": "Enrichment is switched off in the %s playbook (enrich: none), so no lookup "
                                          "was made." % playbook.name}
        deal = None
        if args.get("deal"):
            deal, error = self._resolve(args.get("deal"))
            if error:
                return error
        kind = str(args.get("kind") or ("company" if playbook.enrich == "company" else "person")).lower()
        if kind not in ("person", "company"):
            return {"ok": False, "error": "kind is person or company"}
        if kind == "company":
            lookup = {"website": str(args.get("website") or ""), "name": str(args.get("company") or (deal or {}).get("company") or "")}
        else:
            lookup = {"email": str(args.get("email") or (deal or {}).get("email") or ""),
                      "linkedin": str(args.get("linkedin") or (deal or {}).get("linkedin") or ""),
                      "name": str(args.get("name") or (deal or {}).get("name") or ""),
                      "company": str(args.get("company") or (deal or {}).get("company") or "")}
        if kind == "person":  # one identifier, strongest first, so a record enriched once keeps hitting the cache
            if lookup["email"]:
                lookup = {"email": lookup["email"]}
            elif lookup["linkedin"]:
                lookup = {"linkedin": lookup["linkedin"]}
        elif lookup["website"]:
            lookup = {"website": lookup["website"]}
        cache_key = "pdl:%s:%s" % (kind, "|".join("%s=%s" % (k, v.lower()) for k, v in sorted(lookup.items()) if v))
        result = self.store.cache_get(cache_key, max_age_days=90)
        cached = result is not None
        if result is None:
            month = "pdl_calls:" + st.today()[:7]
            cap = _int(self.setting("pdl_monthly_cap", 50), 50, 0, 10 ** 6)
            used = _int(self.store.meta_get(month, "0"), 0, 0, 10 ** 9)
            if cap and used >= cap:
                return {"ok": False, "error": "The monthly People Data Labs cap (%d lookups) is used up. "
                                              "Raise pdl_monthly_cap in the plugin's settings to continue." % cap}
            if kind == "company":
                result = sources.pdl_company(key, http=self.http, **lookup)
            else:
                result = sources.pdl_person(key, http=self.http, **lookup)
            if not result.get("ok"):
                return result
            self.store.bump(month)
            self.store.cache_put(cache_key, result)
        out = dict(result, kind=kind, cached=cached)
        profile = result.get("profile") or {}
        if not result.get("match"):
            out["summary"] = "People Data Labs has no match for that %s." % kind
            return out
        out["summary"] = _profile_line(kind, profile)
        if deal is not None:
            self._apply_profile(deal, kind, profile, playbook)
            out["deal"] = self.view(self.store.get(deal["id"]), playbook)
        return out

    def _apply_profile(self, deal: dict, kind: str, profile: dict, playbook: Playbook) -> None:
        """Fill blanks only: enrichment never overwrites what a person typed."""
        values, fields = {}, {"enriched": st.today()}
        keys = {f["key"] for f in playbook.fields}
        if kind == "person":
            if not deal["company"] and profile.get("company"):
                values["company"] = profile["company"]
            if not deal["linkedin"] and profile.get("linkedin"):
                values["linkedin"] = profile["linkedin"]
            for key in ("title", "location"):
                if key in keys and not deal["fields"].get(key) and profile.get(key):
                    fields[key] = profile[key]
        else:
            for key, source in (("industry", "industry"), ("company_size", "size"), ("location", "location")):
                if key in keys and not deal["fields"].get(key) and profile.get(source):
                    fields[key] = profile[source]
        self.store.update(deal["id"], actor="People Data Labs", fields=fields, **values)
        self.store.log(deal["id"], "note", "Enriched: " + _profile_line(kind, profile), actor="People Data Labs")

    # -- gtm_notes ------------------------------------------------------------------------------
    def _note_sources(self, days: int) -> tuple:
        notes, problems = [], []
        folder = str(self.setting("notes_dir", "") or "")
        if folder:
            notes += sources.folder_notes(folder, days)
        key = self.secret(GRANOLA_KEY)
        if key:
            found, problem = sources.granola_notes(key, days, http=self.http)
            notes += found
            if problem:
                problems.append(problem)
        status = {"folder": folder or "not set (notes_dir)", "granola": "connected" if key else "not set (GRANOLA_API_KEY)"}
        return notes, status, problems

    def notes(self, args: dict) -> dict:
        action = str(args.get("action") or "inbox").lower()
        days = _int(args.get("days") or self.setting("notes_days", 7) or 7, 7, 1, 3650)
        if action == "inbox":
            notes, status, problems = self._note_sources(days)
            waiting = [n for n in notes if not self.store.note_seen(n["id"])]
            open_deals = self.store.list(status="open")
            for note in waiting:
                note["suggested"] = _suggest(note, open_deals)
                note.pop("path", None)
            out = {"ok": True, "notes": waiting[:25], "sources": status}
            if problems:
                out["problems"] = problems
            if not waiting and status["folder"].startswith("not set") and status["granola"].startswith("not set"):
                out["hint"] = ("No meeting-note source is set up. Point notes_dir at a folder of Markdown or text notes, "
                               "or add GRANOLA_API_KEY, in the plugin's settings.")
            return out
        note_id = str(args.get("note") or "").strip()
        if not note_id:
            return {"ok": False, "error": "pass the note id from the inbox"}
        if action == "read":
            return self._read_note(note_id)
        if action == "skip":
            self.store.mark_note(note_id)
            return {"ok": True, "skipped": note_id}
        if action == "file":
            deal, error = self._resolve(args.get("deal"))
            if error:
                return error
            summary = str(args.get("summary") or "").strip()
            if not summary:
                return {"ok": False, "error": "file needs a summary: two or three sentences on what was said and agreed"}
            args, error = _due(args)
            if error:
                return error
            actor = self.actor()
            kind = str(args.get("kind") or "call").lower()
            self.store.log(deal["id"], kind if kind in LOG_KINDS else "call", summary[:4000], actor=actor, ref=note_id)
            follow = {k: args.get(k) for k in ("next_step", "next_due") if args.get(k) is not None}
            if follow:
                self.store.update(deal["id"], actor=actor, **follow)
            self.store.mark_note(note_id, deal["id"])
            deal = self.store.get(deal["id"])
            self._changed(deal)
            return {"ok": True, "filed": note_id, "deal": self.view(deal, activities=3)}
        return {"ok": False, "error": "unknown action '%s'" % action, "actions": ["inbox", "read", "file", "skip"]}

    def _read_note(self, note_id: str) -> dict:
        if note_id.startswith("granola:"):
            key = self.secret(GRANOLA_KEY)
            if not key:
                return {"ok": False, "error": "Granola is not set up (GRANOLA_API_KEY)"}
            return sources.read_granola_note(key, note_id, http=self.http)
        folder = str(self.setting("notes_dir", "") or "")
        for note in sources.folder_notes(folder, days=3650):
            if note["id"] == note_id:
                return {"ok": True, "id": note_id, "source": "folder", "title": note["title"], "when": note["when"],
                        "text": sources.read_folder_note(note)}
        return {"ok": False, "error": "no note with id %s (the file may have changed; list the inbox again)" % note_id}

    # -- gtm_playbook ---------------------------------------------------------------------------
    # -- shapes and stages: how a person sets a pipeline up --------------------------------------------
    # Three shapes (close deals, fill roles, generate leads), each on one shipped playbook. A person picks one and sets
    # the stages their business uses (two to seven); a pipeline whose stages differ from the shape's gets its own copy of
    # the playbook, so two pipelines of the same shape can each have their own stages.
    def shapes(self) -> list:
        out = []
        for shape in SHAPES:
            base = self.library.load(shape["base"])
            out.append(dict(shape, record=base.terms["deal"], records=base.terms["deals"], group=base.terms["group"],
                            groups=base.terms["groups"], stages=[{"id": s["id"], "label": s["label"]} for s in base.stages],
                            won=base.won["label"], lost=base.lost["label"]))
        return out

    @staticmethod
    def _shape(ref: Any) -> Optional[dict]:
        want = str(ref or "").strip().lower()
        return next((shape for shape in SHAPES if want in (shape["id"], shape["base"], shape["title"].lower())), None)

    def _staged(self, base: Playbook, current: Optional[Playbook], stages: Any, won: Any, lost: Any) -> tuple:
        """A playbook's data with these stages (labels, or ``{id, label}``): ``(data, error)``. A stage keeps its id, odds
        and timing when it is one the pipeline already has (by id, or by the same label), so records stay where they are."""
        rows = []
        for raw in stages if isinstance(stages, list) else []:
            if isinstance(raw, dict):
                rows.append({"id": str(raw.get("id") or "").strip(), "label": " ".join(str(raw.get("label") or "").split())[:40]})
            else:
                rows.append({"id": "", "label": " ".join(str(raw or "").split())[:40]})
        rows = [row for row in rows if row["label"]]
        if not 2 <= len(rows) <= MOST_STAGES:
            return None, {"ok": False, "error": "a pipeline has 2 to %d stages before it is won or lost; got %d" % (MOST_STAGES, len(rows))}
        lowered = [row["label"].lower() for row in rows]
        if len(set(lowered)) != len(lowered):
            return None, {"ok": False, "error": "two stages have the same name"}
        known = {s["id"]: s for s in (current or base).stages}
        by_label = {s["label"].lower(): s for s in (current or base).stages}
        for s in base.stages:
            known.setdefault(s["id"], s)
            by_label.setdefault(s["label"].lower(), s)
        won_label = " ".join(str(won or "").split())[:40] or (current or base).won["label"]
        lost_label = " ".join(str(lost or "").split())[:40] or (current or base).lost["label"]
        ends = {(current or base).won["id"], (current or base).lost["id"]}
        used, clean = set(), []
        for index, row in enumerate(rows):
            old = known.get(row["id"]) or by_label.get(row["label"].lower())
            if old and old["id"] not in used:
                stage_id = old["id"]
            else:
                stage_id = re.sub(r"[^a-z0-9]+", "-", row["label"].lower()).strip("-")[:30] or "stage"
                base_id, n = stage_id, 2
                while stage_id in used or stage_id in ends or stage_id in known:
                    stage_id, n = "%s-%d" % (base_id, n), n + 1
                old = None
            used.add(stage_id)
            stage = {"id": stage_id, "label": row["label"]}
            if old:
                for key in ("stale_days", "win_probability", "advance"):
                    if old.get(key) not in (None, ""):
                        stage[key] = old[key]
            else:
                stage["win_probability"] = int(round(10 + 75 * index / max(1, len(rows) - 1)))     # an even climb, until history takes over
            clean.append(stage)
        source = current or base
        order = [s["id"] for s in source.stages] + [source.won["id"]]
        for index, stage in enumerate(clean):          # a button's own words stay only while the stage after it is the same
            following = clean[index + 1]["id"] if index + 1 < len(clean) else source.won["id"]
            if stage.get("advance"):
                was = order[order.index(stage["id"]) + 1] if stage["id"] in order[:-1] else None
                if was != following:
                    stage.pop("advance")
        data = dict((current or base).data)
        data["stages"] = clean
        data["won"] = dict((current or base).won, label=won_label)
        data["lost"] = dict((current or base).lost, label=lost_label)
        drafts = dict(data.get("drafts") or {})
        drafts["on_enter"] = {key: value for key, value in (drafts.get("on_enter") or {}).items() if key in used or key in ends}
        data["drafts"] = drafts
        return data, None

    def set_stages(self, stages: Any, won: Any = None, lost: Any = None, shape: Any = None, actor: str = "") -> dict:
        """Set up the pipeline in hand: its shape (one of three) and its stages. Refused when a record would be left in a
        stage the pipeline no longer has."""
        if self.tour():
            return {"ok": False, "error": "On a tour the stages are the sample's own. Leave the tour to set up your desk."}
        current = self.playbook()
        chosen = self._shape(shape) if shape else self._shape(current.shape)
        if chosen is None:
            return {"ok": False, "error": "the shape is one of: %s" % ", ".join(s["title"] for s in SHAPES)}
        base = self.library.load(chosen["base"])
        same_shape = chosen["id"] == current.shape
        data, error = self._staged(base, current if same_shape else None, stages, won, lost)
        if error:
            return error
        keep = {stage["id"] for stage in data["stages"]} | {data["won"]["id"], data["lost"]["id"]}
        left = [deal for deal in self.store.list(status="open") if deal["stage"] not in keep]
        if left:
            stages_left = sorted({current.label(deal["stage"]) for deal in left})
            return {"ok": False, "error": "%d open %s would be left in %s, which the pipeline would no longer have. Move them first%s. Nothing was changed."
                    % (len(left), current.terms["deals"] if len(left) > 1 else current.terms["deal"], ", ".join(stages_left),
                       "" if same_shape else ", or open a new pipeline for this other kind of work")}
        here = self.current()
        plain = [{"id": s["id"], "label": s["label"]} for s in base.stages]
        if [{"id": s["id"], "label": s["label"]} for s in data["stages"]] == plain and data["won"]["label"] == base.won["label"] \
                and data["lost"]["label"] == base.lost["label"]:
            name = base.name                            # the shape as it ships: no copy needed
        else:
            slug = re.sub(r"[^a-z0-9]+", "-", here["name"].lower()).strip("-") or "mine"
            name = ("%s-%s" % (base.name, slug))[:40].strip("-")
            if name == base.name:
                name = (base.name + "-mine")[:40]
            data.update(name=name, shape=chosen["id"], title="%s · %s" % (chosen["title"], here["name"])[:80],
                        door=chosen["title"], summary=chosen["motion"])
            try:
                self.library.save(json.dumps(data, indent=1, ensure_ascii=False))
            except PlaybookError as exc:
                return {"ok": False, "error": "Those stages could not be saved: %s. Nothing was changed." % exc}
        if here["first"] and not getattr(self, "can_configure", True):
            return {"ok": False, "ask": "Set the first Stoa pipeline to the %s playbook." % name, "playbook": name}
        out = self._use(name)
        playbook = self.library.load(name)
        out.update(ok=True, pipeline=here["name"], shape=chosen["title"], stages=[s["label"] for s in playbook.stages],
                   won=playbook.won["label"], lost=playbook.lost["label"])
        return out

    def setup_pipeline(self, verb: str, shape: Any, stages: Any = None, won: Any = None, lost: Any = None, name: Any = None) -> dict:
        """The page's way to set a pipeline up. ``first``: the desk's first pipeline, on a desk nobody has set up yet.
        ``add``: a new pipeline beside the others. ``edit``: the pipeline in hand (its shape, its stages)."""
        chosen = self._shape(shape)
        if chosen is None:
            return {"ok": False, "error": "choose a shape: %s" % ", ".join(s["title"] for s in SHAPES)}
        if stages is None:
            stages = [{"id": s["id"], "label": s["label"]} for s in self.library.load(chosen["base"]).stages]
        title = " ".join(str(name or "").split())[:60]
        if verb == "add":
            if not title:
                return {"ok": False, "error": "a new pipeline needs a name"}
            made = self.pipeline_add(title, chosen["base"])
            if not made.get("ok"):
                return made
            with self.on(made["added"]["id"]):
                out = self.set_stages(stages, won, lost, chosen["id"])
            if not out.get("ok"):
                self.pipeline_remove(made["added"]["id"])          # nothing in it yet: as if it had not been opened
                return out
            return dict(out, added=made["added"])
        if verb == "first" and title:
            renamed = self.pipeline_rename(MAIN, title)
            if not renamed.get("ok"):
                return renamed
        return self.set_stages(stages, won, lost, chosen["id"])

    def add_fields(self, labels: list) -> tuple:
        """Add fields to the playbook in use (the desk's own copy of it), for columns a list brought that had no place.
        Returns ``({label: key}, None)``, or ``(None, error)``. A label that is already a field maps to it."""
        if self.tour():
            return None, {"ok": False, "error": "On a tour the playbook stays as it is. Nothing was changed."}
        playbook = self.playbook()
        fields = [dict(field) for field in playbook.fields]
        taken = {field["key"] for field in fields} | {"name", "company", "group", "grp", "email", "phone", "linkedin", "value", "owner",
                                                      "stage", "source", "note", "notes", "record", "next_step", "next_due", "close_on"}
        mapping, added = {}, []
        for label in labels:
            clean = " ".join(str(label or "").split())[:60]
            if not clean:
                continue
            same = next((field for field in fields if field["label"].lower() == clean.lower() or field["key"] == clean.lower()), None)
            if same:
                mapping[label] = same["key"]
                continue
            key = re.sub(r"[^a-z0-9]+", "_", clean.lower()).strip("_")[:40] or "field"
            base, n = key, 2
            while key in taken:
                key, n = "%s_%d" % (base, n), n + 1
            taken.add(key)
            fields.append({"key": key, "label": clean})
            added.append(clean)
            mapping[label] = key
        if not added:
            return mapping, None
        if len(fields) > 12:
            return None, {"ok": False, "error": "A playbook holds at most 12 fields; %s has %d. Keep some of these columns in notes "
                                                "instead. Nothing was changed." % (playbook.title, len(playbook.fields))}
        data = dict(playbook.data, fields=fields)
        try:
            self.library.save(json.dumps(data, indent=1, ensure_ascii=False))
        except PlaybookError as exc:
            return None, {"ok": False, "error": "Could not add those fields: %s. Nothing was changed." % exc}
        return mapping, None

    def playbook_tool(self, args: dict) -> dict:
        action = str(args.get("action") or "show").lower()
        library = self.library
        try:
            if action == "pipelines":
                listed = self.pipelines()
                return {"ok": True, "count": len(listed), "pipelines": [{"name": entry["name"], "playbook": entry["playbook"], "runs": entry["title"],
                                                                        "first": entry["first"]} for entry in listed]}
            if action == "shapes":
                return {"ok": True, "shapes": self.shapes(),
                        "note": "Three shapes. Ask which fits, then the stages their business uses (2 to %d), and set them with action='stages'." % MOST_STAGES}
            if action == "stages":
                return self.set_stages(args.get("stages"), args.get("won"), args.get("lost"), args.get("shape"))
            if action == "add_pipeline":
                if self._shape(args.get("shape")) is not None:
                    return self.setup_pipeline("add", args.get("shape"), args.get("stages"), args.get("won"), args.get("lost"),
                                               args.get("pipeline_name") or args.get("to"))
                return self.pipeline_add(args.get("pipeline_name") or args.get("to"), args.get("playbook") or args.get("name"))
            if action == "rename_pipeline":
                return self.pipeline_rename(self._pipe().get("id") or MAIN, args.get("to") or args.get("pipeline_name"))
            if action == "list":
                return {"ok": True, "active": self.playbook().name, "playbooks": library.catalog()}
            if action == "show":
                name = str(args.get("name") or self.playbook().name)
                playbook = library.load(name)
                return {"ok": True, "active": self.playbook().name, "playbook": playbook.to_dict(), "yaml": library.source(name)}
            if action == "template":
                return {"ok": True, "yaml": library.source(DEFAULT),
                        "rules": "name: lowercase-with-dashes. 2 to 9 open stages in order, each with id, label, optional "
                                 "stale_days (a number) and advance (the button label, in quotes). won and lost are the two "
                                 "closing stages; enrich is person, company or none (none hides the Enrich button); "
                                 "lost.reasons is the list offered on the card. drafts.on_enter.<stage id> is what the "
                                 "agent writes when a record enters that stage; placeholders: {name} {company} {group} "
                                 "{client} {stage} {reason} {owner} {value}. fields are extra facts shown on cards; group_fields are "
                                 "the facts kept on a group itself (a search's fee terms). delivery is up to six stages a "
                                 "won record moves through afterwards; a stage's win_probability (0 to 100) feeds the "
                                 "forecast. map_group says where a company added from a market map goes: 'map' when a "
                                 "group is something the team runs and one map feeds it (a search, a campaign, a lead "
                                 "list for a client), 'company' when each company is a group of its own (an account)."}
            if action == "save":
                playbook = library.save(str(args.get("yaml") or ""))
                out = {"ok": True, "saved": playbook.name, "stages": [s["label"] for s in playbook.stages]}
                if args.get("activate"):
                    out.update(self._use(playbook.name))
                return out
            if action == "use":
                return self._use(str(args.get("name") or ""))
        except PlaybookError as exc:
            return {"ok": False, "error": str(exc)}
        except (TypeError, AttributeError, KeyError) as exc:    # a shape the checks did not foresee
            return {"ok": False, "error": "that playbook does not hold together: %s" % exc}
        return {"ok": False, "error": "unknown action '%s'" % action,
                "actions": ["list", "show", "use", "save", "template", "pipelines", "add_pipeline", "rename_pipeline"]}

    def _use(self, name: str) -> dict:
        playbook = self.library.load(name)
        here = self._pipe().get("id") or MAIN
        if here == MAIN:
            self._set_config("playbook", playbook.name)
        else:                                       # another pipeline's playbook is kept with the pipeline
            with self._pipes_lock:
                data = dict(self._pipes())
                data["more"] = [dict(entry, playbook=playbook.name) if isinstance(entry, dict) and entry.get("id") == here else entry
                                for entry in data.get("more") or []]
                self._pipes_write(data)
        known = set(playbook.stage_ids()) | {playbook.won["id"], playbook.lost["id"]}
        strays = [d for d in self.store.list(status="all") if d["stage"] not in known]
        out = {"ok": True, "active": playbook.name, "summary": playbook.describe()}
        if strays:
            out["warning"] = ("%d existing records sit in stages this playbook does not have. They are kept and shown "
                              "under 'Other stages'; move them with gtm_deal(action='move')." % len(strays))
        return out

    # -- gtm_export -----------------------------------------------------------------------------
    def export_scope(self, ref: Any) -> tuple:
        """``(group keys, what to call it, error)`` for an export of part of the desk: one group by name, or every
        group a client has. ``(None, "", None)`` for the whole desk."""
        if not str(ref or "").strip():
            return None, "", None
        names = self._group_names()
        name, error = self._resolve_group(ref, names)
        if not error:
            return {st.group_key(name)}, name, None
        if "several" in error["error"]:
            return None, "", error
        key = st.org_key(ref)
        mine = [group for group in self.store.grps() if key and group["client"] and st.org_key(group["client"]) == key]
        if mine:                                    # not a group's name but a client's: everything that is for them
            return {group["key"] for group in mine}, mine[0]["client"], None
        return None, "", error

    def export_rows(self, groups: Optional[set] = None, share: bool = False) -> tuple:
        """``(header, rows)`` for a CSV of the desk or of some groups. ``share`` is the copy to hand someone outside
        the team: the playbook's own words for the columns and stages, and nothing that is the team's own business
        (ids, value, owner, next steps, dates, where a record came from, why one was lost)."""
        playbook = self.playbook()
        if not share:
            header, rows = self.store.export_rows([field["key"] for field in playbook.fields], groups)
            return ["group" if name == "grp" else name for name in header], rows
        full = self.table(5000, groups)             # ID, record, company, group, Stage, value, owner, Next step, Due, Email, LinkedIn, facts…, Updated
        keep = [1, 2, 3, 4, 9, 10] + list(range(11, len(full["columns"]) - 1))
        return [full["columns"][i] for i in keep], [[row[i] for i in keep] for row in full["rows"]]

    def export(self, args: dict) -> dict:
        action = str(args.get("action") or "export").lower()
        playbook = self.playbook()
        scope, label, error = self.export_scope(args.get("group"))
        if error and action in ("export", "table"):
            return dict(error, error=error["error"] + ". Nothing was written.")
        if action == "export":
            try:
                stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                share = bool(args.get("share"))
                path = self.export_path(args.get("path") or args.get("name"),
                                        "%s%s-%s.csv" % (st.slugify(label)[:60] if label else "pipeline", "-to-share" if share else "", stamp))
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            header, rows = self.export_rows(scope, share)
            partial = path.with_name(".%s.%d.part" % (path.name, os.getpid()))     # written beside it, then moved into place
            try:
                with partial.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(header)
                    writer.writerows(rows)
                os.replace(partial, path)
            finally:
                if partial.exists():
                    partial.unlink()
            out = {"ok": True, "path": str(path), "rows": len(rows), "columns": header,
                   "note": "A plain CSV in the plugin's exports folder. It opens in Google Sheets or Excel as is."
                           + (" This is the copy to share: value, owner, next steps, dates, ids and sources are left out."
                              if share else " It has the team's own columns (value, owner, next steps, source): for a copy to "
                                            "hand someone outside, export again with share=true.")}
            if label:
                out["only"] = label
            return out
        if action == "import":
            path, refused = self.csv_path(args.get("path"))
            if refused:
                return refused
            return self.import_csv(path, dry_run=bool(args.get("dry_run")))
        if action == "table":
            out = self.table(_int(args.get("limit") or 1000, 1000, 1, 5000), scope)
            if label:
                out["only"] = label
            return out
        return {"ok": False, "error": "unknown action '%s'" % action, "actions": ["export", "import", "table"]}

    def table(self, limit: int = 1000, groups: Optional[set] = None) -> dict:
        """The pipeline as a header row and rows, in the words of the playbook: what goes into a
        spreadsheet tab. The same table read back through gtm_intake changes nothing, and an edit
        made in the spreadsheet comes back as exactly that edit (the ID column says which record)."""
        playbook = self.playbook()
        terms = playbook.terms
        columns = (["ID", terms["deal"].capitalize(), terms["company"], terms["group"].capitalize(), "Stage", terms["value"],
                    terms["owner"].capitalize(), "Next step", "Due", "Email", "LinkedIn"]
                   + [field["label"] for field in playbook.fields] + ["Updated"])
        deals = sorted((d for d in self.store.list(status="all") if groups is None or st.group_key(d["grp"]) in groups),
                       key=lambda d: (playbook.position(d["stage"]) if d["status"] == "open" else 99, d["grp"].lower(), d["name"].lower()))
        rows = [[d["id"], d["name"], d["company"], d["grp"], playbook.label(d["stage"]), d["value"] or "", d["owner"], d["next_step"],
                 d["next_due"], d["email"], d["linkedin"]] + [str(d["fields"].get(field["key"], "")) for field in playbook.fields]
                + [d["updated_at"][:10]] for d in deals[:limit]]
        out = {"ok": True, "columns": columns, "rows": rows, "count": len(rows),
               "note": "Write columns as the first row and rows beneath it. Keep the ID column: it is how an edit made in "
                       "the spreadsheet finds its record when the tab is read back with gtm_intake."}
        if len(deals) > limit:
            out["truncated"] = len(deals) - limit
        return out

    def import_csv(self, path: Path, dry_run: bool = False) -> dict:
        """``hermes stoa import``: a CSV typed in at a terminal, added straight away (no review). Headers are read the
        way a dropped list's are ("Client Name", "Contact email", "First name"), and what has no place is kept in notes."""
        if not path.is_file():
            return {"ok": False, "error": "no file at %s" % path}
        playbook = self.playbook()
        rows, problems = self.intake._read_csv(path)
        added, skipped = 0, 0
        for number, raw in enumerate(rows, start=2):
            try:
                row, warnings, _ = self.intake._clean(raw, playbook)
                problems.extend("row %d: %s" % (number, warning) for warning in warnings)
                name = row.get("name", "")
                if not name:
                    problems.append("row %d: no name" % number)
                    continue
                group, company = row.get("grp", "") or "", row.get("company", "") or ""
                if playbook.group_is_company():      # an account or a client is a company: either fills the other
                    group, company = group or company, company or group
                if any(d["name"].lower() == name.lower() and d["grp"].lower() == group.lower()
                       for d in self.store.list(status="all", query=name)):
                    skipped += 1
                    continue
                if dry_run:
                    added += 1
                    continue
                stage = row.get("stage") or playbook.first_stage()
                status = playbook.status_of(stage)
                with self.store.operation():
                    deal = self.store.add(name, stage if status == "open" else playbook.first_stage(), actor="import",
                                          company=company, grp=group, email=row.get("email", ""), phone=row.get("phone", ""),
                                          linkedin=row.get("linkedin", ""), value=row.get("value"), owner=row.get("owner", ""),
                                          next_step=row.get("next_step", ""), next_due=row.get("next_due", ""), close_on=row.get("close_on", ""),
                                          source=row.get("source", "") or "import", notes=self.intake._kept_notes(row),
                                          fields=row.get("fields") or None)
                    if status != "open":
                        self._move(deal, stage, playbook, "import", row.get("lost_reason", ""))
                    if row.get("note"):
                        self.store.log(deal["id"], "note", str(row["note"])[:4000], actor="import")
                added += 1
            except Exception as exc:        # one bad row is reported, the rest still come in
                problems.append("row %d: %s" % (number, exc))
        out = {"ok": True, "added": added, "skipped_existing": skipped, "dry_run": dry_run}
        if problems:
            out["problems"] = problems[:20]
        return out

    # -- slash command and prompt section -------------------------------------------------------
    def slash(self, raw: str, session: Optional[dict] = None) -> str:
        """``/gtm`` on every surface. Typed in Slack, a record or a map is posted as the real thing,
        buttons and all, with no model in between; everywhere else (and on failure) it is text."""
        raw = (raw or "").strip()
        playbook = self.playbook()
        word = raw.lower()
        in_slack = bool(session) and session.get("platform") == "slack" and self.slack is not None and self.slack.available
        if in_slack:        # a command opens no agent session, so there is none for a later click to hand back to
            session = dict(session, session_key="")
        if word in ("", "board"):
            if not self.store.count():
                return ("The desk is empty. Tell me about a %s to add it, or run `hermes stoa demo` for sample data."
                        % playbook.terms["deal"])
            return cards.board_text(playbook, self.store.list(status="open"))
        if word == "help":
            return ("/gtm — the pipeline board\n/gtm today — what needs you, most pressing first\n/gtm cash — won, invoiced, collected, overdue, and the forecast\n/gtm brief — the morning brief\n/gtm stale — records gone quiet\n"
                    "/gtm due — next steps due\n/gtm map [market] — a market map\n/gtm <name> — one record's card\n"
                    "/gtm playbooks — available playbooks\nIn Slack, type !gtm: Slack keeps the slash for its own commands.")
        if word == "map" or word.startswith("map "):
            if in_slack and self.markets.post({"market": raw[3:].strip()}, session).get("posted"):
                return ""       # the map itself is the reply
            shown = self.markets.show({"market": raw[3:].strip()})
            if shown.get("ok"):
                return shown["map"]
            names = shown.get("markets")
            return shown["error"] + ("\n" + "\n".join("  " + n for n in names) if names else "")
        if word == "brief":
            return self.brief_text()
        if word == "today":
            return self.today_text()
        if word == "cash":
            return self.cash_text()
        if word in ("stale", "due"):
            data = self.pipeline({"view": word})
            if not data["deals"]:
                return "Nothing %s." % ("has gone quiet" if word == "stale" else "is due")
            return "\n".join(["%s (%d)" % ("Gone quiet" if word == "stale" else "Due", data["count"])]
                             + ["  " + cards._row(self.store.get(d["id"]), playbook) for d in data["deals"][:20]])
        if word == "playbooks":
            active = playbook.name
            return "\n".join("%s %s — %s" % ("●" if p["name"] == active else "○", p["name"], p.get("summary") or p.get("error", ""))
                             for p in self.library.catalog())
        deal, error = self._resolve(raw)
        if error:
            matches = error.get("matches")
            if matches:
                return error["error"] + ":\n" + "\n".join("  %s — %s" % (m["id"], m["name"]) for m in matches)
            return error["error"]
        if in_slack and self.card({"deal": deal["id"]}, session).get("posted"):
            return ""           # the card itself is the reply
        return self.text_card(deal)

    def brief_text(self) -> str:
        data = self.brief()
        playbook = self.playbook()
        lines = ["%s · %s" % (playbook.title, data["date"]),
                 "%d open %s%s" % (data["open"], playbook.terms["deals"],
                                   " · " + cards.money(data["open_value"], playbook.currency) if data["open_value"] else "")]
        if data["due"]:
            lines += ["", "Due (%d)" % len(data["due"])] + [
                "  %s — %s (%s)" % (d["name"], d.get("next_step", ""), d.get("next_due", "")) for d in data["due"]]
        if data["stale"]:
            lines += ["", "Gone quiet (%d)" % len(data["stale"])] + [
                "  %s — %s, %dd" % (d["name"], d["stage_label"], d["quiet_days"]) for d in data["stale"]]
        week = data["this_week"]
        lines += ["", "This week: %d new, %d stage moves, %d won, %d lost"
                  % (week["new"], week["stage_moves"], len(week["won"]), len(week["lost"]))]
        if data["notes_waiting"]:
            lines.append("%d meeting notes waiting to be filed" % data["notes_waiting"])
        if not data["due"] and not data["stale"]:
            lines.append("Nothing is due and nothing has gone quiet.")
        return "\n".join(lines)

    def prompt_section(self) -> str:
        try:
            playbook = self.playbook()
            several = [] if self.tour() else self.pipelines()
        except Exception:
            return ""
        if len(several) > 1:
            lines = []
            for entry in several:
                book = self.library.load_or_default(entry["playbook"])
                lines.append("%s (%s, grouped by %s)" % (entry["name"], book.terms["deals"], book.terms["group"]))
            text = ("## Stoa GTM desk\n"
                    "You keep this team's pipelines with the gtm_* tools; the desk is the source of truth, so read it before "
                    "answering and write to it when something changes. It has %d pipelines: %s. Every gtm tool takes "
                    "`pipeline` (a name); left out, it means the first, and each answer names the pipeline it used: say "
                    "it. Ask when it is unclear which one is meant. Look a name up with gtm_deal(action=\"get\") before "
                    "saying it cannot be found. For how to run the desk, load the skill stoa:desk; to map a market, "
                    "stoa:market-map. A change to the desk stays on the desk: do not also write it to another system "
                    "unless asked.")
            full = text % (len(several), "; ".join(lines))
            if len(full) > 900:                     # many pipelines, or long names: their names alone, cut to fit
                room = max(8, (900 - len(text % (len(several), ""))) // len(several) - 2)
                full = text % (len(several), "; ".join(entry["name"][:room] for entry in several))
            return full
        return ("## Stoa GTM desk\n"
                "You keep this team's pipeline with the gtm_* tools; the desk is the source of truth, so read it "
                "before answering pipeline questions and write to it when something changes. When someone names a "
                "person, company or record, look it up with gtm_deal(action=\"get\") before saying it cannot be "
                "found, whatever else you can search; to show one, use gtm_card. %s "
                "Say \"%s\" and \"%s\", not \"deal\" or \"group\", unless those are the words. "
                "For how to run the desk, load the skill stoa:desk. To map a market or find new leads, load stoa:market-map.%s"
                % (playbook.describe(), playbook.terms["deal"], playbook.terms["group"],
                   " Sample data is on show (a tour): everything on the desk is made up. Say so when you report from it, "
                   "and copy none of it into another system."
                   if self.tour() else
                   " A change to the desk stays on the desk: do not also write it to another system (a sheet, another CRM) unless asked."))


def _version() -> str:
    try:
        found = re.search(r"^version:\s*[\"']?([0-9][^\"'\s]*)", (Path(__file__).parent / "plugin.yaml").read_text(encoding="utf-8"), re.M)
    except OSError:
        found = None
    return found.group(1) if found else ""


def _profile_line(kind: str, profile: dict) -> str:
    if kind == "company":
        bits = [profile.get("name"), profile.get("industry"), profile.get("size") and "%s people" % profile["size"],
                profile.get("location"), profile.get("founded") and "founded %s" % profile["founded"]]
    else:
        role = " at ".join(b for b in (profile.get("title"), profile.get("company")) if b)
        bits = [profile.get("name"), role, profile.get("location")]
        if profile.get("experience"):
            previous = [j["company"] for j in profile["experience"][1:4] if j.get("company")]
            if previous:
                bits.append("previously " + ", ".join(previous))
    return " · ".join(str(b) for b in bits if b) or "match found"


def _suggest(note: dict, deals: list) -> list:
    """Which open records a note is probably about: attendee email first, then a name in the title."""
    emails = {a.get("email", "").lower() for a in note.get("attendees") or [] if a.get("email")}
    names = {a.get("name", "").lower() for a in note.get("attendees") or [] if a.get("name")}
    title = (note.get("title") or "").lower()
    out = []
    for deal in deals:
        why = ""
        if deal["email"] and deal["email"].lower() in emails:
            why = "attendee email"
        elif deal["name"].lower() in names:
            why = "attendee name"
        elif len(deal["name"]) > 3 and deal["name"].lower() in title:
            why = "name in title"
        elif deal["company"] and len(deal["company"]) > 3 and deal["company"].lower() in title:
            why = "company in title"
        if why:
            out.append({"id": deal["id"], "name": deal["name"], "why": why})
    return out[:4]
