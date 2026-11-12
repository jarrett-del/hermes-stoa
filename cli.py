"""``hermes stoa``: the desk from a terminal. Status, sample data, CSV in and out."""
from __future__ import annotations

from pathlib import Path

from . import cards, demo
from . import store as st


def setup(subparser) -> None:
    subs = subparser.add_subparsers(dest="stoa_command")
    subs.add_parser("status", help="Playbook, record counts, and which integrations are connected")
    subs.add_parser("board", help="Print the pipeline board")
    subs.add_parser("brief", help="Print what needs attention today")
    subs.add_parser("playbooks", help="List playbooks")
    market = subs.add_parser("map", help="Print a market map (or list the maps)")
    market.add_argument("market", nargs="*", default=[])
    market.add_argument("--export", action="store_true", help="Write the map as a web page and print its path")
    market.add_argument("--share", action="store_true", help="With --export: leave out buyers and pipeline status")
    use = subs.add_parser("use", help="Switch the active playbook")
    use.add_argument("name")
    history = subs.add_parser("history", help="The ledger: every change, what it replaced and what it left (for one record, or the whole desk)")
    history.add_argument("record", nargs="*", default=[])
    history.add_argument("--limit", type=int, default=30)
    auto = subs.add_parser("autonomy", help="What a scheduled job may change by itself: ask, routine or full (no level: show it)")
    auto.add_argument("level", nargs="?", default="", choices=["", "ask", "routine", "full"])
    sample = subs.add_parser("demo", help="Take a tour: show a worked example in place of your own desk, which is left untouched")
    sample.add_argument("which", nargs="?", default="", choices=["", "recruiting", "saas"],
                        help="recruiting: an agency with about $1.2M in open fees. saas: a company selling a fraud prevention "
                             "tool, about $2.5M of pipeline. Left out: the one that fits the active playbook")
    sample.add_argument("--leave", dest="leave", action="store_true", help="Leave the tour: back to your own desk. Nothing in it is touched")
    sample.add_argument("--remove-samples", "--clear", dest="clear", action="store_true",
                        help="Remove the sample records a version before 0.10 put in your own desk (and leave the tour, if one is on)")
    export = subs.add_parser("export", help="Write the pipeline to a CSV file in the plugin's exports folder")
    export.add_argument("path", nargs="?", default="", metavar="name", help="A file name (optional). It is written to the exports folder.")
    export.add_argument("--group", default="", help="Only one group (a search, an account) by name, or everything that is for one client")
    export.add_argument("--share", action="store_true", help="The copy to hand someone outside: without value, owner, next steps, dates, ids or sources")
    imp = subs.add_parser("import", help="Add records from a CSV file")
    imp.add_argument("path")
    imp.add_argument("--dry-run", action="store_true")


