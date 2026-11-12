"""Intake: take in a list of people or companies from anywhere, show what it would change, and
write only what was approved.

The agent reads whatever it was sent (a PDF, a spreadsheet, a pasted list, a photo of a sign-up
sheet) and hands the rows here; a CSV on disk can be read directly. The desk then works out, with
no model involved, which rows are new, which would change a record already on the desk, which are
already there, and which it cannot place. Nothing is written until someone presses Apply or says so.
"""
from __future__ import annotations

import csv
import hashlib
import io
import uuid
from pathlib import Path
from typing import Any, Optional

from . import cards
from . import store as st

MAX_ROWS = 500
APPLICABLE = ("new", "update")
ALIASES = {"group": "grp", "account": "grp", "deal": "name", "contact": "name", "person": "name", "full_name": "name",
           "candidate": "name", "amount": "value", "deal_size": "value", "linkedin_url": "linkedin", "next_action": "next_step",
           "due": "next_due", "due_date": "next_due", "next_date": "next_due", "organization": "company",
           "organisation": "company", "employer": "company", "notes": "note", "comment": "note", "comments": "note",
           "e-mail": "email", "email_address": "email", "record_id": "record", "id": "record",
           "close": "close_on", "close_date": "close_on", "expected_close": "close_on", "closing": "close_on"}
# "record" is the id of the record a row is about, to settle a match; "deal" in a spreadsheet header means the name.
CORE = ("name", "company", "grp", "email", "linkedin", "stage", "value", "owner", "next_step", "next_due", "source", "note", "record",
        "close_on")
READ_ONLY = ("updated", "status", "last_touch", "added", "created")     # columns the desk writes out and never reads back


def _text(value: Any, limit: int = 500) -> str:
    if value is None or isinstance(value, (bool, list, dict)):
        return ""
    return " ".join(str(value).split())[:limit]


def _evidence(value: Any) -> list:
    """What a proposal rests on, as up to four short items: ``{kind, at, who, text, ref}``."""
    if isinstance(value, (str, dict)):
        value = [value]
    out = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        entry = {"kind": _text(item.get("kind") or item.get("type"), 20).lower(),
                 "at": _text(item.get("at") or item.get("date") or item.get("when"), 40),
                 "who": _text(item.get("who") or item.get("from"), 80),
                 "text": _text(item.get("text") or item.get("quote") or item.get("summary"), 400),
                 "ref": _text(item.get("ref") or item.get("id"), 200)}
        if entry["text"] or entry["ref"]:
            out.append(entry)
    return out[:4]


def _person(value: Any) -> dict:
    """Someone a row would add to a record: ``{name, email, title, role}`` ({} when there is no name)."""
    if not isinstance(value, dict) or not _text(value.get("name"), 120):
        return {}
    return {key: _text(value.get(key), 200) for key in ("name", "email", "title", "role", "linkedin", "phone")}


def _person_line(person: dict) -> str:
    return ", ".join(bit for bit in (person.get("name"), person.get("role") or person.get("title"), person.get("email")) if bit)


def digest(entry: dict) -> str:
    """What a row would do, as a short hash: the same proposal made twice hashes the same. A change
    hashes with what it would replace, so once the record moves on, the proposal counts as new."""
    if entry.get("kind") == "new" or not entry.get("deal"):
        row = entry.get("row") or {}
        basis = ["new", (row.get("email") or "").lower() or _link(row.get("linkedin", "")) or str(entry.get("name") or "").lower(),
                 str(entry.get("group") or "").lower()]
    else:
        changes = [c for c in entry.get("changes") or [] if c.get("key") != "note"] or list(entry.get("changes") or [])
        basis = ["update", entry["deal"]] + sorted("%s|%s|%s" % (c.get("key"), str(c.get("from") or "").lower(), str(c.get("to") or "").lower())
                                                 for c in changes)
    return hashlib.sha1("\n".join(basis).encode("utf-8")).hexdigest()[:20]


def _link(url: str) -> str:
    url = (url or "").strip().lower()
    for prefix in ("https://", "http://", "www."):
        if url.startswith(prefix):
            url = url[len(prefix):]
    return url.rstrip("/")


