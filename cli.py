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
    sample = subs.add_parser("demo", help="Load fictional sample records for the active playbook")
    sample.add_argument("--clear", action="store_true", help="Remove the sample records instead")
    export = subs.add_parser("export", help="Write the pipeline to a CSV file")
    export.add_argument("path", nargs="?", default="")
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
        print("  playbook      %s (%s)" % (playbook.name, playbook.title))
        print("  records       %d open, %d closed" % (len(open_deals), len(deals) - len(open_deals)))
        value = sum(d["value"] for d in open_deals)
        if value:
            print("  open value    %s" % cards.money(value, playbook.currency))
        print("  data          %s" % (desk.data_dir / "desk.db"))
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
    elif command == "demo":
        if args.clear:
            print("Removed %d sample records." % demo.clear(desk))
        else:
            print("Added %d sample records for the %s playbook and a sample market map. "
                  "`hermes stoa demo --clear` removes them." % (demo.load(desk), desk.playbook().name))
    elif command == "export":
        result = desk.export({"action": "export", "path": args.path or None})
        print("Wrote %d records to %s" % (result["rows"], result["path"]) if result.get("ok") else result.get("error"))
    elif command == "import":
        result = desk.import_csv(Path(args.path).expanduser(), dry_run=args.dry_run)
        if result.get("ok"):
            print("%s %d records, skipped %d already on the desk."
                  % ("Would add" if args.dry_run else "Added", result["added"], result["skipped_existing"]))
            for problem in result.get("problems", []):
                print("  " + problem)
        else:
            print(result.get("error"))
    else:
        print("Usage: hermes stoa <status|board|brief|map|playbooks|use|demo|export|import>")
