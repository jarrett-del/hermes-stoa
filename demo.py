"""Fictional sample records, so a new desk has something on it. Every person and company is made up."""
from __future__ import annotations

import datetime as dt

SOURCE = "stoa-demo"

# (name, company, group, stage index, value, next step, due in days, days quiet, fields, note)
RECRUITING = [
    ("Dana Whitfield", "Kestrel Labs", "Staff Backend · Northwind", 2, 42000, "Send submission note to Northwind", 0, 1,
     {"title": "Senior Backend Engineer", "location": "Denver, CO", "comp": "$210k base"},
     "Phone screen went well. Wants a smaller team, strong on distributed systems, four weeks notice."),
    ("Marcus Oyelaran", "Fathom Data", "Staff Backend · Northwind", 1, 42000, "Follow up on outreach", -1, 6,
     {"title": "Staff Engineer", "location": "Austin, TX"}, "Replied once, asked for the comp range."),
    ("Priya Raman", "Lumen Health", "Staff Backend · Northwind", 4, 45000, "Debrief after onsite", 1, 0,
     {"title": "Principal Engineer", "location": "Remote", "comp": "$240k base"}, "Onsite with the CTO on Thursday."),
    ("Tomás Ibarra", "Quill & Co", "Founding Designer · Halcyon", 0, 30000, "", None, 6,
     {"title": "Product Designer", "location": "Brooklyn, NY"}, "Found through the design community list."),
    ("Ingrid Solberg", "Meridian Freight", "Founding Designer · Halcyon", 3, 30000, "Chase Halcyon for feedback", -2, 5,
     {"title": "Lead Product Designer", "location": "Chicago, IL", "comp": "$185k base"}, "Submitted Monday. No word from the client yet."),
    ("Wei-Lin Chao", "Brightside AI", "Head of Growth · Ostrava", 5, 55000, "Walk through the offer", 0, 0,
     {"title": "Director of Growth", "location": "San Francisco, CA", "comp": "$230k base plus equity"}, "Offer went out this morning."),
    ("Callum Reyes", "Tidewater", "Head of Growth · Ostrava", 1, 55000, "Book phone screen", 2, 1,
     {"title": "Growth Lead", "location": "Seattle, WA"}, "Open to a call next week."),
]

SALES = [
    ("Dana Whitfield", "Kestrel Labs", "Kestrel Labs", 2, 48000, "Send recap and pricing", 0, 1,
     {"title": "VP Operations", "use_case": "Replace spreadsheet forecasting"}, "Discovery call done. Budget owner is the CFO."),
    ("Marcus Oyelaran", "Fathom Data", "Fathom Data", 1, 22000, "Book discovery call", -1, 6,
     {"title": "Head of Data"}, "Inbound from the webinar. Asked about SSO."),
    ("Priya Raman", "Lumen Health", "Lumen Health", 4, 120000, "Reply on redlines", 1, 0,
     {"title": "COO", "competitor": "Incumbent vendor", "decision_date": "end of month"}, "Legal sent redlines on the MSA."),
    ("Tomás Ibarra", "Quill & Co", "Quill & Co", 0, 9000, "", None, 8, {"title": "Founder"}, "Referred by an existing customer."),
    ("Ingrid Solberg", "Meridian Freight", "Meridian Freight", 3, 64000, "Chase for a decision", -2, 5,
     {"title": "Director of Logistics", "use_case": "Carrier scorecards"}, "Proposal sent Monday. No reply yet."),
    ("Wei-Lin Chao", "Brightside AI", "Brightside AI", 3, 36000, "Demo for the wider team", 3, 0,
     {"title": "Chief of Staff"}, "Wants a second demo with engineering."),
    ("Callum Reyes", "Tidewater", "Tidewater", 1, 15000, "Send case study", 2, 1, {"title": "Ops Manager"}, "Qualified on the first call."),
]


