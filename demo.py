"""Sample data: two worked examples, so a new desk has a real-sized pipeline on it.

Every person and company here is made up. A playbook can describe any pipeline; these two show the
range. ``recruiting`` is an agency with about $1.2M in open fees across eight searches, and a map
of where the candidates for one of those searches work now. ``saas`` is a B2B software company
selling a fraud prevention tool, with about $2.5M of open pipeline and a map of its market.
"""
from __future__ import annotations

import datetime as dt
import re

SOURCE = "stoa-demo"
DEFAULT = "saas"
RETIRED_MAPS = ("Regional freight brokers (demo)",)     # the sample map of earlier versions: `clear` takes it away too

# -- a recruiting agency -------------------------------------------------------------------------
# (name, current company, search, stage, fee, next step, due in days, days quiet, days on the desk, owner, fields, note)
# stage: an index into the playbook's stages, "won", or ("lost", stage index it was lost from, reason)
RECRUITING = [
    ("Dana Whitfield", "Kestrel Labs", "Staff Backend · Northwind", 2, 42000, "Send submission note to Northwind", 0, 1, 9, "Maya",
     {"title": "Senior Backend Engineer", "location": "Denver, CO", "comp": "$210k base", "notice": "Four weeks"},
     "Phone screen went well. Wants a smaller team, strong on distributed systems, four weeks notice."),
    ("Marcus Oyelaran", "Fathom Data", "Staff Backend · Northwind", 1, 42000, "Follow up on outreach", -1, 6, 12, "Maya",
     {"title": "Staff Engineer", "location": "Austin, TX"}, "Replied once, asked for the comp range."),
    ("Priya Raman", "Pallas Systems", "Staff Backend · Northwind", 4, 42000, "Debrief after onsite", 1, 0, 21, "Maya",
     {"title": "Principal Engineer", "location": "Remote", "comp": "$240k base"}, "Onsite with the CTO on Thursday."),
    ("Elias Brandt", "Cinder Systems", "Staff Backend · Northwind", 3, 42000, "Chase Northwind for feedback", 2, 2, 14, "Maya",
     {"title": "Senior Software Engineer", "location": "Chicago, IL", "comp": "$205k base"},
     "Submitted alongside Dana. Northwind likes the streaming background."),
    ("Noor Haddad", "Marlowe", "Staff Backend · Northwind", 0, 42000, "", None, 1, 2, "Maya",
     {"title": "Backend Engineer", "location": "Toronto, ON"}, "Found through the systems meetup list."),

    ("Tomás Ibarra", "Sable Studio", "Founding Designer · Halcyon", 0, 36000, "", None, 6, 7, "Jonah",
     {"title": "Product Designer", "location": "Brooklyn, NY"}, "Found through the design community list."),
    ("Ingrid Solberg", "Juniper", "Founding Designer · Halcyon", 3, 36000, "Chase Halcyon for feedback", -2, 5, 16, "Jonah",
     {"title": "Lead Product Designer", "location": "Chicago, IL", "comp": "$185k base"}, "Submitted Monday. No word from the client yet."),
    ("Amara Nwosu", "Wren", "Founding Designer · Halcyon", 2, 36000, "Send portfolio review notes", 1, 1, 10, "Jonah",
     {"title": "Senior Product Designer", "location": "Atlanta, GA", "comp": "$170k base"},
     "Strong systems work. Wants founding-level equity."),

    ("Wei-Lin Chao", "Vireo", "Head of Growth · Ostrava", 5, 55000, "Walk through the offer", 0, 0, 34, "Maya",
     {"title": "Director of Growth", "location": "San Francisco, CA", "comp": "$230k base plus equity"}, "Offer went out this morning."),
    ("Callum Reyes", "Tessera", "Head of Growth · Ostrava", 1, 55000, "Book phone screen", 2, 1, 6, "Maya",
     {"title": "Growth Lead", "location": "Seattle, WA"}, "Open to a call next week."),
    ("Sofia Marchetti", "Corvid", "Head of Growth · Ostrava", 4, 55000, "Prep for the CEO round", 3, 1, 25, "Maya",
     {"title": "VP Growth", "location": "New York, NY", "comp": "$250k base"}, "Second round done. The CEO round is Tuesday."),

    ("Rafael Mendes", "Orchard", "VP Engineering · Lumen Health", 3, 72000, "Line up the client interview", 1, 1, 18, "Jonah",
     {"title": "Senior Director of Engineering", "location": "Boston, MA", "comp": "$290k base"},
     "Lumen wants to meet. Waiting on calendars."),
    ("Hannah Lindqvist", "Basalt", "VP Engineering · Lumen Health", 2, 72000, "Write up the screen", 0, 2, 11, "Jonah",
     {"title": "VP Engineering", "location": "Remote", "comp": "$300k base", "notice": "Two months"},
     "Has run eighty engineers. Health-data experience is thin."),
    ("Kwame Asante", "Lantern", "VP Engineering · Lumen Health", 1, 72000, "Send the role brief", 4, 0, 4, "Jonah",
     {"title": "Head of Engineering", "location": "Washington, DC"}, "Warm introduction from a past placement."),

    ("Yuki Tanaka", "Cobalt", "ML Platform Lead · Brightside AI", 4, 58000, "Collect interview feedback", -1, 3, 23, "Maya",
     {"title": "Staff ML Engineer", "location": "Seattle, WA", "comp": "$260k base"}, "The panel ran long, in a good way."),
    ("Leila Farahani", "Thistle", "ML Platform Lead · Brightside AI", 3, 58000, "Chase Brightside for a slot", 2, 1, 13, "Maya",
     {"title": "ML Platform Engineer", "location": "Remote", "comp": "$235k base"}, "Built the feature store at her last company."),
    ("Owen Gallagher", "Ridgeway", "ML Platform Lead · Brightside AI", 1, 58000, "Follow up on outreach", -3, 7, 15, "Maya",
     {"title": "Senior ML Engineer", "location": "Dublin, IE"}, "Opened the message, no reply."),
    ("Mei Sato", "Saffron", "ML Platform Lead · Brightside AI", 0, 58000, "", None, 0, 1, "Maya",
     {"title": "ML Infrastructure Lead", "location": "San Jose, CA"}, "Spoke at the platform summit."),

    ("Gabriela Rocha", "Harborline", "Head of Risk · Tidewater Pay", 2, 60000, "Send submission note", 1, 0, 8, "Jonah",
     {"title": "Director of Fraud Strategy", "location": "Miami, FL", "comp": "$245k base"},
     "Cut chargebacks by half at Harborline."),
    ("Daniel Okonkwo", "Meridian", "Head of Risk · Tidewater Pay", 1, 60000, "Book phone screen", 3, 2, 5, "Jonah",
     {"title": "Head of Risk Operations", "location": "London, UK"}, "Interested if relocation is covered."),

    ("Zara Petrovic", "Fathom Data", "Senior Product Manager · Quill & Co", 0, 40000, "", None, 3, 3, "Maya",
     {"title": "Product Manager", "location": "Austin, TX"}, "Referred by Marcus."),
    ("Henry Blackwood", "Cinder Systems", "Senior Product Manager · Quill & Co", 0, 40000, "", None, 9, 9, "Maya",
     {"title": "Senior Product Manager", "location": "Portland, OR"}, "On the shortlist from last year's search."),

    ("Isabel Navarro", "Juniper", "Enterprise AE · Alder & Finch", 1, 38000, "Send comp details", 0, 1, 6, "Jonah",
     {"title": "Enterprise Account Executive", "location": "Denver, CO"}, "Asked how base and commission split."),
    ("Tobias Engel", "Wren", "Enterprise AE · Alder & Finch", 0, 38000, "", None, 2, 2, "Jonah",
     {"title": "Account Executive", "location": "Chicago, IL"}, "Top of the leaderboard two years running."),

    ("Aiko Mori", "Basalt", "Principal Engineer · Northwind", "won", 48000, "", None, 5, 41, "Maya",
     {"title": "Principal Engineer", "location": "Remote"}, "Signed. Starts on the first."),
    ("Lucas Ferreira", "Sable Studio", "Head of Design · Ostrava", "won", 44000, "", None, 12, 52, "Jonah",
     {"title": "Design Director", "location": "Lisbon, PT"}, "Accepted after one round of negotiation."),
    ("Chidi Eze", "Cobalt", "Staff Data Engineer · Brightside AI", "won", 52000, "", None, 20, 60, "Maya",
     {"title": "Staff Data Engineer", "location": "Seattle, WA"}, "Placed. Brightside came back for the ML platform search."),
    ("Bianca Russo", "Thistle", "Staff Backend · Northwind", ("lost", 3, "Client passed"), 42000, "", None, 4, 19, "Maya",
     {"title": "Senior Engineer", "location": "Remote"}, "Northwind wanted more payments experience."),
    ("Oliver Stein", "Orchard", "VP Engineering · Lumen Health", ("lost", 1, "Not interested"), 72000, "", None, 8, 14, "Jonah",
     {"title": "VP Engineering", "location": "Boston, MA"}, "Happy where he is. Ask again in a year."),
    ("Nadia Rahman", "Ridgeway", "ML Platform Lead · Brightside AI", ("lost", 4, "Took another offer"), 58000, "", None, 3, 27, "Maya",
     {"title": "Staff ML Engineer", "location": "Remote"}, "Took the competing offer the day before the final round."),
    ("Felix Wagner", "Wren", "Founding Designer · Halcyon", ("lost", 2, "Comp mismatch"), 36000, "", None, 10, 22, "Jonah",
     {"title": "Principal Designer", "location": "Berlin, DE"}, "Forty thousand above the band."),
    ("Grace Adeyemi", "Tessera", "Head of Growth · Ostrava", ("lost", 1, "Timing"), 55000, "", None, 15, 30, "Maya",
     {"title": "Head of Growth", "location": "New York, NY"}, "Mid-launch. Open to talking in the spring."),
]

