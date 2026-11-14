"""Playbooks: the one file that turns the generic desk into a recruiting desk, a sales desk, or yours.

A playbook names the thing that moves through the pipeline, the stages it moves through, what the
buttons say, and what the agent should write when a stage changes. Built-in playbooks ship beside
this module in ``playbooks/``; playbooks the user (or the agent) writes live in the plugin's data
directory and win over a built-in of the same name.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

BUILTIN_DIR = Path(__file__).parent / "playbooks"
DEFAULT = "b2b-sales"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
TERM_DEFAULTS = {"deal": "deal", "deals": "deals", "group": "account", "groups": "accounts",
                 "company": "Company", "value": "Value", "owner": "Owner"}


class PlaybookError(ValueError):
    pass


def _load_yaml(text: str) -> Any:
    try:
        from ruamel.yaml import YAML       # the YAML library Hermes ships
    except ImportError:                    # outside Hermes (the plugin's own tests): PyYAML, if that is what is installed
        import yaml
        return yaml.safe_load(text)
    return YAML(typ="safe").load(text)


class Playbook:
    def __init__(self, data: dict, origin: str = "builtin"):
        self.origin = origin
        self.data = validate(data)
        self.name: str = self.data["name"]
        self.title: str = self.data.get("title") or self.name
        self.summary: str = self.data.get("summary") or ""
        self.door: str = self.data.get("door") or self.title     # what the first-launch chooser calls it
        self.terms: dict = dict(TERM_DEFAULTS, **(self.data.get("terms") or {}))
        self.currency: str = self.data.get("currency") or "USD"
        self.stages: list = self.data["stages"]
        self.won: dict = self.data["won"]
        self.lost: dict = self.data["lost"]
        self.fields: list = self.data.get("fields") or []
        self.group_fields: list = self.data.get("group_fields") or []      # the facts kept on a group (a search's fee terms)
        # A company added from a market map joins the group named after the map ("map": a search, a campaign), or
        # becomes a group of its own ("company": an account). "" when the playbook does not say: older playbooks.
        self.map_group: str = self.data.get("map_group") or ""
        self.drafts: dict = self.data.get("drafts") or {}
        self.default_stale: int = int(self.data.get("stale_days") or 7)
        self.delivery: list = self.data.get("delivery") or []      # what happens after a win, in order: [{id, label}]
        self.enrich: str = self.data.get("enrich") or "person"
        # Who is billed on this desk: "record" (a won record is invoiced: a deal, a placement, an engagement) or
        # "none" (records are never billed themselves: leads worked for a client, whose fee is kept elsewhere).
        self.billing: str = self.data.get("billing") or "record"

    def group_is_company(self) -> bool:
        """Whether a group here is a company (an account, a client) rather than a piece of work (a search, a campaign).
        Then a record's company and its group are the same organisation, seen from two sides."""
        rule = str(self.map_group or "").lower()
        return rule == "company" or (not rule and str(self.terms.get("group") or "").lower() in ("account", "client", "company", "customer"))

    # -- stages ---------------------------------------------------------------------------------
    def stage_ids(self) -> list:
        return [s["id"] for s in self.stages]

    def first_stage(self) -> str:
        return self.stages[0]["id"]

    def stage(self, stage_id: str) -> Optional[dict]:
        for stage in self.stages:
            if stage["id"] == stage_id:
                return stage
        if stage_id == self.won["id"]:
            return self.won
        if stage_id == self.lost["id"]:
            return self.lost
        return None

    def reason(self, text: Any) -> str:
        """Why a record was lost, in the playbook's own words where they fit: a listed reason however it is
        capitalised, or the one the text names. Otherwise the text itself, kept short and never cut mid-word."""
        said = " ".join(str(text or "").split())
        if not said:
            return ""
        low = said.lower()
        for listed in self.lost["reasons"]:
            if listed.lower() == low:
                return listed
        named = [listed for listed in self.lost["reasons"] if listed.lower() in low]
        if len(named) == 1:
            return named[0]
        if re.fullmatch(r"[a-z]{4,}", low):      # one word ("Compensation"): the one reason with a word that starts the same way
            near = [listed for listed in self.lost["reasons"]
                    if any(len(word) >= 4 and (word.startswith(low[:4])) for word in re.findall(r"[a-z]+", listed.lower()))]
            if len(near) == 1:
                return near[0]
        return said if len(said) <= 60 else said[:60].rsplit(" ", 1)[0].rstrip(" ,;:-")

    def label(self, stage_id: str) -> str:
        stage = self.stage(stage_id)
        return stage["label"] if stage else stage_id

    def position(self, stage_id: str) -> int:
        """0-based index among the open stages; -1 for a closed or unknown stage."""
        ids = self.stage_ids()
        return ids.index(stage_id) if stage_id in ids else -1

    def next_stage(self, stage_id: str) -> Optional[str]:
        pos = self.position(stage_id)
        if pos < 0:
            return None
        return self.stages[pos + 1]["id"] if pos + 1 < len(self.stages) else self.won["id"]

    def status_of(self, stage_id: str) -> str:
        if stage_id == self.won["id"]:
            return "won"
        if stage_id == self.lost["id"]:
            return "lost"
        return "open"

    def resolve_stage(self, text: str) -> Optional[str]:
        """Accept an id, a label, 'next', 'won' or 'lost' in any case."""
        want = (text or "").strip().lower()
        if not want:
            return None
        if want in ("won", "win", "closed-won", "closed won"):
            return self.won["id"]
        if want in ("lost", "lose", "closed-lost", "closed lost"):
            return self.lost["id"]
        for stage in self.stages + [self.won, self.lost]:
            if want in (stage["id"].lower(), stage["label"].lower()):
                return stage["id"]
        for stage in self.stages + [self.won, self.lost]:
            if stage["label"].lower().startswith(want) or stage["id"].startswith(want.replace(" ", "-")):
                return stage["id"]
        return None

    def delivery_label(self, delivery_id: str) -> str:
        return next((item["label"] for item in self.delivery if item["id"] == delivery_id), delivery_id)

    def stale_days(self, stage_id: str) -> int:
        stage = self.stage(stage_id) or {}
        return int(stage.get("stale_days") or self.default_stale)

    def advance_label(self, stage_id: str) -> str:
        stage = self.stage(stage_id) or {}
        nxt = self.next_stage(stage_id)
        return stage.get("advance") or ("Move to %s" % self.label(nxt) if nxt else "Advance")

    # -- prompts --------------------------------------------------------------------------------
    def draft_prompt(self, kind: str, stage_id: str = "") -> str:
        """The instruction handed to the agent after a click. ``kind``: enter, follow_up, lost."""
        if kind == "enter":
            return ((self.drafts.get("on_enter") or {}).get(stage_id) or "").strip()
        return (self.drafts.get(kind) or "").strip()

    def describe(self) -> str:
        """A few lines the agent reads at the top of every session."""
        flow = " → ".join(s["label"] for s in self.stages)
        return ("Playbook: %s. One %s per record, grouped by %s. Stages: %s → %s (won), or %s (lost)."
                % (self.title, self.terms["deal"], self.terms["group"], flow, self.won["label"], self.lost["label"]))

    def to_dict(self) -> dict:
        return dict(self.data, origin=self.origin)