MARKET = {
    "name": "Regional freight brokers (demo)",
    "brief": "Fictional sample map. Freight brokerages with 50 to 500 people in the US Midwest that still run "
             "carrier scorecards in spreadsheets.",
    "segments": [{"name": "Asset-light brokers", "description": "Pure brokerage, no trucks of their own"},
                 {"name": "Hybrid carriers", "description": "Own fleet plus a brokerage arm"},
                 {"name": "Digital-first", "description": "Venture-backed, app-led brokerages"}],
    "requirements": ["50 to 500 people", "Scorecards kept in spreadsheets", "Own compliance team", "Carrier base growing"],
}
COMPANIES = [
    {"company": "Meridian Freight", "website": "meridianfreight.example", "segment": "Asset-light brokers", "fit": 5,
     "size": "220 people", "location": "Chicago, IL", "why": "Posted two carrier-compliance analyst roles this month.",
     "signal": "Hiring for the problem we solve", "uses": "Spreadsheets",
     "requirements": {"50 to 500 people": "220 on its careers page", "Scorecards kept in spreadsheets": "Job ad asks for advanced Excel for carrier scoring",
                      "Own compliance team": "Two compliance analyst roles open", "Carrier base growing": True},
     "people": [{"name": "Renata Okafor", "title": "Director of Carrier Compliance", "why": "Owns the scorecards the job ads describe"}]},
    {"company": "Tidewater Logistics", "website": "tidewater.example", "segment": "Asset-light brokers", "fit": 4,
     "size": "140 people", "location": "Columbus, OH", "why": "COO spoke about carrier quality at a regional conference.",
     "uses": "Spreadsheets",
     "requirements": {"50 to 500 people": "140 people", "Scorecards kept in spreadsheets": "COO: 'we still live in spreadsheets'",
                      "Own compliance team": "", "Carrier base growing": True},
     "people": [{"name": "Miles Brandt", "title": "Chief Operating Officer"}]},
    {"company": "Halcyon Transport", "website": "halcyontransport.example", "segment": "Asset-light brokers", "fit": 3,
     "size": "60 people", "location": "Des Moines, IA", "why": "Small ops team; likely still on spreadsheets.", "uses": "Spreadsheets",
     "requirements": {"50 to 500 people": "60 people", "Scorecards kept in spreadsheets": "", "Own compliance team": "",
                      "Carrier base growing": ""}},
    {"company": "Northwind Carriers", "website": "northwindcarriers.example", "segment": "Hybrid carriers", "fit": 4,
     "size": "480 people", "location": "Indianapolis, IN", "why": "Opened a brokerage arm last year and is scaling it.",
     "signal": "New business line", "uses": "CarrierWatch", "also": "Asset-light brokers",
     "requirements": {"50 to 500 people": "480 people", "Scorecards kept in spreadsheets": "", "Own compliance team": "Compliance lead named on site",
                      "Carrier base growing": "Brokerage arm added 300 carriers in a year"},
     "people": [{"name": "Dalia Reyes-Moore", "title": "VP Brokerage"}]},
    {"company": "Ostrava Lines", "website": "ostravalines.example", "segment": "Hybrid carriers", "fit": 2,
     "size": "300 people", "location": "Omaha, NE", "why": "Mostly dedicated contract freight; brokerage is a sideline.",
     "uses": "In-house tool",
     "requirements": {"50 to 500 people": "300 people", "Scorecards kept in spreadsheets": "", "Own compliance team": "",
                      "Carrier base growing": ""}},
    {"company": "Brightside Freight", "website": "brightsidefreight.example", "segment": "Digital-first", "fit": 5,
     "size": "90 people", "location": "Minneapolis, MN", "why": "Raised a Series A and named carrier vetting as a priority.",
     "signal": "Fresh funding", "uses": "Spreadsheets",
     "requirements": {"50 to 500 people": "90 people", "Scorecards kept in spreadsheets": "Founder post: 'vetting is a Google Sheet'",
                      "Own compliance team": "Hiring its first compliance manager", "Carrier base growing": "Doubling carriers this year"},
     "people": [{"name": "Tobias Lindqvist", "title": "Head of Carrier Operations"}]},
    {"company": "Quill Cargo", "website": "quillcargo.example", "segment": "Digital-first", "fit": 3,
     "size": "55 people", "location": "Kansas City, MO", "why": "Building in-house tooling; may prefer to build.",
     "uses": "In-house tool", "also": "Asset-light brokers",
     "requirements": {"50 to 500 people": "55 people", "Scorecards kept in spreadsheets": "", "Own compliance team": "",
                      "Carrier base growing": True}},
]
READING = {
    "reading": "Most of this market still scores carriers by hand: five of seven companies run on spreadsheets or something "
               "home-built, and only one has bought a tool. The strongest fits are growing brokerages that have just hired, or "
               "are hiring, their first compliance people.\nHybrid carriers are the harder sell. Their brokerage arms are young, and "
               "the one that has bought software bought it for the fleet side.",
    "implications": ["Lead with the two companies that are hiring for compliance now; the pain is already budgeted.",
                     "Pitch against the spreadsheet, not against a competitor: only one company uses one.",
                     "A compliance team is the rarest requirement, so sell to operations where there is none."],
    "first_calls": [{"target": "Brightside Freight", "why": "Fresh funding, and hiring its first compliance manager."},
                    {"target": "Meridian Freight", "why": "Two compliance roles open this month."},
                    {"target": "Northwind Carriers", "why": "A year-old brokerage arm adding carriers fast."}],
}


