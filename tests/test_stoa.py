"""Tests for the desk, with no Hermes and no network. Run: python3 -m unittest discover -s tests"""
import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_package():
    """Import the plugin directory as the package ``stoa`` whatever the checkout folder is called."""
    if "stoa" in sys.modules:
        return sys.modules["stoa"]
    spec = importlib.util.spec_from_file_location("stoa", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["stoa"] = module
    spec.loader.exec_module(module)
    return module


stoa = _load_package()
from stoa import cards, demo, sources  # noqa: E402
from stoa.desk import Desk  # noqa: E402
from stoa.playbook import Library, Playbook, PlaybookError, validate  # noqa: E402
from stoa.slackdesk import ACTIONS, SlackDesk  # noqa: E402
from stoa.store import Ambiguous, Store, _number, slugify  # noqa: E402


def make_desk(tmp, playbook="recruiting", secrets=None, http=None, config=None):
    settings = dict({"playbook": playbook}, **(config or {}))
    return Desk(data_dir=lambda: Path(tmp), get_config=lambda k, default=None: settings.get(k, default),
                set_config=settings.__setitem__, get_secret=lambda n: (secrets or {}).get(n),
                actor=lambda: "tester", http=http or (lambda *a, **k: (0, {"error": "no network in tests"})))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def test_add_get_and_unique_ids(self):
        a = self.store.add("Dana Whitfield", "sourced", grp="Staff Backend")
        b = self.store.add("Dana Whitfield", "sourced", grp="Staff Backend")
        self.assertEqual(a["id"], "dana-whitfield-staff-backend")
        self.assertEqual(b["id"], "dana-whitfield-staff-backend-2")
        self.assertEqual(self.store.get(a["id"])["stage"], "sourced")

    def test_ids_fit_slack_option_values(self):
        deal = self.store.add("A" * 200, "sourced", grp="B" * 200)
        self.assertLessEqual(len("snooze7|" + deal["id"]), 75)

    def test_find_by_fragment_and_ambiguity(self):
        self.store.add("Dana Whitfield", "sourced")
        self.store.add("Dana Okafor", "sourced")
        self.assertEqual(self.store.find("whitfield")["name"], "Dana Whitfield")
        with self.assertRaises(Ambiguous):
            self.store.find("dana")
        self.assertIsNone(self.store.find("nobody"))

    def test_find_prefers_the_open_record(self):
        old = self.store.add("Sam Lee", "sourced", grp="A")
        self.store.move(old["id"], "passed", status="lost")
        new = self.store.add("Sam Lee", "sourced", grp="B")
        self.assertEqual(self.store.find("Sam Lee")["id"], new["id"])

    def test_update_only_logs_real_changes(self):
        deal = self.store.add("Dana", "sourced")
        before = len(self.store.activities(deal["id"]))
        self.store.update(deal["id"], company="")
        self.assertEqual(len(self.store.activities(deal["id"])), before)
        self.store.update(deal["id"], value="$42k", fields={"comp": "210k"})
        updated = self.store.get(deal["id"])
        self.assertEqual(updated["value"], 42000)
        self.assertEqual(updated["fields"]["comp"], "210k")

    def test_money_parsing(self):
        self.assertEqual(_number("$1.2M"), 1200000)
        self.assertEqual(_number("48,000"), 48000)
        self.assertEqual(_number("junk"), 0)
        self.assertEqual(slugify("  Ünï & Co!  "), "n-co")

    def test_file_store_survives_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "desk.db")
            Store(path).add("Dana", "sourced")
            self.assertEqual(Store(path).count(), 1)