def _days(value: Any, where: str) -> Optional[int]:
    """A whole number of days, or ``None`` when not given. Anything else is a mistake worth naming."""
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise PlaybookError("%s must be a number of days, got %r" % (where, value))
    try:
        days = int(float(value))
    except (ValueError, OverflowError):
        raise PlaybookError("%s must be a number of days, got %r" % (where, value))
    if not 1 <= days <= 365:
        raise PlaybookError("%s must be between 1 and 365 days, got %r" % (where, value))
    return days


def _text(value: Any, where: str, limit: int = 4000) -> str:
    """Text from a YAML value. ``advance: yes`` parses as true, not as a label, so say so."""
    if value in (None, ""):
        return ""
    if isinstance(value, bool) or isinstance(value, (dict, list)):
        raise PlaybookError("%s must be text (put it in quotes), got %r" % (where, value))
    return str(value).strip()[:limit]


def validate(data: Any) -> dict:
    """Check a playbook and return it in one known shape. Everything that reads a playbook later
    relies on this, so whatever passes here cannot break a card, the board or the brief."""
    if not isinstance(data, dict):
        raise PlaybookError("a playbook is a mapping with name, stages, won and lost")
    name = str(data.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise PlaybookError("name must be lowercase letters, digits and dashes (max 40), got %r" % name)
    stages = data.get("stages")
    if not isinstance(stages, list) or not 2 <= len(stages) <= 9:
        raise PlaybookError("stages must be a list of 2 to 9 open stages")
    seen, clean = set(), []
    for raw in stages:
        if isinstance(raw, str):
            raw = {"id": re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-"), "label": raw}
        if not isinstance(raw, dict) or not raw.get("id"):
            raise PlaybookError("each stage needs an id")
        stage = dict(raw, id=str(raw["id"]), label=_text(raw.get("label"), "a stage label", 60) or str(raw["id"]))
        if not NAME_RE.match(stage["id"]):
            raise PlaybookError("stage id %r must be lowercase letters, digits and dashes" % stage["id"])
        if stage["id"] in seen:
            raise PlaybookError("stage id %r is used twice" % stage["id"])
        stage["stale_days"] = _days(raw.get("stale_days"), "stale_days for stage %r" % stage["id"])
        odds = raw.get("win_probability")
        if odds not in (None, ""):
            try:
                stage["win_probability"] = max(0, min(100, int(float(odds))))
            except (TypeError, ValueError):
                raise PlaybookError("win_probability for stage %r must be a number from 0 to 100" % stage["id"])
        stage["advance"] = _text(raw.get("advance"), "advance for stage %r" % stage["id"], 70)
        stage = {key: value for key, value in stage.items() if value not in (None, "")}
        seen.add(stage["id"])
        clean.append(stage)
    out = dict(data, name=name, stages=clean)
    out["stale_days"] = _days(data.get("stale_days"), "stale_days") or 7
    for key, limit in (("title", 80), ("door", 40), ("summary", 300), ("currency", 8)):
        out[key] = _text(data.get(key), key, limit)
    terms = data.get("terms") or {}
    if not isinstance(terms, dict):
        raise PlaybookError("terms must be a mapping such as {deal: candidate, group: role}")
    out["terms"] = {str(key): _text(value, "terms.%s" % key, 40) for key, value in terms.items()
                    if key in TERM_DEFAULTS and _text(value, "terms.%s" % key, 40)}
    drafts = data.get("drafts") or {}
    if not isinstance(drafts, dict):
        raise PlaybookError("drafts must be a mapping with on_enter, follow_up and lost")
    on_enter = drafts.get("on_enter") or {}
    if not isinstance(on_enter, dict):
        raise PlaybookError("drafts.on_enter must map a stage id to what the agent should write")
    out["drafts"] = {"on_enter": {str(key): _text(value, "drafts.on_enter.%s" % key) for key, value in on_enter.items()},
                     "follow_up": _text(drafts.get("follow_up"), "drafts.follow_up"),
                     "lost": _text(drafts.get("lost"), "drafts.lost")}
    for key, fallback in (("won", {"id": "won", "label": "Won"}), ("lost", {"id": "lost", "label": "Lost"})):
        end = data.get(key) or fallback
        if isinstance(end, str):
            end = {"id": re.sub(r"[^a-z0-9]+", "-", end.lower()).strip("-"), "label": end}
        if not isinstance(end, dict):
            raise PlaybookError("%s must be a stage with an id and a label" % key)
        end = dict(end, id=str(end.get("id") or fallback["id"]),
                   label=_text(end.get("label"), "the %s label" % key, 60) or fallback["label"])
        if not NAME_RE.match(end["id"]) or end["id"] in seen:
            raise PlaybookError("%s stage id %r is invalid or already used" % (key, end["id"]))
        seen.add(end["id"])
        out[key] = end
    reasons = out["lost"].get("reasons") or ["Not a fit", "No budget", "Timing", "Went quiet"]
    if not isinstance(reasons, list):
        raise PlaybookError("lost.reasons must be a list")
    out["lost"]["reasons"] = [str(r)[:60] for r in reasons if r not in (None, "")][:10] or ["Not a fit"]
    fields = []
    if not isinstance(data.get("fields") or [], list):
        raise PlaybookError("fields must be a list")
    for raw in data.get("fields") or []:
        if isinstance(raw, str):
            raw = {"key": re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_"), "label": raw}
        if isinstance(raw, dict) and raw.get("key"):
            fields.append({"key": str(raw["key"]), "label": str(raw.get("label") or raw["key"])})
    out["fields"] = fields[:12]
    group_fields = []
    if not isinstance(data.get("group_fields") or [], list):
        raise PlaybookError("group_fields must be a list")
    for raw in data.get("group_fields") or []:
        if isinstance(raw, str):
            raw = {"key": re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_"), "label": raw}
        if isinstance(raw, dict) and raw.get("key"):
            group_fields.append({"key": str(raw["key"]), "label": str(raw.get("label") or raw["key"])})
    out["group_fields"] = group_fields[:12]
    rule = str(data.get("map_group") or "").strip().lower()
    if rule not in ("", "map", "company"):
        raise PlaybookError("map_group is 'map' (a company added from a market map joins the group named after the map) "
                            "or 'company' (it becomes a group of its own)")
    out["map_group"] = rule
    after = data.get("delivery") or []
    if not isinstance(after, list) or len(after) > 6:
        raise PlaybookError("delivery must be a list of at most 6 stages (what happens after a win)")
    out["delivery"] = []
    for raw in after:
        if isinstance(raw, str):
            raw = {"id": re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-"), "label": raw}
        if not isinstance(raw, dict) or not NAME_RE.match(str(raw.get("id") or "")):
            raise PlaybookError("each delivery stage needs an id (lowercase letters, digits and dashes)")
        if any(item["id"] == str(raw["id"]) for item in out["delivery"]):
            raise PlaybookError("delivery stage id %r is used twice" % raw["id"])
        out["delivery"].append({"id": str(raw["id"]), "label": _text(raw.get("label"), "a delivery stage label", 60) or str(raw["id"])})
    if out.get("enrich") not in (None, "person", "company", "none"):
        raise PlaybookError("enrich must be person, company or none")
    billing = str(data.get("billing") or "record").strip().lower()
    if billing not in ("record", "none"):
        raise PlaybookError("billing is 'record' (a won record is invoiced) or 'none' (records are never billed themselves)")
    out["billing"] = billing
    return out


class Library:
    """Built-in playbooks plus the ones saved under ``<plugin data>/playbooks``."""

    def __init__(self, user_dir: Optional[Path] = None):
        self.user_dir = Path(user_dir) if user_dir else None

    def _files(self) -> dict:
        found = {}
        for origin, folder in (("builtin", BUILTIN_DIR), ("custom", self.user_dir)):
            if folder and folder.is_dir():
                for path in sorted(folder.glob("*.yaml")):
                    found[path.stem] = (origin, path)
        return found

    def names(self) -> list:
        return sorted(self._files())

    def catalog(self) -> list:
        out = []
        for name, (origin, path) in sorted(self._files().items()):
            try:
                pb = Playbook(_load_yaml(path.read_text(encoding="utf-8")), origin)
                out.append({"name": name, "title": pb.title, "door": pb.door, "summary": pb.summary, "origin": origin,
                            "stages": [s["label"] for s in pb.stages], "won": pb.won["label"],
                            "words": "%s, %s" % (pb.terms["deal"], pb.terms["group"])})
            except Exception as exc:  # a broken custom file should not hide the rest
                out.append({"name": name, "origin": origin, "error": str(exc)})
        return out

    def load(self, name: str) -> Playbook:
        entry = self._files().get((name or "").strip())
        if entry is None:
            raise PlaybookError("no playbook named %r (have: %s)" % (name, ", ".join(self.names())))
        origin, path = entry
        return Playbook(_load_yaml(path.read_text(encoding="utf-8")), origin)

    def load_or_default(self, name: str) -> Playbook:
        try:
            return self.load(name or DEFAULT)
        except Exception:
            return self.load(DEFAULT)

    def source(self, name: str) -> str:
        entry = self._files().get(name)
        if entry is None:
            raise PlaybookError("no playbook named %r" % name)
        return entry[1].read_text(encoding="utf-8")

    def save(self, text: str) -> Playbook:
        """Validate YAML text and store it as a custom playbook. Returns the parsed playbook."""
        if self.user_dir is None:
            raise PlaybookError("no data directory to save playbooks in")
        if len(text) > 20000:
            raise PlaybookError("playbook is too long (20,000 characters max)")
        try:
            data = _load_yaml(text)
        except Exception as exc:
            raise PlaybookError("not valid YAML: %s" % str(exc).split("\n\nTo suppress")[0][:400])
        playbook = Playbook(data, "custom")
        self.user_dir.mkdir(parents=True, exist_ok=True)
        (self.user_dir / (playbook.name + ".yaml")).write_text(text.rstrip() + "\n", encoding="utf-8")
        return playbook