def load_market(desk) -> int:
    markets = desk.markets
    if markets._market(MARKET["name"]) is not None:
        return 0
    markets.define(dict(MARKET))
    added = len(markets.add({"market": MARKET["name"], "companies": COMPANIES})["added"])
    markets.reading(dict(READING, market=MARKET["name"]))
    return added


def _rows(playbook) -> list:
    return RECRUITING if playbook.name == "recruiting" else SALES


def load(desk) -> int:
    playbook = desk.playbook()
    store = desk.store
    today = dt.date.today()
    count = 0
    for name, company, group, index, value, step, due, quiet, fields, note in _rows(playbook):
        if any(d["name"] == name and d["source"] == SOURCE for d in store.list(status="all", query=name)):
            continue
        stage = playbook.stages[min(index, len(playbook.stages) - 1)]["id"]
        keys = {f["key"] for f in playbook.fields}
        deal = store.add(name, stage, actor="demo", company=company, grp=group, value=value, owner="you",
                         next_step=step, next_due=(today + dt.timedelta(days=due)).isoformat() if due is not None else "",
                         source=SOURCE, email="%s@example.com" % name.split()[0].lower(),
                         fields={k: v for k, v in fields.items() if k in keys})
        store.log(deal["id"], "note", note, actor="demo")
        if quiet:  # age the record so the stale list has something to show
            stamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=quiet)).strftime("%Y-%m-%dT%H:%M:%SZ")
            with store._conn() as conn:
                conn.execute("UPDATE activities SET at = ? WHERE deal_id = ?", (stamp, deal["id"]))
                conn.execute("UPDATE deals SET updated_at = ?, stage_at = ?, created_at = ? WHERE id = ?",
                             (stamp, stamp, stamp, deal["id"]))
        count += 1
    load_market(desk)
    return count


def clear(desk) -> int:
    store = desk.store
    doomed = [d for d in store.list(status="all") if d["source"] == SOURCE]
    for deal in doomed:
        store.delete(deal["id"])
    market = desk.markets._market(MARKET["name"])
    if market is not None:
        tracked = [t["deal_id"] for t in desk.markets.targets(market["id"]) if t["deal_id"]]
        for deal_id in tracked:
            store.delete(deal_id)
        with store._conn() as conn:
            conn.execute("DELETE FROM targets WHERE market_id = ?", (market["id"],))
            conn.execute("DELETE FROM markets WHERE id = ?", (market["id"],))
            conn.execute("DELETE FROM cards WHERE deal_id = ?", ("map:" + market["id"],))
    return len(doomed)