class Intake:
    def __init__(self, desk):
        self.desk = desk

    @property
    def store(self) -> st.Store:
        return self.desk.store

    # -- the tool -------------------------------------------------------------------------------
    def run(self, args: dict, session: Optional[dict] = None) -> dict:
        action = str(args.get("action") or "propose").lower()
        if action == "propose":
            return self.propose(args, session or {})
        if action == "list":
            return {"ok": True, "waiting": self.waiting()}
        intake = self._find(args.get("id"))
        if intake is None:
            return {"ok": False, "error": "no such list; gtm_intake(action='list') shows the ones waiting",
                    "waiting": self.waiting()}
        if action == "show":
            return dict(self.view(intake["id"]), ok=True, review=self.text(intake))
        if action == "apply":
            return self.apply({"id": intake["id"], "rows": args.get("rows"), "skip": args.get("skip")}, self.desk.actor())
        if action == "discard":
            return self.discard(intake["id"])
        return {"ok": False, "error": "unknown action '%s'" % action, "actions": ["propose", "show", "apply", "discard", "list"]}

    def _find(self, ref: Any) -> Optional[dict]:
        ref = str(ref or "").strip()
        if ref:
            return self.store.intake_get(ref)
        waiting = self.store.intake_list("open", limit=2)
        return waiting[0] if len(waiting) == 1 else None      # "apply it" is clear when only one is waiting

    # -- propose --------------------------------------------------------------------------------
    def propose(self, args: dict, session: dict) -> dict:
        rows, source = args.get("rows"), _text(args.get("source"), 80)
        problems: list = []
        if args.get("path"):
            path, refused = self.desk.csv_path(args["path"])     # a .csv or .tsv inside the desk's import folders
            if refused:
                return refused
            rows, problems = self._read_csv(path)
            source = source or path.name
        columns = args.get("columns")
        if isinstance(columns, list) and columns and isinstance(rows, list):       # a table: a header row, then rows of cells
            names = [str(column) for column in columns]
            rows = [dict(zip(names, row)) for row in rows if isinstance(row, (list, tuple)) and any(_text(cell) for cell in row)]
        if not isinstance(rows, list) or not rows:
            return {"ok": False, "error": "pass rows (a list of objects with at least a name or a company) or the path of a CSV"}
        if len(rows) > MAX_ROWS:
            return {"ok": False, "error": "at most %d rows at a time; send the rest as a second list" % MAX_ROWS}
        playbook = self.desk.playbook()
        # A proposal is a change Hermes suggests from what it found; a list is rows somebody sent. A
        # declined proposal is remembered, and each one is approved or declined by itself.
        proposal = (str(args.get("kind") or "").lower() == "proposal" or session.get("platform") == "cron"
                    or any(isinstance(row, dict) and row.get("evidence") for row in rows))
        plan, ignored = self.plan(rows, playbook, origin="proposal" if proposal else "list")
        intake_id = "in-" + uuid.uuid4().hex[:8]
        self.store.intake_put(intake_id, source or ("a proposal" if proposal else "a list"), plan, "proposal" if proposal else "list")
        self.desk.announce()
        intake = self.store.intake_get(intake_id)
        out = dict(self.view(intake_id), ok=True)
        declined = [row for row in plan if row["kind"] == "declined"]
        if declined:
            out["declined_before"] = len(declined)
        if ignored:
            out["ignored_columns"] = sorted(ignored)[:20]
        if problems:
            out["problems"] = problems[:20]
        if not any(row["kind"] in APPLICABLE for row in plan):
            out["note"] = ("Nothing here is new: the person declined %s before, and the desk does not ask twice. Do not propose "
                           "it again unless something has changed." % ("this" if len(declined) == 1 else "these")) if declined \
                else "Nothing here would change the desk. Say so in one line."
            self.store.intake_finish(intake_id, "discarded", {"reason": "nothing to apply"})
            return out
        if session.get("platform") == "cron":       # nobody is there to show a review to: it waits on the Today tab
            out.pop("rows", None)
            out["note"] = ("Held for approval. The person approves or declines each one on the Today tab; nothing has been "
                           "written and you cannot apply it. Mention in one line that proposals are waiting.")
            return out
        waiting = ("The list is waiting for approval; nothing has been written. Do not apply it yourself unless the "
                   "person tells you to. To apply some rows only, pass rows=[...] or skip=[...] by row number.")
        slack = self.desk.slack
        if self.desk.desktop_open(session):
            out["directive"] = "::stoa-intake{id=\"%s\"}" % intake_id
            out["note"] = ("Show the review by writing that directive as a paragraph by itself, exactly as given. The app "
                           "draws it with a tick box per row and an Apply button. Add one line on what you found. " + waiting)
            del out["rows"]
        elif session.get("platform") == "slack" and slack is not None and slack.available and session.get("chat_id"):
            posted = slack.post_blocks(session["chat_id"], self.blocks(intake), self.headline(intake), session.get("thread_id") or "",
                                       asked_by=session.get("user_id") or "", key="intake:" + intake_id)
            if posted.get("ok"):
                out["posted"] = True
                out["note"] = "The review is posted with Apply and Discard buttons. One line is enough. " + waiting
                del out["rows"]
            else:
                out["review"] = self.text(intake)
                out["note"] = "Show this review as it is, then ask whether to apply it. " + waiting
        else:
            out["review"] = self.text(intake)
            out["note"] = "Show this review as it is, then ask whether to apply it. " + waiting
        return out

    def propose_file(self, name: str, text: str) -> dict:
        """A CSV handed straight to the desk (dropped on the page): the same review, with no model in between."""
        name = _text(name, 80) or "a list"
        first = text.lstrip("\ufeff").split("\n", 1)[0]
        rows, problems = self._parse_csv(text.lstrip("\ufeff"), tab=name.lower().endswith(".tsv") or ("\t" in first and "," not in first))
        if not rows:
            return {"ok": False, "error": "no rows could be read from %s; it needs a header row and at least one more" % name}
        out = self.propose({"rows": rows, "source": name}, {})
        if out.get("ok") and problems:
            out["problems"] = problems[:20]
        return out

    def _read_csv(self, path: Path) -> tuple:
        data = path.read_bytes()
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:      # a CSV saved by Excel on Windows
            text = data.decode("cp1252", errors="replace")
        return self._parse_csv(text, tab=path.suffix.lower() == ".tsv")

    @staticmethod
    def _parse_csv(text: str, tab: bool = False) -> tuple:
        dialect = "excel-tab" if tab else "excel"
        rows, problems = [], []
        for number, raw in enumerate(csv.DictReader(io.StringIO(text, newline=""), dialect=dialect), start=2):
            if None in raw:
                problems.append("line %d: more cells than columns; the extra ones were ignored" % number)
                raw.pop(None, None)
            if any((value or "").strip() for value in raw.values()):
                rows.append(raw)
        return rows, problems

    # -- working out what each row would do -----------------------------------------------------
    def _clean(self, raw: Any, playbook) -> tuple:
        """One row as the desk's own fields: ``(row, warnings, ignored column names)``."""
        aliases = dict(ALIASES)
        for term, key in (("group", "grp"), ("deal", "name"), ("value", "value"), ("owner", "owner"), ("company", "company")):
            aliases[playbook.terms[term].lower().replace(" ", "_")] = key
        field_keys = {field["key"] for field in playbook.fields}
        by_label = {field["label"].lower(): field["key"] for field in playbook.fields}
        row: dict = {"fields": {}}
        warnings, ignored = [], []
        raw = raw if isinstance(raw, dict) else {}
        row["evidence"] = _evidence(raw.get("evidence"))
        row["case"] = _text(raw.get("why") or raw.get("because"), 300)
        row["person"] = _person(raw.get("person"))
        row["lost_reason"] = _text(raw.get("lost_reason"), 60)
        bill, came = raw.get("invoice"), raw.get("payment")
        # money a proposal may carry: an invoice to put on the record, or a payment that came in on one
        row["invoice"] = {"amount": st.to_cents(bill.get("amount")) / 100, "number": _text(bill.get("number"), 40),
                          "due": _text(bill.get("due") or bill.get("due_on"), 40), "note": _text(bill.get("note"), 300)} \
            if isinstance(bill, dict) and st.to_cents(bill.get("amount")) > 0 else {}
        row["payment"] = {"invoice": _text(came.get("invoice") or came.get("number"), 40),
                          "amount": st.to_cents(came.get("amount")) / 100 if came.get("amount") not in (None, "") else None,
                          "on": _text(came.get("on") or came.get("date"), 40)} if isinstance(came, dict) else {}
        for column, value in raw.items():
            if column is None or column in ("fields", "evidence", "why", "because", "person", "lost_reason", "invoice", "payment"):
                continue
            label = str(column).strip().lower()
            key = label.replace(" ", "_")
            key = aliases.get(key, key)
            text = value if key == "value" and isinstance(value, (int, float)) and not isinstance(value, bool) else _text(value)
            if key in READ_ONLY:
                continue
            if key in CORE:
                row[key] = text
            elif key in field_keys:
                row["fields"][key] = text
            elif label in by_label:
                row["fields"][by_label[label]] = text
            elif text:
                ignored.append(str(column).strip())
        if isinstance(raw.get("fields"), dict):
            for key, value in raw["fields"].items():
                if key in field_keys:
                    row["fields"][key] = _text(value)
                elif _text(value):
                    ignored.append(str(key))
        row["fields"] = {key: value for key, value in row["fields"].items() if value}
        row["name"] = _text(row.get("name")) or _text(row.get("company"))
        due = st.parse_due(row.get("next_due", ""))
        if due is None:
            warnings.append("due date '%s' was not understood and was left out" % _text(row.get("next_due"), 30))
            due = ""
        row["next_due"] = due
        close = st.parse_due(row.get("close_on", ""))
        if close is None:
            warnings.append("close date '%s' was not understood and was left out" % _text(row.get("close_on"), 30))
            close = ""
        row["close_on"] = close
        stage = playbook.resolve_stage(_text(row.get("stage")))
        if _text(row.get("stage")) and not stage:
            warnings.append("stage '%s' is not in this playbook" % _text(row.get("stage"), 30))
        row["stage"] = stage or ""
        row["value"] = st._number(row.get("value"))
        return row, warnings, ignored

    def _match(self, row: dict, deals: list) -> tuple:
        """``(record, why, candidates)``: the record this row is about, or why there is none or several."""
        if row.get("record"):
            forced = self.store.get(str(row["record"]))
            if forced:
                return forced, "", []
        email, link = (row.get("email") or "").lower(), _link(row.get("linkedin", ""))
        name, company, grp = row["name"].lower(), (row.get("company") or "").lower(), (row.get("grp") or "").lower()

        def same_name(deal):
            return deal["name"].lower() == name and (not company or not deal["company"] or deal["company"].lower() == company)

        if email:
            pool = [d for d in deals if d["email"].lower() == email]
            pool = pool or [d for d in deals if same_name(d) and not d["email"]]      # the same person, email not yet on file
        elif link:
            pool = [d for d in deals if d["linkedin"] and _link(d["linkedin"]) == link]
            pool = pool or [d for d in deals if same_name(d) and not d["linkedin"]]
        else:
            pool = [d for d in deals if same_name(d)]
        if not pool:
            return None, "", []
        if grp:
            here = [d for d in pool if d["grp"].lower() == grp]
            if not here:
                blank = [d for d in pool if not d["grp"]]
                if len(blank) == 1:
                    return blank[0], "", []
                return None, "also on the desk under %s" % (pool[0]["grp"] or "no group"), []
            pool = here
        if len(pool) > 1:
            still_open = [d for d in pool if d["status"] == "open"]
            if len(still_open) == 1:
                pool = still_open
        if len(pool) == 1:
            return pool[0], "", []
        return None, "matches %d records" % len(pool), pool[:5]

    def _changes(self, record: dict, row: dict, playbook) -> list:
        """What applying this row would change on the record: ``[{key, field, from, to}]``."""
        terms = playbook.terms
        named = (("name", terms["deal"].capitalize()),) if row.get("record") else ()      # found by id: the name may be the edit
        labels = named + (("company", terms["company"]), ("grp", terms["group"].capitalize()), ("email", "Email"), ("linkedin", "LinkedIn"),
                  ("owner", terms["owner"].capitalize()), ("next_step", "Next step"), ("next_due", "Due"), ("close_on", "Expected close"))
        changes = []
        for key, label in labels:
            new, old = _text(row.get(key)), str(record.get(key) or "")
            differs = _link(new) != _link(old) if key == "linkedin" else new.lower() != old.strip().lower()
            if new and differs:
                changes.append({"key": key, "field": label, "from": old, "to": new})
        if row.get("value") and abs(row["value"] - (record.get("value") or 0)) > 0.005:
            changes.append({"key": "value", "field": terms["value"], "from": cards.money(record.get("value") or 0, playbook.currency),
                            "to": cards.money(row["value"], playbook.currency)})
        field_labels = {field["key"]: field["label"] for field in playbook.fields}
        for key, new in (row.get("fields") or {}).items():
            old = str((record.get("fields") or {}).get(key) or "")
            if new and new.lower() != old.strip().lower():
                changes.append({"key": "fields." + key, "field": field_labels.get(key, key), "from": old, "to": new})
        if row.get("stage") and row["stage"] != record["stage"]:
            changes.append({"key": "stage", "field": "Stage", "from": playbook.label(record["stage"]), "to": playbook.label(row["stage"])})
        person = row.get("person") or {}
        if person:
            known = [record] + self.store.people(record["id"])
            same = [p for p in known if (person.get("email") and (p.get("email") or "").lower() == person["email"].lower())
                    or p["name"].lower() == person["name"].lower()]
            if not same:
                changes.append({"key": "person", "field": "Person", "from": "", "to": _person_line(person)})
        bill, came = row.get("invoice") or {}, row.get("payment") or {}
        if bill:
            changes.append({"key": "invoice", "field": "Invoice", "from": "", "to": " ".join(bit for bit in (
                cards.money(bill["amount"], playbook.currency), "#" + bill["number"] if bill["number"] else "",
                "due " + bill["due"] if bill["due"] else "") if bit)})
        if came:
            changes.append({"key": "payment", "field": "Payment", "from": "", "to": "%s%s" % (
                cards.money(came["amount"], playbook.currency) if came.get("amount") else "in full",
                " on invoice #" + came["invoice"] if came.get("invoice") else "")})
        if _text(row.get("note")):
            changes.append({"key": "note", "field": "Note", "from": "", "to": _text(row.get("note"), 160)})
        return changes

    def plan(self, rows: list, playbook, origin: str = "list") -> tuple:
        deals = self.store.list(status="all")
        declined = self.store.declined() if origin == "proposal" else {}
        plan, ignored, seen = [], set(), {}
        for number, raw in enumerate(rows, start=1):
            row, warnings, extra = self._clean(raw, playbook)
            ignored.update(extra)
            entry = {"n": number, "name": row["name"], "company": _text(row.get("company")), "group": _text(row.get("grp")),
                     "stage": row["stage"], "row": row, "warnings": warnings, "changes": [], "why": "", "deal": "",
                     "evidence": row.pop("evidence"), "case": row.pop("case")}
            plan.append(entry)
            if not row["name"] and row.get("record"):
                known = self.store.get(str(row["record"]))
                if known:                                   # a row that names a record by its id needs no name as well
                    row["name"] = known["name"]
                    entry.update(name=known["name"], company=known["company"], group=known["grp"])
            if not row["name"]:
                entry.update(kind="problem", why="no name or company on this row")
                continue
            identity = (row.get("record") or (row.get("email") or "").lower() or _link(row.get("linkedin", ""))
                        or row["name"].lower())
            key = (identity, entry["group"].lower())
            several = origin == "proposal" and bool(row.get("record"))      # two suggestions about one record are two suggestions
            if key in seen and not several:
                entry.update(kind="same", why="same as row %d" % seen[key])
                continue
            seen[key] = number
            record, why, candidates = self._match(row, deals)
            if record is not None:
                entry["deal"] = record["id"]
                entry["changes"] = self._changes(record, row, playbook)
                entry["kind"] = "update" if entry["changes"] else "same"
                entry["why"] = "" if entry["changes"] else "already on the desk, nothing new"
            elif candidates:
                entry.update(kind="unclear", why=why + ": " + ", ".join(
                    "%s (%s)" % (c["name"], c["grp"] or c["company"] or c["id"]) for c in candidates) + ". Say which, or pass its id as `record`.")
            else:
                entry.update(kind="new", why=why)
            if entry["kind"] in APPLICABLE:
                entry["hash"] = digest(entry)
                if entry["hash"] in declined:       # asked before and turned down: the desk does not ask twice
                    entry.update(kind="declined", why="declined on %s; not proposed again" % declined[entry["hash"]][:10])
        return plan, ignored

    # -- reading a list back --------------------------------------------------------------------
    @staticmethod
    def counts(intake: dict) -> dict:
        tally = {"new": 0, "update": 0, "same": 0, "unclear": 0, "problem": 0, "declined": 0, "done": 0}
        for row in intake["rows"]:
            kind = "done" if row.get("done") else row.get("kind", "problem")      # decided one by one already
            tally[kind] = tally.get(kind, 0) + 1
        tally["rows"] = len(intake["rows"])
        return tally

    def view(self, intake_id: str) -> Optional[dict]:
        """A list as every surface shows it (the raw row values stay in the ledger)."""
        intake = self.store.intake_get(intake_id)
        if intake is None:
            return None
        playbook = self.desk.playbook()
        return {"id": intake["id"], "source": intake["source"], "status": intake["status"], "at": intake["at"],
                "origin": intake.get("origin") or "list", "counts": self.counts(intake), "result": intake["result"],
                "rows": [self._shown(row, playbook) for row in intake["rows"]]}

    @staticmethod
    def _shown(row: dict, playbook) -> dict:
        return {"n": row["n"], "kind": row["kind"], "name": row["name"], "company": row["company"], "group": row["group"],
                "stage": playbook.label(row["stage"] or playbook.first_stage()) if row["kind"] == "new" else "",
                "changes": [{"field": c["field"], "from": c["from"], "to": c["to"]} for c in row["changes"]],
                "why": row["why"], "warnings": row["warnings"], "deal": row["deal"], "applied": bool(row.get("applied")),
                "done": row.get("done") or "", "case": row.get("case") or "", "evidence": row.get("evidence") or []}

    def waiting(self) -> list:
        return [{"id": intake["id"], "source": intake["source"], "at": intake["at"], "origin": intake.get("origin") or "list",
                 "counts": self.counts(intake)} for intake in self.store.intake_list("open", limit=20)]

    def queue(self, limit: int = 60) -> list:
        """What is waiting for a person, oldest first: each proposed change by itself (with what it
        rests on), and each list someone sent as one item to review as a whole."""
        playbook = self.desk.playbook()
        items = []
        for intake in reversed(self.store.intake_list("open", limit=20)):
            if (intake.get("origin") or "list") != "proposal":
                items.append({"intake": intake["id"], "list": True, "source": intake["source"], "at": intake["at"],
                              "counts": self.counts(intake)})
                continue
            for row in intake["rows"]:
                if row["kind"] in APPLICABLE and not row.get("done"):
                    items.append(dict(self._shown(row, playbook), intake=intake["id"], source=intake["source"], at=intake["at"]))
        return items[:limit]

    def headline(self, intake: dict) -> str:
        tally, terms = self.counts(intake), self.desk.playbook().terms
        if intake["status"] == "applied":
            result = intake["result"]
            return "%s: %d added, %d updated%s" % (intake["source"], result.get("added", 0), result.get("updated", 0),
                                                   ", %d left out" % result["left"] if result.get("left") else "")
        if intake["status"] == "discarded":
            return "%s: discarded, nothing written" % intake["source"]
        bits = ["%d new %s" % (tally["new"], terms["deal"] if tally["new"] == 1 else terms["deals"]),
                "%d to update" % tally["update"] if tally["update"] else "",
                "%d already there" % tally["same"] if tally["same"] else "",
                "%d unclear" % tally["unclear"] if tally["unclear"] else "",
                "%d unusable" % tally["problem"] if tally["problem"] else ""]
        return "%s: %s" % (intake["source"], ", ".join(bit for bit in bits if bit))

    @staticmethod
    def _line(row: dict, stage_label: str = "") -> str:
        who = " · ".join(bit for bit in (row["name"] or "(no name)", row["company"] if row["company"] != row["name"] else "",
                                         row["group"]) if bit)
        if row["kind"] == "new":
            return "%d. %s%s" % (row["n"], who, " (%s)" % stage_label if stage_label else "")
        if row["kind"] == "update":
            parts = []
            for change in row["changes"]:
                field = change["field"].lower()
                if change.get("key") == "note":
                    parts.append("note added")
                elif change.get("key") == "person":
                    parts.append("add %s" % change["to"])
                elif change["from"]:
                    parts.append("%s %s → %s" % (field, change["from"], change["to"]))
                else:
                    parts.append("%s added" % field)
            return "%d. %s: %s%s" % (row["n"], who, "; ".join(parts), " (%s)" % row["case"] if row.get("case") else "")
        return "%d. %s — %s" % (row["n"], who, row["why"])

    def text(self, intake: dict, limit: int = 40) -> str:
        playbook = self.desk.playbook()
        lines = ["Intake · " + self.headline(intake)]
        for kind, title in (("new", "New"), ("update", "Updates"), ("unclear", "Unclear"), ("problem", "Cannot use"), ("same", "Already there"),
                            ("declined", "Declined before")):
            rows = [row for row in intake["rows"] if row["kind"] == kind and not row.get("done")]
            if not rows:
                continue
            lines += ["", "%s (%d)" % (title, len(rows))]
            shown = rows[:limit] if kind in APPLICABLE else rows[:8]
            lines += ["  " + self._line(row, playbook.label(row["stage"] or playbook.first_stage())) for row in shown]
            if len(rows) > len(shown):
                lines.append("  … and %d more" % (len(rows) - len(shown)))
        return "\n".join(lines)

    def blocks(self, intake: dict) -> list:
        """The review as a Slack message: what would change, and the two buttons."""
        escape = cards._escape
        playbook = self.desk.playbook()
        rows = [row for row in intake["rows"] if row["kind"] in APPLICABLE and not row.get("done")]
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*Intake*  ·  %s" % escape(self.headline(intake))}}]
        if intake["status"] == "open":
            listed = [escape(self._line(row, playbook.label(row["stage"] or playbook.first_stage())))[:180] for row in rows[:20]]
            if len(rows) > 20:
                listed.append("… and %d more" % (len(rows) - 20))
            if listed:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(listed)[:2900]}})
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                           "Nothing is written yet. To leave rows out, reply with their numbers."}]})
            blocks.append({"type": "actions", "block_id": cards.PREFIX + "intake", "elements": [
                {"type": "button", "style": "primary", "action_id": cards.PREFIX + "intake", "value": "apply|" + intake["id"],
                 "text": {"type": "plain_text", "text": "Apply %d" % len(rows)}},
                {"type": "button", "action_id": cards.PREFIX + "intake_discard", "value": "discard|" + intake["id"],
                 "text": {"type": "plain_text", "text": "Discard"}}]})
        return blocks

    # -- apply ----------------------------------------------------------------------------------
    @staticmethod
    def _numbers(value: Any) -> Optional[set]:
        if value in (None, "", "all") or value == []:
            return None
        if not isinstance(value, (list, tuple, set)):
            value = [value]
        out = set()
        for item in value:
            try:
                out.add(int(float(item)))
            except (TypeError, ValueError, OverflowError):
                continue
        return out

    def _write(self, row: dict, playbook, actor: str, label: str) -> Optional[dict]:
        """Write one approved row. Everything it changes is one action, so it can be taken back as one."""
        with self.store.operation():
            return self._add(row, playbook, actor, label) if row["kind"] == "new" else self._update(row, playbook, actor, label)

    def _remember(self, intake: dict, row: dict) -> None:
        """A proposal was turned down: do not make it again. (A row left out of a list someone sent is just left out.)"""
        if (intake.get("origin") or "list") == "proposal":
            self.store.decline(row.get("hash") or digest(row), row.get("deal") or "", self._line(row)[:300])

    def _close(self, intake: dict, actor: str, problems: Optional[list] = None) -> dict:
        """Every row has been decided: the list is finished, with what came of it."""
        done = [row for row in intake["rows"] if row.get("done")]
        result = {"added": len([r for r in done if r["done"] == "applied" and r["kind"] == "new"]),
                  "updated": len([r for r in done if r["done"] == "applied" and r["kind"] == "update"]),
                  "left": len([r for r in done if r["done"] != "applied"]), "problems": (problems or [])[:20], "by": actor}
        status = "applied" if result["added"] or result["updated"] else "discarded"
        self.store.intake_finish(intake["id"], status, result if status == "applied" else dict(result, reason="declined"), intake["rows"])
        return result

    def apply(self, args: dict, actor: str = "") -> dict:
        intake = self.store.intake_get(str(args.get("id") or ""))
        if intake is None:
            return {"ok": False, "error": "no such list"}
        if intake["status"] != "open" or not self.store.intake_claim(intake["id"]):
            now = self.store.intake_get(intake["id"])
            return {"ok": True, "applied": False, "status": now["status"], "note": "Already dealt with: " + self.headline(now)}
        wanted, skipped = self._numbers(args.get("rows")), self._numbers(args.get("skip")) or set()
        playbook = self.desk.playbook()
        label = ("intake: " + intake["source"])[:60]
        added, updated, left, problems, records = 0, 0, 0, [], []
        for row in intake["rows"]:
            if row.get("done"):                 # decided by itself earlier
                continue
            row["applied"] = False
            if row["kind"] not in APPLICABLE:
                continue
            if (wanted is not None and row["n"] not in wanted) or row["n"] in skipped:
                left += 1
                row["done"] = "declined"
                self._remember(intake, row)
                continue
            try:
                deal = self._write(row, playbook, actor, label)
            except Exception as exc:        # one row that cannot be written is reported; the rest still land
                problems.append("row %d (%s): %s" % (row["n"], row["name"], exc))
                row["done"] = "left"
                continue
            if deal is None:
                left += 1
                row["done"] = "left"
                continue
            row["applied"], row["deal"], row["done"] = True, deal["id"], "applied"
            records.append(deal["id"])
            added, updated = added + (row["kind"] == "new"), updated + (row["kind"] == "update")
            self.desk._changed(deal)
        self._close(intake, actor, problems)
        self.desk.announce()
        return {"ok": True, "applied": True, "id": intake["id"], "added": added, "updated": updated, "left_out": left,
                "problems": problems[:20], "records": records[:50],
                "note": "Done. Say what was added and updated in one or two lines; do not list every record."}

    def decide(self, intake_id: Any, number: Any, verb: str, actor: str = "") -> dict:
        """Approve or decline one row by itself. The list stays open until its last row is decided."""
        intake = self.store.intake_get(str(intake_id or ""))
        if intake is None:
            return {"ok": False, "error": "no such list"}
        if verb not in ("approve", "decline"):
            return {"ok": False, "error": "approve or decline"}
        if intake["status"] != "open" or not self.store.intake_claim(intake["id"]):
            now = self.store.intake_get(intake["id"])
            return {"ok": True, "decided": False, "note": "Already dealt with: " + self.headline(now)}
        numbers = self._numbers(number) or set()
        row = next((r for r in intake["rows"] if r["n"] in numbers and r["kind"] in APPLICABLE and not r.get("done")), None)
        out = {"ok": True, "decided": False, "note": "That one is already dealt with."}
        problems = []
        try:
            if row is not None and verb == "approve":
                try:
                    deal = self._write(row, self.desk.playbook(), actor, ("intake: " + intake["source"])[:60])
                except Exception as exc:
                    deal, problems = None, ["row %d (%s): %s" % (row["n"], row["name"], exc)]
                row["applied"], row["done"] = deal is not None, "applied" if deal is not None else "left"
                if deal is not None:
                    row["deal"] = deal["id"]
                    self.desk._changed(deal)
                out = {"ok": True, "decided": True, "applied": deal is not None, "deal": row.get("deal") or "",
                       "note": "Done." if deal is not None else (problems[0] if problems else "Nothing to change: the record already says so.")}
            elif row is not None:
                row["applied"], row["done"] = False, "declined"
                self._remember(intake, row)
                out = {"ok": True, "decided": True, "applied": False, "deal": row.get("deal") or "",
                       "note": "Declined. It will not be proposed again." if (intake.get("origin") or "list") == "proposal" else "Left out."}
        finally:
            if any(r["kind"] in APPLICABLE and not r.get("done") for r in intake["rows"]):
                self.store.intake_finish(intake["id"], "open", intake["result"], intake["rows"])      # the rest still wait
            else:
                self._close(intake, actor, problems)
        self.desk.announce()
        return out

    def _add(self, row: dict, playbook, actor: str, label: str) -> Optional[dict]:
        values = row["row"]
        group = values.get("grp") or ""
        for existing in self.store.list(status="open", query=row["name"]):     # added by someone else since the review
            if existing["name"].lower() == row["name"].lower() and existing["grp"].lower() == group.lower():
                return None
        stage = row["stage"] or playbook.first_stage()
        closing = playbook.status_of(stage) != "open"
        deal = self.store.add(row["name"], playbook.first_stage() if closing else stage, actor=actor,
                              company=values.get("company") or "", grp=group, email=values.get("email") or "",
                              linkedin=values.get("linkedin") or "", value=values.get("value"), owner=values.get("owner") or "",
                              next_step=values.get("next_step") or "", next_due=values.get("next_due") or "",
                              close_on=values.get("close_on") or "",
                              source=values.get("source") or label, fields=values.get("fields") or None)
        if closing:
            deal = self.desk._move(deal, stage, playbook, actor, values.get("lost_reason") or "") or deal
        if values.get("person"):
            self.store.person_add(deal["id"], actor=actor, **values["person"])
        if _text(values.get("note")):
            self.store.log(deal["id"], "note", _text(values["note"], 4000), actor=actor)
        return self.store.get(deal["id"])

    def _update(self, row: dict, playbook, actor: str, label: str) -> Optional[dict]:
        record = self.store.get(row["deal"])
        if record is None:
            raise KeyError("the record is no longer on the desk")
        values = row["row"]
        changes = self._changes(record, values, playbook)      # against the record as it is now, not as it was at review
        if not changes:
            return None
        plain = {c["key"]: (values["value"] if c["key"] == "value" else c["to"]) for c in changes
                 if c["key"] not in ("stage", "note", "person", "invoice", "payment") and not c["key"].startswith("fields.")}
        fields = {c["key"][len("fields."):]: c["to"] for c in changes if c["key"].startswith("fields.")}
        if plain or fields:
            record = self.store.update(record["id"], actor=actor, fields=fields or None, **plain)
        if any(c["key"] == "stage" for c in changes):
            record = self.desk._move(record, values["stage"], playbook, actor, values.get("lost_reason") or "") or self.store.get(record["id"])
        if any(c["key"] == "person" for c in changes):
            self.store.person_add(record["id"], actor=actor, **values["person"])
        if any(c["key"] == "invoice" for c in changes):
            due, problem = self.desk._due_on(values["invoice"].get("due"))
            if problem:
                raise ValueError(problem["error"])
            self.store.invoice_add(record["id"], values["invoice"]["amount"], actor=actor, number=values["invoice"].get("number") or "",
                                   due_on=due, note=values["invoice"].get("note") or "")
        if any(c["key"] == "payment" for c in changes):
            found, problem = self.desk._which_invoice(record, values["payment"].get("invoice"))
            if problem:
                raise ValueError(problem["error"])
            self.store.invoice_pay(found["id"], values["payment"].get("amount"), on=st.parse_due(values["payment"].get("on") or "") or "", actor=actor)
        if _text(values.get("note")):
            self.store.log(record["id"], "note", "%s (%s)" % (_text(values["note"], 4000), label), actor=actor)
        return self.store.get(record["id"])

    def discard(self, intake_id: str) -> dict:
        intake = self.store.intake_get(intake_id)
        if intake is None:
            return {"ok": False, "error": "no such list"}
        if intake["status"] == "open" and self.store.intake_claim(intake_id):
            for row in intake["rows"]:
                if row["kind"] in APPLICABLE and not row.get("done"):
                    row["done"] = "declined"
                    self._remember(intake, row)
            self.store.intake_finish(intake_id, "discarded", {"reason": "discarded"}, intake["rows"])
            self.desk.announce()
            return {"ok": True, "discarded": True, "note": "Discarded. Nothing was written."}
        return {"ok": True, "discarded": False, "status": intake["status"], "note": "Already dealt with: " + self.headline(intake)}

    def click(self, verb: str, intake_id: str, actor: str = "") -> dict:
        """A Slack button: apply everything applicable, or discard."""
        return self.apply({"id": intake_id}, actor) if verb == "apply" else self.discard(intake_id)