# A talent map: where the candidates for one search work now. Adding a company puts its named person
# on the desk as a sourced candidate in that search.
TALENT_MARKET = {
    "name": "Staff Backend · Northwind",
    "brief": "Fictional sample map. Where the staff backend engineers Northwind wants are working now: companies that "
             "run their own payment rails or ledgers at scale.",
    "segments": [{"name": "Payments infrastructure", "description": "Processors, gateways and ledgers"},
                 {"name": "Banking platforms", "description": "Banking-as-a-service and core banking"},
                 {"name": "Marketplaces", "description": "Marketplaces that built their own payments"}],
    "requirements": ["Runs its own ledger or payment rails", "Go or Rust in production", "Engineering team of 50 to 400",
                     "Recent reorganisation or layoffs"],
}
TALENT_COMPANIES = [
    {"company": "Ironbridge Payments", "website": "ironbridge.example", "segment": "Payments infrastructure", "fit": 5,
     "size": "310 people", "location": "Chicago, IL", "why": "Rebuilt its ledger in Go last year and has just cut a platform team.",
     "signal": "Reorganised in the last quarter",
     "requirements": {"Runs its own ledger or payment rails": "Engineering blog: 'our double-entry ledger'", "Go or Rust in production": "Go, per its job ads",
                      "Engineering team of 50 to 400": "About 180 engineers", "Recent reorganisation or layoffs": "Platform team folded into payments in September"},
     "people": [{"name": "Samira Costa", "title": "Staff Software Engineer", "why": "Led the ledger rebuild"},
                {"name": "Jude Okafor", "title": "Senior Backend Engineer"}]},
    {"company": "Quayside Pay", "website": "quayside.example", "segment": "Payments infrastructure", "fit": 4,
     "size": "140 people", "location": "Austin, TX", "why": "Small team running card and bank rails end to end.",
     "requirements": {"Runs its own ledger or payment rails": "Direct card and bank integrations", "Go or Rust in production": "Rust in the settlement service",
                      "Engineering team of 50 to 400": "About 70 engineers", "Recent reorganisation or layoffs": ""},
     "people": [{"name": "Anders Holm", "title": "Principal Engineer", "why": "Owns settlement"}]},
    {"company": "Farrow Ledger", "website": "farrowledger.example", "segment": "Payments infrastructure", "fit": 4,
     "size": "90 people", "location": "Remote", "why": "Ledger-as-a-service: every engineer has built what Northwind needs.",
     "signal": "Missed its last funding round", "also": "Banking platforms",
     "requirements": {"Runs its own ledger or payment rails": "The ledger is the product", "Go or Rust in production": "Go",
                      "Engineering team of 50 to 400": "About 55 engineers", "Recent reorganisation or layoffs": "Flat round, hiring frozen"},
     "people": [{"name": "Lena Fischer", "title": "Staff Engineer"}]},
    {"company": "Copperline", "website": "copperline.example", "segment": "Payments infrastructure", "fit": 2,
     "size": "1,900 people", "location": "San Francisco, CA", "why": "Deep bench, but well paid and hard to move.",
     "requirements": {"Runs its own ledger or payment rails": "Yes, at very large scale", "Go or Rust in production": "",
                      "Engineering team of 50 to 400": "", "Recent reorganisation or layoffs": ""}},
    {"company": "Lodestar Bank Platform", "website": "lodestar.example", "segment": "Banking platforms", "fit": 5,
     "size": "260 people", "location": "New York, NY", "why": "Laid off a fifth of engineering in October; its core is written in Go.",
     "signal": "Layoffs in October",
     "requirements": {"Runs its own ledger or payment rails": "Core banking ledger", "Go or Rust in production": "Go",
                      "Engineering team of 50 to 400": "About 150 engineers", "Recent reorganisation or layoffs": "A fifth of engineering let go"},
     "people": [{"name": "Marisol Vega", "title": "Staff Backend Engineer", "why": "Posted that she is looking"},
                {"name": "Theo Marsh", "title": "Senior Software Engineer"}]},
    {"company": "Tidemark", "website": "tidemark.example", "segment": "Banking platforms", "fit": 3,
     "size": "420 people", "location": "Charlotte, NC", "why": "Good engineers, mostly on the JVM.",
     "requirements": {"Runs its own ledger or payment rails": "Card issuing platform", "Go or Rust in production": "",
                      "Engineering team of 50 to 400": "About 210 engineers", "Recent reorganisation or layoffs": ""},
     "people": [{"name": "Priscilla Dunn", "title": "Principal Engineer"}]},
    {"company": "Anchorage BaaS", "website": "anchoragebaas.example", "segment": "Banking platforms", "fit": 4,
     "size": "120 people", "location": "Salt Lake City, UT", "why": "Sponsor-bank trouble has engineers taking calls.",
     "signal": "Lost a bank partner",
     "requirements": {"Runs its own ledger or payment rails": "Program ledger for its fintech clients", "Go or Rust in production": "Go and some Rust",
                      "Engineering team of 50 to 400": "About 60 engineers", "Recent reorganisation or layoffs": "Partner bank exit announced"},
     "people": [{"name": "Ravi Menon", "title": "Senior Staff Engineer"}]},
    {"company": "Haven Homeshare", "website": "havenhomeshare.example", "segment": "Marketplaces", "fit": 3,
     "size": "800 people", "location": "Seattle, WA", "why": "Built host payouts in-house; the payments group is about forty people.",
     "also": "Payments infrastructure",
     "requirements": {"Runs its own ledger or payment rails": "In-house payouts ledger", "Go or Rust in production": "Go in payments",
                      "Engineering team of 50 to 400": "", "Recent reorganisation or layoffs": ""},
     "people": [{"name": "Elena Petrou", "title": "Staff Engineer, Payments"}]},
    {"company": "Stallholder", "website": "stallholder.example", "segment": "Marketplaces", "fit": 4,
     "size": "230 people", "location": "Portland, OR", "why": "Moved off a processor to its own rails this year.",
     "signal": "New payments platform launched",
     "requirements": {"Runs its own ledger or payment rails": "Migrated to its own rails in the spring", "Go or Rust in production": "Rust",
                      "Engineering team of 50 to 400": "About 110 engineers", "Recent reorganisation or layoffs": ""},
     "people": [{"name": "Callie Nguyen", "title": "Senior Backend Engineer", "why": "Led the migration"}]},
    {"company": "Rally Rides", "website": "rallyrides.example", "segment": "Marketplaces", "fit": 2,
     "size": "640 people", "location": "Los Angeles, CA", "why": "Payments run through a processor; little ledger work in-house.",
     "requirements": {"Runs its own ledger or payment rails": "", "Go or Rust in production": "Go",
                      "Engineering team of 50 to 400": "", "Recent reorganisation or layoffs": "Closed two city teams"}},
]
TALENT_READING = {
    "reading": "The people Northwind wants are concentrated in a handful of smaller payments and banking companies, and three of "
               "the best pools have just been shaken loose: a reorganisation, a round of layoffs, and a lost bank partner.\n"
               "The large names have the depth but not the movement. Their engineers are well paid and nobody is leaving.",
    "implications": ["Start where something has just changed: Lodestar, Ironbridge and Anchorage.",
                     "Lead with the ledger work. It is the one thing all the strong pools have in common.",
                     "Leave Copperline and Rally Rides until the first pass is done."],
    "first_calls": [{"target": "Lodestar Bank Platform", "why": "A fifth of engineering let go last month, and the core is in Go."},
                    {"target": "Ironbridge Payments", "why": "The team that rebuilt the ledger was just reorganised."},
                    {"target": "Anchorage BaaS", "why": "A lost bank partner has senior engineers taking calls."},
                    {"target": "Stallholder", "why": "They have just finished exactly the migration Northwind is starting."}],
}

