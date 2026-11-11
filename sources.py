"""Outside data: People Data Labs enrichment and meeting notes (a notes folder, or Granola).

Standard library only. Nothing here reads a credential: callers pass the key they got from
Hermes's secret scope. Every network call in the plugin goes through ``http_json`` below, to
exactly two hosts: api.peopledatalabs.com and public-api.granola.ai.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

PDL = "https://api.peopledatalabs.com/v5"
GRANOLA = "https://public-api.granola.ai/v1"
NOTE_SUFFIXES = (".md", ".markdown", ".txt")
NOTE_CHARS = 24000


def http_json(url: str, *, headers: Optional[dict] = None, params: Optional[dict] = None, timeout: int = 30) -> tuple:
    """GET a JSON document. Returns ``(status, body)``; never raises for an HTTP error status."""
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    request = urllib.request.Request(url, headers=dict({"Accept": "application/json",
                                                        "User-Agent": "hermes-stoa"}, **(headers or {})))
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            try:
                body = json.loads(exc.read() or b"{}")
            except ValueError:
                body = {}
            return exc.code, body
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return 0, {"error": str(exc)}
    return 0, {"error": "request failed"}


# -- People Data Labs ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    """PDL's free tier returns ``true`` in place of fields it hides; keep real strings only."""
    return value.strip() if isinstance(value, str) else ""


def summarize_person(data: dict) -> dict:
    jobs = []
    for exp in (data.get("experience") or [])[:5]:
        company = (exp.get("company") or {}).get("name") or ""
        title = (exp.get("title") or {}).get("name") or ""
        if company or title:
            jobs.append({"title": title, "company": company, "start": _text(exp.get("start_date")),
                         "end": _text(exp.get("end_date")) or ("present" if exp.get("is_primary") else "")})
    schools = []
    for edu in (data.get("education") or [])[:2]:
        school = (edu.get("school") or {}).get("name") or ""
        if school:
            schools.append({"school": school, "degrees": (edu.get("degrees") or [])[:2]})
    out = {
        "name": _text(data.get("full_name")).title(),
        "title": _text(data.get("job_title")),
        "company": _text(data.get("job_company_name")),
        "company_industry": _text(data.get("job_company_industry")),
        "company_size": _text(data.get("job_company_size")),
        "location": _text(data.get("location_name")) or _text(data.get("location_country")),
        "linkedin": _text(data.get("linkedin_url")),
        "experience": jobs,
        "education": schools,
        "skills": [s for s in (data.get("skills") or []) if isinstance(s, str)][:12],
    }
    return {k: v for k, v in out.items() if v}


def summarize_company(data: dict) -> dict:
    location = data.get("location") if isinstance(data.get("location"), dict) else {}
    out = {
        "name": _text(data.get("display_name")) or _text(data.get("name")),
        "website": _text(data.get("website")),
        "industry": _text(data.get("industry")),
        "size": _text(data.get("size")),
        "employees": data.get("employee_count") if isinstance(data.get("employee_count"), int) else None,
        "founded": data.get("founded") if isinstance(data.get("founded"), int) else None,
        "location": _text(location.get("name")),
        "linkedin": _text(data.get("linkedin_url")),
        "summary": _text(data.get("summary"))[:500],
        "tags": [t for t in (data.get("tags") or []) if isinstance(t, str)][:8],
    }
    return {k: v for k, v in out.items() if v}


def pdl_person(key: str, *, email: str = "", linkedin: str = "", name: str = "", company: str = "",
               http: Callable = http_json) -> dict:
    params = {"email": email, "profile": linkedin, "min_likelihood": 6}
    if not (email or linkedin):
        if not (name and company):
            return {"ok": False, "error": "need an email, a LinkedIn URL, or a name plus company"}
        params.update({"name": name, "company": company})
    status, body = http(PDL + "/person/enrich", headers={"X-Api-Key": key}, params=params)
    if status == 200 and isinstance(body.get("data"), dict):
        return {"ok": True, "match": True, "likelihood": body.get("likelihood"), "profile": summarize_person(body["data"])}
    if status == 404:
        return {"ok": True, "match": False}
    return {"ok": False, "error": _pdl_error(status, body)}


def pdl_company(key: str, *, website: str = "", name: str = "", http: Callable = http_json) -> dict:
    if not (website or name):
        return {"ok": False, "error": "need a company website or name"}
    status, body = http(PDL + "/company/enrich", headers={"X-Api-Key": key}, params={"website": website, "name": name})
    if status == 200:
        return {"ok": True, "match": True, "profile": summarize_company(body)}
    if status == 404:
        return {"ok": True, "match": False}
    return {"ok": False, "error": _pdl_error(status, body)}