class PlaybookTests(unittest.TestCase):
    def test_builtins_load_and_validate(self):
        library = Library()
        self.assertEqual(library.names(), ["agency", "b2b-sales", "recruiting"])
        for name in library.names():
            playbook = library.load(name)
            self.assertGreaterEqual(len(playbook.stages), 2)
            for stage in playbook.stages:
                self.assertLessEqual(len(playbook.advance_label(stage["id"])), 70)
            self.assertNotIn("error", library.catalog()[library.names().index(name)])

    def test_stage_navigation(self):
        playbook = Library().load("recruiting")
        self.assertEqual(playbook.next_stage("sourced"), "contacted")
        self.assertEqual(playbook.next_stage("offer"), "placed")
        self.assertIsNone(playbook.next_stage("placed"))
        self.assertEqual(playbook.resolve_stage("Client Interview"), "interview")
        self.assertEqual(playbook.resolve_stage("won"), "placed")
        self.assertEqual(playbook.resolve_stage("lost"), "passed")
        self.assertEqual(playbook.status_of("passed"), "lost")

    def test_validation_rejects_bad_playbooks(self):
        for bad in ({}, {"name": "Bad Name", "stages": ["a", "b"]}, {"name": "x", "stages": ["only"]},
                    {"name": "x", "stages": [{"id": "a"}, {"id": "a"}]},
                    {"name": "x", "stages": ["a", "b"], "won": {"id": "a"}}):
            with self.assertRaises(PlaybookError):
                validate(bad)
        ok = validate({"name": "tiny", "stages": ["New", "In talks"]})
        self.assertEqual([s["id"] for s in ok["stages"]], ["new", "in-talks"])
        self.assertEqual(ok["won"]["id"], "won")

    def test_save_custom_playbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Library(Path(tmp))
            text = "name: dental\ntitle: Dental supply desk\nstages:\n  - {id: lead, label: Lead}\n  - {id: trial, label: Trial order}\n"
            self.assertEqual(library.save(text).name, "dental")
            self.assertIn("dental", library.names())
            with self.assertRaises(PlaybookError):
                library.save("name: [unclosed")


class DeskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name)

    def add(self, **kw):
        args = dict({"action": "add", "name": "Dana Whitfield", "company": "Kestrel Labs", "group": "Staff Backend"}, **kw)
        return self.desk.deal(args)["deal"]

    def test_add_is_idempotent(self):
        first = self.add()
        again = self.desk.deal({"action": "add", "name": "dana whitfield", "group": "staff backend"})
        self.assertTrue(again["exists"])
        self.assertEqual(again["deal"]["id"], first["id"])
        self.assertEqual(self.desk.store.count(), 1)

    def test_move_close_reopen(self):
        deal = self.add()
        moved = self.desk.deal({"action": "move", "deal": "dana", "stage": "next"})["deal"]
        self.assertEqual(moved["stage"], "contacted")
        self.assertEqual(self.desk.deal({"action": "move", "deal": deal["id"], "stage": "Screened"})["deal"]["stage"], "screened")
        closed = self.desk.deal({"action": "close", "deal": deal["id"], "outcome": "lost", "reason": "Comp mismatch"})["deal"]
        self.assertEqual((closed["status"], closed["lost_reason"]), ("lost", "Comp mismatch"))
        reopened = self.desk.deal({"action": "reopen", "deal": deal["id"]})["deal"]
        self.assertEqual((reopened["status"], reopened["stage"]), ("open", "screened"))
        self.assertFalse(self.desk.deal({"action": "move", "deal": deal["id"], "stage": "nonsense"})["ok"])

    def test_log_sets_next_step_and_get_returns_activity(self):
        deal = self.add()
        self.desk.deal({"action": "log", "deal": deal["id"], "kind": "call", "text": "Good call.", "next_step": "Send JD",
                        "next_due": "2020-01-01"})
        got = self.desk.deal({"action": "get", "deal": deal["id"]})
        self.assertEqual(got["deal"]["next_step"], "Send JD")
        self.assertTrue(got["deal"]["due"])
        self.assertEqual(got["deal"]["activity"][0]["text"], "Updated next_step, next_due")
        self.assertIn("Good call.", [a["text"] for a in got["deal"]["activity"]])
        self.assertIn("Next: Send JD", got["card"])

    def test_channel_allowlist(self):
        self.assertTrue(self.desk.allowed({"platform": "slack", "chat_id": "C1"}))
        desk = make_desk(self.tmp.name, config={"channels": ["C1"]})
        self.assertTrue(desk.allowed({"platform": "slack", "chat_id": "C1"}))
        self.assertFalse(desk.allowed({"platform": "slack", "chat_id": "D9"}))
        self.assertFalse(desk.allowed({"platform": "telegram", "chat_id": ""}))
        self.assertTrue(desk.allowed({"platform": "cli"}))
        self.assertTrue(desk.allowed({}))

    def test_unknown_and_missing_refs(self):
        self.assertFalse(self.desk.deal({"action": "get", "deal": "ghost"})["ok"])
        self.assertFalse(self.desk.deal({"action": "get"})["ok"])
        self.assertFalse(self.desk.deal({"action": "explode", "deal": self.add()["id"]})["ok"])

    def test_board_brief_and_slash(self):
        self.assertIn("empty", self.desk.slash(""))
        self.assertEqual(demo.load(self.desk), 7)
        self.assertEqual(demo.load(self.desk), 0)
        board = self.desk.pipeline({})
        self.assertEqual(board["count"], 7)
        self.assertIn("Screened (1)", board["board"])
        brief = self.desk.brief()
        self.assertTrue(brief["due"])
        self.assertTrue(brief["stale"])
        self.assertIn("Marcus Oyelaran", [d["name"] for d in brief["due"]] + [d["name"] for d in brief["stale"]])
        self.assertIn("Gone quiet", self.desk.slash("brief"))
        self.assertIn("Dana Whitfield", self.desk.slash("dana"))
        self.assertIn("● recruiting", self.desk.slash("playbooks"))
        self.assertEqual(self.desk.pipeline({"view": "list", "group": "halcyon"})["count"], 2)
        self.assertEqual(demo.clear(self.desk), 7)

    def test_everything_tools_return_is_json(self):
        demo.load(self.desk)
        for result in (self.desk.pipeline({}), self.desk.brief(), self.desk.pipeline({"view": "activity"}),
                       self.desk.deal({"action": "get", "deal": "priya"}), self.desk.notes({}),
                       self.desk.playbook_tool({"action": "list"}), self.desk.playbook_tool({"action": "show"}),
                       self.desk.playbook_tool({"action": "template"}), self.desk.card({"deal": "priya"})):
            self.assertTrue(json.loads(json.dumps(result))["ok"])

    def test_click_advance_hands_writing_to_the_agent(self):
        deal = self.add(stage="screened")
        result = self.desk.click("advance", deal["id"], "screened", "jo")
        self.assertEqual(result["deal"]["stage"], "submitted")
        self.assertIn("Dana Whitfield", result["followup"])
        self.assertIn("submission note", result["followup"])
        self.assertIn('gtm_deal(action="get", deal="%s")' % deal["id"], result["followup"])
        self.assertNotIn("{", result["followup"])

    def test_double_click_does_not_advance_twice(self):
        deal = self.add(stage="screened")
        self.desk.click("advance", deal["id"], "screened", "jo")
        second = self.desk.click("advance", deal["id"], "screened", "sam")
        self.assertEqual(second["deal"]["stage"], "submitted")
        self.assertIn("Already moved", second["notice"])
        self.assertEqual(second["followup"], "")

    def test_click_lost_snooze_reopen_and_gone(self):
        deal = self.add()
        lost = self.desk.click("lost", deal["id"], "2", "jo")
        self.assertEqual((lost["deal"]["status"], lost["deal"]["lost_reason"]), ("lost", "Comp mismatch"))
        self.assertIn("Comp mismatch", lost["followup"])
        self.assertEqual(self.desk.click("reopen", deal["id"], "", "jo")["deal"]["status"], "open")
        self.assertIn("Snoozed until", self.desk.click("snooze7", deal["id"], "", "jo")["notice"])
        self.assertIn("Draft", self.desk.click("draft", deal["id"], "", "jo")["followup"])
        self.assertIsNone(self.desk.click("advance", "ghost", "", "jo")["deal"])

    def test_last_stage_advances_to_won(self):
        deal = self.add(stage="offer")
        result = self.desk.click("advance", deal["id"], "offer", "jo")
        self.assertEqual((result["deal"]["stage"], result["deal"]["status"]), ("placed", "won"))

    def test_switching_playbooks_keeps_records(self):
        self.add()
        result = self.desk.playbook_tool({"action": "use", "name": "b2b-sales"})
        self.assertEqual(result["active"], "b2b-sales")
        self.assertIn("1 existing records", result["warning"])
        self.assertIn("Other stages (1)", self.desk.pipeline({})["board"])
        self.assertFalse(self.desk.playbook_tool({"action": "use", "name": "nope"})["ok"])

    def test_agent_written_playbook(self):
        text = Library().source("b2b-sales").replace("name: b2b-sales", "name: dental-supply")
        saved = self.desk.playbook_tool({"action": "save", "yaml": text, "activate": True})
        self.assertEqual((saved["saved"], saved["active"]), ("dental-supply", "dental-supply"))
        self.assertEqual(self.desk.playbook().name, "dental-supply")

    def test_csv_round_trip(self):
        demo.load(self.desk)
        exported = self.desk.export({"action": "export"})
        self.assertEqual(exported["rows"], 7)
        self.assertIn("group", exported["columns"])
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        fresh = make_desk(other.name)
        dry = fresh.export({"action": "import", "path": exported["path"], "dry_run": True})
        self.assertEqual((dry["added"], fresh.store.count()), (7, 0))
        self.assertEqual(fresh.export({"action": "import", "path": exported["path"]})["added"], 7)
        again = fresh.export({"action": "import", "path": exported["path"]})
        self.assertEqual((again["added"], again["skipped_existing"]), (0, 7))
        priya = fresh.store.find("Priya Raman")
        self.assertEqual((priya["stage"], priya["value"], priya["fields"]["comp"]), ("interview", 45000, "$240k base"))

    def test_import_messy_headers(self):
        path = Path(self.tmp.name) / "leads.csv"
        path.write_text("Candidate,Company,Search,Stage,Amount\nAda Park,Orbit,Staff Backend,Client interview,$30k\n,x,,,\n")
        result = self.desk.import_csv(path)
        self.assertEqual((result["added"], result["problems"]), (1, ["row 3: no name"]))
        ada = self.desk.store.find("Ada Park")
        self.assertEqual((ada["grp"], ada["stage"], ada["value"]), ("Staff Backend", "interview", 30000))


