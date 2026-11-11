"""The desk: everything the tools, the Slack buttons, the slash command and the CLI can do.

No Hermes imports here. ``__init__.register`` hands in the few host services the desk needs
(settings, secrets, the data directory), which keeps this module testable on its own.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import cards, sources
from . import store as st
from .intake import Intake
from .market import Markets
from .playbook import DEFAULT, Library, Playbook, PlaybookError

log = logging.getLogger("hermes.plugins.stoa")

PDL_KEY = "PDL_API_KEY"
GRANOLA_KEY = "GRANOLA_API_KEY"
LOG_KINDS = ("note", "call", "email", "meeting", "task")
HANDOFF_FRESH = 90  # seconds a "Drafting..." line is believed without news (a restart loses the watcher that clears it)
DESKTOP_FRESH = 90  # seconds since the Desktop app or Webapp half last checked in for it to count as open


def _int(value: Any, default: int, low: int = 0, high: int = 10000) -> int:
    """A whole number from a tool argument, whatever was actually passed, kept within sensible bounds."""
    try:
        number = int(float(value)) if isinstance(value, str) else int(value)
    except (TypeError, ValueError, OverflowError):
        number = default
    return max(low, min(number, high))


def _due(args: dict) -> tuple:
    """``(args with next_due as YYYY-MM-DD, error)``. A due date that cannot be read is an error, not a guess."""
    if args.get("next_due") is None:
        return args, None
    due = st.parse_due(args["next_due"])
    if due is None:
        return args, {"ok": False, "error": "next_due should be a date such as %s (tomorrow, friday, +3d and 12/01/2026 "
                                            "also work); got '%s'. Nothing was changed."
                                            % ((dt.date.today() + dt.timedelta(days=3)).isoformat(), str(args["next_due"])[:40])}
    return dict(args, next_due=due), None


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

    @property
    def store(self) -> st.Store:
        path = str(self.data_dir / "desk.db")
        if path not in self._stores:
            self._stores[path] = st.Store(path)
        return self._stores[path]

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

    def playbook(self) -> Playbook:
        return self.library.load_or_default(str(self.setting("playbook", DEFAULT)))

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
        if not allow:
            return True
        session = session or {}
        if session.get("platform", "") in ("", "cli", "tui", "desktop", "local"):
            return True
        if session.get("platform") == "cron":        # scheduled jobs have no chat: they are opted in by name
            return "cron" in allow
        return session.get("chat_id", "") in allow

    def desktop_open(self, session: Optional[dict] = None) -> bool:
        """Whether this conversation is in the Hermes Desktop app or Webapp with Stoa's half loaded,
        where a card or a map can be drawn live in the conversation instead of pasted as text."""
        return (session or {}).get("platform") in ("local", "desktop") and time.time() - self.desktop_seen <= DESKTOP_FRESH

    def can_enrich(self, playbook: Optional[Playbook] = None) -> bool:
        """Whether a record can be enriched: the key is set and the playbook has not switched it off."""
        return bool(self.secret(PDL_KEY)) and (playbook or self.playbook()).enrich != "none"

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
             touches: Optional[dict] = None) -> dict:
        """A deal as the model sees it: empty values dropped, playbook words applied.

        ``touches`` is ``store.touches()`` when the caller is showing many records at once.
        """
        playbook = playbook or self.playbook()
        touch = touches.get(deal["id"], "") if touches is not None else self.store.last_touch(deal["id"])
        out = {"id": deal["id"], "name": deal["name"], "company": deal["company"], "group": deal["grp"],
               "stage": deal["stage"], "stage_label": playbook.label(deal["stage"]), "status": deal["status"],
               "value": deal["value"], "owner": deal["owner"], "next_step": deal["next_step"], "next_due": deal["next_due"],
               "email": deal["email"], "linkedin": deal["linkedin"], "source": deal["source"],
               "lost_reason": deal["lost_reason"], "snoozed_until": deal["snoozed_until"],
               "fields": {k: v for k, v in deal["fields"].items() if not k.startswith("_")},
               "last_touch": touch, "stale": cards.is_stale(playbook, deal, touch), "due": cards.is_due(deal)}
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
               "open_value": sum(d["value"] for d in deals if d["status"] == "open")}
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
            rows.append({"stage": stage["id"], "label": stage["label"], "count": len(here), "value": sum(d["value"] for d in here)})
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
            "open": len(open_deals), "open_value": sum(d["value"] for d in open_deals),
            "by_stage": self._by_stage(playbook, open_deals),
            "due": sorted(due, key=lambda d: d.get("next_due", ""))[:15],
            "stale": sorted(stale, key=lambda d: -d["quiet_days"])[:15],
            "this_week": {"new": len([d for d in open_deals if d["created_at"] >= week_ago]), "stage_moves": len(moves),
                          "won": [{"name": d["name"], "value": d["value"]} for d in won],
                          "lost": [{"name": d["name"], "reason": d["lost_reason"]} for d in lost]},
            "notes_waiting": len(notes.get("notes") or []),
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
        won_value = 0.0
        for deal in (deals if deals is not None else self.store.list(status="all")):
            if deal["status"] == "open":
                far = playbook.position(deal["stage"])
                if far < 0:
                    continue
                here[far] += 1
                value[far] += deal["value"]
            elif deal["status"] == "won":
                far = count - 1
                won += 1
                won_value += deal["value"]
            else:
                far = max(0, playbook.position(str(deal["fields"].get("_prev_stage") or "")))
                lost[far] += 1
            if deal["source"].startswith("market map"):
                from_map += 1
            for index in range(far + 1):
                reached[index] += 1
        return {"stages": [{"id": s["id"], "label": s["label"], "reached": reached[i], "here": here[i], "lost": lost[i],
                            "value": value[i]} for i, s in enumerate(stages)],
                "won": won, "won_value": won_value, "won_label": playbook.won["label"], "lost": sum(lost),
                "lost_label": playbook.lost["label"], "from_map": from_map, "total": reached[0] if reached else 0}

    # -- gtm_deal -------------------------------------------------------------------------------
    def deal(self, args: dict) -> dict:
        action = str(args.get("action") or "get").lower()
        playbook = self.playbook()
        actor = self.actor()
        args, error = _due(args)
        if error and action in ("add", "update", "log"):
            return error
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
                                                  ("next_step", "next_step"), ("next_due", "next_due"), ("source", "source"))}
            fields = args.get("fields") if isinstance(args.get("fields"), dict) else None
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
            self.store.log(deal["id"], kind if kind in LOG_KINDS else "note", text[:4000], actor=actor)
            follow = {k: args.get(k) for k in ("next_step", "next_due") if args.get(k) is not None}
            if follow:
                self.store.update(deal["id"], actor=actor, **follow)
            deal = self.store.get(deal["id"])
        elif action == "snooze":
            deal = self.store.snooze(deal["id"], _int(args.get("days") if args.get("days") is not None else 3, 3, 0, 365), actor=actor)
        else:
            return {"ok": False, "error": "unknown action '%s'" % action,
                    "actions": ["get", "add", "update", "move", "close", "reopen", "log", "snooze"]}
        self._changed(deal)
        return {"ok": True, "action": action, "deal": self.view(deal, playbook, activities=3)}

    def _add(self, args: dict, playbook: Playbook, actor: str) -> dict:
        name = str(args.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "add needs a name"}
        group = str(args.get("group") or "").strip()
        if not args.get("force"):
            for existing in self.store.list(status="open", query=name):
                if existing["name"].lower() == name.lower() and existing["grp"].lower() == group.lower():
                    return {"ok": True, "exists": True, "deal": self.view(existing, playbook),
                            "note": "Already on the desk; nothing was added. Pass force=true to add a second record."}
        stage = playbook.resolve_stage(str(args.get("stage") or "")) or playbook.first_stage()
        if playbook.status_of(stage) != "open":
            return {"ok": False, "error": "add a record at an open stage, then close it"}
        deal = self.store.add(
            name, stage, actor=actor, company=str(args.get("company") or ""), grp=group,
            email=str(args.get("email") or ""), linkedin=str(args.get("linkedin") or ""), value=args.get("value"),
            owner=str(args.get("owner") or ""), next_step=str(args.get("next_step") or ""),
            next_due=str(args.get("next_due") or ""), source=str(args.get("source") or ""),
            fields=args.get("fields") if isinstance(args.get("fields"), dict) else None)
        if args.get("note"):
            self.store.log(deal["id"], "note", str(args["note"])[:4000], actor=actor)
        return {"ok": True, "action": "add", "deal": self.view(self.store.get(deal["id"]), playbook)}

    def _move(self, deal: dict, stage: str, playbook: Playbook, actor: str, reason: str = "") -> Optional[dict]:
        """Move a record from the stage it was read at. ``None`` means someone else moved it first."""
        return self.store.move(deal["id"], stage, status=playbook.status_of(stage), reason=reason, actor=actor,
                               label=playbook.label(stage), expect=deal["stage"])

    def _reopen(self, deal: dict, playbook: Playbook, actor: str, stage: str = "") -> Optional[dict]:
        target = playbook.resolve_stage(stage) or deal["fields"].get("_prev_stage") or playbook.first_stage()
        if playbook.status_of(target) != "open" or playbook.position(target) < 0:
            target = playbook.first_stage()
        return self.store.move(deal["id"], target, status="open", actor=actor, label=playbook.label(target), expect=deal["stage"])

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
                       value=cards.money(deal["value"], playbook.currency))
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
        result = self.slack.post_card(channel, deal["id"], thread, session.get("session_key") or "" if here else "")
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error") or "Slack did not accept the card", "card": text}
        return {"ok": True, "posted": True, "channel": result.get("channel", channel), "ts": result.get("ts", ""),
                "note": "The card is posted with its buttons. Do not repeat its contents; a one-line confirmation is enough."}

    # -- gtm_enrich -----------------------------------------------------------------------------
    def enrich(self, args: dict) -> dict:
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
    def playbook_tool(self, args: dict) -> dict:
        action = str(args.get("action") or "show").lower()
        library = self.library
        try:
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
                                 "{stage} {reason} {owner} {value}. fields are extra facts shown on cards."}
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
        return {"ok": False, "error": "unknown action '%s'" % action, "actions": ["list", "show", "use", "save", "template"]}

    def _use(self, name: str) -> dict:
        playbook = self.library.load(name)
        self._set_config("playbook", playbook.name)
        known = set(playbook.stage_ids()) | {playbook.won["id"], playbook.lost["id"]}
        strays = [d for d in self.store.list(status="all") if d["stage"] not in known]
        out = {"ok": True, "active": playbook.name, "summary": playbook.describe()}
        if strays:
            out["warning"] = ("%d existing records sit in stages this playbook does not have. They are kept and shown "
                              "under 'Other stages'; move them with gtm_deal(action='move')." % len(strays))
        return out

    # -- gtm_export -----------------------------------------------------------------------------
    def export(self, args: dict) -> dict:
        action = str(args.get("action") or "export").lower()
        playbook = self.playbook()
        if action == "export":
            folder = self.data_dir / "exports"
            folder.mkdir(parents=True, exist_ok=True)
            path = Path(str(args["path"])).expanduser() if args.get("path") else \
                folder / ("pipeline-%s.csv" % dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
            header, rows = self.store.export_rows([f["key"] for f in playbook.fields])
            header = ["group" if h == "grp" else h for h in header]
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(header)
                writer.writerows(rows)
            return {"ok": True, "path": str(path), "rows": len(rows), "columns": header,
                    "note": "A plain CSV. It opens in Google Sheets or Excel as is."}
        if action == "import":
            if not args.get("path"):
                return {"ok": False, "error": "import needs the path of a CSV file"}
            return self.import_csv(Path(str(args["path"])).expanduser(), dry_run=bool(args.get("dry_run")))
        return {"ok": False, "error": "unknown action '%s'" % action, "actions": ["export", "import"]}

    def import_csv(self, path: Path, dry_run: bool = False) -> dict:
        if not path.is_file():
            return {"ok": False, "error": "no file at %s" % path}
        playbook = self.playbook()
        field_keys = {f["key"] for f in playbook.fields}
        aliases = {"group": "grp", "account": "grp", playbook.terms["group"].lower(): "grp", "deal": "name", "contact": "name",
                   playbook.terms["deal"].lower(): "name", "amount": "value", "linkedin_url": "linkedin", "next step": "next_step",
                   "due": "next_due", "organization": "company", "organisation": "company"}
        added, skipped, problems = 0, 0, []
        data = path.read_bytes()
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:      # a CSV saved by Excel on Windows
            text = data.decode("cp1252", errors="replace")
        for number, raw in enumerate(csv.DictReader(io.StringIO(text, newline="")), start=2):
            try:
                row = {}
                for column, value in raw.items():
                    if column is None:      # more cells than the header has columns (a stray comma)
                        problems.append("row %d: more cells than columns; the extra ones were ignored" % number)
                        continue
                    key = column.strip().lower()
                    row[aliases.get(key, key.replace(" ", "_"))] = (value or "").strip()
                name = row.get("name", "")
                if not name:
                    problems.append("row %d: no name" % number)
                    continue
                due = st.parse_due(row.get("next_due", ""))
                if due is None:
                    problems.append("row %d: due date '%s' was not understood and was left blank" % (number, row["next_due"][:30]))
                    due = ""
                stage = playbook.resolve_stage(row.get("stage", "")) or playbook.first_stage()
                group = row.get("grp", "")
                if any(d["name"].lower() == name.lower() and d["grp"].lower() == group.lower()
                       for d in self.store.list(status="all", query=name)):
                    skipped += 1
                    continue
                if dry_run:
                    added += 1
                    continue
                status = playbook.status_of(stage)
                deal = self.store.add(name, stage if status == "open" else playbook.first_stage(), actor="import",
                                      company=row.get("company", ""), grp=group, email=row.get("email", ""),
                                      linkedin=row.get("linkedin", ""), value=row.get("value"), owner=row.get("owner", ""),
                                      next_step=row.get("next_step", ""), next_due=due,
                                      source=row.get("source", "") or "import",
                                      fields={k: row[k] for k in field_keys if row.get(k)})
                if status != "open":
                    self._move(deal, stage, playbook, "import", row.get("lost_reason", ""))
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
            return ("/gtm — the pipeline board\n/gtm brief — what needs you today\n/gtm stale — records gone quiet\n"
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
        except Exception:
            return ""
        return ("## Stoa GTM desk\n"
                "You keep this team's pipeline with the gtm_* tools; the desk is the source of truth, so read it "
                "before answering pipeline questions and write to it when something changes. When someone names a "
                "person, company or record, look it up with gtm_deal(action=\"get\") before saying it cannot be "
                "found, whatever else you can search; to show one, use gtm_card. %s "
                "Say \"%s\" and \"%s\", not \"deal\" or \"group\", unless those are the words. "
                "For how to run the desk, load the skill stoa:desk. To map a market or find new leads, load stoa:market-map."
                % (playbook.describe(), playbook.terms["deal"], playbook.terms["group"]))


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