def run(desk, args) -> None:
    command = getattr(args, "stoa_command", None)
    if command in (None, "status"):
        playbook = desk.playbook()
        deals = desk.store.list(status="all")
        open_deals = [d for d in deals if d["status"] == "open"]
        print("Stoa desk")
        tour = desk.tour()
        if tour:
            print("  ON TOUR       sample data (%s); your own desk is untouched. Leave with: hermes stoa demo --leave" % tour["example"])
        print("  playbook      %s (%s)" % (playbook.name, playbook.title))
        print("  records       %d open, %d closed" % (len(open_deals), len(deals) - len(open_deals)))
        value = st.total(open_deals)
        if value:
            print("  open value    %s" % cards.money(value, playbook.currency))
        print("  data          %s" % (desk.data_dir / (tour["file"] if tour else "desk.db")))
        print("  enrichment    %s" % ("People Data Labs connected" if desk.can_enrich() else "off (set PDL_API_KEY)"))
        notes_dir = desk.setting("notes_dir", "")
        print("  notes folder  %s" % (notes_dir or "not set (notes_dir)"))
        print("  Granola       %s" % ("connected" if desk.secret("GRANOLA_API_KEY") else "off (set GRANOLA_API_KEY)"))
    elif command == "board":
        print(desk.slash("board"))
    elif command == "brief":
        print(desk.brief_text())
    elif command == "map":
        name = " ".join(args.market)
        if args.export:
            result = desk.market({"action": "export", "market": name, "share": args.share})
            print(result.get("path") or result.get("error"))
        elif not name and len(desk.markets.list_markets()) != 1:
            maps = desk.markets.list_markets()
            print("\n".join("%s — %d companies, %d in the pipeline" % (m["name"], m["companies"], m["in_pipeline"]) for m in maps)
                  or "No market maps yet. Ask Hermes to map a market for you.")
        else:
            print(desk.slash("map " + name))
    elif command == "playbooks":
        print(desk.slash("playbooks"))
    elif command == "use":
        result = desk.playbook_tool({"action": "use", "name": args.name})
        print(result.get("summary") or result.get("error"))
        if result.get("warning"):
            print(result["warning"])
    elif command == "history":
        ref = " ".join(args.record)
        deal = None
        if ref:
            try:
                deal = desk.store.find(ref) or desk.store.find(ref, archived=True)
            except st.Ambiguous as exc:
                print("'%s' matches several records: %s" % (ref, ", ".join(d["id"] for d in exc.matches)))
                return
            if deal is None:
                print("no record matches '%s'" % ref)
                return
            print("%s (%s)" % (deal["name"], deal["id"]))
        playbook = desk.playbook()
        shown = lambda key, value: (cards.money(int(value or 0) / 100, playbook.currency) or "0") if key == "value_cents" \
            else ("nothing" if value in (None, "") else str(value))          # noqa: E731
        lines = desk.store.ledger(deal["id"] if deal else "", limit=max(1, min(int(args.limit or 30), 500)))
        for line in reversed(lines):
            changed = ["%s: %s → %s" % (key.replace("value_cents", "value"), shown(key, line["before"].get(key)), shown(key, line["after"][key]))
                       for key in line["after"] if line["op"] in ("update", "undo") and key in line["before"]]
            print("%s  %-7s %-8s %s%s%s" % (line["at"][:16].replace("T", " "), line["op"], line["entity"], line["summary"],
                                            "  [%s]" % "; ".join(changed) if changed else "", "  by " + line["actor"] if line["actor"] else ""))
        if not lines:
            print("Nothing on the ledger yet." if not deal else "Nothing on the ledger for this record: it has not changed since 0.12.")
    elif command == "autonomy":
        from .desk import AUTONOMY
        if args.level:
            result = desk.set_autonomy(args.level)
            print(result.get("error") or "Autonomy is now: %s. %s" % (result["label"], result["summary"]))
        else:
            level = desk.autonomy()
            for key, entry in AUTONOMY.items():
                print("%s %-8s %s. %s" % ("●" if key == level else "○", key, entry["label"], entry["summary"]))
    elif command == "demo":
        if getattr(args, "leave", False) or args.clear:
            was = desk.tour_end()
            removed = demo.clear(desk) if args.clear else 0     # sample records an earlier version loaded into the desk itself
            print("%s%s" % ("Left the tour: this is your own desk again." if was else "No tour was on.",
                            " Removed %d sample records from your desk." % removed if removed else ""))
            left = 0 if args.clear else desk.stray_samples()
            if left:
                print("%d sample records from an earlier version are still in your own desk. Remove them with: "
                      "hermes stoa demo --remove-samples" % left)
            return
        result = desk.tour_start(args.which)
        example = demo.EXAMPLES[result["example"]]
        other = "saas" if result["example"] == "recruiting" else "recruiting"
        print("On tour: the example of %s. %s, and a market map of %d companies." % (
            example["title"], result["summary"], len(example["companies"])))
        print("Everything in it is made up and kept in a file of its own; your own desk is untouched and comes back with "
              "`hermes stoa demo --leave`. `hermes stoa demo %s` shows the other example." % other)
    elif command == "export":
        result = desk.export({"action": "export", "path": args.path or None, "group": getattr(args, "group", ""), "share": getattr(args, "share", False)})
        print("Wrote %d records to %s" % (result["rows"], result["path"]) if result.get("ok") else result.get("error"))
    elif command == "import":
        path, refused = desk.csv_path(args.path, trusted=True)        # typed by a person at a terminal: any folder, CSV only
        result = refused or desk.import_csv(path, dry_run=args.dry_run)
        if result.get("ok"):
            print("%s %d records, skipped %d already on the desk."
                  % ("Would add" if args.dry_run else "Added", result["added"], result["skipped_existing"]))
            for problem in result.get("problems", []):
                print("  " + problem)
        else:
            print(result.get("error"))
    else:
        print("Usage: hermes stoa <status|board|brief|map|playbooks|use|autonomy|history|demo|export|import>")
