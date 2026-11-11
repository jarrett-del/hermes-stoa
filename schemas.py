"""Tool schemas: what the model reads to decide when to reach for the desk."""

_DEAL_REF = {"type": "string", "description": "The record's id, or a name or part of one. Ambiguous names return the candidates."}

GTM_PIPELINE = {
    "name": "gtm_pipeline",
    "description": (
        "Read the team's go-to-market pipeline from the Stoa desk. Use it before answering any question about the "
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
        "Read or change one record on the Stoa desk (a deal, candidate or opportunity, depending on the playbook). "
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
            "next_due": {"type": "string", "description": "Due date for the next step, YYYY-MM-DD."},
            "fields": {"type": "object", "description": "Extra facts keyed by the playbook's field keys; an empty value removes a key."},
            "note": {"type": "string", "description": "add: an opening note for the activity log."},
            "kind": {"type": "string", "enum": ["note", "call", "email", "meeting", "task"], "description": "log: what happened."},
            "text": {"type": "string", "description": "log: what was said or done, in a sentence or three."},
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
        "Show one record as a card. In Slack this posts an interactive card with buttons (advance to the next stage, "
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
        "Look up a person or a company in People Data Labs and, when `deal` is given, fill the record's blank fields "
        "(never overwriting what a person entered). Each uncached lookup spends one paid credit and counts against "
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
        "Turn meeting notes into pipeline updates. action='inbox' lists recent call notes that have not been filed "
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

GTM_PLAYBOOK = {
    "name": "gtm_playbook",
    "description": (
        "The playbook is what fits the desk to a business: what a record is called, the stages, the button labels, and "
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
        "Move the pipeline in or out as a CSV file. action='export' writes every record to a CSV (default: the "
        "plugin's exports folder) and returns the path; the file opens in Google Sheets or Excel as is. "
        "action='import' adds records from a CSV with a name column (company, group, stage, value, owner, email, "
        "linkedin, next_step, next_due and playbook fields are picked up when present); existing records are skipped. "
        "Use dry_run=true first to see what an import would add."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["export", "import"]},
            "path": {"type": "string", "description": "import: the CSV to read. export: where to write (optional)."},
            "dry_run": {"type": "boolean", "description": "import: count what would be added without adding it."},
        },
        "required": ["action"],
    },
}
