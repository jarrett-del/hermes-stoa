"""Tool schemas: what the model reads to decide when to reach for the desk."""

_DEAL_REF = {"type": "string", "description": "The record's id, or a name or part of one. Ambiguous names return the candidates."}

GTM_PIPELINE = {
    "name": "gtm_pipeline",
    "description": (
        "Read the team's pipeline from the Stoa desk: the board, the brief, due, quiet. "
        "Use it before answering any question about the "
        "pipeline, who is at which stage, what is due, or what has gone quiet. view='board' gives every open record by "
        "stage; 'brief' gives what needs attention today (due next steps, records gone quiet, this week's movement); "
        "'stale' and 'due' filter to those; 'list' is a flat filtered list; 'activity' is the recent activity log."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "view": {"type": "string", "enum": ["board", "brief", "list", "stale", "due", "activity"], "description": "Default board."},
            "group": {"type": "string", "description": "Filter by group (account, search, client: whatever the playbook calls it)."},
            "stage": {"type": "string", "description": "Filter by stage id or label."},
            "owner": {"type": "string", "description": "Filter by owner."},
            "status": {"type": "string", "enum": ["open", "won", "lost", "all"], "description": "Default open."},
            "query": {"type": "string", "description": "Text to match in name, company, group or next step."},
            "days": {"type": "integer", "description": "For view='activity': how many days back (default 7)."},
            "limit": {"type": "integer", "description": "Maximum records returned (default 60)."},
        },
    },
}

GTM_DEAL = {
    "name": "gtm_deal",
    "description": (
        "Look up, add or change one person, company or deal on the Stoa desk. "
        "Look here (action='get', by name) before telling anyone a person or company cannot be found, whatever else "
        "you can search. A record is a deal, candidate or opportunity, depending on the playbook. "
        "Actions: get (full record plus activity log), add, update (fields, value, owner, next step), move (to a stage, "
        "or stage='next'), close (outcome won or lost, with a reason), reopen, log (record a note, call, email or "
        "meeting, optionally setting the next step), snooze (hide from the stale list for N days). Call this whenever "
        "the user tells you something happened with a prospect, so the desk stays the source of truth. Any card "
        "already posted in Slack for the record updates itself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["get", "add", "update", "move", "close", "reopen", "log", "snooze"]},
            "deal": _DEAL_REF,
            "name": {"type": "string", "description": "add/update: the person or deal name."},
            "company": {"type": "string"},
            "group": {"type": "string", "description": "add/update: the account, search or client this belongs to."},
            "stage": {"type": "string", "description": "add: starting stage (default first). move: target stage id or label, or 'next'."},
            "value": {"type": "number", "description": "Deal size or fee, as a number."},
            "owner": {"type": "string"},
            "email": {"type": "string"},
            "linkedin": {"type": "string"},
            "source": {"type": "string", "description": "Where the record came from (referral, inbound, event…)."},
            "next_step": {"type": "string", "description": "The single next action. Pass an empty string to clear it."},
            "next_due": {"type": "string", "description": "Due date for the next step: YYYY-MM-DD (tomorrow, friday and +3d also work; '' clears it)."},
            "fields": {"type": "object", "description": "Extra facts keyed by the playbook's field keys; an empty value removes a key."},
            "note": {"type": "string", "description": "add: an opening note for the activity log."},
            "kind": {"type": "string", "enum": ["note", "call", "email", "meeting", "task"], "description": "log: what happened."},
            "text": {"type": "string", "description": "log: what was said or done, in a sentence or three."},
            "ref": {"type": "string", "description": "log: an id for the thing being logged (an email, message or call id). The same ref is logged only once."},
            "outcome": {"type": "string", "enum": ["won", "lost"], "description": "close: the outcome."},
            "reason": {"type": "string", "description": "close/move to lost: why."},
            "days": {"type": "integer", "description": "snooze: how many days (0 clears)."},
            "force": {"type": "boolean", "description": "add: create even if an open record with this name and group exists."},
        },
        "required": ["action"],
    },
}

