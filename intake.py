"""Intake: take in a list of people or companies from anywhere, show what it would change, and
write only what was approved.

The agent reads whatever it was sent (a PDF, a spreadsheet, a pasted list, a photo of a sign-up
sheet) and hands the rows here; a CSV on disk can be read directly. The desk then works out, with
no model involved, which rows are new, which would change a record already on the desk, which are
already there, and which it cannot place. Nothing is written until someone presses Apply or says so.
"""
from __future__ import annotations

import csv
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
           "e-mail": "email", "email_address": "email", "record_id": "record", "id": "record"}
# "record" is the id of the record a row is about, to settle a match; "deal" in a spreadsheet header means the name.
CORE = ("name", "company", "grp", "email", "linkedin", "stage", "value", "owner", "next_step", "next_due", "source", "note", "record")


def _text(value: Any, limit: int = 500) -> str:
    if value is None or isinstance(value, (bool, list, dict)):
        return ""
    return " ".join(str(value).split())[:limit]


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
            path = Path(str(args["path"])).expanduser()
            if not path.is_file():
                return {"ok": False, "error": "no file at %s" % path}
            if path.suffix.lower() not in (".csv", ".tsv", ".txt"):
                return {"ok": False, "error": "only a CSV can be read from a path. For anything else (a PDF, a spreadsheet, "
                                              "an image), read it yourself and pass what you find as rows."}
            rows, problems = self._read_csv(path)
            source = source or path.name
        if not isinstance(rows, list) or not rows:
            return {"ok": False, "error": "pass rows (a list of objects with at least a name or a company) or the path of a CSV"}
        if len(rows) > MAX_ROWS:
            return {"ok": False, "error": "at most %d rows at a time; send the rest as a second list" % MAX_ROWS}
        playbook = self.desk.playbook()
        plan, ignored = self.plan(rows, playbook)
        intake_id = "in-" + uuid.uuid4().hex[:8]
        self.store.intake_put(intake_id, source or "a list", plan)
        self.desk.announce()
        intake = self.store.intake_get(intake_id)
        out = dict(self.view(intake_id), ok=True)
        if ignored:
            out["ignored_columns"] = sorted(ignored)[:20]
        if problems:
            out["problems"] = problems[:20]
        if not any(row["kind"] in APPLICABLE for row in plan):
            out["note"] = "Nothing here would change the desk. Say so in one line."
            self.store.intake_finish(intake_id, "discarded", {"reason": "nothing to apply"})
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
            posted = slack.post_blocks(session["chat_id"], self.blocks(intake), self.headline(intake), session.get("thread_id") or "")
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

    def _read_csv(self, path: Path) -> tuple:
        data = path.read_bytes()
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:      # a CSV saved by Excel on Windows
            text = data.decode("cp1252", errors="replace")
        dialect = "excel-tab" if path.suffix.lower() == ".tsv" else "excel"
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
        aliases[playbook.terms["group"].lower()] = "grp"
        aliases[playbook.terms["deal"].lower()] = "name"
        field_keys = {field["key"] for field in playbook.fields}
        by_label = {field["label"].lower(): field["key"] for field in playbook.fields}
        row: dict = {"fields": {}}
        warnings, ignored = [], []
        raw = raw if isinstance(raw, dict) else {}
        for column, value in raw.items():
            if column is None or column == "fields":
                continue
            label = str(column).strip().lower()
            key = label.replace(" ", "_")
            key = aliases.get(key, key)
            text = value if key == "value" and isinstance(value, (int, float)) and not isinstance(value, bool) else _text(value)
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
        labels = (("company", terms["company"]), ("grp", terms["group"].capitalize()), ("email", "Email"), ("linkedin", "LinkedIn"),
                  ("owner", terms["owner"].capitalize()), ("next_step", "Next step"), ("next_due", "Due"))
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
        if _text(row.get("note")):
            changes.append({"key": "note", "field": "Note", "from": "", "to": _text(row.get("note"), 160)})
        return changes

    def plan(self, rows: list, playbook) -> tuple:
        deals = self.store.list(status="all")
        plan, ignored, seen = [], set(), {}
        for number, raw in enumerate(rows, start=1):
            row, warnings, extra = self._clean(raw, playbook)
            ignored.update(extra)
            entry = {"n": number, "name": row["name"], "company": _text(row.get("company")), "group": _text(row.get("grp")),
                     "stage": row["stage"], "row": row, "warnings": warnings, "changes": [], "why": "", "deal": ""}
            plan.append(entry)
            if not row["name"]:
                entry.update(kind="problem", why="no name or company on this row")
                continue
            identity = (row.get("record") or (row.get("email") or "").lower() or _link(row.get("linkedin", ""))
                        or row["name"].lower())
            key = (identity, entry["group"].lower())
            if key in seen:
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
        return plan, ignored

    # -- reading a list back --------------------------------------------------------------------
    @staticmethod
    def counts(intake: dict) -> dict:
        tally = {"new": 0, "update": 0, "same": 0, "unclear": 0, "problem": 0}
        for row in intake["rows"]:
            tally[row.get("kind", "problem")] = tally.get(row.get("kind", "problem"), 0) + 1
        tally["rows"] = len(intake["rows"])
        return tally

    def view(self, intake_id: str) -> Optional[dict]:
        """A list as every surface shows it (the raw row values stay in the ledger)."""
        intake = self.store.intake_get(intake_id)
        if intake is None:
            return None
        playbook = self.desk.playbook()
        return {"id": intake["id"], "source": intake["source"], "status": intake["status"], "at": intake["at"],
                "counts": self.counts(intake), "result": intake["result"],
                "rows": [{"n": row["n"], "kind": row["kind"], "name": row["name"], "company": row["company"], "group": row["group"],
                          "stage": playbook.label(row["stage"] or playbook.first_stage()) if row["kind"] == "new" else "",
                          "changes": [{"field": c["field"], "from": c["from"], "to": c["to"]} for c in row["changes"]],
                          "why": row["why"], "warnings": row["warnings"], "deal": row["deal"],
                          "applied": bool(row.get("applied"))} for row in intake["rows"]]}

    def waiting(self) -> list:
        return [{"id": intake["id"], "source": intake["source"], "at": intake["at"], "counts": self.counts(intake)}
                for intake in self.store.intake_list("open", limit=5)]

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
                elif change["from"]:
                    parts.append("%s %s → %s" % (field, change["from"], change["to"]))
                else:
                    parts.append("%s added" % field)
            return "%d. %s: %s" % (row["n"], who, "; ".join(parts))
        return "%d. %s — %s" % (row["n"], who, row["why"])

    def text(self, intake: dict, limit: int = 40) -> str:
        playbook = self.desk.playbook()
        lines = ["Intake · " + self.headline(intake)]
        for kind, title in (("new", "New"), ("update", "Updates"), ("unclear", "Unclear"), ("problem", "Cannot use"), ("same", "Already there")):
            rows = [row for row in intake["rows"] if row["kind"] == kind]
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
        rows = [row for row in intake["rows"] if row["kind"] in APPLICABLE]
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
            row["applied"] = False
            if row["kind"] not in APPLICABLE:
                continue
            if (wanted is not None and row["n"] not in wanted) or row["n"] in skipped:
                left += 1
                continue
            try:
                deal = self._add(row, playbook, actor, label) if row["kind"] == "new" else self._update(row, playbook, actor, label)
            except Exception as exc:        # one row that cannot be written is reported; the rest still land
                problems.append("row %d (%s): %s" % (row["n"], row["name"], exc))
                continue
            if deal is None:
                left += 1
                continue
            row["applied"], row["deal"] = True, deal["id"]
            records.append(deal["id"])
            added, updated = added + (row["kind"] == "new"), updated + (row["kind"] == "update")
            self.desk._changed(deal)
        result = {"added": added, "updated": updated, "left": left, "problems": problems[:20], "by": actor}
        self.store.intake_finish(intake["id"], "applied", result, intake["rows"])
        self.desk.announce()
        return {"ok": True, "applied": True, "id": intake["id"], "added": added, "updated": updated, "left_out": left,
                "problems": problems[:20], "records": records[:50],
                "note": "Done. Say what was added and updated in one or two lines; do not list every record."}

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
                              source=values.get("source") or label, fields=values.get("fields") or None)
        if closing:
            deal = self.desk._move(deal, stage, playbook, actor) or deal
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
                 if c["key"] not in ("stage", "note") and not c["key"].startswith("fields.")}
        fields = {c["key"][len("fields."):]: c["to"] for c in changes if c["key"].startswith("fields.")}
        if plain or fields:
            record = self.store.update(record["id"], actor=actor, fields=fields or None, **plain)
        if any(c["key"] == "stage" for c in changes):
            record = self.desk._move(record, values["stage"], playbook, actor) or self.store.get(record["id"])
        if _text(values.get("note")):
            self.store.log(record["id"], "note", "%s (%s)" % (_text(values["note"], 4000), label), actor=actor)
        return self.store.get(record["id"])

    def discard(self, intake_id: str) -> dict:
        intake = self.store.intake_get(intake_id)
        if intake is None:
            return {"ok": False, "error": "no such list"}
        if intake["status"] == "open" and self.store.intake_claim(intake_id):
            self.store.intake_finish(intake_id, "discarded", {"reason": "discarded"})
            self.desk.announce()
            return {"ok": True, "discarded": True, "note": "Discarded. Nothing was written."}
        return {"ok": True, "discarded": False, "status": intake["status"], "note": "Already dealt with: " + self.headline(intake)}

    def click(self, verb: str, intake_id: str, actor: str = "") -> dict:
        """A Slack button: apply everything applicable, or discard."""
        return self.apply({"id": intake_id}, actor) if verb == "apply" else self.discard(intake_id)