# -- a B2B SaaS company selling a fraud prevention tool ------------------------------------------
# (buyer, company, account, stage, deal size, next step, due in days, days quiet, days on the desk, owner, fields, note)
SAAS = [
    ("Renata Okafor", "Harborline Pay", "Harborline Pay", 4, 240000, "Reply on redlines", 1, 0, 62, "Alex",
     {"title": "Head of Risk", "use_case": "Account takeover at login", "competitor": "In-house rules", "decision_date": "End of month"},
     "Legal sent redlines on the MSA. Security review passed last week."),
    ("Tobias Lindqvist", "Brightside BNPL", "Brightside BNPL", 4, 180000, "Send the revised order form", 0, 1, 55, "Priyanka",
     {"title": "VP Credit Risk", "use_case": "Synthetic identities at onboarding", "competitor": "Legacy vendor", "decision_date": "This quarter"},
     "They want a two-year term with a first-year ramp."),
    ("Dalia Reyes-Moore", "Northwind Marketplace", "Northwind Marketplace", 4, 150000, "Confirm the procurement call", 2, 1, 48, "Alex",
     {"title": "Director of Trust and Safety", "use_case": "Seller payout fraud", "competitor": "Manual review"},
     "Procurement joined the last call. Asked for references in marketplaces."),
    ("Miles Brandt", "Tidewater Remit", "Tidewater Remit", 4, 96000, "Chase the signature", -1, 4, 70, "Tom",
     {"title": "Chief Operating Officer", "use_case": "Mule accounts", "competitor": "Processor's built-in tool", "decision_date": "Overdue"},
     "Verbal yes two weeks ago. The contract is sitting with the CFO."),

    ("Anika Sharma", "Lumen Wallet", "Lumen Wallet", 3, 210000, "Walk the CFO through pricing", 3, 1, 40, "Priyanka",
     {"title": "Chief Risk Officer", "use_case": "Account takeover and card testing", "competitor": "In-house rules", "decision_date": "Mid next month"},
     "Proposal sent. Pilot results showed a 38% drop in takeover losses."),
    ("Jonas Weber", "Kestrel Lending", "Kestrel Lending", 3, 140000, "Chase for a decision", -2, 5, 44, "Tom",
     {"title": "Head of Fraud", "use_case": "First-party fraud on loans", "competitor": "Legacy vendor"},
     "Proposal sent Monday. No reply yet."),
    ("Carmen Delgado", "Quill Commerce", "Quill Commerce", 3, 120000, "Send the security questionnaire back", 1, 0, 33, "Alex",
     {"title": "VP Payments", "use_case": "Chargebacks at checkout", "competitor": "Processor's built-in tool", "decision_date": "Before peak season"},
     "Wants to be live before the holiday peak."),
    ("Samuel Osei", "Meridian Tickets", "Meridian Tickets", 3, 84000, "Second demo for engineering", 4, 2, 29, "Priyanka",
     {"title": "Director of Payments", "use_case": "Bot purchases and resale fraud", "competitor": "Manual review"},
     "Engineering wants to see the integration before they sign off."),
    ("Freya Nilsson", "Ostrava Games", "Ostrava Games", 3, 60000, "Answer the pricing questions", 0, 1, 26, "Tom",
     {"title": "Head of Player Trust", "use_case": "Stolen cards on in-game purchases", "competitor": "In-house rules"},
     "Asked whether pricing scales with volume or with seats."),

    ("Mateo Alvarez", "Halcyon Travel", "Halcyon Travel", 2, 160000, "Send recap and pilot plan", 0, 1, 21, "Alex",
     {"title": "SVP Payments", "use_case": "Chargebacks on high-value bookings", "competitor": "Legacy vendor", "decision_date": "Next quarter"},
     "Discovery done. Chargebacks run at 1.4% of bookings."),
    ("Imani Clarke", "Fathom Exchange", "Fathom Exchange", 2, 110000, "Technical deep dive", 2, 0, 18, "Priyanka",
     {"title": "Head of Compliance and Risk", "use_case": "Account takeover and mule accounts", "competitor": "In-house rules"},
     "Their rules engine is two engineers and a spreadsheet."),
    ("Viktor Novak", "Cinder Rides", "Cinder Rides", 2, 90000, "Share the marketplace case study", 1, 2, 17, "Tom",
     {"title": "Director of Risk", "use_case": "Promo abuse and fake drivers", "competitor": "Manual review"},
     "Promo abuse cost them a launch city last year."),
    ("Hye-jin Park", "Alder Rentals", "Alder Rentals", 2, 72000, "Book the follow-up", -1, 6, 24, "Alex",
     {"title": "VP Operations", "use_case": "Fake listings and deposit fraud", "competitor": "Nothing yet"},
     "Good first call, then went quiet."),
    ("Lorenzo Ricci", "Saffron Eats", "Saffron Eats", 2, 60000, "Send the refund-abuse numbers", 3, 1, 15, "Priyanka",
     {"title": "Head of Payments", "use_case": "Refund abuse", "competitor": "Processor's built-in tool"},
     "Refund abuse is their top fraud line item."),
    ("Abena Mensah", "Pallas Pay", "Pallas Pay", 2, 48000, "Loop in their engineering lead", 5, 0, 12, "Tom",
     {"title": "Risk Lead", "use_case": "Card testing", "competitor": "In-house rules"},
     "Small team. She is the whole risk function."),

    ("Ethan Caldwell", "Juniper Insure", "Juniper Insure", 1, 130000, "Book the discovery call", 1, 1, 10, "Alex",
     {"title": "VP Claims and Fraud", "use_case": "Claims payout fraud"}, "Inbound after the webinar. Budget is set for next year."),
    ("Sana Qureshi", "Ridgeway Auto", "Ridgeway Auto", 1, 85000, "Send the one-pager", 0, 2, 9, "Priyanka",
     {"title": "Director of Credit Risk", "use_case": "Synthetic identities on auto loans", "competitor": "Legacy vendor"},
     "Their vendor contract ends in the spring."),
    ("Pavel Dvorak", "Marlowe Freight Pay", "Marlowe Freight Pay", 1, 70000, "Book the discovery call", -3, 8, 16, "Tom",
     {"title": "Head of Finance Operations", "use_case": "Invoice and payout fraud"}, "Qualified on the first call. No reply to two follow-ups."),
    ("Naomi Feldman", "Corvid Gaming", "Corvid Gaming", 1, 54000, "Share the integration guide", 2, 0, 7, "Alex",
     {"title": "Trust and Safety Lead", "use_case": "Account sharing and stolen cards", "competitor": "In-house rules"},
     "Wants to know how long the integration takes."),
    ("Kofi Boateng", "Tessera Events", "Tessera Events", 1, 45000, "Book the discovery call", 4, 1, 6, "Priyanka",
     {"title": "Payments Manager", "use_case": "Bot purchases"}, "Referred by Meridian Tickets."),
    ("Ines Carvalho", "Vireo Crypto", "Vireo Crypto", 1, 36000, "Confirm who owns fraud", 3, 2, 8, "Tom",
     {"title": "Compliance Officer", "use_case": "Account takeover"}, "Compliance owns it today. Risk hire planned."),

    ("Arjun Mehta", "Orchard Payroll", "Orchard Payroll", 0, 75000, "First call", 1, 0, 3, "Alex",
     {"title": "VP Risk", "use_case": "Payroll payout fraud"}, "Downloaded the payroll fraud report."),
    ("Clara Jensen", "Basalt Bank", "Basalt Bank", 0, 60000, "First call", 2, 1, 4, "Priyanka",
     {"title": "Head of Financial Crime"}, "Met at the fintech conference."),
    ("Dmitri Volkov", "Lantern Loans", "Lantern Loans", 0, 40000, "", None, 6, 6, "Tom",
     {"title": "Fraud Manager"}, "Came in through the pricing page."),
    ("Fatima Zahra", "Sable Subscriptions", "Sable Subscriptions", 0, 36000, "Reply to her question", 0, 1, 2, "Alex",
     {"title": "Director of Billing", "use_case": "Card testing on free trials"}, "Asked whether we cover trial abuse."),
    ("Luca Bianchi", "Wren Wallet", "Wren Wallet", 0, 24000, "", None, 9, 9, "Priyanka",
     {"title": "Founder"}, "Seed stage. Probably early."),
    ("Rosa Jimenez", "Cobalt Checkout", "Cobalt Checkout", 0, 18000, "First call", 3, 0, 1, "Tom",
     {"title": "Head of Operations"}, "Signed up for the newsletter, then booked time."),
    ("Theo Lambert", "Thistle Travel", "Thistle Travel", 0, 12000, "", None, 2, 2, "Alex",
     {"title": "Finance Lead"}, "Referred by Halcyon Travel."),

    ("Grace Holloway", "Sterling Marketplace", "Sterling Marketplace", "won", 130000, "", None, 6, 85, "Alex",
     {"title": "VP Trust and Safety", "use_case": "Seller payout fraud"}, "Signed a two-year term. Kickoff is next week."),
    ("Omar Haddad", "Kite Pay", "Kite Pay", "won", 95000, "", None, 13, 74, "Priyanka",
     {"title": "Head of Risk", "use_case": "Account takeover"}, "Won against the legacy vendor on integration time."),
    ("Beatrice Lang", "Aurora Lending", "Aurora Lending", "won", 110000, "", None, 20, 90, "Tom",
     {"title": "Chief Credit Officer", "use_case": "Synthetic identities"}, "Pilot cut synthetic approvals by a third."),
    ("Nikhil Rao", "Dune Delivery", "Dune Delivery", "won", 60000, "", None, 27, 66, "Alex",
     {"title": "Director of Payments", "use_case": "Refund abuse"}, "Started with refunds. Promo abuse is the expansion."),
    ("Sylvie Martin", "Pinecrest Pay", "Pinecrest Pay", ("lost", 3, "Chose a competitor"), 120000, "", None, 5, 58, "Priyanka",
     {"title": "VP Risk", "competitor": "Legacy vendor"}, "Stayed with the incumbent after a price cut."),
    ("Hugo Bergstrom", "Gable Homes", "Gable Homes", ("lost", 2, "No budget"), 80000, "", None, 9, 37, "Tom",
     {"title": "Head of Operations"}, "Budget went to a compliance project."),
    ("Leah Abrams", "Quartz Credit", "Quartz Credit", ("lost", 1, "Went quiet"), 65000, "", None, 12, 45, "Alex",
     {"title": "Fraud Lead"}, "Three follow-ups, no reply."),
    ("Marco Santoro", "Voyager Stays", "Voyager Stays", ("lost", 4, "Bad timing"), 140000, "", None, 3, 80, "Priyanka",
     {"title": "SVP Payments"}, "Acquisition announced; all vendor decisions frozen."),
    ("Ayesha Karim", "Finch Micro", "Finch Micro", ("lost", 0, "Not a fit"), 15000, "", None, 16, 20, "Tom",
     {"title": "Founder"}, "Too small today. Fewer than a thousand transactions a month."),
    ("Declan Byrne", "Harrow Bets", "Harrow Bets", ("lost", 2, "Chose a competitor"), 90000, "", None, 22, 51, "Alex",
     {"title": "Head of Payments", "competitor": "In-house rules"}, "Decided to keep building in-house."),
]