GTM_CARD = {
    "name": "gtm_card",
    "description": (
        "Show one Stoa desk record as a card, with buttons in Slack. "
        "In Slack this posts an interactive card with buttons (advance to the next stage, "
        "draft a follow-up, enrich, close with a reason, snooze); a click changes the stage at once and asks you to "
        "write the follow-up in the card's thread. Outside Slack it returns the card as text to show the user. Use it "
        "after adding a record, when the user asks to see someone, or to bring a record into a channel."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "deal": _DEAL_REF,
            "channel": {"type": "string", "description": "Slack channel id to post in. Default: the current Slack conversation."},
            "top_level": {"type": "boolean", "description": "Post at the top level of the channel instead of in the current thread."},
        },
        "required": ["deal"],
    },
}

GTM_ENRICH = {
    "name": "gtm_enrich",
    "description": (
        "Look up a person or company in People Data Labs (paid, capped). "
        "When `deal` is given it fills the record's blank fields, never overwriting what a person entered. Each uncached lookup spends one paid credit and counts against "
        "the monthly cap, so use it when the user asks or when a record headed for outreach lacks the basics. Results "
        "are cached for 90 days. Needs PDL_API_KEY."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "deal": {"type": "string", "description": "Record to enrich and update. Its email, LinkedIn URL, name and company are used for the lookup."},
            "kind": {"type": "string", "enum": ["person", "company"], "description": "Default comes from the playbook."},
            "email": {"type": "string"},
            "linkedin": {"type": "string", "description": "LinkedIn profile URL."},
            "name": {"type": "string", "description": "Person's name (needs company too when there is no email or LinkedIn URL)."},
            "company": {"type": "string"},
            "website": {"type": "string", "description": "Company website, for kind='company'."},
        },
    },
}

GTM_NOTES = {
    "name": "gtm_notes",
    "description": (
        "Turn meeting notes into updates on the Stoa desk. action='inbox' lists recent call notes that have not been filed "
        "yet (from the team's notes folder and, if connected, Granola), each with the records it probably belongs to. "
        "'read' returns one note's text. 'file' attaches your short summary of a note to a record's activity log and "
        "can set the next step; 'skip' dismisses a note that is not about the pipeline. After a call, read the note, "
        "file it on the right record, and propose the stage change rather than making it yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["inbox", "read", "file", "skip"]},
            "note": {"type": "string", "description": "read/file/skip: the note id from the inbox."},
            "deal": {"type": "string", "description": "file: the record this note belongs to."},
            "summary": {"type": "string", "description": "file: two or three sentences on what was said and agreed. Only what the note states."},
            "next_step": {"type": "string", "description": "file: the next step that came out of the call."},
            "next_due": {"type": "string", "description": "file: its due date, YYYY-MM-DD."},
            "days": {"type": "integer", "description": "inbox: how many days back to look (default 7)."},
        },
        "required": ["action"],
    },
}

