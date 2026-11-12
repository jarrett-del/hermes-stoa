"""``hermes stoa``: the desk from a terminal. Status, sample data, CSV in and out."""
from __future__ import annotations

from pathlib import Path

from . import cards, demo


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
    auto = subs.add_parser("autonomy", help="What a scheduled job may change by itself: ask, routine or full (no level: show it)")
    auto.add_argument("level", nargs="?", default="", choices=["", "ask", "routine", "full"])
    sample = subs.add_parser("demo", help="Take a tour: show a worked example in place of your own desk, which is left untouched")
    sample.add_argument("which", nargs="?", default="", choices=["", "recruiting", "saas"],
                        help="recruiting: an agency with about $1.2M in open fees. saas: a company selling a fraud prevention "
                             "tool, about $2.5M of pipeline. Left out: the one that fits the active playbook")
    sample.add_argument("--clear", "--leave", dest="clear", action="store_true",
                        help="Leave the tour: back to your own desk, and any sample records an earlier version put in it are removed")
    export = subs.add_parser("export", help="Write the pipeline to a CSV file in the plugin's exports folder")
    export.add_argument("path", nargs="?", default="", metavar="name", help="A file name (optional). It is written to the exports folder.")
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
        value = sum(d["value"] for d in open_deals)
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
        if args.clear:
            was = desk.tour_end()
            removed = demo.clear(desk)            # sample records an earlier version loaded into the desk itself
            print("%s%s" % ("Left the tour: this is your own desk again." if was else "No tour was on.",
                            " Removed %d sample records from your desk." % removed if removed else ""))
            return
        result = desk.tour_start(args.which)
        example = demo.EXAMPLES[result["example"]]
        other = "saas" if result["example"] == "recruiting" else "recruiting"
        print("On tour: the example of %s. %s, and a market map of %d companies." % (
            example["title"], result["summary"], len(example["companies"])))
        print("Everything in it is made up and kept in a file of its own; your own desk is untouched and comes back with "
              "`hermes stoa demo --leave`. `hermes stoa demo %s` shows the other example." % other)
    elif command == "export":
        result = desk.export({"action": "export", "path": args.path or None})
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
        print("Usage: hermes stoa <status|board|brief|map|playbooks|use|autonomy|demo|export|import>")