FRAUD_MARKET = {
    "name": "Fintechs and marketplaces exposed to payment fraud (demo)",
    "brief": "Fictional sample map. Companies that move money for consumers with a small risk team: the ones a fraud "
             "prevention tool pays back fastest.",
    "segments": [{"name": "Neobanks and wallets", "description": "Consumer accounts and cards"},
                 {"name": "Marketplaces", "description": "Pay sellers, drivers or hosts"},
                 {"name": "Lenders and BNPL", "description": "Credit decisions at onboarding"},
                 {"name": "Ticketing and travel", "description": "High-value orders, heavy chargebacks"}],
    "requirements": ["Takes card or instant payments", "Risk team under ten people", "Fraud losses are visible", "Launching a new market or product"],
}
FRAUD_COMPANIES = [
    {"company": "Calder Bank", "website": "calderbank.example", "segment": "Neobanks and wallets", "fit": 5, "size": "280 people",
     "location": "New York, NY", "why": "Hiring its first fraud analysts after a wave of account-takeover complaints.",
     "signal": "Hiring for the problem we solve", "uses": "In-house rules",
     "requirements": {"Takes card or instant payments": "Debit card and instant transfers", "Risk team under ten people": "Three risk roles on the team page",
                      "Fraud losses are visible": "App reviews mention drained accounts", "Launching a new market or product": "Credit card launching in the spring"},
     "people": [{"name": "Noelle Ferrand", "title": "VP Risk", "why": "Posting the fraud analyst roles"}]},
    {"company": "Pinnacle Wallet", "website": "pinnaclewallet.example", "segment": "Neobanks and wallets", "fit": 4, "size": "150 people",
     "location": "Miami, FL", "why": "Instant payouts launched this year; fraud tooling is the processor's default.",
     "signal": "New product launched", "uses": "Processor's built-in tool",
     "requirements": {"Takes card or instant payments": "Instant payouts", "Risk team under ten people": "One risk lead",
                      "Fraud losses are visible": "", "Launching a new market or product": "Instant payouts went live in June"},
     "people": [{"name": "Diego Salazar", "title": "Head of Payments"}]},
    {"company": "Mosaic Money", "website": "mosaicmoney.example", "segment": "Neobanks and wallets", "fit": 3, "size": "600 people",
     "location": "San Francisco, CA", "why": "Large risk team and a vendor already in place.", "uses": "Legacy vendor",
     "requirements": {"Takes card or instant payments": "Cards and transfers", "Risk team under ten people": "",
                      "Fraud losses are visible": "", "Launching a new market or product": "Expanding to Canada"}},
    {"company": "Sparrow Pay", "website": "sparrowpay.example", "segment": "Neobanks and wallets", "fit": 4, "size": "70 people",
     "location": "Austin, TX", "why": "Teen banking app; chargebacks doubled according to its own blog.", "uses": "Manual review",
     "also": "Lenders and BNPL",
     "requirements": {"Takes card or instant payments": "Debit card", "Risk team under ten people": "Founder-led risk",
                      "Fraud losses are visible": "Blog post on doubled chargebacks", "Launching a new market or product": ""},
     "people": [{"name": "Hattie Brooks", "title": "Chief Operating Officer"}]},
    {"company": "Fernway Freelance", "website": "fernway.example", "segment": "Marketplaces", "fit": 5, "size": "210 people",
     "location": "Denver, CO", "why": "Payout fraud named as a risk in its funding announcement; reviews every payout by hand.",
     "signal": "Fresh funding", "uses": "Manual review",
     "requirements": {"Takes card or instant payments": "Card pay-ins, instant payouts", "Risk team under ten people": "Four people in trust and safety",
                      "Fraud losses are visible": "Named in the funding announcement", "Launching a new market or product": "Opening in the UK"},
     "people": [{"name": "Marguerite Lowe", "title": "Head of Trust and Safety", "why": "Quoted on payout fraud"}]},
    {"company": "Bramble Market", "website": "bramblemarket.example", "segment": "Marketplaces", "fit": 4, "size": "320 people",
     "location": "Portland, OR", "why": "Second-hand goods marketplace with a growing fake-seller problem.", "uses": "In-house rules",
     "requirements": {"Takes card or instant payments": "Cards", "Risk team under ten people": "Six on the risk team",
                      "Fraud losses are visible": "Seller forum threads on scams", "Launching a new market or product": ""},
     "people": [{"name": "Idris Coleman", "title": "Director of Marketplace Integrity"}]},
    {"company": "Swiftcart", "website": "swiftcart.example", "segment": "Marketplaces", "fit": 3, "size": "900 people",
     "location": "Chicago, IL", "why": "Grocery delivery; refund abuse is public, but risk is a large in-house team.", "uses": "In-house rules",
     "requirements": {"Takes card or instant payments": "Cards", "Risk team under ten people": "",
                      "Fraud losses are visible": "Refund abuse covered in the press", "Launching a new market or product": "Alcohol delivery"}},
    {"company": "Loftline", "website": "loftline.example", "segment": "Marketplaces", "fit": 4, "size": "130 people",
     "location": "Nashville, TN", "why": "Short-stay rentals; deposit fraud and fake listings, with nobody owning risk.", "uses": "Manual review",
     "also": "Ticketing and travel",
     "requirements": {"Takes card or instant payments": "Cards", "Risk team under ten people": "No risk hire yet",
                      "Fraud losses are visible": "Host complaints about fake bookings", "Launching a new market or product": ""},
     "people": [{"name": "Wyatt Greer", "title": "VP Operations"}]},
    {"company": "Brio Credit", "website": "briocredit.example", "segment": "Lenders and BNPL", "fit": 5, "size": "190 people",
     "location": "Charlotte, NC", "why": "New head of risk, and synthetic identities called out on the last earnings call.",
     "signal": "New executive", "uses": "Legacy vendor",
     "requirements": {"Takes card or instant payments": "Instant loan disbursement", "Risk team under ten people": "Eight on the risk team",
                      "Fraud losses are visible": "Charge-offs blamed on synthetic identities", "Launching a new market or product": "Small-business loans"},
     "people": [{"name": "Cornelius Achebe", "title": "Head of Risk", "why": "Joined two months ago"}]},
    {"company": "Tallyup", "website": "tallyup.example", "segment": "Lenders and BNPL", "fit": 4, "size": "240 people",
     "location": "Salt Lake City, UT", "why": "Pay-later at checkout for mid-size retailers; fraud is the processor's default.",
     "uses": "Processor's built-in tool",
     "requirements": {"Takes card or instant payments": "Cards at checkout", "Risk team under ten people": "Five analysts",
                      "Fraud losses are visible": "", "Launching a new market or product": "In-store pay-later pilot"},
     "people": [{"name": "Mirela Popescu", "title": "Director of Fraud"}]},
    {"company": "Keystone Auto Finance", "website": "keystoneauto.example", "segment": "Lenders and BNPL", "fit": 2, "size": "1,400 people",
     "location": "Dallas, TX", "why": "Long vendor contracts and a slow buying process.", "uses": "Legacy vendor",
     "requirements": {"Takes card or instant payments": "", "Risk team under ten people": "",
                      "Fraud losses are visible": "", "Launching a new market or product": ""}},
    {"company": "Encore Tickets", "website": "encoretickets.example", "segment": "Ticketing and travel", "fit": 5, "size": "160 people",
     "location": "Los Angeles, CA", "why": "Bots and stolen cards on every big sale; chargebacks named in a customer notice.",
     "signal": "Public incident last month", "uses": "Processor's built-in tool",
     "requirements": {"Takes card or instant payments": "Cards", "Risk team under ten people": "Two people",
                      "Fraud losses are visible": "Customer notice after a botted sale", "Launching a new market or product": "Resale marketplace"},
     "people": [{"name": "Josephine Tran", "title": "VP Payments", "why": "Signed the customer notice"}]},
    {"company": "Wayfarer Flights", "website": "wayfarerflights.example", "segment": "Ticketing and travel", "fit": 3, "size": "450 people",
     "location": "Atlanta, GA", "why": "High-value bookings and chargebacks, but a vendor is already in place.", "uses": "Legacy vendor",
     "requirements": {"Takes card or instant payments": "Cards", "Risk team under ten people": "",
                      "Fraud losses are visible": "Chargebacks cited in its annual report", "Launching a new market or product": ""},
     "people": [{"name": "Raymond Castillo", "title": "Director of Revenue Protection"}]},
    {"company": "Lakehouse Stays", "website": "lakehousestays.example", "segment": "Ticketing and travel", "fit": 4, "size": "95 people",
     "location": "Minneapolis, MN", "why": "Boutique stays; every disputed booking is handled by the finance lead.", "uses": "Manual review",
     "requirements": {"Takes card or instant payments": "Cards", "Risk team under ten people": "Nobody owns fraud",
                      "Fraud losses are visible": "", "Launching a new market or product": "Adding experiences"},
     "people": [{"name": "Greta Lindahl", "title": "Finance Director"}]},
]
FRAUD_READING = {
    "reading": "Almost nobody here has bought a modern fraud tool. Ten of fourteen companies run on rules they wrote, the "
               "processor's default, or people reviewing by hand, and most of those have a risk team you could seat at one table.\n"
               "The pull is strongest where the losses have gone public: a customer notice, an earnings call, a funding "
               "announcement that names fraud as a risk. Large teams with a vendor in place are the slow end of the map.",
    "implications": ["Lead where the losses have gone public; someone there already owns the problem.",
                     "Sell against doing it by hand, not against a competitor: only four have a vendor.",
                     "A small risk team is the common thread, so pitch time saved as much as losses avoided.",
                     "Marketplaces and travel overlap; one case study covers both."],
    "first_calls": [{"target": "Brio Credit", "why": "A new head of risk, two months in, with synthetic identities on the earnings call."},
                    {"target": "Encore Tickets", "why": "A botted sale last month ended in a public customer notice."},
                    {"target": "Fernway Freelance", "why": "Fresh funding, and payout fraud named as the risk."},
                    {"target": "Calder Bank", "why": "Hiring its first fraud analysts ahead of a credit card launch."},
                    {"target": "Loftline", "why": "Deposit fraud with nobody owning it."}],
}