def _pdl_error(status: int, body: dict) -> str:
    if status in (401, 403):
        return "People Data Labs rejected the API key"
    if status == 402:
        return "People Data Labs account is out of credits"
    if status == 429:
        return "People Data Labs rate limit reached; try again in a minute"
    message = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else body.get("error")
    return "People Data Labs request failed (%s)%s" % (status or "network", ": %s" % message if message else "")


# -- Meeting notes ------------------------------------------------------------------------------
def folder_notes(folder: str, days: int = 7) -> list:
    """Markdown and text files in a folder (and one level below), newest first.

    Works with anything that writes notes to disk: an Obsidian vault folder, an export from an
    open-source meeting recorder, or files you drop in by hand.
    """
    root = Path(folder).expanduser() if folder else None
    if root is None or not root.is_dir():
        return []
    cutoff = time.time() - days * 86400
    out = []
    for path in list(root.glob("*")) + list(root.glob("*/*")):
        if not path.is_file() or path.suffix.lower() not in NOTE_SUFFIXES or path.name.startswith("."):
            continue
        stat = path.stat()
        if stat.st_mtime < cutoff:
            continue
        ident = hashlib.sha1(("%s|%d|%d" % (path, stat.st_size, int(stat.st_mtime))).encode()).hexdigest()[:12]
        out.append({"id": "file:" + ident, "source": "folder", "path": str(path), "title": _file_title(path),
                    "when": dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "attendees": []})
    return sorted(out, key=lambda n: n["when"], reverse=True)


def _file_title(path: Path) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(15):
                line = handle.readline()
                if not line:
                    break
                if line.startswith("#"):
                    return line.lstrip("#").strip()[:120]
                match = re.match(r"^title:\s*(.+)$", line, re.I)
                if match:
                    return match.group(1).strip().strip("\"'")[:120]
    except OSError:
        pass
    return path.stem.replace("_", " ").replace("-", " ")[:120]


def read_folder_note(note: dict) -> str:
    try:
        return Path(note["path"]).read_text(encoding="utf-8", errors="replace")[:NOTE_CHARS]
    except OSError as exc:
        return "(could not read %s: %s)" % (note.get("path"), exc)


def granola_notes(key: str, days: int = 7, http: Callable = http_json, limit: int = 60) -> tuple:
    """Recent Granola notes as ``(notes, error)``. Summaries only; transcripts are never fetched."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out, cursor = [], None
    while len(out) < limit:
        status, page = http(GRANOLA + "/notes", headers={"Authorization": "Bearer " + key},
                            params={"updated_after": since, "page_size": 30, "cursor": cursor})
        if status != 200:
            return out, ("Granola rejected the API key" if status in (401, 403) else "Granola request failed (%s)" % (status or "network"))
        for note in page.get("notes") or []:
            out.append(_granola_brief(note))
        cursor = page.get("cursor")
        if not page.get("hasMore") or not cursor:
            break
    return sorted(out, key=lambda n: n["when"], reverse=True)[:limit], ""


def _granola_people(note: dict) -> list:
    event = note.get("calendar_event") or {}
    people = {}
    for person in (note.get("attendees") or []) + (event.get("invitees") or []):
        email = (person.get("email") or "").lower()
        if email:
            people[email] = person.get("name") or people.get(email) or ""
    return [{"email": email, "name": name} for email, name in sorted(people.items())]


def _granola_brief(note: dict) -> dict:
    event = note.get("calendar_event") or {}
    return {"id": "granola:" + str(note.get("id")), "source": "granola", "title": note.get("title") or "(untitled)",
            "when": event.get("scheduled_start_time") or note.get("created_at") or "",
            "attendees": _granola_people(note), "url": note.get("web_url") or ""}


def read_granola_note(key: str, note_id: str, http: Callable = http_json) -> dict:
    raw_id = note_id.split(":", 1)[-1]
    status, note = http(GRANOLA + "/notes/" + urllib.parse.quote(raw_id, safe=""), headers={"Authorization": "Bearer " + key})
    if status != 200:
        return {"ok": False, "error": "Granola note not available (%s)" % (status or "network")}
    brief = _granola_brief(note)
    parts = [note.get("summary_markdown") or note.get("summary_text") or "(no summary yet)"]
    private = note.get("private_notes_markdown") or note.get("private_notes_text")
    if private:
        parts.append("## My notes\n" + private)
    return dict(brief, ok=True, text="\n\n".join(parts)[:NOTE_CHARS])