GTM_MARKET = {
    "name": "gtm_market",
    "description": (
        "Map a market on the Stoa desk and turn its companies into leads. A market map is a named set of companies sorted into segments. Each "
        "company is checked against the market's requirements with the evidence, and carries a fit score, a buying "
        "signal, what it uses today, and its likely buyers. Actions: define (name, brief, segments, requirements), add "
        "(record or update companies you researched), show (the map read back: how rare each requirement is, who "
        "holds the market, where segments meet, first calls), reading (save your interpretation and the first calls), "
        "list, search (candidate companies through Exa, if connected; otherwise use your own web tools), people (likely "
        "buyers at one company through People Data Labs; each person returned costs a credit), track (turn companies "
        "into pipeline records), dismiss, post (put the map in Slack with an Add to pipeline button on each company), "
        "and export (write the map as one self-contained web page). Use it when the user asks who to sell to, wants "
        "leads in an industry, or wants to see or share a market."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["define", "list", "add", "show", "reading", "search", "people", "track",
                                                  "dismiss", "post", "export"]},
            "market": {"type": "string", "description": "The market's name or id. May be omitted when there is only one."},
            "name": {"type": "string", "description": "define: the market's name, e.g. 'Commercial solar installers, US Southwest'."},
            "brief": {"type": "string", "description": "define: two or three sentences on who the team sells to and what makes a company a fit."},
            "segments": {"type": "array", "description": "define: 3 to 6 segments, each {name, description}.",
                         "items": {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}}}},
            "requirements": {"type": "array", "items": {"type": "string"},
                             "description": "define: 3 to 6 things a fit company shows, each checkable from public information, "
                                            "e.g. '50 to 500 people', 'Runs its own warehouse'."},
            "companies": {"type": "array", "description": "add: the companies to record. Adding one already on the map updates it.",
                          "items": {"type": "object", "properties": {
                              "company": {"type": "string"}, "website": {"type": "string"}, "segment": {"type": "string"},
                              "fit": {"type": "integer", "description": "1 (weak) to 5 (ideal) against the brief. Left out, it is read off the requirements."},
                              "size": {"type": "string", "description": "Headcount as found, e.g. '220 people'."},
                              "location": {"type": "string", "description": "'City, State' or 'City, Country'."},
                              "why": {"type": "string", "description": "One sentence of evidence for the fit."},
                              "signal": {"type": "string", "description": "A reason to reach out now: funding, hiring, a launch."},
                              "source": {"type": "string", "description": "URL the evidence came from."},
                              "uses": {"type": "string", "description": "What it uses today for this job: a vendor's name, 'Spreadsheets', 'In-house tool'."},
                              "also": {"type": "string", "description": "A second segment it could just as well sit in, if any."},
                              "requirements": {"type": "object", "description": "Each requirement you checked, keyed by its text: the "
                                               "evidence as a string if the company shows it, an empty string if it does not."},
                              "people": {"type": "array", "description": "Likely buyers you found.",
                                         "items": {"type": "object", "properties": {
                                             "name": {"type": "string"}, "title": {"type": "string"}, "linkedin": {"type": "string"},
                                             "email": {"type": "string"}, "why": {"type": "string"}}}}},
                              "required": ["company"]}},
            "reading": {"type": "string", "description": "reading: one or two short paragraphs on what the map shows. Only what the map supports."},
            "implications": {"type": "array", "items": {"type": "string"}, "description": "reading: up to four things to do about it."},
            "first_calls": {"type": "array", "description": "reading: up to seven companies to approach first, each {target, why}.",
                            "items": {"type": "object", "properties": {"target": {"type": "string"}, "why": {"type": "string"}}}},
            "segment": {"type": "string", "description": "show: only this segment."},
            "status": {"type": "string", "enum": ["new", "tracked", "dismissed"], "description": "show: filter by status."},
            "query": {"type": "string", "description": "search: a plain description of the kind of company to find."},
            "target": {"type": "string", "description": "people: the company (map id or name)."},
            "titles": {"type": "array", "items": {"type": "string"}, "description": "people: title fragments to match, e.g. ['vp operations', 'head of logistics']."},
            "targets": {"type": "array", "items": {"type": "string"}, "description": "track/dismiss: map ids or company names."},
            "contact": {"type": "string", "description": "track (one target): the person's name, if not already on the map."},
            "title": {"type": "string", "description": "track: the contact's title."},
            "email": {"type": "string"},
            "linkedin": {"type": "string"},
            "reason": {"type": "string", "description": "dismiss: why it is not a fit."},
            "channel": {"type": "string", "description": "post: Slack channel id. Default: the current conversation."},
            "share": {"type": "boolean", "description": "export: leave out buyers' names and pipeline status, for sending outside the team."},
            "limit": {"type": "integer", "description": "search: results (max 15). people: people (max 5, one credit each)."},
        },
        "required": ["action"],
    },
}

GTM_PLAYBOOK = {
    "name": "gtm_playbook",
    "description": (
        "Show, switch or write the playbook that fits the Stoa desk to a business. "
        "A playbook sets what a record is called, the stages, the button labels, and "
        "what you draft when a stage changes. Actions: list, show (the active one, or by name), use (switch), template "
        "(the default playbook's YAML plus the rules, as a starting point), save (validate and store YAML you wrote; "
        "activate=true switches to it). When a user describes their sales motion, interview them briefly, write a "
        "playbook from the template, show it to them, and save it once they agree."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "show", "use", "template", "save"]},
            "name": {"type": "string", "description": "show/use: the playbook name."},
            "yaml": {"type": "string", "description": "save: the full playbook as YAML."},
            "activate": {"type": "boolean", "description": "save: also switch the desk to this playbook."},
        },
        "required": ["action"],
    },
}

