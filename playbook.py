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
        import yaml  # PyYAML ships with Hermes
        return yaml.safe_load(text)
    except ImportError:  # pragma: no cover - fallback for a ruamel-only environment
        from ruamel.yaml import YAML
        return YAML(typ="safe").load(text)


class Playbook:
    def __init__(self, data: dict, origin: str = "builtin"):
        self.origin = origin
        self.data = validate(data)
        self.name: str = self.data["name"]
        self.title: str = self.data.get("title") or self.name
        self.summary: str = self.data.get("summary") or ""
        self.terms: dict = dict(TERM_DEFAULTS, **(self.data.get("terms") or {}))
        self.currency: str = self.data.get("currency") or "USD"
        self.stages: list = self.data["stages"]
        self.won: dict = self.data["won"]
        self.lost: dict = self.data["lost"]
        self.fields: list = self.data.get("fields") or []
        self.drafts: dict = self.data.get("drafts") or {}
        self.default_stale: int = int(self.data.get("stale_days") or 7)
        self.enrich: str = self.data.get("enrich") or "person"

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


def validate(data: Any) -> dict:
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
        stage = dict(raw, id=str(raw["id"]), label=str(raw.get("label") or raw["id"]).strip())
        if not NAME_RE.match(stage["id"]):
            raise PlaybookError("stage id %r must be lowercase letters, digits and dashes" % stage["id"])
        if stage["id"] in seen:
            raise PlaybookError("stage id %r is used twice" % stage["id"])
        seen.add(stage["id"])
        clean.append(stage)
    out = dict(data, name=name, stages=clean)
    for key, fallback in (("won", {"id": "won", "label": "Won"}), ("lost", {"id": "lost", "label": "Lost"})):
        end = data.get(key) or fallback
        if isinstance(end, str):
            end = {"id": re.sub(r"[^a-z0-9]+", "-", end.lower()).strip("-"), "label": end}
        end = dict(end, id=str(end.get("id") or fallback["id"]), label=str(end.get("label") or fallback["label"]))
        if not NAME_RE.match(end["id"]) or end["id"] in seen:
            raise PlaybookError("%s stage id %r is invalid or already used" % (key, end["id"]))
        seen.add(end["id"])
        out[key] = end
    reasons = out["lost"].get("reasons") or ["Not a fit", "No budget", "Timing", "Went quiet"]
    out["lost"]["reasons"] = [str(r)[:60] for r in reasons][:10]
    fields = []
    for raw in data.get("fields") or []:
        if isinstance(raw, str):
            raw = {"key": re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_"), "label": raw}
        if isinstance(raw, dict) and raw.get("key"):
            fields.append({"key": str(raw["key"]), "label": str(raw.get("label") or raw["key"])})
    out["fields"] = fields[:12]
    if out.get("enrich") not in (None, "person", "company", "none"):
        raise PlaybookError("enrich must be person, company or none")
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
                out.append({"name": name, "title": pb.title, "summary": pb.summary, "origin": origin,
                            "stages": [s["label"] for s in pb.stages]})
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
            raise PlaybookError("not valid YAML: %s" % exc)
        playbook = Playbook(data, "custom")
        self.user_dir.mkdir(parents=True, exist_ok=True)
        (self.user_dir / (playbook.name + ".yaml")).write_text(text.rstrip() + "\n", encoding="utf-8")
        return playbook