EXAMPLES = {
    "recruiting": {"playbook": "recruiting", "title": "a recruiting agency", "rows": RECRUITING,
                   "market": TALENT_MARKET, "companies": TALENT_COMPANIES, "reading": TALENT_READING},
    "saas": {"playbook": "b2b-sales", "title": "a B2B software company selling a fraud prevention tool", "rows": SAAS,
             "market": FRAUD_MARKET, "companies": FRAUD_COMPANIES, "reading": FRAUD_READING},
}


def which_for(playbook) -> str:
    """The example that fits a playbook: the recruiting one for recruiting, the sales one for anything else."""
    return "recruiting" if playbook.name == "recruiting" else DEFAULT


def _stamp(days: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _email(name: str, company: str) -> str:
    slug = lambda text: re.sub(r"[^a-z0-9]+", "", text.lower().encode("ascii", "ignore").decode())      # noqa: E731
    first, _, last = name.partition(" ")
    return "%s.%s@%s.example" % (slug(first), slug(last) or "x", slug(company) or "company")


def load_market(desk, which: str = "") -> int:
    example = EXAMPLES[which or which_for(desk.playbook())]
    markets, name = desk.markets, example["market"]["name"]
    if markets._market(name) is not None:
        return 0
    markets.define(dict(example["market"]))
    added = len(markets.add({"market": name, "companies": example["companies"]})["added"])
    markets.reading(dict(example["reading"], market=name))
    return added


def load(desk, which: str = "") -> int:
    """Put an example on the desk. Returns how many records were added (none the second time)."""
    playbook = desk.playbook()
    which = which or which_for(playbook)
    example, store = EXAMPLES[which], desk.store
    today = dt.date.today()
    keys = {field["key"] for field in playbook.fields}
    last = len(playbook.stages) - 1
    count = 0
    for name, company, group, stage, value, step, due, quiet, age, owner, fields, note in example["rows"]:
        if any(d["name"] == name and d["source"] == SOURCE for d in store.list(status="all", query=name)):
            continue
        closing = stage == "won" or isinstance(stage, tuple)
        start = last if stage == "won" else stage[1] if isinstance(stage, tuple) else stage
        deal = store.add(name, playbook.stages[min(start, last)]["id"], actor="demo", company=company, grp=group, value=value,
                         owner=owner, next_step=step, next_due=(today + dt.timedelta(days=due)).isoformat() if due is not None else "",
                         source=SOURCE, email=_email(name, company), fields={k: v for k, v in fields.items() if k in keys})
        store.log(deal["id"], "note", note, actor="demo")
        if closing:
            end = playbook.won if stage == "won" else playbook.lost
            store.move(deal["id"], end["id"], status="won" if stage == "won" else "lost", actor=owner, label=end["label"],
                       reason="" if stage == "won" else stage[2])
        # Age the record: on the desk for `age` days, last touched `quiet` days ago.
        born, touched = _stamp(max(age, quiet)), _stamp(quiet)
        with store._conn() as conn:
            conn.execute("UPDATE activities SET at = ? WHERE deal_id = ?", (touched, deal["id"]))
            conn.execute("UPDATE activities SET at = ? WHERE deal_id = ? AND kind = 'created'", (born, deal["id"]))
            conn.execute("UPDATE deals SET created_at = ?, updated_at = ?, stage_at = ? WHERE id = ?", (born, touched, touched, deal["id"]))
        count += 1
    load_market(desk, which)
    return count


def summary(desk, which: str = "") -> str:
    """One line on what the example put on the desk, in the playbook's own words."""
    from . import cards
    playbook = desk.playbook()
    deals = [d for d in desk.store.list(status="all") if d["source"] == SOURCE]
    live = [d for d in deals if d["status"] == "open"]
    return ("%d open %s worth %s across %d %s, %d %s and %d %s" % (
        len(live), playbook.terms["deals"], cards.money(sum(d["value"] for d in live), playbook.currency),
        len({d["grp"] for d in live}), playbook.terms["groups"], len([d for d in deals if d["status"] == "won"]),
        playbook.won["label"].lower(), len([d for d in deals if d["status"] == "lost"]), playbook.lost["label"].lower()))


def clear(desk) -> int:
    """Remove every sample record and both sample maps (and anything added to the pipeline from them)."""
    store = desk.store
    doomed = [d for d in store.list(status="all") if d["source"] == SOURCE]
    for deal in doomed:
        store.delete(deal["id"])
    for name in [example["market"]["name"] for example in EXAMPLES.values()] + list(RETIRED_MAPS):
        market = desk.markets._market(name)
        if market is None or market["name"] != name:
            continue
        for target in desk.markets.targets(market["id"]):
            if target["deal_id"]:
                store.delete(target["deal_id"])
        with store._conn() as conn:
            conn.execute("DELETE FROM targets WHERE market_id = ?", (market["id"],))
            conn.execute("DELETE FROM markets WHERE id = ?", (market["id"],))
            conn.execute("DELETE FROM cards WHERE deal_id = ?", ("map:" + market["id"],))
    return len(doomed)
