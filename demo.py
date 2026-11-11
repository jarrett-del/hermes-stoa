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
    return count


def clear(desk) -> int:
    store = desk.store
    doomed = [d for d in store.list(status="all") if d["source"] == SOURCE]
    for deal in doomed:
        store.delete(deal["id"])
    return len(doomed)