class EnrichTests(unittest.TestCase):
    PERSON = {"status": 200, "likelihood": 9, "data": {
        "full_name": "dana whitfield", "job_title": "senior backend engineer", "job_company_name": "kestrel labs",
        "location_name": True, "location_country": "united states", "linkedin_url": "linkedin.com/in/danaw",
        "sex": "female", "birth_year": 1990,
        "experience": [{"company": {"name": "kestrel labs"}, "title": {"name": "senior backend engineer"}, "is_primary": True},
                       {"company": {"name": "fathom data"}, "title": {"name": "engineer"}, "end_date": "2021-03"}],
        "skills": ["go", "kafka", True]}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []

        def http(url, headers=None, params=None, timeout=30):
            self.calls.append((url, headers, params))
            return (200, self.PERSON) if params.get("email") != "nobody@example.com" else (404, {})
        self.desk = make_desk(self.tmp.name, secrets={"PDL_API_KEY": "k"}, http=http, config={"pdl_monthly_cap": 2})

    def test_no_key_means_no_call(self):
        desk = make_desk(self.tmp.name, http=lambda *a, **k: self.fail("network call without a key"))
        self.assertFalse(desk.enrich({"email": "a@b.co"})["ok"])
        self.assertFalse(desk.can_enrich())

    def test_enrich_fills_blanks_only_and_caches(self):
        deal = self.desk.deal({"action": "add", "name": "Dana Whitfield", "email": "dana@example.com",
                               "fields": {"title": "Staff Eng (her words)"}})["deal"]
        result = self.desk.enrich({"deal": deal["id"]})
        self.assertTrue(result["match"])
        self.assertEqual(self.calls[0][1], {"X-Api-Key": "k"})
        self.assertNotIn("sex", json.dumps(result))
        self.assertNotIn("birth", json.dumps(result))
        after = self.desk.store.get(deal["id"])
        self.assertEqual(after["company"], "kestrel labs")
        self.assertEqual(after["fields"]["title"], "Staff Eng (her words)")
        self.assertEqual(after["fields"]["location"], "united states")
        self.assertTrue(after["fields"]["enriched"])
        self.assertTrue(self.desk.enrich({"deal": deal["id"]})["cached"])
        self.assertEqual(len(self.calls), 1)

    def test_monthly_cap_and_no_match(self):
        self.assertFalse(self.desk.enrich({"email": "nobody@example.com"})["match"])
        self.desk.enrich({"email": "one@example.com"})
        capped = self.desk.enrich({"email": "two@example.com"})
        self.assertIn("cap", capped["error"])
        self.assertEqual(len(self.calls), 2)

    def test_person_lookup_needs_an_identifier(self):
        self.assertFalse(sources.pdl_person("k", name="Dana", http=lambda *a, **k: self.fail("should not call"))["ok"])


class NotesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.notes_dir = Path(self.tmp.name) / "notes"
        self.notes_dir.mkdir()
        (self.notes_dir / "2026-11-10 dana.md").write_text("# Call with Dana Whitfield\n\nShe wants to move by January.\n")
        (self.notes_dir / "groceries.txt").write_text("milk\n")
        (self.notes_dir / "skip.pdf").write_text("not a note")
        self.desk = make_desk(self.tmp.name, config={"notes_dir": str(self.notes_dir)})
        self.deal = self.desk.deal({"action": "add", "name": "Dana Whitfield"})["deal"]

    def test_inbox_suggests_reads_files_and_empties(self):
        inbox = self.desk.notes({"action": "inbox"})
        self.assertEqual(len(inbox["notes"]), 2)
        note = [n for n in inbox["notes"] if n["title"] == "Call with Dana Whitfield"][0]
        self.assertEqual(note["suggested"][0]["id"], self.deal["id"])
        self.assertNotIn("path", note)
        self.assertIn("January", self.desk.notes({"action": "read", "note": note["id"]})["text"])
        self.assertFalse(self.desk.notes({"action": "file", "note": note["id"], "deal": self.deal["id"]})["ok"])
        filed = self.desk.notes({"action": "file", "note": note["id"], "deal": "dana", "summary": "Wants to move by January.",
                                 "next_step": "Send two roles"})
        self.assertEqual(filed["deal"]["next_step"], "Send two roles")
        other = [n for n in inbox["notes"] if n["id"] != note["id"]][0]
        self.desk.notes({"action": "skip", "note": other["id"]})
        self.assertEqual(self.desk.notes({"action": "inbox"})["notes"], [])
        self.assertEqual(self.desk.brief()["notes_waiting"], 0)

    def test_no_source_gives_a_hint(self):
        desk = make_desk(self.tmp.name)
        self.assertIn("notes_dir", desk.notes({})["hint"])

    def test_granola_notes(self):
        def http(url, headers=None, params=None, timeout=30):
            self.assertEqual(headers, {"Authorization": "Bearer g"})
            if url.endswith("/notes"):
                return 200, {"notes": [{"id": "not_1", "title": "Intro", "created_at": "2026-11-09T10:00:00Z",
                                        "attendees": [{"email": "Dana@Example.com", "name": "Dana"}]}], "hasMore": False}
            return 200, {"id": "not_1", "title": "Intro", "summary_markdown": "Talked comp.", "private_notes_text": "likes remote"}
        desk = make_desk(self.tmp.name, secrets={"GRANOLA_API_KEY": "g"}, http=http)
        desk.deal({"action": "update", "deal": self.deal["id"], "email": "dana@example.com"})
        inbox = desk.notes({})
        self.assertEqual(inbox["notes"][0]["suggested"][0]["why"], "attendee email")
        text = desk.notes({"action": "read", "note": "granola:not_1"})["text"]
        self.assertIn("Talked comp.", text)
        self.assertIn("likes remote", text)

    def test_granola_bad_key_is_reported(self):
        desk = make_desk(self.tmp.name, secrets={"GRANOLA_API_KEY": "bad"}, http=lambda *a, **k: (401, {}))
        self.assertIn("rejected", desk.notes({})["problems"][0])


class CardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name)
        demo.load(self.desk)

    def test_blocks_are_valid_block_kit(self):
        for deal in self.desk.store.list(status="all"):
            blocks = self.desk.blocks(deal)
            self.assertLessEqual(len(blocks), 50)
            for block in blocks:
                if block["type"] == "section" and "text" in block:
                    self.assertLessEqual(len(block["text"]["text"]), 3000)
                if block["type"] == "section" and "fields" in block:
                    self.assertLessEqual(len(block["fields"]), 10)
                if block["type"] == "context":
                    self.assertLessEqual(len(block["elements"]), 10)
                if block["type"] == "actions":
                    self.assertLessEqual(len(block["elements"]), 25)
                    for element in block["elements"]:
                        self.assertTrue(ACTIONS.match(element["action_id"]), element["action_id"])
                        for option in element.get("options", []):
                            self.assertLessEqual(len(option["value"]), 75)
                            self.assertLessEqual(len(option["text"]["text"]), 75)
                        if element["type"] == "button":
                            self.assertLessEqual(len(element["text"]["text"]), 75)

    def test_open_card_has_the_playbooks_button_and_closed_card_reopens(self):
        dana = self.desk.store.find("Dana Whitfield")
        actions = self.desk.blocks(dana)[-1]["elements"]
        self.assertEqual(actions[0]["text"]["text"], "Submit to client")
        self.assertEqual(actions[0]["value"], dana["id"] + "|screened")
        self.assertNotIn("stoa_enrich", [e["action_id"] for e in actions])
        self.desk.deal({"action": "close", "deal": dana["id"], "outcome": "lost", "reason": "Timing"})
        closed = self.desk.blocks(self.desk.store.get(dana["id"]))[-1]["elements"]
        self.assertEqual([e["action_id"] for e in closed], ["stoa_reopen"])

    def test_meter_and_escaping(self):
        playbook = self.desk.playbook()
        dana = self.desk.store.find("Dana Whitfield")
        self.assertEqual(cards.meter(playbook, dana), "▰▰▰▱▱▱  Screened · 3 of 6")
        odd = self.desk.deal({"action": "add", "name": "A <b> & co"})["deal"]
        self.assertIn("A &lt;b&gt; &amp; co", self.desk.blocks(self.desk.store.get(odd["id"]))[0]["text"]["text"])

    def test_card_without_slack_returns_text(self):
        result = self.desk.card({"deal": "dana"}, {"platform": "telegram"})
        self.assertFalse(result["posted"])
        self.assertIn("Dana Whitfield · Kestrel Labs", result["card"])


