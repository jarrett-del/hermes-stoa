# Development

Standard library only. The desk logic has no Hermes imports, so the tests run anywhere:

```
python3 -m unittest discover -s tests     # 263 tests, no network
hermes plugins validate . --install-deps
hermes plugins doctor . --ci
```

| File | What it holds |
|---|---|
| `__init__.py` | `register(ctx)`: tools, Slack handlers, `/gtm`, CLI, blueprints, skills |
| `desk.py` | Everything the desk can do |
| `store.py` | The SQLite ledger |
| `playbook.py`, `playbooks/` | Playbook loading, validation, and the built-ins |
| `cards.py` | Block Kit cards and their text twins |
| `slackdesk.py` | Posting cards and answering button clicks |
| `intake.py` | Lists sent in: matching rows to records, the review, and applying what was approved |
| `market.py` | Market maps: segments, requirements, what the map adds up to, and the step into the pipeline |
| `page.py` | A market map as one self-contained web page |
| `sources.py` | People Data Labs, Exa and meeting-note sources |
| `dashboard/` | The web tab: `plugin_api.py` (routes, shared with the app half), `dist/index.js` and `dist/style.css` (hand-written, no build step) |
| `desktop/plugin.js` | The Desktop app and Webapp half: the pane, the page, and the card and map drawn in a conversation |
| `desktop/art.json` | The stoa in the page header (`head`) and on the welcome screen (`welcome`), as grids of cells, one cell a CSS pixel and one base64 character: how bright the stone is, and whether a dark theme and a light theme have a dot there. `plugin_api.py` hands it to the page at `/art`, and `paint` in `plugin.js` draws it on a canvas for the theme, in color or in one ink. It is made outside this repository by a small ray marcher and a stipple pass; `artgrid.py` there writes this file |