GTM_EXPORT = {
    "name": "gtm_export",
    "description": (
        "Move the Stoa desk's pipeline in or out as a CSV file. action='export' writes every record to a CSV (default: the "
        "plugin's exports folder) and returns the path; the file opens in Google Sheets or Excel as is. "
        "action='import' adds records from a CSV with a name column (company, group, stage, value, owner, email, "
        "linkedin, next_step, next_due and playbook fields are picked up when present); existing records are skipped. "
        "Use dry_run=true first to see what an import would add. For a file someone sends, prefer gtm_intake: "
        "it shows what would change, including updates to existing records, before anything is written. "
        "action='table' returns the pipeline as columns and rows, ready to write into a spreadsheet tab such as a "
        "Google Sheet; read that tab back later with gtm_intake(columns=..., rows=...) to bring edits home."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["export", "import", "table"]},
            "path": {"type": "string", "description": "import: a .csv or .tsv file in the desk's imports folder, the configured import folder or the working directory. export: a file name for the CSV (optional); it is always written to the plugin's exports folder."},
            "dry_run": {"type": "boolean", "description": "import: count what would be added without adding it."},
        },
        "required": ["action"],
    },
}

GTM_INTAKE = {
    "name": "gtm_intake",
    "description": (
        "Review a list of people or companies before it is saved to the Stoa desk. "
        "Use it whenever someone sends or pastes more than a couple of records: a PDF, a spreadsheet, an export from "
        "another tool, a photo of a sign-up sheet, a long message. Read the material yourself, then call "
        "action='propose' with what you found as rows (or with the path of a CSV, which the desk reads itself). The desk "
        "works out which rows are new, which would change a record already there, which are already there and which it "
        "cannot place, and shows the person a review to approve. Nothing is written until they press Apply or tell you "
        "to: then action='apply' (rows=[...] or skip=[...] by row number for part of it). action='show' reads a list "
        "back, 'list' shows the ones waiting, 'discard' drops one. Never add a long list record by record instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["propose", "show", "apply", "discard", "list"]},
            "source": {"type": "string", "description": "propose: where the list came from, in a few words (\"leads.pdf\", \"conference badge scans\")."},
            "rows": {
                "description": "propose: the records, at most 500. apply: the row numbers to apply (default: every new and changed row).",
                "type": "array",
                "items": {
                    "anyOf": [{"type": "integer"}, {"type": "array", "items": {}}, {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "The person, or the company when the record is a company."},
                            "company": {"type": "string"},
                            "group": {"type": "string", "description": "The account, search or client it belongs to, if any."},
                            "email": {"type": "string"},
                            "linkedin": {"type": "string"},
                            "stage": {"type": "string", "description": "Only when the material states it."},
                            "value": {"type": "number"},
                            "owner": {"type": "string"},
                            "next_step": {"type": "string"},
                            "next_due": {"type": "string", "description": "YYYY-MM-DD."},
                            "note": {"type": "string", "description": "Anything worth keeping that has no field: logged on the record."},
                            "fields": {"type": "object", "description": "The playbook's own fields, by key."},
                            "record": {"type": "string", "description": "The id of the record this row is about, to settle an unclear match."},
                        },
                    }],
                },
            },
            "path": {"type": "string", "description": "propose: a .csv or .tsv file to read instead of rows. It has to be in the desk's imports folder, the configured import folder or the working directory."},
            "columns": {"type": "array", "items": {"type": "string"},
                        "description": "propose: the header row, when rows are lists of cells (a spreadsheet tab read as a table)."},
            "id": {"type": "string", "description": "show/apply/discard: the list's id. May be left out when only one is waiting."},
            "skip": {"type": "array", "items": {"type": "integer"}, "description": "apply: row numbers to leave out."},
        },
        "required": ["action"],
    },
}