class FakeClient:
    def __init__(self):
        self.posts, self.updates = [], []

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"channel": kwargs["channel"], "ts": "1700000000.%06d" % len(self.posts)}

    async def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ok": True}


class FakeApp:
    def __init__(self):
        self.client = FakeClient()


class SlackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name)
        demo.load(self.desk)
        self.injected = []
        self.accept = True
        self.accept_session = True

        def inject(content, origin=None, session_key=None):
            self.injected.append((content, origin or session_key))
            if session_key is not None:
                return self.accept_session
            return self.accept
        self.slack = SlackDesk(self.desk, inject)
        self.desk.slack = self.slack
        self.app = FakeApp()
        self.slack.wire(self.app, None)
        self.dana = self.desk.store.find("Dana Whitfield")
        self.acks = 0

    async def ack(self):
        self.acks += 1

    def body(self, ts="1700000000.000001", thread_ts=None):
        message = {"ts": ts}
        if thread_ts:
            message["thread_ts"] = thread_ts
        return {"user": {"id": "U1", "name": "jo"}, "channel": {"id": "C1"}, "team": {"id": "T1"}, "message": message}

    async def test_tool_posts_a_card_from_a_worker_thread(self):
        result = await asyncio.to_thread(self.desk.card, {"deal": "dana"}, {"platform": "slack", "chat_id": "C1"})
        self.assertTrue(result["posted"])
        post = self.app.client.posts[0]
        self.assertEqual(post["channel"], "C1")
        self.assertNotIn("thread_ts", post)
        self.assertEqual(self.desk.store.cards(self.dana["id"])[0]["channel"], "C1")

    async def test_advance_click_moves_updates_and_injects(self):
        await self.slack.on_action(self.ack, self.body(), {"action_id": "stoa_advance", "value": self.dana["id"] + "|screened"})
        self.assertEqual(self.acks, 1)
        self.assertEqual(self.desk.store.get(self.dana["id"])["stage"], "submitted")
        self.assertEqual(len(self.app.client.updates), 1)
        self.assertIn("Submitted · 4 of 6", self.app.client.updates[0]["blocks"][0]["text"]["text"])
        content, origin = self.injected[0]
        self.assertIn("submission note", content)
        self.assertEqual((origin["platform"], origin["chat_id"], origin["thread_id"], origin["user_id"]),
                         ("slack", "C1", "1700000000.000001", "U1"))
        self.assertEqual(self.app.client.posts, [])
        self.assertEqual(self.desk.store.activities(self.dana["id"], limit=1)[0]["actor"], "jo")

    async def test_refused_injection_parks_the_task_and_leaves_a_hint(self):
        self.accept = False
        await self.slack.on_action(self.ack, self.body(thread_ts="1699999999.000001"),
                                   {"action_id": "stoa_draft", "value": self.dana["id"]})
        self.assertEqual(self.app.client.posts[0]["thread_ts"], "1699999999.000001")
        self.assertIn("Reply here", self.app.client.posts[0]["text"])
        got = self.desk.deal({"action": "get", "deal": self.dana["id"]})
        self.assertIn("Draft a short, warm follow-up", got["pending_task"])
        self.assertNotIn("pending_task", self.desk.deal({"action": "get", "deal": self.dana["id"]}))

    async def test_card_posted_from_a_conversation_hands_back_to_that_session(self):
        session = {"platform": "slack", "chat_id": "C1", "thread_id": "1690000000.000100", "session_key": "ses_abc"}
        result = await asyncio.to_thread(self.desk.card, {"deal": "dana"}, session)
        self.assertEqual(self.app.client.posts[0]["thread_ts"], "1690000000.000100")
        body = self.body(ts=result["ts"], thread_ts="1690000000.000100")
        await self.slack.on_action(self.ack, body, {"action_id": "stoa_advance", "value": self.dana["id"] + "|screened"})
        self.assertEqual(self.injected, [(self.injected[0][0], "ses_abc")])
        await self.slack.on_action(self.ack, body, {"action_id": "stoa_more", "selected_option": {"value": "refresh|" + self.dana["id"]}})
        self.assertEqual(self.desk.store.card_session("C1", result["ts"]), "ses_abc")

    async def test_session_refused_falls_back_to_the_thread(self):
        self.accept_session = False
        session = {"platform": "slack", "chat_id": "C1", "thread_id": "", "session_key": "ses_gone"}
        result = await asyncio.to_thread(self.desk.card, {"deal": "dana"}, session)
        await self.slack.on_action(self.ack, self.body(ts=result["ts"]), {"action_id": "stoa_draft", "value": self.dana["id"]})
        self.assertEqual(self.injected[0][1], "ses_gone")
        self.assertEqual(self.injected[1][1]["thread_id"], result["ts"])

    async def test_card_sent_to_another_channel_is_top_level_without_a_session(self):
        session = {"platform": "slack", "chat_id": "C1", "thread_id": "1690000000.000100", "session_key": "ses_abc"}
        result = await asyncio.to_thread(self.desk.card, {"deal": "dana", "channel": "C9"}, session)
        self.assertNotIn("thread_ts", self.app.client.posts[0])
        self.assertEqual(self.desk.store.card_session("C9", result["ts"]), "")

    async def test_menus_and_other_cards_refresh(self):
        await asyncio.to_thread(self.slack.post_card, "C2", self.dana["id"])
        await self.slack.on_action(self.ack, self.body(), {"action_id": "stoa_more", "selected_option": {"value": "snooze7|" + self.dana["id"]}})
        self.assertTrue(self.desk.store.get(self.dana["id"])["snoozed_until"])
        self.assertEqual(sorted(u["channel"] for u in self.app.client.updates), ["C1", "C2"])
        self.assertIn("Snoozed until", self.app.client.posts[-1]["text"])
        await self.slack.on_action(self.ack, self.body(), {"action_id": "stoa_lost", "selected_option": {"value": self.dana["id"] + "|3"}})
        self.assertEqual(self.desk.store.get(self.dana["id"])["lost_reason"], "Client passed")
        self.assertIn("Client passed", self.injected[-1][0])

    async def test_tool_change_refreshes_posted_cards(self):
        await asyncio.to_thread(self.slack.post_card, "C1", self.dana["id"])
        await asyncio.to_thread(self.desk.deal, {"action": "move", "deal": self.dana["id"], "stage": "next"})
        self.assertEqual(len(self.app.client.updates), 1)

    async def test_bad_clicks_never_raise(self):
        await self.slack.on_action(self.ack, {}, {"action_id": "stoa_advance", "value": ""})
        await self.slack.on_action(self.ack, self.body(), {"action_id": "stoa_advance", "value": "ghost|x"})
        self.assertIn("no longer", self.app.client.posts[-1]["text"])
        self.assertEqual(self.acks, 2)

    async def test_not_connected_falls_back_to_text(self):
        self.slack.app = None
        result = await asyncio.to_thread(self.desk.card, {"deal": "dana"}, {"platform": "slack", "chat_id": "C1"})
        self.assertFalse(result["posted"])
        self.assertIn("card", result)


if __name__ == "__main__":
    unittest.main()
