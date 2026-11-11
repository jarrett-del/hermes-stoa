"""Tests for the desk, with no Hermes and no network. Run: python3 -m unittest discover -s tests"""
import asyncio
import datetime as dt
import importlib.util
import json
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EN = chr(0x2013)   # the dash in "50-199" style size bands


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
import fixture  # noqa: E402  (tests/fixture.py: the small desk most tests start from)
from stoa import cards, demo, schemas, sources  # noqa: E402
from stoa.desk import Desk  # noqa: E402
from stoa.playbook import Library, Playbook, PlaybookError, validate  # noqa: E402
from stoa.slackdesk import ACTIONS, SlackDesk  # noqa: E402
from stoa.store import Ambiguous, Store, _number, parse_due, slugify  # noqa: E402


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
        self.assertEqual(slugify("  Ünï & Co!  "), "ünï-co")

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
        self.assertEqual(fixture.load(self.desk), 7)
        self.assertEqual(fixture.load(self.desk), 0)
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
        self.assertEqual(fixture.clear(self.desk), 7)

    def test_everything_tools_return_is_json(self):
        fixture.load(self.desk)
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

    def test_twenty_clicks_at_once_move_the_stage_once(self):
        import threading
        deal = self.add(stage="screened")
        results = []
        threads = [threading.Thread(target=lambda i=i: results.append(self.desk.click("advance", deal["id"], "screened", "u%d" % i)))
                   for i in range(20)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(self.desk.store.get(deal["id"])["stage"], "submitted")
        self.assertEqual(len([r for r in results if r["followup"]]), 1)
        self.assertEqual(len([r for r in results if "Already moved" in r["notice"]]), 19)
        self.assertEqual(len([a for a in self.desk.store.activities(deal["id"], limit=100) if a["kind"] == "stage"]), 1)

    def test_a_tool_move_that_loses_the_race_changes_nothing(self):
        deal = self.add(stage="screened")
        stale = self.desk.store.get(deal["id"])
        self.desk.click("advance", deal["id"], "screened", "jo")
        self.assertIsNone(self.desk._move(stale, "interview", self.desk.playbook(), "agent"))
        self.assertEqual(self.desk.store.get(deal["id"])["stage"], "submitted")
        closed = self.desk.deal({"action": "close", "deal": deal["id"], "outcome": "lost", "reason": "Timing"})["deal"]
        self.assertEqual(self.desk.store.get(deal["id"])["fields"]["_prev_stage"], "submitted")
        self.assertNotIn("Updated fields", [a["text"] for a in self.desk.store.activities(deal["id"], limit=20)])
        self.assertEqual(closed["status"], "lost")

    def test_numbers_and_lists_of_the_wrong_shape_are_forgiven(self):
        fixture.load(self.desk)
        for args in ({"limit": "x"}, {"limit": [1]}, {"view": "activity", "days": "99999999999999999999"}, {"view": "activity", "limit": 10 ** 30},
                     {"limit": None}, {"limit": -5}, {"limit": "12.0"}):
            self.assertTrue(self.desk.pipeline(args)["ok"], args)
        self.assertTrue(self.desk.deal({"action": "snooze", "deal": "priya", "days": "soon"})["ok"])
        self.assertEqual(self.desk.deal({"action": "snooze", "deal": "priya", "days": 0})["deal"].get("snoozed_until"), None)
        self.assertTrue(self.desk.deal({"action": "get", "deal": "priya", "activities": {}})["ok"])
        self.assertTrue(self.desk.notes({"days": "many"})["ok"])
        for targets in (-1, 3.5, True, {"a": 1}, [None, {}], None):
            self.assertFalse(self.desk.market({"action": "track", "targets": targets})["ok"])
            self.assertFalse(self.desk.market({"action": "dismiss", "targets": targets})["ok"])

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

    def test_flow_counts_how_far_each_record_got(self):
        fixture.load(self.desk)
        self.desk.deal({"action": "close", "deal": "Wei-Lin Chao", "outcome": "won"})
        self.desk.deal({"action": "close", "deal": "Ingrid Solberg", "outcome": "lost", "reason": "Client passed"})
        self.desk.market({"action": "track", "targets": ["Brightside Freight"]})
        flow = self.desk.flow()
        self.assertEqual([s["reached"] for s in flow["stages"]], [8, 6, 4, 3, 2, 1])
        self.assertEqual([s["here"] for s in flow["stages"]], [2, 2, 1, 0, 1, 0])
        self.assertEqual([s["lost"] for s in flow["stages"]], [0, 0, 0, 1, 0, 0])
        self.assertEqual((flow["won"], flow["lost"], flow["from_map"], flow["total"]), (1, 1, 1, 8))
        for i, stage in enumerate(flow["stages"]):  # what arrives either stays, is lost, or moves on
            onward = flow["stages"][i + 1]["reached"] if i + 1 < len(flow["stages"]) else flow["won"]
            self.assertEqual(stage["reached"], stage["here"] + stage["lost"] + onward)

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
        fixture.load(self.desk)
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


class ActivitySweepTests(unittest.TestCase):
    """What a sweep of email, calls and Slack leans on: the same thing is logged once, and a stage is proposed, not changed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name)
        fixture.load(self.desk)

    def test_the_same_email_is_logged_once_however_often_the_sweep_runs(self):
        dana = self.desk.store.find("Dana Whitfield")["id"]
        before = len(self.desk.store.activities(dana, limit=50))
        first = self.desk.deal({"action": "log", "deal": dana, "kind": "email", "text": "Dana sent her availability for next week.", "ref": "gmail:18c2f0"})
        again = self.desk.deal({"action": "log", "deal": dana, "kind": "email", "text": "Dana sent her availability for next week.", "ref": "gmail:18c2f0"})
        other = self.desk.deal({"action": "log", "deal": dana, "kind": "email", "text": "Dana confirmed Tuesday.", "ref": "gmail:18c2f9"})
        self.assertEqual((first["ok"], first.get("already"), again.get("already"), other.get("already")), (True, None, True, None))
        self.assertEqual(len(self.desk.store.activities(dana, limit=50)), before + 2)
        plain = [self.desk.deal({"action": "log", "deal": dana, "text": "Left a voicemail."}) for _ in range(2)]      # no ref: logged as told
        self.assertEqual([entry.get("already") for entry in plain], [None, None])
        self.assertEqual(len(self.desk.store.activities(dana, limit=50)), before + 4)

    def test_a_stage_change_the_sweep_spots_waits_for_the_person(self):
        dana = self.desk.store.find("Dana Whitfield")
        proposed = self.desk.intake.run({"action": "propose", "source": "Activity sweep", "rows": [
            {"record": dana["id"], "stage": "Submitted", "note": "Northwind asked for her profile on Tuesday."}]})
        self.assertEqual((proposed["counts"]["update"], proposed["counts"]["new"]), (1, 0))
        self.assertEqual(self.desk.store.get(dana["id"])["stage"], dana["stage"])                     # nothing moved yet
        applied = self.desk.intake.apply({"id": proposed["id"]}, "app")
        self.assertEqual((applied["ok"], applied["updated"]), (True, 1))
        self.assertEqual(self.desk.store.get(dana["id"])["stage"], "submitted")


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
        fixture.load(self.desk)

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


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []

        def http(url, headers=None, params=None, timeout=30, data=None):
            self.calls.append((url, headers, data))
            if "exa.ai" in url:
                return 200, {"results": [{"title": "Acme Freight", "url": "https://www.acmefreight.example/about", "text": "A  broker\nin Ohio"}]}
            return 200, {"total": 12, "data": [{"full_name": "ada park", "job_title": "vp operations", "job_company_name": "meridian freight",
                                                "linkedin_url": "linkedin.com/in/adapark"}]}
        self.http = http
        self.desk = make_desk(self.tmp.name, playbook="b2b-sales", http=http, config={"pdl_monthly_cap": 4})
        self.market = self.desk.market

    def define(self):
        return self.market({"action": "define", "name": "Freight brokers, Midwest", "brief": "Brokers with 50-500 people.",
                            "segments": ["Asset-light", {"name": "Hybrid", "description": "own trucks too"}]})

    def add(self):
        return self.market({"action": "add", "companies": [
            {"company": "Meridian Freight, Inc.", "website": "https://www.meridian.example/x", "segment": "Asset-light", "fit": 5,
             "why": "Hiring compliance analysts", "size": "220 people", "location": "Chicago"},
            {"company": "Northwind Carriers", "segment": "hybrid", "fit": 9},
            {"company": "Loose One"}, {"website": "nameless.example"}]})

    def test_empty_states(self):
        self.assertIn("define", self.market({"action": "show"})["error"])
        self.assertEqual(self.market({"action": "list"})["markets"], [])
        self.assertFalse(self.market({"action": "nonsense"})["ok"])
        self.assertFalse(self.market({"action": "define"})["ok"])

    def test_define_add_show(self):
        self.assertEqual(self.define()["market"]["id"], "freight-brokers-midwest")
        self.assertTrue(self.define()["updated"])
        added = self.add()
        self.assertEqual((len(added["added"]), added["total"], len(added["problems"])), (3, 3, 1))
        again = self.market({"action": "add", "companies": [{"company": "meridian freight", "signal": "Raised a round"}]})
        self.assertEqual((again["added"], again["updated"], again["total"]), ([], ["meridian freight"], 3))
        shown = self.market({"action": "show"})
        self.assertEqual([s["segment"] for s in shown["segments"]], ["Asset-light", "hybrid", "Unsorted"])
        first = shown["segments"][0]["companies"][0]
        self.assertEqual((first["domain"], first["fit"], first["signal"]), ("meridian.example", 5, "Raised a round"))
        self.assertEqual(shown["segments"][1]["companies"][0]["fit"], 5)
        self.assertIn("●●●●● Meridian Freight, Inc. · 220 people · Chicago", shown["map"])
        self.assertIn("3 companies · 0 in the pipeline", self.desk.slash("map"))
        self.assertEqual(self.market({"action": "list"})["markets"][0]["companies"], 3)
        json.dumps(shown)

    def test_track_creates_pipeline_records_once(self):
        self.define(); self.add()
        result = self.market({"action": "track", "targets": ["Meridian Freight, Inc.", "ghost co"]})
        self.assertEqual(result["not_on_a_map"], ["ghost co"])
        deal = self.desk.store.get(result["added_to_pipeline"][0]["deal"])
        self.assertEqual((deal["stage"], deal["company"], deal["grp"], deal["next_step"]),
                         ("lead", "Meridian Freight, Inc.", "Meridian Freight, Inc.", "Find the right contact"))
        self.assertIn("market map: Freight brokers", deal["source"])
        self.assertIn("Hiring compliance analysts", self.desk.store.activities(deal["id"])[0]["text"])
        again = self.market({"action": "track", "targets": ["Meridian Freight, Inc."]})
        self.assertEqual(again["already_in_pipeline"], ["Meridian Freight, Inc."])
        self.assertEqual(self.desk.store.count(), 1)
        self.assertIn("[in pipeline]", self.market({"action": "show"})["map"])
        self.assertFalse(self.market({"action": "dismiss", "targets": ["Meridian Freight, Inc."]})["ok"])

    def test_track_with_a_contact_and_recruiting_grouping(self):
        self.define(); self.add()
        one = self.market({"action": "track", "targets": ["Northwind Carriers"], "contact": "Ada Park", "title": "VP Ops",
                           "email": "ada@northwind.example"})
        deal = self.desk.store.get(one["added_to_pipeline"][0]["deal"])
        self.assertEqual((deal["name"], deal["email"], deal["fields"]["title"], deal["next_step"]),
                         ("Ada Park", "ada@northwind.example", "VP Ops", "First outreach"))
        self.desk._set_config("playbook", "recruiting")
        two = self.market({"action": "track", "targets": ["Loose One"]})
        self.assertEqual(self.desk.store.get(two["added_to_pipeline"][0]["deal"])["grp"], "Freight brokers, Midwest")

    def test_dismiss_hides_from_the_map(self):
        self.define(); self.add()
        self.assertEqual(self.market({"action": "dismiss", "targets": ["Loose One"], "reason": "Too small"})["dismissed"], ["Loose One"])
        self.assertNotIn("Loose One", self.market({"action": "show"})["map"])
        self.assertEqual(len(self.market({"action": "show", "status": "dismissed"})["segments"][0]["companies"]), 1)

    def test_search_and_people_need_keys(self):
        self.define(); self.add()
        self.assertIn("web search", self.market({"action": "search", "query": "freight brokers"})["error"])
        self.assertIn("web search", self.market({"action": "people", "target": "Northwind Carriers"})["error"])
        self.assertEqual(self.calls, [])

    def test_search_and_people_with_keys(self):
        desk = make_desk(self.tmp.name, playbook="b2b-sales", http=self.http, config={"pdl_monthly_cap": 5},
                         secrets={"EXA_API_KEY": "e", "PDL_API_KEY": "p"})
        desk.market({"action": "define", "name": "Freight"})
        desk.market({"action": "add", "companies": [{"company": "Meridian Freight", "website": "meridian.example"}]})
        found = desk.market({"action": "search", "query": "freight brokers in Ohio"})
        self.assertEqual(found["results"][0], {"title": "Acme Freight", "url": "https://www.acmefreight.example/about",
                                               "domain": "acmefreight.example", "text": "A broker in Ohio"})
        self.assertEqual((self.calls[0][1], self.calls[0][2]["category"]), ({"x-api-key": "e"}, "company"))
        people = desk.market({"action": "people", "target": "Meridian Freight", "titles": ["VP Operations'; DROP", "logistics"], "limit": 9})
        self.assertEqual((people["people"][0]["name"], people["credits_used"]), ("Ada Park", 1))
        sql = self.calls[1][2]["sql"]
        self.assertEqual(sql, "SELECT * FROM person WHERE job_company_website='meridian.example' AND "
                              "(job_title LIKE '%vp operations drop%' OR job_title LIKE '%logistics%')")
        self.assertEqual(self.calls[1][2]["size"], 5)
        self.assertEqual(self.calls[1][1], {"X-Api-Key": "p"})
        self.assertIn("cap", desk.market({"action": "people", "target": "Meridian Freight", "limit": 5})["error"])
        self.assertEqual(len(self.calls), 2)

    def test_the_map_reads_itself_back(self):
        fixture.load(self.desk)
        s = self.market({"action": "show"})
        self.assertEqual(s["numbers"], {"companies": 7, "segments": 3, "in_pipeline": 0, "with_signal": 3, "buyers_named": 4,
                                        "assessed": 7, "bridges": 2, "avg_fit": 3.7, "dismissed": 0})
        self.assertEqual([(r["label"], r["rate"], r["of"]) for r in s["requirements"]],
                         [("50 to 500 people", 100, 7), ("Scorecards kept in spreadsheets", 43, 7),
                          ("Own compliance team", 43, 7), ("Carrier base growing", 71, 7)])
        self.assertEqual([(h["name"], h["count"], h["share"]) for h in s["holds"]],
                         [("Spreadsheets", 4, 57), ("In-house tool", 2, 29), ("CarrierWatch", 1, 14)])
        self.assertEqual(sorted(tuple(b["between"]) for b in s["bridges"]),
                         [("Asset-light brokers", "Digital-first"), ("Asset-light brokers", "Hybrid carriers")])
        self.assertEqual([(c["company"], c["buyer"]) for c in s["first_calls"]],
                         [("Brightside Freight", "Tobias Lindqvist"), ("Meridian Freight", "Renata Okafor"),
                          ("Northwind Carriers", "Dalia Reyes-Moore")])
        self.assertEqual({r["name"] for r in s["regions"]}, {"IL", "OH", "IA", "IN", "NE", "MN", "MO"})
        self.assertEqual([(z["band"], z["count"]) for z in s["sizes"]], [("50\u2013199", 4), ("200\u2013999", 3)])
        self.assertIn("only one has bought a tool", s["market"]["reading"])
        self.assertEqual(len(s["market"]["implications"]), 3)
        meridian = s["segments"][0]["companies"][0]
        self.assertEqual(meridian["uses"], "Spreadsheets")
        self.assertEqual([r["shown"] for r in meridian["requirements"]], [True, True, True, True])
        self.assertEqual(meridian["requirements"][1]["evidence"], "Job ad asks for advanced Excel for carrier scoring")
        for heading in ("How rare each requirement is", " 43%  Own compliance team (3 of 7 checked)", "Who holds this market today", "Where the segments meet",
                        "First calls", " 57%  Spreadsheets (4)", "1. Brightside Freight \u2014 Fresh funding"):
            self.assertIn(heading, s["map"])
        json.dumps(s)

    def test_requirements_set_the_fit_and_unknown_ones_are_reported(self):
        self.market({"action": "define", "name": "Solar", "requirements": ["Owns the roof", "Over 50 staff", "Has a facilities lead", "Bills over 10k"]})
        added = self.market({"action": "add", "companies": [
            {"company": "Two Of Four", "requirements": {"Owns the roof": "Deed on file", "over 50 staff": "120 people", "Has a facilities lead": ""}},
            {"company": "None Shown", "requirements": {"Owns the roof": "", "Wears a hat": "yes"}},
            {"company": "Scored By Hand", "fit": 5, "requirements": {"Owns the roof": ""}}]})
        self.assertEqual(added["problems"], ["None Shown: 'Wears a hat' is not one of this market's requirements"])
        fits = {c["company"]: c["fit"] for seg in self.market({"action": "show"})["segments"] for c in seg["companies"]}
        self.assertEqual(fits, {"Two Of Four": 3, "None Shown": 1, "Scored By Hand": 5})
        self.market({"action": "add", "companies": [{"company": "Two Of Four", "requirements": {"Bills over 10k": "Tariff sheet"}},
                                                      {"company": "Scored By Hand", "requirements": {"Over 50 staff": "90 people"}}]})
        shown = self.market({"action": "show"})
        # each requirement is measured only among the companies actually checked for it
        rates = {r["label"]: (r["shown"], r["of"]) for r in shown["requirements"]}
        self.assertEqual(rates, {"Owns the roof": (1, 3), "Over 50 staff": (2, 2), "Has a facilities lead": (0, 1), "Bills over 10k": (1, 1)})
        # fit follows the requirements as more is learned, unless it was set by hand
        fits = {c["company"]: c["fit"] for seg in shown["segments"] for c in seg["companies"]}
        self.assertEqual(fits, {"Two Of Four": 4, "None Shown": 1, "Scored By Hand": 5})
        # rewording the requirements does not count old evidence against the new wording
        self.market({"action": "define", "name": "Solar", "requirements": ["Flat roof", "Pays its own power bill"]})
        reworded = self.market({"action": "show"})
        self.assertEqual([(r["label"], r["of"]) for r in reworded["requirements"]], [("Flat roof", 0), ("Pays its own power bill", 0)])
        self.assertEqual(reworded["numbers"]["assessed"], 0)
        self.assertNotIn("How rare each requirement is", reworded["map"])

    def test_review_findings_stay_fixed(self):
        from stoa.market import _clean, _key, _people, size_band
        self.assertEqual([size_band(x) for x in ("Founded 2015, 80 people", "$40M ARR, 300 people", "250 key staff", "about 1.2k employees")],
                         ["50%s199" % EN, "200%s999" % EN, "200%s999" % EN, "1,000+"])
        self.assertEqual([_clean(x) for x in (False, True, 0, None, [], {"a": 1}, "  two   words ", 42)], ["", "", "", "", "", "", "two words", "42"])
        self.assertEqual(len({_key("\u65e5\u672c\u8a9e\u306e\u8981\u4ef6"), _key("\u5225\u306e\u8981\u4ef6"), _key("Caf\u00e9 owner"), _key("Cafe owner")}), 4)
        merged = _people([{"name": "A B", "title": "x"}, {"name": "a b", "title": "VP", "linkedin": "li/ab"}, {"name": "C D", "linkedin": "LI/AB"}])
        self.assertEqual(merged, [{"name": "C D", "title": "VP", "linkedin": "LI/AB"}])
        self.assertEqual(len(_people([{"name": "P%d" % i} for i in range(9)])), 6)
        self.market({"action": "define", "name": "Rooms", "requirements": ["\u65e5\u672c\u8a9e\u306e\u8981\u4ef6", "\u5225\u306e\u8981\u4ef6"]})
        self.market({"action": "add", "companies": [
            {"company": "Kyoto Inn", "signal": False, "uses": False, "why": 0, "requirements": {"\u65e5\u672c\u8a9e\u306e\u8981\u4ef6": "yes", "\u5225\u306e\u8981\u4ef6": ""},
             "people": [{"name": "Miles Brandt", "title": "COO", "linkedin": "li/miles", "email": "miles@inn.example"}]}]})
        shown = self.market({"action": "show"})
        self.assertEqual([(r["shown"], r["of"]) for r in shown["requirements"]], [(1, 1), (0, 1)])
        self.assertEqual((shown["numbers"]["with_signal"], shown["holds"]), (0, []))
        tracked = self.market({"action": "track", "targets": ["Kyoto Inn"], "contact": "miles brandt", "title": "Chief Operating Officer"})
        deal = self.desk.store.get(tracked["added_to_pipeline"][0]["deal"])
        self.assertEqual((deal["email"], deal["linkedin"], deal["fields"].get("title")),
                         ("miles@inn.example", "li/miles", "Chief Operating Officer"))

    def test_scheduled_jobs_are_opted_in_by_name(self):
        desk = make_desk(self.tmp.name, config={"channels": ["C1"]})
        self.assertFalse(desk.allowed({"platform": "cron", "chat_id": ""}))
        self.assertTrue(make_desk(self.tmp.name, config={"channels": ["C1", "cron"]}).allowed({"platform": "cron"}))
        self.assertTrue(make_desk(self.tmp.name).allowed({"platform": "cron"}))

    def test_first_calls_fall_back_to_the_strongest_fits(self):
        self.define()
        self.market({"action": "add", "companies": [
            {"company": "A1", "segment": "Asset-light", "fit": 5, "signal": "Raised"}, {"company": "A2", "segment": "Asset-light", "fit": 5},
            {"company": "A3", "segment": "Asset-light", "fit": 5}, {"company": "H1", "segment": "Hybrid", "fit": 4},
            {"company": "Weak", "segment": "Hybrid", "fit": 2}]})
        self.market({"action": "track", "targets": ["A2"]})
        calls = self.market({"action": "show"})["first_calls"]
        self.assertEqual([c["company"] for c in calls], ["A1", "A3", "H1"])
        saved = self.market({"action": "reading", "reading": "Thin market.", "implications": ["Go wide"],
                             "first_calls": [{"target": "H1", "why": "Knows us"}, {"target": "Nobody"}]})
        self.assertEqual((saved["saved"], saved["not_on_the_map"]), (["reading", "implications", "first_calls"], ["Nobody"]))
        shown = self.market({"action": "show"})
        self.assertEqual([(c["company"], c["why"]) for c in shown["first_calls"]], [("H1", "Knows us")])
        self.assertEqual((shown["market"]["reading"], shown["market"]["implications"]), ("Thin market.", ["Go wide"]))

    def test_buyers_found_are_kept_and_used(self):
        desk = make_desk(self.tmp.name, playbook="b2b-sales", http=self.http, secrets={"PDL_API_KEY": "p"})
        desk.market({"action": "define", "name": "Freight"})
        desk.market({"action": "add", "companies": [{"company": "Meridian Freight", "website": "meridian.example",
                                                      "people": ["Sam Ito - Ops Lead"]}]})
        found = desk.market({"action": "people", "target": "Meridian Freight", "titles": ["vp operations"], "limit": "x"})
        self.assertTrue(found["saved_to_map"])
        people = desk.market({"action": "show"})["segments"][0]["companies"][0]["people"]
        self.assertEqual([(p["name"], p.get("title")) for p in people], [("Sam Ito", "Ops Lead"), ("Ada Park", "vp operations")])
        desk.market({"action": "people", "target": "Meridian Freight"})
        self.assertEqual(len(desk.market({"action": "show"})["segments"][0]["companies"][0]["people"]), 2)
        tracked = desk.market({"action": "track", "targets": ["Meridian Freight"]})["added_to_pipeline"][0]
        deal = desk.store.get(tracked["deal"])
        self.assertEqual((tracked["contact"], deal["name"], deal["fields"]["title"], deal["next_step"]),
                         ("Sam Ito", "Sam Ito", "Ops Lead", "First outreach"))

    def test_export_writes_one_safe_page(self):
        fixture.load(self.desk)
        self.market({"action": "add", "companies": [{"company": "<script>alert(1)</script> & Sons", "segment": "Digital-first",
                                                      "source": "javascript:alert(1)", "why": "\"quoted\" <b>bold</b>"}]})
        self.market({"action": "track", "targets": ["Meridian Freight"]})
        full = self.market({"action": "export"})
        html = Path(full["path"]).read_text(encoding="utf-8")
        self.assertTrue(full["path"].endswith(".html") and full["bytes"] > 5000)
        for must in ("Regional freight brokers (demo)", "How rare each requirement is", "Who holds this market today",
                     "Where the segments meet", "First calls", "Renata Okafor", "In the pipeline", "&lt;script&gt;alert(1)&lt;/script&gt; &amp; Sons"):
            self.assertIn(must, html)
        for never in ("<script", "href=\"javascript", " src=", "@import", "<b>bold</b>"):
            self.assertNotIn(never, html)
        shared = Path(self.market({"action": "export", "share": True})["path"]).read_text(encoding="utf-8")
        for hidden in ("Renata Okafor", "Tobias Lindqvist", "In the pipeline", "Buyers named"):
            self.assertNotIn(hidden, shared)
        self.assertIn("Meridian Freight", shared)

    def test_an_older_database_is_upgraded_in_place(self):
        import sqlite3
        path = Path(self.tmp.name) / "desk.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE markets (id TEXT PRIMARY KEY, name TEXT NOT NULL, brief TEXT NOT NULL DEFAULT '', segments TEXT NOT NULL DEFAULT '[]', "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
            "CREATE TABLE targets (id TEXT PRIMARY KEY, market_id TEXT NOT NULL, company TEXT NOT NULL, domain TEXT NOT NULL DEFAULT '', "
            "segment TEXT NOT NULL DEFAULT '', fit INTEGER NOT NULL DEFAULT 3, size TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '', "
            "why TEXT NOT NULL DEFAULT '', signal TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'new', "
            "deal_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
            "INSERT INTO markets VALUES ('old', 'Old map', '', '[]', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');"
            "INSERT INTO targets VALUES ('t1', 'old', 'Acme', '', 'A', 4, '80 people', 'Leeds, UK', 'w', '', '', 'new', '', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');")
        conn.commit()
        conn.close()
        shown = self.market({"action": "show", "market": "old"})
        self.assertEqual((shown["numbers"]["companies"], shown["regions"], shown["requirements"]), (1, [{"name": "UK", "count": 1}], []))
        self.market({"action": "add", "market": "old", "companies": [{"company": "Acme", "uses": "Spreadsheets"}]})
        self.assertEqual(self.market({"action": "show", "market": "old"})["holds"][0]["name"], "Spreadsheets")

    def test_places_and_sizes(self):
        from stoa.market import region_of, size_band
        self.assertEqual([region_of(x) for x in ("Chicago, IL", "Denver, CO (DTC area, mobile)", "Remote", "", "Lyon, France")],
                         ["IL", "CO", "Remote", "", "France"])
        self.assertEqual([size_band(x) for x in ("220 people", "1,200 employees", "55", "50-200 staff", "2 locations", "", "1.5k people")],
                         ["200\u2013999", "1,000+", "50\u2013199", "50\u2013199", "", "", "1,000+"])

    def test_map_blocks_fit_slack_limits(self):
        self.define()
        self.market({"action": "add", "companies": [
            {"company": "Co %d <&>" % i, "segment": "Seg %d" % (i % 7), "fit": i % 5 + 1, "why": "w" * 390} for i in range(60)]})
        blocks = self.desk.markets.blocks("freight-brokers-midwest")
        self.assertLessEqual(len(blocks), 50)
        for block in blocks:
            if block["type"] == "section":
                self.assertLessEqual(len(block["text"]["text"]), 3000)
                if "accessory" in block:
                    self.assertTrue(ACTIONS.match(block["accessory"]["action_id"]))
        self.assertIn("more on the map", blocks[-1]["elements"][0]["text"])
        self.assertIn("&lt;&amp;&gt;", json.dumps(blocks, ensure_ascii=False))

    def test_demo_market_loads_and_clears(self):
        fixture.load(self.desk)
        self.assertIn("7 companies", self.desk.slash("map"))
        self.market({"action": "track", "targets": ["Brightside Freight"]})
        fixture.clear(self.desk)
        self.assertEqual((self.desk.store.count(), self.market({"action": "list"})["markets"]), (0, []))


class SampleDataTests(unittest.TestCase):
    """The two worked examples people see: a recruiting agency and a company selling a fraud prevention tool."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def desk(self, playbook):
        return make_desk(str(Path(self.tmp.name) / playbook), playbook=playbook)

    def test_each_example_is_a_real_sized_pipeline(self):
        for which, playbook, low, high, groups in (("recruiting", "recruiting", 1_150_000, 1_250_000, 8),
                                                   ("saas", "b2b-sales", 2_450_000, 2_550_000, 20)):
            Path(self.tmp.name, playbook).mkdir()
            desk = self.desk(playbook)
            added = demo.load(desk)                                                # no name: the one that fits the playbook
            deals = desk.store.list(status="all")
            live = [d for d in deals if d["status"] == "open"]
            self.assertEqual(added, len(demo.EXAMPLES[which]["rows"]))
            self.assertTrue(low <= sum(d["value"] for d in live) <= high, (which, sum(d["value"] for d in live)))
            self.assertGreaterEqual(len(live), 24)
            self.assertGreaterEqual(len({d["grp"] for d in live}), groups)
            self.assertTrue(all(row["count"] for row in desk.brief()["by_stage"]), which)   # nothing looks empty
            self.assertGreaterEqual(len([d for d in deals if d["status"] == "won"]), 3)
            lost = [d for d in deals if d["status"] == "lost"]
            self.assertGreaterEqual(len(lost), 5)
            self.assertTrue(all(d["lost_reason"] in desk.playbook().lost["reasons"] for d in lost))
            self.assertTrue(all(d["email"].endswith(".example") and d["source"] == "stoa-demo" for d in deals))
            self.assertTrue(desk.brief()["due"] and desk.brief()["stale"])
            self.assertEqual(demo.load(desk), 0)                                   # loading twice adds nothing

    def test_each_example_has_its_own_map_with_a_reading_and_first_calls(self):
        for which, playbook in (("recruiting", "recruiting"), ("saas", "b2b-sales")):
            Path(self.tmp.name, playbook).mkdir()
            desk = self.desk(playbook)
            demo.load(desk)
            maps = desk.markets.list_markets()
            self.assertEqual([m["name"] for m in maps], [demo.EXAMPLES[which]["market"]["name"]])
            summary = desk.markets.summary(maps[0]["id"], text=False)
            self.assertGreaterEqual(summary["numbers"]["companies"], 10)
            self.assertEqual(summary["numbers"]["assessed"], summary["numbers"]["companies"])
            self.assertGreaterEqual(len(summary["first_calls"]), 4)
            self.assertTrue(summary["market"]["reading"] and summary["bridges"])
            self.assertTrue(all(0 < rate["rate"] <= 100 for rate in summary["requirements"]))

    def test_a_person_from_the_talent_map_joins_that_search_as_a_candidate(self):
        Path(self.tmp.name, "recruiting").mkdir()
        desk = self.desk("recruiting")
        demo.load(desk)
        made = desk.markets.click_track(desk.markets.target("Lodestar Bank Platform")["id"], "maya")["deal"]
        self.assertEqual((made["name"], made["company"], made["grp"], made["stage"]),
                         ("Marisol Vega", "Lodestar Bank Platform", "Staff Backend · Northwind", "sourced"))
        self.assertIn(made["id"], [d["id"] for d in desk.store.list(grp="Staff Backend · Northwind")])

    def test_clear_takes_all_of_it_away_and_nothing_else(self):
        Path(self.tmp.name, "b2b-sales").mkdir()
        desk = self.desk("b2b-sales")
        mine = desk.deal({"action": "add", "name": "A real deal", "value": 5000})["deal"]
        demo.load(desk)
        desk.markets.click_track(desk.markets.target("Brio Credit")["id"], "alex")
        self.assertEqual(demo.clear(desk), len(demo.EXAMPLES["saas"]["rows"]))
        self.assertEqual([d["id"] for d in desk.store.list(status="all")], [mine["id"]])
        self.assertEqual(desk.markets.list_markets(), [])


class HardeningTests(unittest.TestCase):
    """One test per thing a review found: each of these was a way to lose data, spend money or hang."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name)

    def test_due_dates_are_read_the_way_people_write_them(self):
        base = dt.date(2026, 11, 10)        # a Tuesday
        for said, meant in (("2026-12-01", "2026-12-01"), ("2026-12-01T09:00:00Z", "2026-12-01"), ("12/01/2026", "2026-12-01"),
                            ("25/12/2026", "2026-12-25"), ("12/1", "2026-12-01"), ("1 Dec", "2026-12-01"), ("Dec 1, 2026", "2026-12-01"),
                            ("December 3rd", "2026-12-03"), ("Jan 5", "2027-01-05"), ("today", "2026-11-10"),
                            ("Tomorrow", "2026-11-11"), ("friday", "2026-11-13"), ("next Tue", "2026-11-17"), ("+3d", "2026-11-13"),
                            ("in 2 weeks", "2026-11-24"), ("next week", "2026-11-17"), ("", ""), (None, "")):
            self.assertEqual(parse_due(said, base), meant, said)
        for nonsense in ("soon", "Q1", "13/13/2026", "2026-02-30", "whenever", "9" * 40):
            self.assertIsNone(parse_due(nonsense, base), nonsense)

    def test_a_due_date_that_cannot_be_read_is_refused_not_stored(self):
        refused = self.desk.deal({"action": "add", "name": "Ada Quill", "next_due": "soonish"})
        self.assertFalse(refused["ok"])
        self.assertIn("next_due", refused["error"])
        self.assertIsNone(self.desk.store.find("Ada Quill"))
        added = self.desk.deal({"action": "add", "name": "Ada Quill", "next_step": "Call", "next_due": "tomorrow"})["deal"]
        self.assertEqual(added["next_due"], (dt.date.today() + dt.timedelta(days=1)).isoformat())
        self.assertFalse(self.desk.deal({"action": "update", "deal": added["id"], "next_due": "the 45th"})["ok"])
        self.assertFalse(self.desk.deal({"action": "log", "deal": added["id"], "text": "Spoke", "next_due": "later"})["ok"])
        self.assertEqual(len(self.desk.store.activities(added["id"], limit=50)), 1)      # the refused log wrote nothing
        cleared = self.desk.deal({"action": "update", "deal": added["id"], "next_due": ""})["deal"]
        self.assertNotIn("next_due", cleared)

    def test_old_free_text_due_dates_do_not_show_as_overdue_forever(self):
        deal = self.desk.store.add("Old Row", "sourced", next_due="12/01/2099")     # written before dates were checked
        self.assertFalse(cards.is_due(deal))
        self.assertTrue(cards.is_due(self.desk.store.add("Older Row", "sourced", next_due="01/02/2020")))
        self.assertFalse(cards.is_due(self.desk.store.add("Vague Row", "sourced", next_due="Friday-ish")))
        self.desk.pipeline({"view": "due"})
        self.desk.brief()

    def test_a_failed_open_does_not_leave_the_ledger_locked(self):
        store = Store(str(Path(self.tmp.name) / "desk.db"))
        store.add("Dana Whitfield", "sourced")
        good, store.path = store.path, str(Path(self.tmp.name) / "no-such-folder" / "desk.db")
        with self.assertRaises(sqlite3.OperationalError):
            store.get("dana-whitfield")
        store.path = good
        seen = []
        worker = threading.Thread(target=lambda: seen.append(store.get("dana-whitfield")), daemon=True)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive(), "another thread is still waiting for the ledger")
        self.assertEqual(seen[0]["name"], "Dana Whitfield")

    def test_names_in_any_script_get_their_own_record(self):
        first = self.desk.deal({"action": "add", "name": "张伟"})["deal"]
        second = self.desk.deal({"action": "add", "name": "李娜"})["deal"]
        third = self.desk.deal({"action": "add", "name": "Ольга Петрова"})["deal"]
        self.assertEqual(len({first["id"], second["id"], third["id"], "deal"}), 4)
        self.assertEqual(self.desk.store.find("李娜")["id"], second["id"])
        moved = self.desk.deal({"action": "move", "deal": "李娜", "stage": "next"})["deal"]
        self.assertEqual(moved["id"], second["id"])
        self.assertEqual(self.desk.store.get(first["id"])["stage"], "sourced")
        self.desk.store.add("!!!", "sourced")                                    # nothing id-safe at all: id "deal"
        self.assertIsNone(self.desk.store.find("???"))                           # and "???" is not that record
        self.assertLessEqual(len("snooze7|" + self.desk.store.add("名" * 200, "sourced")["id"]), 75)

    def test_import_survives_ragged_rows_bad_dates_and_excel_encoding(self):
        path = Path(self.tmp.name) / "in.csv"
        path.write_bytes(("name,company,due\n"
                          "Zoë Marsh,Acme, Inc,2026-12-01\n"          # an unquoted comma: one cell too many
                          "Ben Okoro,Fathom,sometime\n"
                          ",Nameless,\n"
                          "Cy Tran,Kestrel,12/05/2026\n").encode("cp1252"))
        dry = self.desk.import_csv(path, dry_run=True)
        self.assertEqual((dry["ok"], dry["added"]), (True, 3))
        out = self.desk.import_csv(path)
        self.assertEqual((out["ok"], out["added"]), (True, 3))
        self.assertEqual(self.desk.store.find("Cy Tran")["next_due"], "2026-12-05")
        self.assertEqual(self.desk.store.find("Ben Okoro")["next_due"], "")
        self.assertEqual(self.desk.store.find("Zoë Marsh")["company"], "Acme")
        problems = " | ".join(out["problems"])
        self.assertIn("more cells", problems)
        self.assertIn("sometime", problems)
        self.assertIn("no name", problems)
        self.assertEqual(self.desk.import_csv(path)["added"], 0)                  # running it again adds nothing

    def test_enrich_none_means_no_button_and_no_paid_lookup(self):
        desk = make_desk(self.tmp.name, secrets={"PDL_API_KEY": "k"}, http=lambda *a, **k: self.fail("a paid lookup was made"))
        source = desk.library.source("recruiting").replace("name: recruiting", "name: quiet-desk")
        self.assertIn("\nenrich: person", source)
        saved = desk.playbook_tool({"action": "save", "yaml": source.replace("\nenrich: person", "\nenrich: none"), "activate": True})
        self.assertTrue(saved["ok"], saved)
        self.assertEqual(desk.playbook().enrich, "none")
        self.assertFalse(desk.can_enrich())
        deal = desk.deal({"action": "add", "name": "Dana Whitfield", "email": "dana@example.com"})["deal"]
        self.assertNotIn("stoa_enrich", json.dumps(desk.blocks(desk.store.get(deal["id"]))))
        self.assertIn("switched off", desk.enrich({"deal": deal["id"]})["error"])
        self.assertIn("switched off", desk.click("enrich", deal["id"], "", "U1")["notice"])

    def test_a_playbook_that_would_break_later_is_refused_at_save(self):
        good = self.desk.library.source("recruiting").replace("name: recruiting", "name: mine")
        for label, broken in (
                ("stale_days words", good.replace("stale_days: 4", 'stale_days: "4 days"', 1)),
                ("stale_days zero", good.replace("stale_days: 4", "stale_days: -3", 1)),
                ("top-level words", good.replace("\nstale_days: 5", "\nstale_days: a week", 1)),
                ("advance true", good.replace("advance: Reach out", "advance: true", 1)),     # a boolean in every YAML
                ("terms list", good.split("\nterms:")[0] + "\nterms: [candidate, role]\n" + "stages:" + good.split("\nstages:")[1]),
                ("drafts text", good.split("\ndrafts:")[0] + "\ndrafts: write something nice\n")):
            self.assertNotEqual(broken, good, label)
            result = self.desk.playbook_tool({"action": "save", "yaml": broken, "activate": True})
            self.assertFalse(result["ok"], label)
            self.assertTrue(result["error"], label)
        self.assertEqual(self.desk.playbook().name, "recruiting")
        self.assertNotIn("mine", self.desk.library.names())
        with self.assertRaises(PlaybookError):
            validate({"name": "x", "stages": ["a", "b"], "lost": {"reasons": "price"}})
        playbook = Playbook({"name": "x", "stages": [{"id": "a", "stale_days": "3"}, "B"], "terms": {"deal": None, "odd": 1}})
        self.assertEqual((playbook.stale_days("a"), playbook.stale_days("b"), playbook.terms["deal"]), (3, 7, "deal"))

    def test_last_touch_for_every_record_in_one_query_matches_one_at_a_time(self):
        fixture.load(self.desk)
        store = self.desk.store
        some = store.list(status="all")[0]
        store.snooze(some["id"], 3)                     # bookkeeping: must not count as a touch
        touches = store.touches()
        for deal in store.list(status="all"):
            self.assertEqual(touches.get(deal["id"], ""), store.last_touch(deal["id"]), deal["id"])
        self.desk.park_followup(some["id"], "write it")
        self.assertEqual(store.meta_prefix("pending:"), {some["id"]: "write it"})


class IntakeTests(unittest.TestCase):
    """A list sent in from anywhere: reviewed first, and only what is approved gets written."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desk = make_desk(self.tmp.name)
        fixture.load(self.desk)
        self.dana = self.desk.store.find("Dana Whitfield")
        self.rows = [
            {"name": "Ada Quill", "company": "Fathom Data", "group": "Staff Backend · Northwind", "email": "ada@fathom.example",
             "Current title": "Staff Engineer", "note": "Met at the meetup", "favourite colour": "green"},
            {"name": "Dana Whitfield", "email": "dana@example.com", "stage": "Submitted", "linkedin": "https://www.linkedin.com/in/danaw/",
             "note": "Resume updated"},
            {"name": "Priya Raman", "email": "priya@example.com"},                                # on the desk, nothing new
            {"name": "ada quill", "company": "Fathom Data", "group": "Staff Backend · Northwind", "email": "ADA@fathom.example"},
            {"company": "Kestrel Labs"},                                                          # a company with no person
            {"email": "nobody@example.com"},                                                      # nothing to call it
            {"name": "Ben Okoro", "next_due": "sometime soon", "stage": "Vibing"},
        ]

    def propose(self, **extra):
        return self.desk.intake.run(dict({"action": "propose", "source": "meetup list", "rows": self.rows}, **extra))

    def test_the_review_says_what_each_row_would_do_and_writes_nothing(self):
        before = (self.desk.store.count(), len(self.desk.store.activities(limit=500)))
        out = self.propose()
        self.assertTrue(out["ok"])
        self.assertEqual([row["kind"] for row in out["rows"]], ["new", "update", "same", "same", "new", "problem", "new"])
        self.assertEqual(out["counts"], {"new": 3, "update": 1, "same": 2, "unclear": 0, "problem": 1, "rows": 7})
        self.assertEqual(out["ignored_columns"], ["favourite colour"])
        changes = {c["field"]: (c["from"], c["to"]) for c in out["rows"][1]["changes"]}
        self.assertEqual(changes["Stage"], ("Screened", "Submitted"))
        self.assertEqual(changes["LinkedIn"], ("", "https://www.linkedin.com/in/danaw/"))
        self.assertIn("Note", changes)
        self.assertNotIn("Email", changes)                                                 # the same email is not a change
        self.assertIn("same as row 1", out["rows"][3]["why"])
        self.assertIn("not understood", " ".join(out["rows"][6]["warnings"]))
        self.assertIn("not in this playbook", " ".join(out["rows"][6]["warnings"]))
        self.assertIn("3 new candidates, 1 to update", out["review"])
        self.assertIn("stage Screened → Submitted", out["review"])
        self.assertEqual((self.desk.store.count(), len(self.desk.store.activities(limit=500))), before)
        self.assertEqual([w["id"] for w in self.desk.intake.waiting()], [out["id"]])

    def test_apply_writes_what_was_reviewed_and_only_once(self):
        out = self.propose()
        done = self.desk.intake.run({"action": "apply"})                                   # only one waiting: no id needed
        self.assertEqual((done["added"], done["updated"], done["left_out"], done["problems"]), (3, 1, 0, []))
        ada = self.desk.store.find("Ada Quill")
        self.assertEqual((ada["company"], ada["email"], ada["stage"], ada["fields"]["title"]),
                         ("Fathom Data", "ada@fathom.example", "sourced", "Staff Engineer"))
        self.assertEqual(ada["source"], "intake: meetup list")
        self.assertIn("Met at the meetup", [a["text"] for a in self.desk.store.activities(ada["id"])])
        dana = self.desk.store.get(self.dana["id"])
        self.assertEqual((dana["stage"], dana["linkedin"]), ("submitted", "https://www.linkedin.com/in/danaw/"))
        self.assertTrue(any("Resume updated" in a["text"] for a in self.desk.store.activities(dana["id"])))
        self.assertEqual(self.desk.store.find("Kestrel Labs")["name"], "Kestrel Labs")     # a company row is its own record
        self.assertEqual(self.desk.store.find("Ben Okoro")["next_due"], "")
        again = self.desk.intake.run({"action": "apply", "id": out["id"]})
        self.assertFalse(again["applied"])
        self.assertIn("3 added, 1 updated", again["note"])
        self.assertEqual(len(self.desk.store.list(status="all", query="Ada Quill")), 1)
        self.assertEqual(self.desk.intake.waiting(), [])
        self.assertEqual(self.desk.intake.view(out["id"])["status"], "applied")

    def test_part_of_a_list_can_be_applied_and_the_rest_left(self):
        out = self.propose()
        done = self.desk.intake.run({"action": "apply", "id": out["id"], "skip": [1, "7"]})
        self.assertEqual((done["added"], done["updated"], done["left_out"]), (1, 1, 2))
        self.assertIsNone(self.desk.store.find("Ada Quill"))
        second = self.propose()
        done = self.desk.intake.run({"action": "apply", "id": second["id"], "rows": [1]})
        self.assertEqual((done["added"], done["updated"]), (1, 0))
        self.assertEqual(self.desk.store.get(self.dana["id"])["stage"], "submitted")

    def test_discard_and_unknown_lists(self):
        out = self.propose()
        self.assertTrue(self.desk.intake.run({"action": "discard", "id": out["id"]})["discarded"])
        self.assertFalse(self.desk.intake.run({"action": "apply", "id": out["id"]})["applied"])
        self.assertIsNone(self.desk.store.find("Ada Quill"))
        self.assertFalse(self.desk.intake.run({"action": "apply", "id": "in-nope"})["ok"])
        self.assertFalse(self.desk.intake.run({"action": "propose", "rows": []})["ok"])
        self.assertFalse(self.desk.intake.run({"action": "propose", "rows": [{"name": "x"}] * 501})["ok"])
        nothing = self.desk.intake.run({"action": "propose", "rows": [{"name": "Priya Raman"}]})
        self.assertIn("Nothing here would change the desk", nothing["note"])
        self.assertEqual(self.desk.intake.waiting(), [])

    def test_the_same_person_in_another_search_is_new_and_two_matches_are_unclear(self):
        self.desk.deal({"action": "add", "name": "Dana Whitfield", "group": "Head of Platform · Lumen", "force": True})
        out = self.desk.intake.run({"action": "propose", "rows": [
            {"name": "Dana Whitfield", "group": "Data Lead · Tidewater"},
            {"name": "Dana Whitfield", "owner": "Jo"},
            {"name": "Dana Whitfield", "owner": "Jo", "record": self.dana["id"]}]})
        first, second, third = out["rows"]
        self.assertEqual((first["kind"], second["kind"], third["kind"]), ("new", "unclear", "update"))
        self.assertIn("also on the desk under", first["why"])
        self.assertIn("matches 2 records", second["why"])
        self.assertEqual(third["deal"], self.dana["id"])

    def test_a_csv_is_read_by_the_desk_and_other_files_are_the_agents_to_read(self):
        path = Path(self.tmp.name) / "leads.csv"
        path.write_bytes("Candidate,Company,Search,Due\nZo\u00eb Marsh,Acme,Staff Backend \u00b7 Northwind,12/05/2026\n,,,\n".encode("cp1252"))
        out = self.desk.intake.run({"action": "propose", "path": str(path)})
        self.assertEqual((out["source"], out["counts"]["new"], out["counts"]["rows"]), ("leads.csv", 1, 1))
        self.desk.intake.run({"action": "apply", "id": out["id"]})
        zoe = self.desk.store.find("Zo\u00eb Marsh")
        self.assertEqual((zoe["grp"], zoe["next_due"]), ("Staff Backend \u00b7 Northwind", "2026-12-05"))
        pdf = Path(self.tmp.name) / "leads.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        self.assertIn("read it yourself", self.desk.intake.run({"action": "propose", "path": str(pdf)})["error"])

    def test_the_pipeline_written_to_a_sheet_and_read_back_changes_nothing(self):
        table = self.desk.export({"action": "table"})
        self.assertEqual(table["columns"][:5], ["ID", "Candidate", "Current company", "Search", "Stage"])   # the playbook's words
        self.assertEqual(table["count"], self.desk.store.count())
        self.assertIn("Current title", table["columns"])
        back = self.desk.intake.run({"action": "propose", "source": "Google Sheet", "columns": table["columns"], "rows": table["rows"]})
        self.assertEqual((back["counts"]["same"], back["counts"]["new"], back["counts"]["update"]), (table["count"], 0, 0))
        self.assertNotIn("ignored_columns", back)                                          # "Updated" is the desk's own column
        self.assertIn("Nothing here would change the desk", back["note"])

    def test_edits_made_in_the_sheet_come_back_as_exactly_those_edits(self):
        table = self.desk.export({"action": "table"})
        columns, rows = table["columns"], [list(row) for row in table["rows"]]
        at = {name: index for index, name in enumerate(columns)}
        dana = next(row for row in rows if row[at["ID"]] == self.dana["id"])
        dana[at["Stage"]] = "Submitted"                                                    # someone moved her in the Sheet
        dana[at["Candidate"]] = "Dana Whitfield-Okafor"                                    # and corrected her name
        other = next(row for row in rows if row[at["ID"]] != self.dana["id"])
        other[at["Fee"]] = "$61k"
        other[at["Due"]] = "12/05/2026"
        rows.append(["", "Ada Quill", "Fathom Data", "Staff Backend · Northwind", "Contacted"] + [""] * (len(columns) - 5))
        rows.append([""] * len(columns))                                                   # an empty line at the bottom
        back = self.desk.intake.run({"action": "propose", "source": "Google Sheet", "columns": columns, "rows": rows})
        self.assertEqual((back["counts"]["update"], back["counts"]["new"], back["counts"]["problem"]), (2, 1, 0))
        changed = {row["deal"]: {c["field"]: c["to"] for c in row["changes"]} for row in back["rows"] if row["kind"] == "update"}
        self.assertEqual(changed[self.dana["id"]], {"Candidate": "Dana Whitfield-Okafor", "Stage": "Submitted"})
        self.assertEqual(changed[other[at["ID"]]], {"Fee": "$61k", "Due": "2026-12-05"})
        self.desk.intake.run({"action": "apply", "id": back["id"]})
        dana_now = self.desk.store.get(self.dana["id"])
        self.assertEqual((dana_now["name"], dana_now["stage"]), ("Dana Whitfield-Okafor", "submitted"))
        self.assertEqual(self.desk.store.get(other[at["ID"]])["value"], 61000.0)
        self.assertEqual(self.desk.store.find("Ada Quill")["stage"], "contacted")
        again = self.desk.export({"action": "table"})                                      # mirror again: the Sheet and the desk agree
        settled = self.desk.intake.run({"action": "propose", "columns": again["columns"], "rows": again["rows"]})
        self.assertEqual((settled["counts"]["update"], settled["counts"]["new"]), (0, 0))

    def test_two_people_pressing_apply_write_the_list_once(self):
        out = self.propose()
        results = []
        workers = [threading.Thread(target=lambda: results.append(self.desk.intake.apply({"id": out["id"]}, "u"))) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(len([r for r in results if r.get("applied")]), 1)
        self.assertEqual(len(self.desk.store.list(status="all", query="Ada Quill")), 1)

    def test_in_the_app_the_review_is_drawn_in_the_conversation(self):
        self.desk.desktop_seen = time.time()
        out = self.desk.intake.run({"action": "propose", "source": "x", "rows": self.rows}, {"platform": "desktop"})
        self.assertEqual(out["directive"], '::stoa-intake{id="%s"}' % out["id"])
        self.assertNotIn("rows", out)
        self.assertNotIn("review", out)


class FakeCtx:
    """The part of Hermes's plugin context that ``register`` uses, recording what it is given."""

    def __init__(self, settings):
        self.settings = settings
        self.tools, self.sections, self.commands, self.blueprints, self.skills = {}, {}, {}, [], []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.settings[key] = value

    def inject_message(self, *args, **kwargs):
        return False

    def register_tool(self, name, toolset, schema, handler, emoji=""):
        self.tools[name] = handler

    def register_system_prompt_section(self, name, fn, max_chars=0):
        self.sections[name] = (fn, max_chars)

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler

    def register_automation_blueprint(self, name, **kwargs):
        self.blueprints.append(name)

    def register_skill(self, name, path):
        self.skills.append(name)

    def register_platform_handler(self, *args, **kwargs): pass
    def register_slack_action_handler(self, *args, **kwargs): pass
    def register_cli_command(self, *args, **kwargs): pass


class RegisterTests(unittest.TestCase):
    """``register`` against a stand-in for Hermes: what gets wired, and where the desk shows itself."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = Path(self.tmp.name) / "data"
        data.mkdir()
        fakes = {"hermes_constants": types.SimpleNamespace(get_hermes_home=lambda: self.tmp.name),
                 "plugins": types.ModuleType("plugins"),
                 "plugins.plugin_storage": types.SimpleNamespace(plugin_data_dir=lambda name: data)}
        patcher = mock.patch.dict(sys.modules, fakes)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = FakeCtx({"playbook": "recruiting", "channels": ["C1"]})
        stoa.register(self.ctx)

    def session(self, **values):
        patcher = mock.patch.object(stoa, "_session", lambda: values)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_everything_the_manifest_promises_is_registered(self):
        self.assertEqual(sorted(self.ctx.tools), ["gtm_card", "gtm_deal", "gtm_enrich", "gtm_export", "gtm_intake", "gtm_market",
                                                  "gtm_notes", "gtm_pipeline", "gtm_playbook"])
        self.assertEqual(sorted(self.ctx.blueprints), ["activity-sweep", "friday-review", "market-watch", "morning-brief",
                                                       "notes-sweep", "sheet-mirror", "sheet-pull"])
        self.assertEqual(sorted(self.ctx.skills), ["desk", "market-map", "playbook-authoring"])
        self.assertIn("gtm", self.ctx.commands)
        self.assertEqual(stoa.LIVE_DESKS[-1].home, Path(self.tmp.name))

    def test_a_tool_that_changes_the_desk_tells_an_open_window(self):
        told = []
        stoa.LIVE_DESKS[-1].announce = lambda: told.append(1)
        self.session(platform="slack", chat_id="C1")
        self.assertTrue(json.loads(self.ctx.tools["gtm_deal"]({"action": "add", "name": "Ada Quill"}))["ok"])
        self.assertEqual(len(told), 1)
        self.ctx.tools["gtm_pipeline"]({})                                                 # reading changes nothing
        self.assertEqual(len(told), 1)
        self.session(platform="slack", chat_id="C2")                                       # a chat the desk is not in
        self.ctx.tools["gtm_deal"]({"action": "add", "name": "Nobody"})
        self.assertEqual(len(told), 1)

    def test_the_desk_stays_out_of_chats_it_is_not_allowed_in(self):
        section, limit = self.ctx.sections["stoa.desk"]
        self.session(platform="slack", chat_id="C2")
        for info in (None, {}, {"platform": ""}, {"platform": "slack"}):       # the host may or may not say where
            self.assertEqual(section(info), "", info)
        self.assertIn("not available", json.loads(self.ctx.tools["gtm_pipeline"]({}))["error"])
        self.assertIn("not available", self.ctx.commands["gtm"](""))

    def test_the_desk_introduces_itself_where_it_is_allowed(self):
        section, limit = self.ctx.sections["stoa.desk"]
        self.session(platform="slack", chat_id="C1")
        text = section({})
        self.assertIn("Stoa GTM desk", text)
        self.assertIn("before saying it cannot be found", text)        # an agent with other places to look still checks here
        self.assertLessEqual(len(text), limit)
        for schema in (schemas.GTM_PIPELINE, schemas.GTM_DEAL, schemas.GTM_CARD, schemas.GTM_MARKET, schemas.GTM_ENRICH,
                       schemas.GTM_NOTES, schemas.GTM_PLAYBOOK, schemas.GTM_EXPORT, schemas.GTM_INTAKE):
            first = " ".join(schema["description"].split()).split(". ")[0]
            self.assertLessEqual(len(first), 79, schema["name"])       # Hermes lists a deferred tool by this one line, cut at 80
            self.assertTrue("Stoa desk" in first or "People Data Labs" in first, schema["name"])
        self.assertTrue(json.loads(self.ctx.tools["gtm_pipeline"]({}))["ok"])
        self.assertIn("Stoa GTM desk", section({"platform": "cli"}))


class DashboardApiTests(unittest.TestCase):
    """The dashboard tab's backend, called directly (no web server needed)."""

    def setUp(self):
        try:
            import fastapi  # noqa: F401
        except ImportError:
            self.skipTest("fastapi is not installed here; Hermes provides it")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        spec = importlib.util.spec_from_file_location("stoa_dash_api_test", ROOT / "dashboard" / "plugin_api.py")
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        self.desk = make_desk(self.tmp.name)
        self.api.desk = lambda: self.desk
        fixture.load(self.desk)

    def test_the_page_can_add_edit_move_and_log_without_a_model(self):
        added = self.api.add_deal({"name": "Nadia Rahim", "company": "Larkspur", "group": "Staff Backend · Northwind", "value": 41000,
                                   "next_step": "Intro call", "next_due": "2030-01-07"})
        nadia = added["deal"]["id"]
        self.assertEqual((added["ok"], added["deal"]["stage"]), (True, "sourced"))
        self.assertTrue(self.api.add_deal({"name": "Nadia Rahim", "group": "Staff Backend · Northwind"})["exists"])    # no second copy
        self.assertEqual(self.api.act(nadia, {"verb": "update", "next_step": "Send the brief", "owner": "Maya", "value": 45000}), {"ok": True})
        self.assertEqual(self.api.act(nadia, {"verb": "move", "stage": "screened"}), {"ok": True})
        self.assertEqual(self.api.act(nadia, {"verb": "log", "kind": "call", "text": "Spoke for ten minutes; keen."}), {"ok": True})
        record = self.desk.store.get(nadia)
        self.assertEqual((record["stage"], record["next_step"], record["owner"], record["value"]), ("screened", "Send the brief", "Maya", 45000))
        self.assertIn("Spoke for ten minutes", [a["text"] for a in self.desk.store.activities(nadia)][0])
        self.assertEqual({a["actor"] for a in self.desk.store.activities(nadia, limit=20)}, {"dashboard"})    # typed by a person, not the agent
        with self.assertRaises(self.api.HTTPException):
            self.api.act(nadia, {"verb": "move", "stage": "no-such-stage"})

    def test_a_csv_dropped_on_the_page_becomes_a_review_without_a_model(self):
        before = self.desk.store.count()
        text = ("Name,Company,Search,Email,Stage\n"
                "Ada Quill,Fathom Data,Staff Backend · Northwind,ada.quill@fathomdata.example,\n"
                "Dana Whitfield,Kestrel Labs,,,Submitted\n"
                ",,,,\n")
        held = self.api.intake_file({"name": "badge scans.csv", "text": text})
        self.assertEqual((held["ok"], held["waiting"], held["counts"]["new"], held["counts"]["update"]), (True, True, 1, 1))
        self.assertIn("badge scans.csv", held["headline"])
        self.assertEqual(self.desk.store.count(), before)                                    # held for approval, nothing written
        self.assertEqual([item["id"] for item in self.api.state()["intakes"]], [held["id"]])
        applied = self.api.intake_act(held["id"], "apply", {})
        self.assertEqual((applied["added"], applied["updated"]), (1, 1))
        self.assertEqual(self.desk.store.find("Dana Whitfield")["stage"], "submitted")
        tabbed = self.api.intake_file({"name": "people.tsv", "text": "Name\tCompany\nBen Okoro\tHarbor Systems\n"})
        self.assertEqual(tabbed["counts"]["new"], 1)
        again = self.api.intake_file({"name": "again.csv", "text": "Name,Company\nAda Quill,Fathom Data\n"})
        self.assertEqual((again["waiting"], again["headline"]), (False, "Nothing in again.csv would change the desk."))
        with self.assertRaises(self.api.HTTPException):
            self.api.intake_file({"name": "empty.csv", "text": "Name,Company\n"})

    def test_a_click_in_the_app_saves_and_returns_the_line_to_send_into_chat(self):
        dana = self.desk.store.find("Dana Whitfield")["id"]
        told = []
        self.desk.announce = lambda: told.append(1)
        drafted = self.api.act(dana, {"verb": "draft"})
        self.assertEqual((drafted["ok"], drafted["parked"]), (True, True))
        self.assertIn("Write the follow-up for Dana Whitfield", drafted["ask"])
        self.assertIn(dana, drafted["ask"])
        self.assertIn("pending_task", self.desk.deal({"action": "get", "deal": dana}))     # the task itself waits on the record
        moved = self.api.act(dana, {"verb": "advance", "extra": "screened"})
        self.assertIn("Dana Whitfield is now at Submitted", moved["ask"])
        self.assertEqual(self.api.act(dana, {"verb": "snooze7"})["ask"], "")               # nothing to write: nothing to say
        target = self.desk.markets.target("Meridian Freight")
        added = self.api.target(target["id"], "track")
        self.assertIn("added to the Stoa desk from the market map", added["ask"])
        self.assertIn(added["deal"], added["ask"])
        self.assertEqual(len(told), 4)                                                     # every change reaches an open window

    def test_cards_and_maps_are_drawn_in_the_conversation_only_while_the_app_is_open(self):
        in_app = {"platform": "desktop", "chat_id": ""}
        dana = self.desk.store.find("Dana Whitfield")["id"]
        self.assertIn("card", self.desk.card({"deal": "dana"}, in_app))                    # nobody has the app open: text
        self.assertTrue(self.api.hello()["ok"])                                            # the app's half checks in
        shown = self.desk.card({"deal": "dana"}, in_app)
        self.assertEqual(shown["directive"], '::stoa-card{id="%s"}' % dana)
        self.assertNotIn("card", shown)
        posted = self.desk.markets.post({"market": ""}, in_app)
        self.assertEqual(posted["directive"], '::stoa-map{market="regional-freight-brokers-demo"}')
        for elsewhere in ({"platform": "cli"}, {"platform": "telegram", "chat_id": "T1"}, {"platform": "slack", "chat_id": "C1"}, {}):
            self.assertNotIn("directive", self.desk.card({"deal": "dana"}, elsewhere), elsewhere)
        self.desk.desktop_seen = time.time() - 600                                         # the window was closed a while ago
        self.assertIn("card", self.desk.card({"deal": "dana"}, in_app))

    def test_the_app_reads_a_waiting_list_and_applies_the_ticked_rows(self):
        out = self.desk.intake.run({"action": "propose", "source": "leads.pdf", "rows": [
            {"name": "Ada Quill", "company": "Fathom Data"}, {"name": "Ben Okoro"}, {"name": "Priya Raman"}]})
        self.assertEqual([w["id"] for w in self.api.state()["intakes"]], [out["id"]])
        view = json.loads(json.dumps(self.api.intake(out["id"])))
        self.assertEqual([(row["n"], row["kind"]) for row in view["rows"]], [(1, "new"), (2, "new"), (3, "same")])
        self.assertNotIn("row", view["rows"][0])                                            # raw values stay in the ledger
        done = self.api.intake_act(out["id"], "apply", {"rows": [2]})
        self.assertEqual((done["added"], done["left_out"]), (1, 1))
        self.assertIsNone(self.desk.store.find("Ada Quill"))
        self.assertEqual(self.api.intake(out["id"])["status"], "applied")
        self.assertEqual(self.api.state()["intakes"], [])

    def test_state_costs_the_same_however_many_records_there_are(self):
        self.desk.store.last_touch = lambda deal_id: self.fail("asked for one record's last touch during /state")
        self.desk.store.meta_get = lambda key, default="": self.fail("asked for one record's pending task during /state")
        self.desk.park_followup(self.desk.store.find("Dana Whitfield")["id"], "write it")
        state = self.api.state()
        waiting = [d["name"] for d in state["deals"] if d["pending"]]
        self.assertEqual(waiting, ["Dana Whitfield"])

    def test_records_from_another_playbook_stay_visible(self):
        self.desk.playbook_tool({"action": "use", "name": "b2b-sales"})
        state = json.loads(json.dumps(self.api.state()))
        known = {stage["id"] for stage in state["playbook"]["stages"]}
        strays = [d for d in state["deals"] if d["status"] == "open" and d["stage"] not in known]
        self.assertTrue(strays)
        self.assertTrue(all(d["position"] == -1 and d["advance"] == "" for d in strays))
        self.assertEqual(state["totals"]["open"], len([d for d in state["deals"] if d["status"] == "open"]))

    def test_state_is_complete_and_serialisable(self):
        state = json.loads(json.dumps(self.api.state()))
        self.assertEqual(state["playbook"]["name"], "recruiting")
        self.assertEqual((state["totals"]["open"], state["totals"]["mapped"], state["totals"]["tracked"]), (7, 7, 0))
        self.assertEqual((state["totals"]["due"], state["totals"]["stale"]), (4, 1))
        self.assertEqual(len(state["markets"][0]["segments"]), 3)
        dana = [d for d in state["deals"] if d["name"] == "Dana Whitfield"][0]
        self.assertEqual((dana["position"], dana["advance"], dana["pending"]), (2, "Submit to client", False))
        self.assertTrue(state["activity"])
        self.assertEqual(state["flow"]["total"], 7)

    def test_advance_parks_the_draft_and_is_idempotent(self):
        dana = self.desk.store.find("Dana Whitfield")
        result = self.api.act(dana["id"], {"verb": "advance", "extra": "screened"})
        self.assertEqual((result["ok"], result["parked"]), (True, True))
        detail = self.api.deal(dana["id"])
        self.assertEqual(detail["stage"], "submitted")
        self.assertIn("submission note", detail["pending_task"])
        again = self.api.act(dana["id"], {"verb": "advance", "extra": "screened"})
        self.assertIn("Already moved", again["notice"])
        self.assertEqual(self.desk.store.get(dana["id"])["stage"], "submitted")

    def test_log_update_close_and_bad_requests(self):
        from fastapi import HTTPException
        dana = self.desk.store.find("Dana Whitfield")
        self.api.act(dana["id"], {"verb": "log", "kind": "call", "text": "Spoke for ten minutes."})
        self.api.act(dana["id"], {"verb": "update", "next_step": "Send two roles", "next_due": "2030-01-01"})
        self.api.act(dana["id"], {"verb": "lost", "extra": "0"})
        detail = self.api.deal(dana["id"])
        self.assertEqual((detail["status"], detail["lost_reason"], detail["next_step"]), ("lost", "Not a fit", "Send two roles"))
        self.assertIn("Spoke for ten minutes.", [a["text"] for a in detail["activity"]])
        self.api.act(dana["id"], {"verb": "reopen"})
        self.assertEqual(self.desk.store.get(dana["id"])["status"], "open")
        for call in (lambda: self.api.act("ghost", {"verb": "advance"}), lambda: self.api.act(dana["id"], {"verb": "explode"}),
                     lambda: self.api.deal("ghost"), lambda: self.api.add_deal({"company": "No name"})):
            with self.assertRaises(HTTPException):
                call()

    def test_add_and_market_actions(self):
        added = self.api.add_deal({"name": "Ada Park", "company": "Orbit", "stage": "contacted"})
        self.assertEqual(self.desk.store.get(added["deal"]["id"])["stage"], "contacted")
        target = self.desk.markets.target("Brightside Freight")
        tracked = self.api.target(target["id"], "track", None)
        self.assertTrue(tracked["ok"])
        self.assertIn("likely buyer", self.api.deal(tracked["deal"])["pending_task"])
        self.assertFalse(self.api.target(target["id"], "track", None)["ok"])
        other = self.desk.markets.target("Quill Cargo")
        self.assertEqual(self.api.target(other["id"], "dismiss", {"reason": "Builds in-house"})["dismissed"], ["Quill Cargo"])
        market = self.api.state()["markets"][0]
        self.assertEqual((market["rates"][0]["rate"], market["holds"][0]["name"], market["first_calls"][0]["company"]),
                         (100, "Spreadsheets", "Brightside Freight"))
        page = self.api.market_page(market["id"], 1)
        self.assertEqual(page["filename"], "regional-freight-brokers-demo-share.html")
        self.assertIn("Meridian Freight", page["html"])
        self.assertNotIn("Renata Okafor", page["html"])
        counts = self.api.state()["markets"][0]["counts"]
        self.assertEqual((counts["mapped"], counts["tracked"], counts["dismissed"]), (6, 1, 1))


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
        fixture.load(self.desk)
        self.injected = []
        self.accept = True
        self.accept_session = True

        def inject(content, origin=None, session_key=None):
            self.injected.append((content, origin or session_key))
            if session_key is not None:
                return self.accept_session
            return self.accept
        self.slack = SlackDesk(self.desk, inject)
        self.slack.grace = 0.05
        self.slack.settle = 0.02
        self.desk.slack = self.slack
        self.app = FakeApp()
        self.slack.wire(self.app, None)
        self.dana = self.desk.store.find("Dana Whitfield")
        self.acks = 0

    async def asyncTearDown(self):
        # card redraws and hand-off watchers run on after a click returns: let them finish before the ledger's folder goes
        others = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        if others:
            await asyncio.wait(others, timeout=5)

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

    async def test_the_command_posts_the_real_card_in_slack_with_no_model(self):
        here = {"platform": "slack", "chat_id": "D1", "thread_id": "1690000000.000100", "session_key": "sess-1"}
        reply = await asyncio.to_thread(self.desk.slash, "dana whitfield", here)
        self.assertEqual(reply, "")                                    # the card is the reply
        post = self.app.client.posts[-1]
        self.assertEqual((post["channel"], post["thread_ts"]), ("D1", "1690000000.000100"))
        self.assertIn("stoa_advance", json.dumps(post["blocks"]))
        self.assertEqual(self.desk.store.card_session("D1", "1700000000.000001"), "")     # a command has no session
        reply = await asyncio.to_thread(self.desk.slash, "map", here)
        self.assertEqual(reply, "")
        self.assertIn("stoa_track", json.dumps(self.app.client.posts[-1]["blocks"]))
        self.assertEqual(len(self.app.client.posts), 2)
        self.assertIn("no record matches", await asyncio.to_thread(self.desk.slash, "nobody at all", here))
        self.assertIn("!gtm", self.desk.slash("help", here))

    async def test_the_command_is_text_everywhere_else(self):
        self.assertIn("Dana Whitfield", self.desk.slash("dana whitfield"))                         # a terminal
        self.assertIn("Dana Whitfield", self.desk.slash("dana whitfield", {"platform": "telegram", "chat_id": "T1"}))
        self.slack.app = None                                                                      # Slack not connected
        self.assertIn("Dana Whitfield", self.desk.slash("dana whitfield", {"platform": "slack", "chat_id": "D1"}))
        self.assertIn("Regional freight brokers", self.desk.slash("map", {"platform": "slack", "chat_id": "D1"}))
        self.assertEqual(self.app.client.posts, [])

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
        # the agent's turn opens the record, which hands it the task and clears it: no hint is needed
        self.assertIn("submission note", self.desk.deal({"action": "get", "deal": self.dana["id"]})["pending_task"])
        await asyncio.sleep(0.2)
        self.assertEqual(self.app.client.posts, [])

    async def test_a_hand_off_hermes_accepts_and_then_drops_still_reaches_the_person(self):
        # Hermes answers yes before it has routed the turn. Here it says yes and no turn ever starts.
        await self.slack.on_action(self.ack, self.body(thread_ts="1699999999.000001"),
                                   {"action_id": "stoa_advance", "value": self.dana["id"] + "|screened"})
        self.assertEqual(len(self.injected), 1)
        self.assertEqual(self.app.client.posts, [])                        # nothing yet: the turn may be starting
        await asyncio.sleep(0.3)
        hint = self.app.client.posts[-1]
        self.assertEqual(hint["thread_ts"], "1699999999.000001")
        self.assertIn("Saved Dana Whitfield. Reply here", hint["text"])
        self.assertEqual(len(self.app.client.posts), 1)
        self.assertIn("Drafting", self.card_text(0))                         # what the card said at first
        self.assertIn("Draft waiting", self.card_text())                     # and what it says once no turn came
        self.assertIn("submission note", self.desk.deal({"action": "get", "deal": self.dana["id"]})["pending_task"])

    def card_text(self, index=-1):
        return json.dumps(self.app.client.updates[index]["blocks"], ensure_ascii=False)

    async def test_the_card_says_drafting_until_the_draft_has_landed(self):
        click = {"action_id": "stoa_draft", "value": self.dana["id"]}
        await self.slack.on_action(self.ack, self.body(), click)
        self.assertEqual(len(self.app.client.updates), 1)                   # one redraw, already carrying the line
        self.assertIn("Drafting", self.card_text())
        self.assertNotIn("Draft waiting", self.card_text())
        self.assertEqual(self.card_text().count("stoa_draft"), 1)           # the line sits with the buttons, not instead of them
        self.desk.deal({"action": "get", "deal": self.dana["id"]})          # the agent's turn opens the record
        await asyncio.sleep(0.2)
        self.assertNotIn("Drafting", self.card_text())                      # the draft has had time to land
        self.assertNotIn("Draft waiting", self.card_text())
        self.assertEqual(self.app.client.posts, [])
        self.assertEqual(self.desk.handoff(self.dana["id"]), "")

    async def test_a_second_press_of_the_same_button_does_not_start_a_second_draft(self):
        self.slack.settle = 0.5                                             # the draft takes a moment to land
        click = {"action_id": "stoa_draft", "value": self.dana["id"]}
        await self.slack.on_action(self.ack, self.body(), click)
        await self.slack.on_action(self.ack, self.body(), click)            # impatient: nothing has arrived yet
        self.desk.deal({"action": "get", "deal": self.dana["id"]})          # the agent picks the task up...
        await self.slack.on_action(self.ack, self.body(), click)            # ...and is still writing
        self.assertEqual(len(self.injected), 1)
        self.assertEqual(self.app.client.posts, [])
        # a different piece of writing for the same record is not swallowed
        await self.slack.on_action(self.ack, self.body(), {"action_id": "stoa_advance", "value": self.dana["id"] + "|screened"})
        self.assertEqual(len(self.injected), 2)
        self.assertIn("submission note", self.injected[1][0])
        self.desk.deal({"action": "get", "deal": self.dana["id"]})
        await asyncio.sleep(1.0)
        self.assertEqual(self.desk.handoff(self.dana["id"]), "")
        await self.slack.on_action(self.ack, self.body(), click)            # once that draft is in, asking again works
        self.assertEqual(len(self.injected), 3)

    async def test_the_card_says_draft_waiting_until_someone_replies(self):
        self.accept = False
        await self.slack.on_action(self.ack, self.body(thread_ts="1699999999.000001"),
                                   {"action_id": "stoa_draft", "value": self.dana["id"]})
        self.assertIn("Draft waiting", self.card_text())
        self.assertIn("Saved Dana Whitfield. Reply here", self.app.client.posts[-1]["text"])
        before = len(self.app.client.updates)
        got = self.desk.deal({"action": "get", "deal": self.dana["id"]})    # the reply's turn opens the record
        self.assertIn("pending_task", got)
        await asyncio.sleep(0.1)
        self.assertGreater(len(self.app.client.updates), before)            # the card is redrawn without the line
        self.assertNotIn("Draft waiting", self.card_text())
        self.assertNotIn("Drafting", self.card_text())

    async def test_a_stale_drafting_line_does_not_outlive_a_restart(self):
        self.desk.park_followup(self.dana["id"], "write it")
        self.desk.store.meta_set("handoff:" + self.dana["id"], "drafting|%d|abc" % (time.time() - 600))
        self.assertEqual(self.desk.handoff(self.dana["id"]), "waiting")     # the watcher died with the old process
        self.assertFalse(self.desk.handoff_same(self.dana["id"], "write it"))
        self.desk.store.delete(self.dana["id"])                             # and a deleted record leaves nothing behind
        self.assertEqual(self.desk.store.meta_prefix("pending:"), {})
        self.assertEqual(self.desk.store.meta_prefix("handoff:"), {})

    async def test_an_intake_review_in_slack_applies_on_one_press(self):
        here = {"platform": "slack", "chat_id": "C1", "thread_id": "1690000000.000100"}
        rows = [{"name": "Ada Quill", "company": "Fathom Data"}, {"name": "Dana Whitfield", "owner": "Jo"}]
        out = await asyncio.to_thread(self.desk.intake.run, {"action": "propose", "source": "badge scans", "rows": rows}, here)
        self.assertTrue(out["posted"])
        post = self.app.client.posts[-1]
        self.assertEqual((post["channel"], post["thread_ts"]), ("C1", "1690000000.000100"))
        text = json.dumps(post["blocks"], ensure_ascii=False)
        self.assertIn("Ada Quill", text)
        self.assertIn("owner you → Jo", text)
        self.assertIn("Apply 2", text)
        self.assertIsNone(self.desk.store.find("Ada Quill"))                                # nothing written yet
        body = self.body(ts="1700000000.000001", thread_ts="1690000000.000100")
        await self.slack.on_action(self.ack, body, {"action_id": "stoa_intake", "value": "apply|" + out["id"]})
        self.assertEqual(self.desk.store.find("Ada Quill")["company"], "Fathom Data")
        self.assertEqual(self.desk.store.get(self.dana["id"])["owner"], "Jo")
        receipt = json.dumps(self.app.client.updates[-1]["blocks"], ensure_ascii=False)
        self.assertIn("1 added, 1 updated", receipt)
        self.assertNotIn("stoa_intake", receipt)                                            # the buttons are gone
        await self.slack.on_action(self.ack, body, {"action_id": "stoa_intake", "value": "apply|" + out["id"]})
        self.assertEqual(len(self.desk.store.list(status="all", query="Ada Quill")), 1)
        self.assertTrue(ACTIONS.match("stoa_intake") and ACTIONS.match("stoa_intake_discard"))

    async def test_a_card_from_the_command_does_not_claim_a_session(self):
        here = {"platform": "slack", "chat_id": "D1", "thread_id": "1690000000.000100", "session_key": "no-such-session"}
        await asyncio.to_thread(self.desk.slash, "dana whitfield", here)
        self.assertEqual(self.desk.store.card_session("D1", "1700000000.000001"), "")
        self.accept = False                                                 # nobody has spoken to the agent in this thread
        await self.slack.on_action(self.ack, self.body(ts="1700000000.000001", thread_ts="1690000000.000100"),
                                   {"action_id": "stoa_advance", "value": self.dana["id"] + "|screened"})
        self.assertEqual([target for _content, target in self.injected if isinstance(target, str)], [])   # no session tried
        self.assertIn("Reply here", self.app.client.posts[-1]["text"])      # told at once, not after the grace period

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

    async def test_map_post_and_add_to_pipeline_click(self):
        session = {"platform": "slack", "chat_id": "C1", "thread_id": "1690000000.000100", "session_key": "ses_map"}
        posted = await asyncio.to_thread(self.desk.market, {"action": "post"}, session)
        self.assertTrue(posted["posted"])
        post = self.app.client.posts[0]
        self.assertEqual((post["thread_ts"], post["text"]), ("1690000000.000100", "Market map"))
        button = [b["accessory"] for b in post["blocks"] if b.get("accessory")][0]
        self.assertEqual(button["text"]["text"], "Add to pipeline")
        body = self.body(ts="1700000000.000001", thread_ts="1690000000.000100")
        await self.slack.on_action(self.ack, body, {"action_id": "stoa_track", "value": button["value"]})
        deal = [d for d in self.desk.store.list() if d["source"].startswith("market map")][0]
        self.assertEqual((deal["stage"], deal["company"]), ("sourced", "Meridian Freight"))
        self.assertIn("in the pipeline", json.dumps(self.app.client.updates[-1]["blocks"]))
        content, target = self.injected[-1]
        self.assertEqual(target, "ses_map")
        self.assertIn("Renata Okafor as the likely buyer", content)
        self.assertEqual((deal["name"], deal["fields"].get("title")), ("Renata Okafor", "Director of Carrier Compliance"))
        self.assertIn(deal["id"], content)
        await self.slack.on_action(self.ack, body, {"action_id": "stoa_track", "value": button["value"]})
        self.assertIn("already in the pipeline", self.app.client.posts[-1]["text"])
        self.assertEqual(len([d for d in self.desk.store.list() if d["source"].startswith("market map")]), 1)

    async def test_map_click_parks_the_task_when_refused(self):
        self.accept = False
        target = self.desk.markets.target("Brightside Freight")
        await self.slack.on_action(self.ack, self.body(), {"action_id": "stoa_track", "value": target["id"]})
        deal = self.desk.store.find("Brightside Freight")
        self.assertIn("Reply here", self.app.client.posts[-1]["text"])
        self.assertIn("likely buyer", self.desk.deal({"action": "get", "deal": deal["id"]})["pending_task"])

    async def test_bad_clicks_never_raise(self):
        await self.slack.on_action(self.ack, {}, {"action_id": "stoa_advance", "value": ""})
        await self.slack.on_action(self.ack, self.body(thread_ts="1690000000.000100"), {"action_id": "stoa_advance", "value": "ghost|x"})
        self.assertIn("no longer", self.app.client.posts[-1]["text"])
        self.assertEqual(self.app.client.posts[-1]["thread_ts"], "1690000000.000100")   # the thread's parent, never a reply
        self.assertEqual(self.acks, 2)

    async def test_not_connected_falls_back_to_text(self):
        self.slack.app = None
        result = await asyncio.to_thread(self.desk.card, {"deal": "dana"}, {"platform": "slack", "chat_id": "C1"})
        self.assertFalse(result["posted"])
        self.assertIn("card", result)


if __name__ == "__main__":
    unittest.main()
