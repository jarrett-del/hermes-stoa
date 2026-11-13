"""Stoa: a go-to-market desk for small teams, inside Hermes.

    στοά, the covered walk beside the agora where the day's business was done.

Map a market, find the leads, run the pipeline. Market maps that become leads with one click,
one SQLite ledger, playbooks that fit it to a niche, Slack cards whose buttons move the pipeline
at once and hand the writing to the agent, meeting notes that become next steps, and optional
People Data Labs enrichment. ``register`` only wires things up; the work lives in ``desk``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import schemas
from .desk import Desk

log = logging.getLogger("hermes.plugins.stoa")
PLUGIN = "stoa"
TOOLSET = "stoa"


def _home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir(PLUGIN)


def _secret_reader(home: Path):
    """A way to read this profile's secrets from anywhere the plugin runs.

    Inside a chat turn Hermes has the profile's secret scope installed and ``get_secret`` just works.
    A Slack button click and the web tab arrive outside any turn; on a gateway that serves several
    profiles nothing says whose keys apply there, and ``get_secret`` refuses. Hermes's own background
    work handles that by installing the profile's scope around the call, and so does this.
    """
    def read(name: str):
        from agent import secret_scope
        if secret_scope.current_secret_scope() is not None or not secret_scope.is_multiplex_active():
            return secret_scope.get_secret(name)
        token = secret_scope.set_secret_scope(secret_scope.build_profile_secret_scope(home), profile_home=str(home))
        try:
            return secret_scope.get_secret(name)
        finally:
            secret_scope.reset_secret_scope(token)
    return read


def _session() -> dict:
    """Where the current turn is happening (empty outside a gateway session)."""
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return {}
    platform = (get_session_env("HERMES_SESSION_PLATFORM") or "").lower()
    if str(get_session_env("HERMES_CRON_SESSION") or "").strip().lower() in ("1", "true", "yes", "on"):
        # A scheduled run. Hermes gives it no platform (its delivery target is not a sender) and marks the run
        # itself instead; read as it stands, it would pass for someone at a terminal and nothing would be held.
        platform = "cron"
    return {"platform": platform,
            "chat_id": get_session_env("HERMES_SESSION_CHAT_ID") or "",
            "thread_id": get_session_env("HERMES_SESSION_THREAD_ID") or "",
            "session_key": get_session_env("HERMES_SESSION_KEY") or "",
            "user_id": get_session_env("HERMES_SESSION_ACTOR_ID") or get_session_env("HERMES_SESSION_USER_ID") or "",
            "actor": get_session_env("HERMES_SESSION_ACTOR_NAME") or get_session_env("HERMES_SESSION_USER_NAME") or ""}


AWAY = {"ok": False, "error": "The Stoa desk is not available in this conversation (the team limits it to certain "
                                "chats). Do not retry here; use your other tools."}
AWAY_TOUR = {"ok": False, "error": "The Stoa desk is showing sample data (a tour), so scheduled jobs are paused until the "
                                     "tour ends. Do nothing else and reply with exactly [SILENT]."}


def _announce() -> None:
    """Tell every open Desktop app or Webapp window that the desk changed, so its pane redraws at
    once. A no-op where no window is connected (the CLI, a scheduled job)."""
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event(PLUGIN, "changed", {})
    except Exception:
        log.debug("stoa: could not announce a change", exc_info=True)


ACROSS = ("today", "brief", "due", "stale")      # the questions about a day: asked of no pipeline in particular, they cover them all


def _tool(fn, desk=None, quiet=False, name=""):
    """Tool handlers return JSON strings and never raise into the agent loop.

    A desk with more than one pipeline: the call says which with ``pipeline`` (a name), or means the first. What
    comes back names the pipeline it used, so the agent and the person can see where a change went.
    """
    def handler(args: dict, **kwargs) -> str:
        try:
            args = dict(args or {})
            if desk is None:
                return json.dumps(fn(args), ensure_ascii=False, default=str)
            session = _session()
            if not desk.allowed(session):
                return json.dumps(AWAY_TOUR if session.get("platform") == "cron" and desk.tour() else AWAY)
            asked = args.pop("pipeline", None)
            listed = [] if desk.tour() else desk.pipelines()        # a tour shows one sample desk, whatever is asked for
            chosen = ""
            if listed and asked not in (None, ""):
                chosen, missing = desk.pipeline_pick(asked)             # a name that is no pipeline is an error, never a guess
                if missing:
                    return json.dumps(dict(missing, error=missing["error"] + ". Nothing was changed."), ensure_ascii=False)
            several = listed if len(listed) > 1 else []
            names = {entry["id"]: entry["name"] for entry in several}
            with desk.on(chosen):
                held = desk.guard(name, args, session)             # a scheduled job, and a change kept for a person
                if held is not None:
                    out = held
                elif several and asked in (None, "") and name == "gtm_pipeline" and str(args.get("view") or "") in ACROSS:
                    out = {"ok": True, "across": [], "note": "One answer for each pipeline on the desk. Say which is which."}
                    for entry in several:
                        with desk.on(entry["id"]):
                            part = fn(dict(args))
                        out["across"].append(dict(part, pipeline=entry["name"]) if isinstance(part, dict) else {"pipeline": entry["name"], "answer": part})
                else:
                    out = fn(args)
            if several and isinstance(out, dict) and "across" not in out:
                out.setdefault("pipeline", names.get(chosen or "main", ""))
                wanted = str(args.get("deal") or "").strip()
                if asked in (None, "") and wanted and out.get("ok") is False and "no record matches" in str(out.get("error")):
                    there = []                                      # not in the first pipeline: say where it is, so nothing is guessed
                    for entry in several:
                        if entry["id"] != (chosen or "main"):
                            with desk.on(entry["id"]):
                                try:
                                    there += [entry["name"]] if desk.store.find(wanted) else []
                                except Exception:
                                    there.append(entry["name"])     # several matches there: it is there
                    if there:
                        out["elsewhere"] = there
                        out["error"] = "%s in %s. It is in: %s. Call again with `pipeline`." % (out["error"], out["pipeline"], ", ".join(there))
            result = json.dumps(out, ensure_ascii=False, default=str)
            if held is not None or not quiet:
                desk.announce()
            return result
        except Exception as exc:
            log.error("stoa: %s failed: %s", getattr(fn, "__name__", "tool"), exc, exc_info=True)
            return json.dumps({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
    return handler


def build_desk(get_config, set_config) -> Desk:
    """A desk that belongs to the profile in scope right now.

    Hermes loads a plugin once per profile, with that profile's home in scope, so the data folder is
    resolved here and kept: later calls can arrive with no profile in scope at all (a button click).
    """
    home, data = _home(), _data_dir()
    desk = Desk(data_dir=lambda: data, get_config=get_config, set_config=set_config, get_secret=_secret_reader(home),
                actor=lambda: "Hermes (scheduled)" if _session().get("platform") == "cron" else _session().get("actor", ""))
    desk.home = home
    return desk


LIVE_DESKS: list = []  # desks of loaded plugin instances, newest last (the dashboard tab reuses the live one)


def register(ctx) -> None:
    desk = build_desk(ctx.get_config, ctx.set_config)
    desk.announce = _announce
    LIVE_DESKS.append(desk)
    del LIVE_DESKS[:-4]

    # -- tools ----------------------------------------------------------------------------------
    for schema, fn, emoji in (
        (schemas.GTM_PIPELINE, desk.pipeline, "🏛️"),
        (schemas.GTM_DEAL, lambda args: desk.reason_asked(args) or desk.deal(args), "🏛️"),
        (schemas.GTM_CARD, lambda args: desk.card(args, _session()), "🏛️"),
        (schemas.GTM_MARKET, lambda args: desk.market(args, _session()), "🗺️"),
        (schemas.GTM_ENRICH, desk.enrich, "🔎"),
        (schemas.GTM_NOTES, desk.notes, "📝"),
        (schemas.GTM_PLAYBOOK, desk.playbook_tool, "📜"),
        (schemas.GTM_EXPORT, desk.export, "📤"),
        (schemas.GTM_INTAKE, lambda args: desk.intake.run(args, _session()), "📥"),
        (schemas.GTM_CONTACT, desk.contact, "👥"),
        (schemas.GTM_INVOICE, desk.invoice, "🧾"),
        (schemas.GTM_GROUP, desk.group, "🗂️"),
    ):
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, emoji=emoji,
                          handler=_tool(fn, desk, quiet=schema is schemas.GTM_PIPELINE, name=schema["name"]))    # a read changes nothing

    # -- Slack cards and their buttons ----------------------------------------------------------
    from .slackdesk import ACTIONS, SlackDesk
    slack = SlackDesk(desk, ctx.inject_message)
    desk.slack = slack
    ctx.register_platform_handler("slack", slack.wire)
    ctx.register_slack_action_handler(ACTIONS, slack.on_action)

    # -- /gtm on every surface, `hermes stoa` in a terminal -------------------------------------
    def slash(raw_args: str) -> str:
        session = _session()
        with desk.on(""):                   # the command is about the first pipeline
            return desk.slash(raw_args, session) if desk.allowed(session) else "The Stoa desk is not available in this chat."

    ctx.register_command("gtm", handler=slash, description="Pipeline board, brief, or one record's card",
                         args_hint="[brief|stale|due|map|playbooks|<name>]")
    from . import cli
    ctx.register_cli_command(name="stoa", help="Stoa GTM desk: status, sample data, CSV import and export",
                             setup_fn=cli.setup, handler_fn=lambda args: cli.run(desk, args))

    # -- what the agent knows without being told ------------------------------------------------
    def section(info) -> str:
        session = _session()
        platform = str((info or {}).get("platform") or "").lower()
        if platform:        # the host says where this prompt is for; without that, trust the session itself
            session = dict(session, platform=platform)
        with desk.on(""):
            return desk.prompt_section() if desk.allowed(session) else ""

    ctx.register_system_prompt_section("stoa.desk", section, max_chars=900)
    skills = Path(__file__).parent / "skills"
    for child in sorted(skills.iterdir()):
        if child.is_dir() and (child / "SKILL.md").exists():
            ctx.register_skill(child.name, child / "SKILL.md")

    # -- automations the team can switch on from /blueprint -------------------------------------
    from .blueprints import BLUEPRINTS
    for blueprint in BLUEPRINTS:
        ctx.register_automation_blueprint(blueprint["name"], **{key: value for key, value in blueprint.items() if key != "name"})
