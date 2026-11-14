/**
 * Stoa in the Hermes Desktop app and the Webapp.
 *
 * One file, no build step. It adds a Stoa page (Today, the pipeline, the market map, the activity
 * log and Setup), a live pane that sits beside the conversation, and three components the agent
 * can draw inside a reply: a record card, a market map, and the review of a list someone sent in.
 *
 * Everything here talks to the plugin's own backend (/api/plugins/stoa, the same routes the web
 * dashboard tab uses) through ctx.rest. A button or a form saves at once, with no model involved;
 * when a click calls for writing, one line is sent into the open chat (host.composer.submit, the
 * same as typing it) and the agent writes the draft there. The page, which has no chat beside it,
 * keeps a conversation of its own for its talk box (see "talking to the desk" below).
 */
import { atom, host, queryClient, useQuery, useValue } from '@hermes/plugin-sdk'
import { createElement as h, useEffect, useRef, useState } from 'react'

let rest = null // ctx.rest, set in register()
let storage = null // ctx.storage, set in register()

// What the pane and the page are narrowed to. One store, so both show the same thing. `stage` is
// not a filter: it is the stage picked out on the board, where every column keeps its cards.
const NO_FILTER = { text: '', owner: '', group: '', stage: '', due: false, quiet: false }
const $filter = atom({ ...NO_FILTER })
function setFilter(patch) {
  const next = { ...$filter.get(), ...patch }
  $filter.set(next)
  // Only whose records and which group are remembered between visits. Anything that hides records
  // for a moment (a search, due, quiet, a picked-out stage) starts clear, so nothing looks missing.
  if (storage) storage.set('filter', { owner: next.owner, group: next.group })
}
const filtering = f => Boolean(f.text || f.owner || f.group || f.due || f.quiet)
function matches(deal, f) {
  if (f.owner && deal.owner !== f.owner) return false
  if (f.group && deal.group !== f.group) return false
  if ((f.due || f.quiet) && !((f.due && deal.due) || (f.quiet && deal.stale && !deal.due))) return false
  if (!f.text) return true
  return [deal.name, deal.company, deal.group, deal.next_step, deal.owner, deal.stage_label].join(' ').toLowerCase().includes(f.text.toLowerCase())
}
const STATE = ['stoa', 'state']
const refresh = () => queryClient.invalidateQueries({ queryKey: ['stoa'] })
const $tab = atom('today') // the page's tab: today | pipeline | cash | market | activity | setup
const $draft = atom('') // words put in the dock for the person to finish and send
let onPage = false // the Stoa page is what is on screen: a click that calls for writing goes to its dock
const $view = atom('board') // the pipeline tab: board | companies | groups
const $pipe = atom('') // which pipeline the page is showing ('' = the first): an id from state.pipelines
const $last = atom(null) // what was just done, while it can be taken back: { deal, name, text, op }
let lastTimer = 0

/** Offer to take back what was just done. The offer goes away by itself. */
function offerUndo(last) {
  if (!last || !onPage) return
  $last.set(last)
  clearTimeout(lastTimer)
  lastTimer = setTimeout(() => { if ($last.get() === last) $last.set(null) }, 14000)
}

/** A record taken off the desk (its add was undone) comes back, log and all. */
async function putBack(dealId) {
  try {
    const result = await rest('/deal/' + encodeURIComponent(dealId) + '/restore', { method: 'POST', body: {} })
    refresh()
    host.notify({ kind: 'success', message: result.name + ' is back on the desk.' })
  } catch (error) {
    host.notifyError(error, 'Stoa could not put that back')
  }
}

async function undo(dealId, op) {
  try {
    const result = await rest('/deal/' + encodeURIComponent(dealId) + '/undo', { method: 'POST', body: { op: op || '' } })
    $last.set(null)
    refresh()
    host.notify({ kind: 'info', message: 'Taken back: ' + result.undid + (result.removed ? '. ' + result.name + ' is off the desk.' : '') })
    return result
  } catch (error) {
    $last.set(null)
    refresh()
    host.notifyError(error, 'Not taken back')     // nothing left to undo, or it would overwrite a later change (the message says which)
    return null
  }
}

// -- small helpers -------------------------------------------------------------------------------
const SYMBOL = { USD: '$', EUR: '€', GBP: '£' }

function money(value, currency) {
  if (!value) return ''
  const sign = SYMBOL[currency] ?? ''
  const text = value >= 1e6 ? (value / 1e6).toFixed(1) + 'M' : value >= 1e4 ? Math.round(value / 1e3) + 'k' : Math.round(value).toLocaleString()
  return sign ? sign + text : text + ' ' + (currency || '')
}

function today() {
  const d = new Date()
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0')
}

function ago(stamp) {
  const then = Date.parse(stamp || '')
  if (!then) return ''
  const hours = (Date.now() - then) / 36e5
  if (hours < 1) return 'just now'
  if (hours < 24) return Math.floor(hours) + 'h ago'
  if (hours < 48) return 'yesterday'
  return Math.floor(hours / 24) + 'd ago'
}

function dueLabel(deal) {
  if (!deal.next_due) return ''
  if (deal.next_due < today()) return 'overdue'
  if (deal.next_due === today()) return 'today'
  const d = new Date(deal.next_due + 'T12:00:00')
  return isNaN(d) ? deal.next_due : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

const dueTone = deal => (deal.due ? (deal.next_due < today() ? 'bad' : 'warn') : '')
const cap = text => (text ? text.charAt(0).toUpperCase() + text.slice(1) : '')
const plural = (n, one, many) => n + ' ' + (n === 1 ? one : many)

function useDesk() {
  return useQuery({ queryKey: STATE, queryFn: () => rest('/state'), refetchInterval: 5000, staleTime: 1500 })
}

/** Send one line into the chat, as if typed: the conversation the card is in, else the one in focus. */
function toChat(text, sessionId) {
  if (onPage) {
    if ($talk.get().busy) {
      host.notify({ kind: 'info', message: 'Hermes is still on the last thing. Ask again when it has answered.' })
      return false
    }
    say(text)
    return true
  }
  const composer = host.composer
  let sent = false
  try {
    const focused = host.state.focusedSessionId ? host.state.focusedSessionId.get() : null
    for (const address of [sessionId, focused, null, 'new']) {
      if (sent || address === undefined || !composer) continue
      sent = composer.submit(address, text) === true
    }
  } catch {
    sent = false
  }
  if (!sent) host.notify({ kind: 'info', message: 'Saved. The draft is waiting on the record: open a chat and ask for it.' })
  return sent
}

async function act(dealId, body, sessionId) {
  try {
    const result = await rest('/deal/' + encodeURIComponent(dealId) + '/act', { method: 'POST', body })
    refresh()
    if (result && result.notice) host.notify({ kind: 'info', message: result.notice })
    if (result && result.ask) toChat(result.ask, sessionId)
    if (result && result.last) offerUndo(result.last)
    return result
  } catch (error) {
    host.notifyError(error, 'Stoa could not save that')
    return null
  }
}

async function track(target, sessionId) {
  try {
    const result = await rest('/target/' + encodeURIComponent(target.id) + '/track', { method: 'POST', body: {} })
    refresh()
    if (result && result.ask) toChat(result.ask, sessionId)
    else if (result && result.notice) host.notify({ kind: 'info', message: result.notice })
    else host.notify({ kind: 'success', message: target.company + ' is in the pipeline.' })
  } catch (error) {
    host.notifyError(error, 'Stoa could not add that company')
  }
}

// -- talking to the desk from its own page -------------------------------------------------------
// The page has no conversation beside it, so it keeps one of its own: an ordinary session named
// "Stoa desk", made and resumed through the gateway the way the app makes any session, and listed
// in the sidebar like any other. What is typed (or dictated) into the dock at the foot of the page
// is submitted to it, and the reply is read off the gateway's event stream (see register) and
// shown above the dock.
const $talk = atom({ busy: false, open: false, turns: [] })
const $attached = atom(null) // a small text file waiting to go with the next message: { name, text }
const talkSession = { runtime: '', stored: '', primed: false, turn: 0 }
const setTalk = patch => $talk.set({ ...$talk.get(), ...patch })
const PRIME = 'This conversation is the talk box on the Stoa desk page. Act on the Stoa desk with the gtm tools, and answer in a few '
  + 'short lines: the page already shows the pipeline. What is said here is for the desk: do not also write it to another system '
  + '(a spreadsheet, another CRM, email) unless asked to.\n\n'

/** The name of the pipeline the page is showing, when the desk has more than one ('' otherwise). */
function onScreen() {
  const state = queryClient.getQueryData(STATE)
  const pipes = (state && state.pipelines) || []
  if (pipes.length < 2) return ''
  const found = pipes.find(item => item.id === state.pipeline)
  return found ? found.name : ''
}

/** Change the turn being answered, which is always the last one. */
function setTurn(patch) {
  const talk = $talk.get()
  if (!talk.turns.length) return
  const turns = talk.turns.slice()
  const last = turns[turns.length - 1]
  turns[turns.length - 1] = { ...last, ...(typeof patch === 'function' ? patch(last) : patch) }
  $talk.set({ ...talk, turns })
}

async function talkOpen() {
  if (talkSession.runtime) return talkSession.runtime
  const stored = talkSession.stored || ((storage && storage.get('talk', {})) || {}).stored || ''
  if (stored) {
    try {
      const resumed = await host.request('session.resume', { session_id: stored, omit_messages: true })
      if (resumed && resumed.session_id) {
        Object.assign(talkSession, { runtime: resumed.session_id, stored, primed: true })
        return talkSession.runtime
      }
    } catch { /* that conversation is gone: start another */ }
  }
  const made = await host.request('session.create', { title: 'Stoa desk' })
  const runtime = (made && made.session_id) || ''
  if (!runtime) throw new Error('Hermes did not open a conversation')
  Object.assign(talkSession, { runtime, stored: made.stored_session_id || runtime, primed: false })
  if (storage) storage.set('talk', { stored: talkSession.stored })
  return runtime
}

async function say(text, file) {
  const line = String(text || '').trim()
  if ((!line && !file) || $talk.get().busy) return
  const turn = ++talkSession.turn
  const shown = line || 'Here is ' + file.name + '.'
  const typed = file ? shown + '\n\nAttached file "' + file.name + '":\n```\n' + file.text + '\n```' : shown
  const here = onScreen()
  const sent = here ? '(On the Stoa page I am looking at the pipeline “' + here + '”.)\n\n' + typed : typed
  $talk.set({ busy: true, open: true,
    turns: $talk.get().turns.slice(-7).concat([{ you: shown, file: file ? file.name : '', reply: '', note: 'Sending…', error: '' }]) })
  const submit = async () => {
    const id = await talkOpen()
    await host.request('prompt.submit', { session_id: id, text: talkSession.primed ? sent : PRIME + sent })
    talkSession.primed = true
  }
  try {
    try {
      await submit()
    } catch (first) {
      if (!talkSession.runtime) throw first
      talkSession.runtime = '' // the gateway let the idle session go: resume it (or start another) and send once more
      await submit()
    }
    if (talkSession.turn === turn && $talk.get().busy) setTurn(last => (last.reply ? {} : { note: 'Hermes is working on it…' }))
    setTimeout(() => {
      if (talkSession.turn !== turn || !$talk.get().busy) return
      setTalk({ busy: false })
      setTurn({ note: 'Still working. Open the conversation to follow it; the desk updates when it is done.' })
    }, 180000)
  } catch (error) {
    setTalk({ busy: false })
    setTurn({ note: '', error: 'Hermes could not take that: ' + String((error && error.message) || error) })
  }
}

function openTalk() {
  if (talkSession.stored) Promise.resolve(host.openSession(talkSession.stored)).catch(() => {})
  else host.notify({ kind: 'info', message: 'Say something to the desk first; the conversation starts then.' })
}

// A file handed to the desk. A CSV is read by the desk itself and comes back as a review, with no
// model in between. A small text file rides along with the next message. Anything else (a PDF, an
// image, a spreadsheet) needs the full chat, which can read it.
const LIST_FILE = /\.(csv|tsv)$/i
const TEXT_FILE = /\.(txt|md|markdown|json|vcf|log|ya?ml)$/i

async function takeFile(file) {
  if (!file) return
  try {
    if (LIST_FILE.test(file.name)) {
      if (file.size > 2e6) return host.notify({ kind: 'warning', message: 'That list is over 2 MB. Send it in parts.' })
      const text = await file.text()
      const result = await rest('/intake', { method: 'POST', body: { name: file.name, text } })
      refresh()
      if (result.unread) { // the desk could not tell which column is the name: Hermes reads it, and asks if it cannot tell either
        if (text.length > 60000) return host.notify({ kind: 'info', message: 'Stoa could not tell which column of ' + file.name + ' holds the name. It is too long for the box here: press "Open the conversation" and drop it there.' })
        host.notify({ kind: 'info', message: result.headline })
        return say('Stoa could not tell which column of this list holds the name. Read it, work out which column is which, and put it on the desk as a list for my review. If you cannot tell, ask me.', { name: file.name, text })
      }
      host.notify({ kind: result.waiting ? 'success' : 'info', message: result.headline })
      const page = document.querySelector('.stoa-page')
      if (result.waiting) $tab.set('today') // where everything waiting for a yes or a no is
      if (result.waiting && page) page.scrollTo({ top: 0, behavior: 'smooth' })
      return
    }
    if ((TEXT_FILE.test(file.name) || String(file.type || '').startsWith('text/')) && file.size <= 60000) {
      $attached.set({ name: file.name, text: await file.text() })
      return
    }
    host.notify({ kind: 'info', message: 'A PDF, an image or a spreadsheet goes through the full chat: press "Open the conversation" and drop it there.' })
  } catch (error) {
    host.notifyError(error, 'Stoa could not read that file')
  }
}

// -- look: the app's own variables only, so every theme carries it -------------------------------
const CSS = `
/* Colour: all of it comes from the app's theme. One accent, the app's own (--ui-accent), used the way the
   app uses it: filled for the main action, tinted with a hairline for "you are here". */
.stoa { font-size: 13px; line-height: 1.45; color: var(--ui-text-primary);
  --stoa-accent: var(--ui-accent);
  --stoa-on-accent: var(--dt-midground-foreground, #fff);
  --stoa-accent-text: color-mix(in oklab, var(--ui-accent) 58%, var(--ui-text-primary));
  --stoa-accent-soft: color-mix(in srgb, var(--ui-accent) 16%, transparent);
  --stoa-accent-line: color-mix(in srgb, var(--ui-accent) 58%, transparent); }
.stoa *, .stoa *::before { box-sizing: border-box; }
.stoa-pane { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.stoa-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 14px 12px 18px; }
.stoa-page { height: 100%; overflow: auto; padding: 28px 34px 0; }
.stoa-wrap { max-width: 1320px; margin: 0 auto; }
.stoa-eyebrow { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 10px; letter-spacing: .14em; text-transform: uppercase; color: var(--ui-text-tertiary); }
.stoa-figure { font-size: 30px; font-weight: 600; letter-spacing: -.02em; line-height: 1.1; font-variant-numeric: tabular-nums; margin-top: 4px; }
.stoa-figure small { font-size: 12px; font-weight: 400; letter-spacing: 0; color: var(--ui-text-tertiary); margin-left: 7px; }
.stoa-page .stoa-figure { font-size: 46px; }
.stoa-rule { height: 2px; border-radius: 2px; margin: 12px 0 4px; background: linear-gradient(90deg, var(--ui-text-primary), var(--ui-text-tertiary) 46%, var(--stoa-accent)); }
.stoa-sub { color: var(--ui-text-tertiary); font-size: 12px; }
.stoa-clip { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stoa-sec { margin-top: 16px; }
.stoa-sechead { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; padding: 0 8px 4px; }
.stoa-row { display: flex; gap: 8px; align-items: flex-start; width: 100%; padding: 6px 8px; border: 0; border-radius: 6px; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-row:hover { background: var(--ui-row-hover-background); }
.stoa-row[data-open="1"] { background: var(--stoa-accent-soft); box-shadow: inset 0 0 0 1px var(--stoa-accent-line); }
.stoa-dot { flex: none; width: 6px; height: 6px; margin-top: 6px; border-radius: 50%; background: var(--ui-text-quaternary); }
.stoa-dot[data-tone="bad"] { background: var(--ui-red); }
.stoa-dot[data-tone="warn"] { background: var(--ui-orange); }
.stoa-dot[data-tone="good"] { background: var(--stoa-accent); }
.stoa-grow { flex: 1; min-width: 0; }
.stoa-name { font-weight: 550; }
.stoa-num { flex: none; font-size: 12px; font-variant-numeric: tabular-nums; color: var(--ui-text-secondary); white-space: nowrap; }
.stoa-num[data-tone="bad"] { color: var(--ui-red); }
.stoa-num[data-tone="warn"] { color: var(--ui-orange); }
.stoa-box { padding: 12px 14px; border: 1px solid var(--ui-stroke-secondary); border-radius: 10px; background: var(--ui-bg-card); }
.stoa-detail { margin: 2px 0 10px; }
.stoa-inchat { max-width: 460px; margin: 6px 0; }
.stoa-meter { display: flex; gap: 3px; margin: 8px 0 4px; }
.stoa-meter i { flex: 1; height: 3px; border-radius: 2px; background: var(--ui-stroke-secondary); }
.stoa-meter i[data-on="1"] { background: var(--stoa-accent); }
.stoa-facts { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 14px; margin: 14px 0 2px; padding-top: 12px; border-top: 1px solid var(--ui-stroke-tertiary); }
.stoa-facts span { display: block; overflow-wrap: anywhere; }
.stoa-next { margin-top: 10px; }
.stoa-log { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--ui-stroke-tertiary); }
.stoa-log p { margin: 0 0 5px; font-size: 12px; color: var(--ui-text-secondary); }
.stoa-btns { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
.stoa-btn, .stoa-select { height: 28px; padding: 0 10px; border: 1px solid var(--ui-stroke-primary); border-radius: 6px; background: transparent; color: var(--ui-text-primary); font: inherit; font-size: 12px; cursor: pointer; }
.stoa-btn, .stoa-select { transition: background .15s ease, opacity .15s ease; }
.stoa-btn:hover, .stoa-select:hover { background: var(--ui-control-hover-background); }
.stoa-btn[data-kind="primary"]:hover { background: var(--stoa-accent); opacity: .88; }
.stoa-btn[data-kind="primary"] { border-color: var(--stoa-accent); background: var(--stoa-accent); color: var(--stoa-on-accent); font-weight: 550; }
.stoa-btn[data-kind="quiet"] { border-color: transparent; color: var(--ui-text-secondary); }
.stoa-btn:disabled, .stoa-select:disabled { opacity: .5; cursor: default; }
.stoa-chip { display: inline-block; padding: 1px 6px; border: 1px solid var(--ui-stroke-secondary); border-radius: 4px; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 10px; letter-spacing: .08em; text-transform: uppercase; color: var(--ui-text-tertiary); white-space: nowrap; }
.stoa-chip[data-tone="bad"] { color: var(--ui-red); border-color: var(--ui-red); }
.stoa-chip[data-tone="warn"] { color: var(--ui-orange); border-color: var(--ui-orange); }
.stoa-chip[data-tone="good"] { color: var(--stoa-accent-text); border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); }
.stoa-chips { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }
.stoa-foot { display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 12px; border-top: 1px solid var(--ui-stroke-tertiary); }
.stoa-tabs { display: inline-flex; gap: 2px; margin: 22px 0 18px; padding: 3px; border: 1px solid var(--ui-stroke-secondary); border-radius: 10px; background: var(--ui-bg-card); }
.stoa-tab { display: inline-flex; align-items: center; gap: 8px; height: 32px; padding: 0 15px; border: 0; border-radius: 7px; background: transparent; color: var(--ui-text-secondary); font: inherit; font-size: 13px; cursor: pointer; outline: none; transition: background .15s ease, color .15s ease; }
.stoa-tab:hover { color: var(--ui-text-primary); background: var(--ui-control-hover-background); }
.stoa-tab[data-on="1"], .stoa-tab[data-on="1"]:hover { background: var(--stoa-accent-soft); box-shadow: inset 0 0 0 1px var(--stoa-accent-line); color: var(--ui-text-primary); font-weight: 600; }
.stoa-tab small { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 10px; opacity: .6; }
.stoa button:focus-visible, .stoa select:focus-visible, .stoa input:focus-visible { outline: 2px solid var(--stoa-accent-line); outline-offset: 2px; }
.stoa-split { display: grid; grid-template-columns: minmax(0, 1fr); gap: 20px; align-items: start; }
.stoa-split[data-side="1"] { grid-template-columns: minmax(0, 1fr) 360px; }
.stoa-split > .stoa-box { position: sticky; top: 0; max-height: calc(100vh - 235px); overflow-y: auto; }   /* it ends above the dock */
.stoa-board { display: grid; grid-auto-flow: column; grid-auto-columns: minmax(176px, 1fr); gap: 12px; overflow-x: auto; padding-bottom: 8px; }
.stoa-colhead { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; padding: 0 2px 8px; }
.stoa-card { display: block; width: 100%; margin-bottom: 8px; padding: 10px 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: 8px; background: var(--ui-bg-card); color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-card { transition: border-color .15s ease, transform .15s ease; }
.stoa-card:hover, .stoa-card[data-open="1"] { border-color: var(--ui-stroke-primary); }
.stoa-card:hover { transform: translateY(-1px); }
.stoa-card[data-open="1"] { border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); }
.stoa-cardfoot { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; margin-top: 6px; font-size: 12px; }
.stoa-cardfoot em { flex: none; font-style: normal; font-size: 11px; color: var(--ui-text-tertiary); }
.stoa-card h5 { display: flex; justify-content: space-between; gap: 8px; margin: 0; font-size: 13px; font-weight: 550; }
.stoa-empty { padding: 10px 2px; color: var(--ui-text-quaternary); }
.stoa-cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; }
.stoa-company { padding: 10px 0; border-top: 1px solid var(--ui-stroke-tertiary); }
.stoa-company:first-of-type { border-top: 0; }
.stoa-fit { letter-spacing: 1px; color: var(--ui-text-primary); white-space: nowrap; }
.stoa-fit s { text-decoration: none; color: var(--ui-text-quaternary); }
.stoa-rate { display: grid; grid-template-columns: minmax(0, 1fr) 90px 44px; gap: 10px; align-items: center; padding: 4px 0; }
.stoa-rate u { display: block; height: 4px; border-radius: 2px; background: var(--ui-stroke-secondary); overflow: hidden; }
.stoa-rate u i { display: block; height: 100%; background: var(--ui-text-primary); }
.stoa-reading { max-width: 70ch; margin: 8px 0 0; color: var(--ui-text-secondary); white-space: pre-line; }
.stoa-h2 { margin: 26px 0 8px; font-size: 15px; font-weight: 600; }
.stoa-filters { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.stoa-filters .stoa-input { flex: 1 1 120px; min-width: 0; height: 28px; padding: 0 9px; border: 1px solid var(--ui-stroke-secondary); border-radius: 6px; background: transparent; color: var(--ui-text-primary); font: inherit; font-size: 12px; outline: none; }\n.stoa-filters .stoa-input:focus { border-color: var(--ui-stroke-primary); }\n.stoa-filters .stoa-input::placeholder { color: var(--ui-text-quaternary); }
.stoa-filters .stoa-select { max-width: 150px; }
.stoa-toggle { height: 28px; padding: 0 9px; border: 1px solid var(--ui-stroke-secondary); border-radius: 6px; background: transparent; color: var(--ui-text-secondary); font: inherit; font-size: 12px; cursor: pointer; }
.stoa-toggle[data-on="1"] { border-color: var(--stoa-accent-line); color: var(--ui-text-primary); background: var(--stoa-accent-soft); }
.stoa-funnel { margin: 0 0 20px; padding: 16px 18px 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: 10px; background: var(--ui-bg-card); }
.stoa-funnel svg { display: block; width: 100%; height: auto; overflow: visible; }
.stoa-funnel text { font-family: ui-monospace, "SF Mono", Menlo, monospace; }
.stoa-fn-block { fill: url(#stoa-fn-fill); }
.stoa-fn-won { fill: var(--stoa-accent); filter: drop-shadow(0 0 7px color-mix(in srgb, var(--stoa-accent) 55%, transparent)); }
.stoa-fn-block, .stoa-fn-won { transform-box: fill-box; transform-origin: center; animation: stoa-rise .7s cubic-bezier(.2, .7, .2, 1) both; transition: filter .15s ease; }
.stoa-fn-link { fill: url(#stoa-fn-flow); }
.stoa-fn-pill { fill: var(--ui-bg-card); stroke: var(--ui-stroke-secondary); }
.stoa-fn-hit { fill: transparent; }
.stoa-fn-step[data-press="1"] { cursor: pointer; }
.stoa-fn-step[data-press="1"]:hover .stoa-fn-block { filter: brightness(1.3); }
.stoa-fn-step[data-on="1"] .stoa-fn-block { fill: var(--stoa-accent); opacity: .85; }
.stoa-fn-step[data-on="1"] .stoa-fn-label { fill: var(--stoa-accent-text); }
.stoa-fn-label { font-size: 9.5px; letter-spacing: .12em; fill: var(--ui-text-tertiary); }
.stoa-fn-num { font-size: 15px; font-weight: 600; fill: var(--ui-text-primary); font-family: inherit !important; }
.stoa-fn-pct { font-size: 11px; fill: var(--ui-text-primary); }
.stoa-fn-sub { font-size: 9px; letter-spacing: .06em; fill: var(--ui-text-tertiary); }
.stoa-funnel-foot { display: flex; flex-wrap: wrap; gap: 4px 18px; margin-top: 6px; font-size: 12px; color: var(--ui-text-tertiary); }
.stoa-funnel-foot b { color: var(--ui-text-primary); font-weight: 600; }
.stoa-list { max-height: 340px; overflow-y: auto; margin-top: 10px; }
.stoa-pick { display: flex; gap: 9px; align-items: flex-start; padding: 7px 0; border-top: 1px solid var(--ui-stroke-tertiary); cursor: pointer; }
.stoa-pick:first-child { border-top: 0; }
.stoa-pick input { flex: none; margin-top: 3px; accent-color: var(--stoa-accent); }
.stoa-pick[data-off="1"] .stoa-grow { opacity: .5; }
.stoa-rest { margin-top: 10px; font-size: 12px; color: var(--ui-text-tertiary); }
.stoa-rest summary { cursor: pointer; }
.stoa-rest p { margin: 4px 0 0 14px; }
.stoa-reply { margin-top: 10px; overflow-wrap: anywhere; }
.stoa-reply p { margin: 0 0 8px; }
.stoa-reply p:last-child { margin-bottom: 0; }
/* The dock floats at the foot of the page; the board fades out beneath it instead of running into it. */
.stoa-dockzone { position: sticky; bottom: 0; z-index: 6; margin: 26px -34px 0; padding: 54px 34px 16px; background: linear-gradient(to top, var(--background, #0d1015) 46%, transparent); pointer-events: none; }
.stoa-dock { width: min(780px, 100%); margin: 0 auto; pointer-events: auto; }
.stoa-glass { border: 1px solid var(--ui-stroke-primary); border-radius: 16px; background: color-mix(in srgb, var(--ui-bg-elevated, var(--background, #14161b)) 84%, transparent); -webkit-backdrop-filter: blur(18px) saturate(1.2); backdrop-filter: blur(18px) saturate(1.2); box-shadow: 0 18px 50px -14px rgba(0, 0, 0, .6), 0 2px 8px rgba(0, 0, 0, .22); }
.stoa-sheet { margin-bottom: 8px; padding: 10px 14px 14px; max-height: min(46vh, 430px); overflow-y: auto; animation: stoa-lift .2s ease both; }
.stoa-sheethead { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 8px; }
.stoa-turn + .stoa-turn { margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--ui-stroke-tertiary); }
.stoa-youline { display: flex; justify-content: flex-end; }
.stoa-you { max-width: 86%; padding: 6px 12px; border-radius: 13px 13px 4px 13px; background: var(--stoa-accent-soft); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--stoa-accent) 24%, transparent); overflow-wrap: anywhere; white-space: pre-wrap; }
.stoa-you small { display: block; margin-top: 2px; font-size: 11px; color: var(--ui-text-tertiary); }
.stoa-working { display: inline-flex; align-items: center; gap: 8px; }
.stoa-working::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--stoa-accent); animation: stoa-blink 1s ease-in-out infinite; }
.stoa-composer { padding: 7px; transition: border-color .15s ease, box-shadow .15s ease; }
.stoa-composer:focus-within { border-color: var(--stoa-accent-line); box-shadow: 0 18px 50px -14px rgba(0, 0, 0, .6), 0 0 0 1px var(--stoa-accent-line); }
.stoa-composerrow { display: flex; align-items: flex-end; gap: 3px; }
.stoa-composer textarea { flex: 1; min-width: 0; min-height: 34px; max-height: 132px; padding: 7px 6px; border: 0; background: transparent; color: var(--ui-text-primary); font: inherit; font-size: 14px; line-height: 1.45; resize: none; }
.stoa .stoa-composer textarea, .stoa .stoa-composer textarea:focus-visible { outline: none; }
.stoa-composer textarea::placeholder { color: var(--ui-text-quaternary); }
.stoa-icon { flex: none; display: inline-flex; align-items: center; justify-content: center; width: 34px; height: 34px; padding: 0; border: 0; border-radius: 10px; background: transparent; color: var(--ui-text-secondary); cursor: pointer; transition: background .15s ease, color .15s ease, opacity .15s ease; }
.stoa-icon:hover { background: var(--ui-control-hover-background); color: var(--ui-text-primary); }
.stoa-icon:disabled { opacity: .4; cursor: default; }
.stoa-icon[data-kind="send"] { margin-left: 3px; border-radius: 50%; background: var(--stoa-accent); color: var(--stoa-on-accent); }
.stoa-icon[data-kind="send"]:hover { background: var(--stoa-accent); opacity: .88; }
.stoa-icon[data-kind="send"]:disabled { opacity: .32; }
.stoa-icon[data-on="1"] { color: var(--ui-red); background: color-mix(in srgb, var(--ui-red) 14%, transparent); animation: stoa-pulse 1.4s ease-in-out infinite; }
.stoa-filechip { display: inline-flex; align-items: center; gap: 6px; margin: 1px 0 6px 4px; padding: 2px 3px 2px 9px; border: 1px solid var(--ui-stroke-secondary); border-radius: 8px; font-size: 12px; color: var(--ui-text-secondary); }
.stoa-hints { display: flex; flex-wrap: wrap; justify-content: center; gap: 6px; margin-bottom: 8px; }
.stoa-hint { height: 28px; padding: 0 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: 999px; background: color-mix(in srgb, var(--ui-bg-elevated, var(--background, #14161b)) 80%, transparent); -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); color: var(--ui-text-secondary); font: inherit; font-size: 12px; cursor: pointer; transition: color .15s ease, border-color .15s ease; }
.stoa-hint:hover { color: var(--ui-text-primary); border-color: var(--ui-stroke-primary); }
.stoa-hint:disabled { opacity: .5; cursor: default; }
.stoa-dropnote { position: sticky; top: 0; z-index: 7; width: fit-content; margin: 0 auto 12px; padding: 8px 18px; border: 1px solid var(--stoa-accent-line); border-radius: 999px; background: var(--stoa-accent-soft); -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); font-size: 13px; }
.stoa-page[data-drop="1"] { box-shadow: inset 0 0 0 2px var(--stoa-accent-line); }
.stoa-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: flex-end; gap: 14px 44px; }
.stoa-headstats { display: flex; flex-wrap: wrap; gap: 10px 36px; padding-bottom: 5px; }
.stoa-headstats b { display: block; margin-top: 3px; font-size: 20px; font-weight: 600; line-height: 1.1; font-variant-numeric: tabular-nums; }
.stoa-tabsrow { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px; }
.stoa-col { min-width: 0; border-radius: 10px; transition: opacity .2s ease, background .15s ease; }
.stoa-col[data-dim="1"] { opacity: .45; }
.stoa-col[data-dim="1"]:hover { opacity: .8; }
.stoa-col[data-spot="1"] .stoa-colhead .stoa-eyebrow { color: var(--stoa-accent-text); }
.stoa-col[data-spot="1"] .stoa-weight i { background: var(--stoa-accent); }
.stoa-col[data-over="1"] { background: var(--stoa-accent-soft); box-shadow: inset 0 0 0 1px var(--stoa-accent-line); }   /* inset: the board clips anything drawn outside a column */
.stoa-weight { height: 2px; margin: 0 2px 9px; border-radius: 2px; background: var(--ui-stroke-tertiary); }
.stoa-weight i { display: block; height: 100%; border-radius: 2px; background: var(--ui-text-tertiary); transition: width .4s ease; }
.stoa-weight[data-won="1"] i { background: var(--stoa-accent); }
.stoa-card[draggable="true"] { cursor: grab; }
.stoa-card[draggable="true"]:active { cursor: grabbing; }
.stoa-split > .stoa-box { animation: stoa-slide .22s ease both; }
@keyframes stoa-rise { from { transform: scaleY(.12); opacity: 0; } }
@keyframes stoa-lift { from { transform: translateY(8px); opacity: 0; } }
@keyframes stoa-slide { from { transform: translateX(10px); opacity: 0; } }
@keyframes stoa-blink { 50% { opacity: .25; } }
@keyframes stoa-pulse { 50% { box-shadow: 0 0 0 5px color-mix(in srgb, var(--ui-red) 16%, transparent); } }
@media (prefers-reduced-motion: reduce) { .stoa *, .stoa *::before { animation: none !important; transition: none !important; } }
.stoa-form { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 10px; margin-top: 12px; }
.stoa-form label { display: block; min-width: 0; }
.stoa-form label[data-wide="1"] { grid-column: 1 / -1; }
.stoa-field { display: block; width: 100%; height: 28px; margin-top: 3px; padding: 0 8px; border: 1px solid var(--ui-stroke-secondary); border-radius: 6px; background: transparent; color: var(--ui-text-primary); font: inherit; font-size: 12px; outline: none; }
.stoa-field:focus { border-color: var(--ui-stroke-primary); }
.stoa-field::placeholder { color: var(--ui-text-quaternary); }
.stoa-edit { margin-top: 12px; padding-top: 2px; border-top: 1px solid var(--ui-stroke-tertiary); }
/* Sample data on show: said plainly, on every surface, with the way back beside it. */
.stoa-tour { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px 16px; margin: -10px 0 20px; padding: 8px 10px 8px 14px; border: 1px dashed var(--stoa-accent-line); border-radius: 10px; background: var(--stoa-accent-soft); }
.stoa-tourline { margin-bottom: 10px; padding: 4px 8px; border: 1px dashed var(--stoa-accent-line); border-radius: 6px; background: var(--stoa-accent-soft); font-size: 11px; }
.stoa-stat { padding: 0; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: default; }
.stoa-stat[data-press="1"] { cursor: pointer; }
.stoa-stat[data-press="1"]:hover b { color: var(--stoa-accent-text); }
/* Today */
.stoa-today { min-width: 0; }
.stoa-todayhead { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; }
.stoa-tsec { margin-top: 20px; }
.stoa-trow { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 2px 12px; align-items: center; padding: 2px 6px 2px 0; border-top: 1px solid var(--ui-stroke-tertiary); border-radius: 0; transition: background .15s ease; }
.stoa-trow[data-open="1"] { background: var(--stoa-accent-soft); box-shadow: inset 2px 0 0 var(--stoa-accent); }
.stoa-tmain { display: flex; gap: 9px; align-items: flex-start; min-width: 0; padding: 7px 8px; border: 0; border-radius: 6px; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-tmain:hover { background: var(--ui-row-hover-background); }
.stoa-tacts { display: flex; gap: 6px; align-items: center; justify-content: flex-end; }
.stoa-tacts:empty { display: none; }
.stoa-nextform { grid-column: 1 / -1; display: grid; grid-template-columns: minmax(0, 1fr) 150px auto auto; gap: 6px 8px; align-items: center; padding: 2px 8px 10px 23px; animation: stoa-lift .18s ease both; }
.stoa-nextform .stoa-field, .stoa-pickdate .stoa-field { margin-top: 0; }
.stoa-box .stoa-nextform { grid-template-columns: minmax(0, 1fr) 132px; padding: 12px 0 0; }
.stoa-box .stoa-nextform .stoa-btn { justify-self: start; }
.stoa-pickdate { display: inline-flex; gap: 4px; align-items: center; }
.stoa-pickdate .stoa-field { width: 138px; }
.stoa-pace { margin: -4px 2px 9px; font-size: 10.5px; color: var(--ui-text-quaternary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
/* Cash: the strip, the forecast, what has been billed, and invoices */
.stoa-cash { min-width: 0; }
.stoa-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(168px, 1fr)); gap: 12px; }
.stoa-tile { padding: 12px 14px 11px; border: 1px solid var(--ui-stroke-secondary); border-radius: 10px; background: var(--ui-bg-card); }
.stoa-tile b { display: block; margin: 5px 0 1px; font-size: 23px; font-weight: 600; letter-spacing: -.015em; line-height: 1.1; font-variant-numeric: tabular-nums; }
.stoa-tile[data-tone="bad"] b { color: var(--ui-red); }
.stoa-tile[data-tone="warn"] b { color: var(--ui-orange); }
.stoa-tile[data-tone="good"] b { color: var(--stoa-accent-text); }
.stoa-fc { display: grid; grid-template-columns: 120px minmax(0, 1fr) 200px; gap: 12px; align-items: center; padding: 5px 0; }
.stoa-fc u { position: relative; display: block; height: 10px; border-radius: 3px; background: var(--ui-stroke-tertiary); text-decoration: none; overflow: hidden; }
.stoa-fc u i, .stoa-fc u b { position: absolute; top: 0; bottom: 0; left: 0; border-radius: 3px; transition: width .4s ease; }
.stoa-fc u i { background: color-mix(in srgb, var(--ui-text-primary) 24%, transparent); }
.stoa-fc u b { background: var(--stoa-accent); }
.stoa-fc[data-key="past"] > span:first-child { color: var(--ui-orange); }
.stoa-fc[data-key="none"] u b { background: color-mix(in srgb, var(--stoa-accent) 55%, transparent); }
.stoa-fc .stoa-num { text-align: right; }
.stoa-billbar { position: relative; height: 4px; margin: 8px 0 2px; border-radius: 2px; background: var(--ui-stroke-secondary); overflow: hidden; }
.stoa-billbar i { position: absolute; top: 0; bottom: 0; left: 0; border-radius: 2px; transition: width .4s ease; }
.stoa-billbar i[data-kind="invoiced"] { background: color-mix(in srgb, var(--ui-text-primary) 42%, transparent); }
.stoa-billbar i[data-kind="paid"] { background: var(--stoa-accent); }
.stoa-irow { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; padding: 8px 0; border-top: 1px solid var(--ui-stroke-tertiary); }
.stoa-irow > .stoa-grow { flex: 1 1 200px; }      /* in a narrow panel the moves drop to a second line; the words are never squeezed */
.stoa-irow > .stoa-tacts { margin-left: auto; }
.stoa-irow[data-state="void"] { opacity: .5; }
.stoa-log > .stoa-irow:first-of-type { border-top: 0; }
/* Approvals, undo, people and companies */
.stoa-approvals { margin-top: 14px; }
.stoa-proposal { margin-bottom: 10px; padding: 11px 14px 12px; border: 1px solid var(--ui-stroke-secondary); border-left: 2px solid var(--stoa-accent); border-radius: 4px 10px 10px 4px; background: var(--ui-bg-card); animation: stoa-lift .2s ease both; }
.stoa-prophead { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
.stoa-proplink { padding: 0; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-proplink.stoa-sub { color: var(--ui-text-tertiary); font-size: 12px; }
.stoa-proplink.stoa-name { font-weight: 600; }
.stoa-proplink:hover { color: var(--stoa-accent-text); }
.stoa-propchange { margin-top: 5px; font-size: 14px; font-weight: 550; overflow-wrap: anywhere; }
.stoa-evidence { margin: 9px 0 0; padding: 2px 0 2px 11px; border-left: 2px solid var(--ui-stroke-primary); color: var(--ui-text-secondary); }
.stoa-evidence small { display: block; margin-bottom: 2px; }
.stoa-evidence span { display: block; overflow-wrap: anywhere; }
.stoa-undobar { display: flex; align-items: center; gap: 10px; width: fit-content; max-width: 100%; margin: 0 auto 8px; padding: 4px 5px 4px 15px; border: 1px solid var(--ui-stroke-primary); border-radius: 999px; background: color-mix(in srgb, var(--ui-bg-elevated, var(--background, #14161b)) 92%, transparent); -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); box-shadow: 0 12px 34px -14px rgba(0, 0, 0, .65); font-size: 12.5px; animation: stoa-lift .2s ease both; }
.stoa-undobar b { font-weight: 600; }
.stoa-undobar .stoa-btn { height: 25px; border-radius: 999px; border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); font-weight: 550; }
.stoa-person { display: flex; gap: 6px; align-items: center; padding: 5px 0; }
.stoa-notes { height: auto; min-height: 62px; margin-top: 5px; padding: 7px 8px; line-height: 1.45; resize: vertical; }
.stoa-waiting { display: block; width: fit-content; margin: -6px 0 16px; padding: 6px 13px; border: 1px solid var(--stoa-accent-line); border-radius: 999px; background: var(--stoa-accent-soft); color: var(--ui-text-primary); font: inherit; font-size: 12.5px; cursor: pointer; }
.stoa-waiting b { font-weight: 600; }
.stoa-viewrow { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px; margin: 0 0 14px; }
.stoa-companies { min-width: 0; }
.stoa-crow { display: grid; grid-template-columns: minmax(0, 2.2fr) minmax(0, 1.1fr) minmax(0, 1.1fr) 64px 84px; gap: 12px; align-items: center; width: 100%; padding: 9px 10px; border: 0; border-top: 1px solid var(--ui-stroke-tertiary); background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.stoa-crow[data-cols="7"] { grid-template-columns: minmax(0, 2fr) minmax(0, 1.2fr) 76px minmax(0, 1fr) minmax(0, 1fr) 52px 80px; }
.stoa-crow:hover { background: var(--ui-row-hover-background); }
.stoa-crow[data-dim="1"] > * { opacity: .55; }
.stoa-newgroup { display: flex; flex-wrap: wrap; gap: 8px; align-items: flex-end; margin: 0 0 12px; max-width: 760px; }
.stoa-pipes { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 14px; }
.stoa-pipe { display: inline-flex; align-items: center; gap: 8px; height: 30px; padding: 0 12px; border: 1px solid var(--ui-stroke-secondary); border-radius: 15px; background: transparent; color: var(--ui-text-secondary); font: inherit; font-size: 12.5px; cursor: pointer; }
.stoa-pipe:hover { border-color: var(--ui-stroke-primary); color: var(--ui-text-primary); }
.stoa-pipe[data-on="1"] { border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); color: var(--ui-text-primary); font-weight: 550; }
.stoa-pipe small { font-size: 11px; opacity: .7; font-variant-numeric: tabular-nums; }
.stoa-piperow { display: flex; gap: 10px; align-items: center; padding: 8px 0; border-top: 1px solid var(--ui-stroke-tertiary); }
.stoa-sechead + .stoa-piperow { border-top: 0; padding-top: 0; }
.stoa-newgroup > label { flex: 1 1 200px; }
.stoa-kinds { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 10px; }
.stoa-proposal[data-follow="1"] { margin-top: -6px; margin-left: 14px; }      /* another suggestion about the record above it */
.stoa-crow[data-open="1"] { background: var(--stoa-accent-soft); box-shadow: inset 2px 0 0 var(--stoa-accent); }
.stoa-crow .stoa-num { text-align: left; }
.stoa-chead, .stoa-chead:hover { border-top: 0; background: transparent; cursor: default; padding-bottom: 6px; }
.stoa-level { display: flex; gap: 10px; align-items: flex-start; width: 100%; margin-bottom: 6px; padding: 9px 11px; border: 1px solid var(--ui-stroke-secondary); border-radius: 8px; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; transition: border-color .15s ease, background .15s ease; }
.stoa-level:hover { border-color: var(--ui-stroke-primary); }
.stoa-level[data-on="1"] { border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); cursor: default; }
.stoa-btn[data-kind="go"] { border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); font-weight: 550; }
.stoa-btn[data-kind="go"]:hover { background: color-mix(in srgb, var(--ui-accent) 30%, transparent); }
.stoa-tacts .stoa-btn:not([data-kind="go"]), .stoa-tacts .stoa-select { border-color: var(--ui-stroke-secondary); color: var(--ui-text-secondary); }
.stoa-tacts .stoa-btn:not([data-kind="go"]):hover, .stoa-tacts .stoa-select:hover { color: var(--ui-text-primary); }
.stoa-tacts .stoa-select { max-width: 112px; }
.stoa-ahead .stoa-row { align-items: center; }
.stoa-aheadrow { display: flex; align-items: center; gap: 2px; margin: 0 -6px; }
.stoa-aheadrow .stoa-row { flex: 1; min-width: 0; }
.stoa-weekline { display: flex; flex-wrap: wrap; gap: 4px 18px; margin-top: 5px; color: var(--ui-text-secondary); }
.stoa-weekline b { font-size: 15px; font-weight: 600; color: var(--ui-text-primary); font-variant-numeric: tabular-nums; }
/* The way in, and Setup */
.stoa-welcome { max-width: 1040px; }
.stoa-doors { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; margin-top: 14px; }
.stoa-door { display: flex; flex-direction: column; gap: 3px; transition: border-color .15s ease, transform .15s ease; }
.stoa-door:hover { border-color: var(--ui-stroke-primary); transform: translateY(-1px); }
.stoa-door[data-on="1"] { border-color: var(--stoa-accent-line); background: var(--stoa-accent-soft); }
.stoa-door .stoa-btns { margin-top: auto; padding-top: 10px; }
.stoa-chain { margin: 8px 0 6px; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 10.5px; letter-spacing: .04em; line-height: 1.7; color: var(--ui-text-secondary); }
.stoa-setup { columns: 2 380px; column-gap: 14px; max-width: 1120px; }
.stoa-setbox { break-inside: avoid; padding-bottom: 14px; }      /* padding, not a margin: a margin at the foot of a column is dropped, and boxes touched */
.stoa-setbox[data-wide="1"] { column-span: all; }
.stoa-check, .stoa-setrow { display: flex; gap: 10px; align-items: flex-start; padding: 9px 0; border-top: 1px solid var(--ui-stroke-tertiary); }
.stoa-sechead + .stoa-check, .stoa-sechead + .stoa-setrow { border-top: 0; padding-top: 0; }
.stoa-check:last-child, .stoa-setrow:last-child { padding-bottom: 0; }
.stoa-tick { flex: none; display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px; margin-top: 1px; border: 1.5px solid var(--ui-stroke-primary); border-radius: 50%; font-style: normal; font-size: 11px; line-height: 1; }
.stoa-check[data-done="1"] .stoa-tick { border-color: var(--stoa-accent); background: var(--stoa-accent); color: var(--stoa-on-accent); }
.stoa-check[data-done="1"] .stoa-name { font-weight: 400; color: var(--ui-text-secondary); }
.stoa-lamp { flex: none; width: 8px; height: 8px; margin-top: 5px; border-radius: 50%; border: 1.5px solid var(--ui-text-quaternary); }
.stoa-lamp[data-on="1"] { border-color: var(--stoa-accent); background: var(--stoa-accent); box-shadow: 0 0 0 3px var(--stoa-accent-soft); }
.stoa-sayline { display: block; width: 100%; margin: 0 0 3px; padding: 5px 8px; border: 0; border-radius: 6px; background: transparent; color: var(--ui-text-secondary); font: inherit; text-align: left; cursor: pointer; }
.stoa-sayline:hover { background: var(--ui-row-hover-background); color: var(--ui-text-primary); }
.stoa-tip { margin: 0 0 7px; color: var(--ui-text-secondary); }
.stoa-tip code { padding: 1px 5px; border: 1px solid var(--ui-stroke-secondary); border-radius: 4px; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11px; color: var(--ui-text-primary); white-space: nowrap; }
.stoa-tip b { font-weight: 550; color: var(--ui-text-primary); }
`

const Style = () => h('style', null, CSS)
const Chip = ({ tone, children }) => h('span', { className: 'stoa-chip', 'data-tone': tone || '' }, children)
const Btn = ({ kind, children, ...props }) => h('button', { type: 'button', className: 'stoa-btn', 'data-kind': kind || '', ...props }, children)

function Fit({ value }) {
  const n = Math.max(0, Math.min(5, Number(value) || 0))
  return h('span', { className: 'stoa-fit', title: 'Fit ' + n + ' of 5' }, '●'.repeat(n), h('s', null, '●'.repeat(5 - n)))
}

// (Setup and Welcome say why with `why(error)`.)
function Unavailable({ error, loading }) {
  return h('div', { className: 'stoa stoa-scroll' }, h(Style), h('div', { className: 'stoa-eyebrow' }, 'Stoa'),
    h('p', { className: 'stoa-sub', style: { marginTop: 8 } }, loading ? 'Opening the desk…'
      : 'The desk is not answering. Enable the stoa plugin for this profile (hermes plugins enable stoa), restart the gateway, and it appears here.'),
    !loading && error ? h('p', { className: 'stoa-sub' }, String(error.message || error)) : null)
}

// -- a form: one labelled input per fact ----------------------------------------------------------
function Field({ label, wide, children, ...props }) {
  return h('label', { 'data-wide': wide ? '1' : '0' }, h('small', { className: 'stoa-eyebrow' }, label),
    children || h('input', { className: 'stoa-field', ...props }))
}

const uniq = values => [...new Set(values.filter(Boolean))].sort()

/** Change a record by hand: its next step, its stage, its facts, and a line for the log. */
function Edit({ deal, state, done }) {
  const pb = state.playbook, terms = pb.terms
  const start = { next_step: deal.next_step || '', next_due: deal.next_due || '', value: deal.value ? String(deal.value) : '', owner: deal.owner || '',
    company: deal.company || '', group: deal.group || '', email: deal.email || '', close_on: deal.close_on || '' }
  const [v, setV] = useState(start)
  const [note, setNote] = useState('')
  const [kind, setKind] = useState('note')
  const [busy, setBusy] = useState(false)
  const set = key => event => setV({ ...v, [key]: event.target.value })
  const send = body => { setBusy(true); return act(deal.id, body).then(result => { setBusy(false); return result }) }
  const save = () => {
    const body = { verb: 'update' }
    for (const key of Object.keys(v)) {
      const now = String(v[key]).trim()
      if (now !== start[key]) body[key] = key === 'value' ? Number(now) || 0 : now
    }
    if (Object.keys(body).length === 1) return done()
    send(body).then(result => { if (result) done() })
  }
  const log = () => { if (note.trim()) send({ verb: 'log', kind, text: note.trim() }).then(result => { if (result) setNote('') }) }
  const open = deal.status === 'open'
  return h('div', { className: 'stoa-edit' },
    h('div', { className: 'stoa-form' },
      h(Field, { label: 'Next step', wide: true, value: v.next_step, onChange: set('next_step') }),
      h(Field, { label: 'Due', type: 'date', value: v.next_due, onChange: set('next_due') }),
      open ? h(Field, { label: 'Stage' }, h('select', { className: 'stoa-field', value: deal.stage, disabled: busy,
        onChange: event => send({ verb: 'move', stage: event.target.value }) },
        pb.stages.map(stage => h('option', { key: stage.id, value: stage.id }, stage.label)))) : h('span'),
      h(Field, { label: cap(terms.value), type: 'number', min: 0, value: v.value, onChange: set('value') }),
      h(Field, { label: cap(terms.owner), value: v.owner, onChange: set('owner') }),
      h(Field, { label: cap(terms.company), value: v.company, onChange: set('company') }),
      h(Field, { label: cap(terms.group), value: v.group, onChange: set('group') }),
      open ? h(Field, { label: 'Expected close', type: 'date', value: v.close_on, title: 'The forecast puts it in this month', onChange: set('close_on') }) : h('span'),
      h(Field, { label: 'Email', type: 'email', value: v.email, onChange: set('email') })),
    h('div', { className: 'stoa-btns' },
      h(Btn, { kind: 'primary', disabled: busy, onClick: save }, 'Save'),
      h(Btn, { kind: 'quiet', disabled: busy, onClick: done }, 'Cancel')),
    h('div', { className: 'stoa-form' },
      h(Field, { label: 'Log what happened', wide: true, value: note, placeholder: 'Called her; she wants to see the comp range', onChange: event => setNote(event.target.value),
        onKeyDown: event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) log() } })),
    h('div', { className: 'stoa-btns', style: { marginTop: 8 } },
      h('select', { className: 'stoa-select', value: kind, 'aria-label': 'What it was', onChange: event => setKind(event.target.value) },
        ['note', 'call', 'email', 'meeting'].map(name => h('option', { key: name, value: name }, cap(name)))),
      h(Btn, { disabled: busy || !note.trim(), onClick: log }, 'Add to log')))
}

/** Put someone or something new on the desk by hand. */
function NewRecord({ state, close, opened }) {
  const pb = state.playbook, terms = pb.terms
  const [v, setV] = useState({ name: '', company: '', group: '', stage: pb.stages[0] ? pb.stages[0].id : '', value: '', owner: '', email: '', next_step: '', next_due: '' })
  const [busy, setBusy] = useState(false)
  const set = key => event => setV({ ...v, [key]: event.target.value })
  const save = () => {
    const body = {}
    for (const key of Object.keys(v)) {
      const now = String(v[key]).trim()
      if (now) body[key] = key === 'value' ? Number(now) || 0 : now
    }
    if (!body.name || busy) return
    setBusy(true)
    rest('/deal', { method: 'POST', body })
      .then(result => {
        refresh()
        host.notify({ kind: result && result.exists ? 'info' : 'success', message: result && result.exists ? body.name + ' is already on the desk.' : body.name + ' is on the desk.' })
        if (result && result.last) offerUndo(result.last)
        opened(result && result.deal ? result.deal.id : '')
      })
      .catch(error => host.notifyError(error, 'Stoa could not add that'))
      .then(() => setBusy(false))
  }
  return h('div', { className: 'stoa-box' },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center' } },
      h('div', { className: 'stoa-name', style: { fontSize: 14 } }, 'New ' + terms.deal),
      h(Btn, { kind: 'quiet', onClick: close, 'aria-label': 'Close' }, '×')),
    h('div', { className: 'stoa-form' },
      h(Field, { label: 'Name', wide: true, autoFocus: true, value: v.name, onChange: set('name') }),
      h(Field, { label: cap(terms.company), value: v.company, onChange: set('company') }),
      h(Field, { label: cap(terms.group), list: 'stoa-new-groups', value: v.group, onChange: set('group') }),
      h(Field, { label: 'Stage' }, h('select', { className: 'stoa-field', value: v.stage, onChange: set('stage') },
        pb.stages.map(stage => h('option', { key: stage.id, value: stage.id }, stage.label)))),
      h(Field, { label: cap(terms.value), type: 'number', min: 0, value: v.value, onChange: set('value') }),
      h(Field, { label: cap(terms.owner), list: 'stoa-new-owners', value: v.owner, onChange: set('owner') }),
      h(Field, { label: 'Email', type: 'email', value: v.email, onChange: set('email') }),
      h(Field, { label: 'Next step', wide: true, value: v.next_step, onChange: set('next_step') }),
      h(Field, { label: 'Due', type: 'date', value: v.next_due, onChange: set('next_due') })),
    h('datalist', { id: 'stoa-new-groups' }, uniq(state.deals.map(deal => deal.group)).map(name => h('option', { key: name, value: name }))),
    h('datalist', { id: 'stoa-new-owners' }, uniq(state.deals.map(deal => deal.owner)).map(name => h('option', { key: name, value: name }))),
    h('div', { className: 'stoa-btns' },
      h(Btn, { kind: 'primary', disabled: busy || !v.name.trim(), onClick: save }, 'Add to the desk'),
      h(Btn, { kind: 'quiet', disabled: busy, onClick: close }, 'Cancel')))
}

// -- one record: what it is, what is next, and the buttons ---------------------------------------
function Record({ id, state: shown, sessionId, close, onCompany, onGroup }) {
  const query = useQuery({ queryKey: ['stoa', 'deal', id], queryFn: () => rest('/deal/' + encodeURIComponent(id)), staleTime: 1500 })
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState(false)
  const [finishing, setFinishing] = useState(false)
  const deal = query.data
  if (query.error) return h('div', { className: 'stoa-box stoa-sub' }, 'That record is no longer on the desk.')
  if (!deal) return h('div', { className: 'stoa-box stoa-sub' }, 'Loading…')
  const elsewhere = Boolean(deal.pipeline && shown.pipeline && deal.pipeline.id !== shown.pipeline)     // from a pipeline the page is not showing
  const state = deal.playbook ? { ...shown, playbook: deal.playbook } : shown
  const pb = state.playbook
  const open = deal.status === 'open'
  const run = body => { setBusy(true); act(id, body, sessionId).then(() => setBusy(false)) }
  const facts = [[cap(pb.terms.group), deal.group], [pb.terms.value, money(deal.value, pb.currency)], [cap(pb.terms.owner), deal.owner],
    ['Expected close', open && deal.close_on ? shortDate(deal.close_on) + (deal.close_on < today() ? ' (past)' : '') : '']]
    .concat(pb.fields.map(field => [field.label, (deal.fields || {})[field.key]]))
    .concat([['Email', deal.email]]).filter(pair => pair[1])
  return h('div', { className: 'stoa-box' },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'flex-start' } },
      h('div', { className: 'stoa-grow' },
        elsewhere ? h('div', { className: 'stoa-eyebrow', style: { marginBottom: 3 } }, deal.pipeline.name) : null,
        h('div', { className: 'stoa-name', style: { fontSize: 14 } }, deal.name),
        deal.company && onCompany ? h('div', null, h('button', { type: 'button', className: 'stoa-proplink stoa-sub', title: 'Everything at ' + deal.company, onClick: () => onCompany(deal.company) }, deal.company + '  ›'))
          : deal.company && deal.company !== deal.name ? h('div', { className: 'stoa-sub' }, deal.company) : null,
        deal.group && onGroup ? h('div', null, h('button', { type: 'button', className: 'stoa-proplink stoa-sub', title: 'The ' + pb.terms.group + ' this is in: its client, its terms and who is on their side',
          onClick: () => onGroup(deal.group) }, deal.group + (deal.client ? ' · for ' + deal.client : '') + (deal.group_status ? ' · ' + deal.group_status.toLowerCase() : '') + '  ›')) : null),
      close ? h(Btn, { kind: 'quiet', onClick: close, 'aria-label': 'Close' }, '×') : null),
    h('div', { className: 'stoa-meter' }, pb.stages.map((stage, index) =>
      h('i', { key: stage.id, 'data-on': deal.status === 'won' || (open && index <= deal.position) ? '1' : '0' }))),
    h('div', { className: 'stoa-sub', style: { display: 'flex', justifyContent: 'space-between' } },
      h('span', null, deal.stage_label + (deal.lost_reason ? ' · ' + deal.lost_reason : '')),
      h('span', null, open && deal.position >= 0 ? deal.position + 1 + ' of ' + pb.stages.length : ago(deal.stage_at))),
    deal.due || deal.stale || deal.pending_task || asleep(deal) ? h('div', { className: 'stoa-chips' },
      deal.due ? h(Chip, { tone: dueTone(deal) }, deal.next_due < today() ? 'overdue' : 'due today') : null,
      deal.stale && !deal.due ? h(Chip, null, 'quiet') : null,
      asleep(deal) ? h(Chip, null, 'snoozed to ' + shortDate(deal.snoozed_until)) : null,
      deal.pending_task ? h(Chip, { tone: 'good' }, 'draft waiting') : null) : null,
    deal.next_step ? h('div', { className: 'stoa-next' }, h('small', { className: 'stoa-eyebrow' }, 'Next'),
      h('div', null, deal.next_step, deal.next_due ? h('span', { className: 'stoa-sub' }, '  ' + dueLabel(deal)) : null)) : null,
    h('div', { className: 'stoa-btns' },
      open && deal.advance ? h(Btn, { kind: 'primary', disabled: busy, onClick: () => run({ verb: 'advance', extra: deal.stage }) }, deal.advance + '  →') : null,
      open && deal.next_step ? h(Btn, { disabled: busy, title: 'The next step is done: log it and say what comes next', onClick: () => setFinishing(!finishing) }, 'Done') : null,
      open ? h(Btn, { disabled: busy, onClick: () => run({ verb: 'draft' }) }, 'Draft follow-up') : null,
      open ? h(Snooze, { deal, busy, run }) : null,
      open ? h('select', { className: 'stoa-select', value: '', disabled: busy, 'aria-label': pb.lost.label,
        onChange: event => { if (event.target.value !== '') run({ verb: 'lost', extra: event.target.value }) } },
        [h('option', { key: 'none', value: '' }, pb.lost.label + '…')].concat(pb.lost.reasons.map((reason, index) =>
          h('option', { key: index, value: String(index) }, reason)))) : null,
      deal.status === 'won' && (deal.delivery_stages || []).length ? h('select', { className: 'stoa-select', value: deal.delivery || '', disabled: busy,
        'aria-label': 'Where the work stands', title: 'Where the work stands', onChange: event => run({ verb: 'update', delivery: event.target.value }) },
        deal.delivery_stages.map(stage => h('option', { key: stage.id, value: stage.id }, stage.label))) : null,
      !open ? h(Btn, { kind: deal.status === 'won' ? '' : 'primary', disabled: busy, onClick: () => run({ verb: 'reopen' }) }, 'Reopen') : null,
      h(Btn, { kind: 'quiet', onClick: () => setEditing(!editing) }, editing ? 'Close editing' : 'Edit')),
    finishing && open && deal.next_step ? h(NextStep, { deal, mode: 'done', close: () => setFinishing(false) }) : null,
    editing ? h(Edit, { key: deal.stage, deal, state, done: () => setEditing(false) }) : null,
    facts.length ? h('div', { className: 'stoa-facts' }, facts.map(pair =>
      h('div', { key: pair[0], style: pair[0] === 'Email' ? { gridColumn: '1 / -1' } : undefined }, h('small', { className: 'stoa-eyebrow' }, pair[0]), h('span', null, String(pair[1]))))) : null,
    close || (deal.invoices || []).length ? h(Money, { deal, pb }) : null,
    close ? h(People, { deal, term: pb.terms.deal }) : (deal.people || []).length ? h('div', { className: 'stoa-log' },
      h('small', { className: 'stoa-eyebrow' }, 'Also on this ' + pb.terms.deal),
      deal.people.map(person => h('p', { key: person.id }, person.name, h('span', { className: 'stoa-sub' }, '  ' + [person.role, person.title].filter(Boolean).join(' · '))))) : null,
    h(RecordNotes, { key: 'notes-' + deal.id, deal }),
    h(RecordActivity, { key: 'log-' + deal.id, deal, sessionId, full: Boolean(close) }),
    deal.undo ? h(Btn, { kind: 'quiet', disabled: busy, title: 'Put the record back as it was before: ' + deal.undo.text,
      onClick: () => { setBusy(true); undo(deal.id, deal.undo.op).then(() => setBusy(false)) } },
    h('span', { style: { display: 'inline-flex', alignItems: 'center', gap: 6 } }, h(Icon, { name: 'back', size: 13 }), 'Take back the last change')) : null)
}

/** A few lines kept on the record itself: what to know about it. Edited in place; saved like any other change, so it can be taken back. */
function RecordNotes({ deal }) {
  const [text, setText] = useState(null) // null: showing what is saved
  const [busy, setBusy] = useState(false)
  const saved = deal.notes || ''
  const changed = text !== null && text.trim() !== saved.trim()
  const save = () => {
    setBusy(true)
    act(deal.id, { verb: 'update', notes: text }).then(result => { setBusy(false); if (result) setText(null) })
  }
  return h('div', { className: 'stoa-log' },
    h('small', { className: 'stoa-eyebrow' }, 'Notes'),
    h('textarea', { className: 'stoa-field stoa-notes', rows: saved ? Math.min(8, saved.split('\n').length + 1) : 2, maxLength: 4000,
      value: text === null ? saved : text, placeholder: 'What to know about them: how they found you, what they care about, what to avoid…',
      onChange: event => setText(event.target.value),
      onKeyDown: event => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && changed) { event.preventDefault(); save() } } }),
    changed ? h('div', { className: 'stoa-btns', style: { marginTop: 8 } }, h(Btn, { kind: 'primary', disabled: busy, onClick: save }, 'Save notes'),
      h(Btn, { kind: 'quiet', disabled: busy, onClick: () => setText(null) }, 'Cancel')) : null)
}

const LOG_KINDS = { call: 'Call', email: 'Email', meeting: 'Meeting', note: 'Note', slack: 'Slack', linkedin: 'LinkedIn', stage: 'Stage',
  created: 'Added', updated: 'Edited', task: 'Task', enriched: 'Lookup', invoice: 'Invoice', payment: 'Payment' }

/** What happened on the record, newest first, with a line to add to it. */
function RecordActivity({ deal, sessionId, full }) {
  const [all, setAll] = useState(false)
  const [line, setLine] = useState('')
  const [busy, setBusy] = useState(false)
  const entries = deal.activity || []
  const shown = all ? entries : entries.slice(0, full ? 6 : 4)
  const add = () => {
    const text = line.trim()
    if (!text) return
    setBusy(true)
    act(deal.id, { verb: 'log', kind: 'note', text }, sessionId).then(result => { setBusy(false); if (result) setLine('') })
  }
  return h('div', { className: 'stoa-log' },
    h('div', { className: 'stoa-sechead', style: { padding: 0, alignItems: 'center' } },
      h('small', { className: 'stoa-eyebrow' }, 'Activity'),
      entries.length > shown.length || all ? h('button', { type: 'button', className: 'stoa-proplink stoa-sub', onClick: () => setAll(!all) },
        all ? 'Fewer' : 'All ' + entries.length) : null),
    full ? h('div', { style: { display: 'flex', gap: 6, margin: '6px 0 8px' } },
      h('input', { className: 'stoa-field stoa-grow', value: line, disabled: busy, placeholder: 'Log a call, an email, a thought…', 'aria-label': 'Add to the log',
        onChange: event => setLine(event.target.value), onKeyDown: event => { if (event.key === 'Enter') { event.preventDefault(); add() } } }),
      line.trim() ? h(Btn, { disabled: busy, onClick: add }, 'Add') : null) : null,
    shown.length ? shown.map((entry, index) =>
      h('p', { key: index, style: entry.undone ? { textDecoration: 'line-through', opacity: 0.55 } : undefined },
        LOG_KINDS[entry.kind] && !['created', 'updated', 'stage'].includes(entry.kind) ? h('b', { style: { fontWeight: 550 } }, LOG_KINDS[entry.kind] + '  ') : null,
        entry.text,
        h('span', { className: 'stoa-sub' }, '  ' + ago(entry.at) + (entry.by ? ' · ' + entry.by : ''))))
      : h('p', { className: 'stoa-sub' }, 'Nothing logged yet.'))
}

// -- a list sent in: tick what to keep, then Apply ------------------------------------------------
function changeText(change) {
  if (change.field === 'Person') return 'add ' + change.to
  if (change.field === 'Due') return change.from ? 'Due: ' + shortDate(change.from) + ' → ' + shortDate(change.to) : 'Due: ' + shortDate(change.to)
  if (change.field === 'Note') return 'note: ' + change.to
  return change.from ? change.field + ': ' + change.from + ' → ' + change.to : change.field + ': ' + change.to
}

function Review({ id }) {
  const query = useQuery({ queryKey: ['stoa', 'intake', id], queryFn: () => rest('/intake/' + encodeURIComponent(id)), staleTime: 1500 })
  const [off, setOff] = useState({})
  const [busy, setBusy] = useState(false)
  const intake = query.data
  if (query.error) return h('div', { className: 'stoa-box stoa-sub' }, 'That list is no longer here.')
  if (!intake) return h('div', { className: 'stoa-box stoa-sub' }, 'Loading…')
  const open = intake.status === 'open'
  const usable = intake.rows.filter(row => (row.kind === 'new' || row.kind === 'update') && (!open || !row.done))
  const rest_ = intake.rows.filter(row => row.kind !== 'new' && row.kind !== 'update')
  const chosen = usable.filter(row => !off[row.n])
  const counts = intake.counts, result = intake.result || {}
  const send = verb => {
    setBusy(true)
    rest('/intake/' + encodeURIComponent(id) + '/' + verb, { method: 'POST', body: verb === 'apply' ? { rows: chosen.map(row => row.n) } : {} })
      .then(done => {
        refresh()
        if (verb === 'apply' && done.applied) host.notify({ kind: 'success', message: done.added + ' added, ' + done.updated + ' updated' })
        else if (done.note) host.notify({ kind: 'info', message: done.note })
        if (done.problems && done.problems.length) host.notify({ kind: 'warning', message: done.problems[0] })
      })
      .catch(error => host.notifyError(error, 'Stoa could not apply that list'))
      .then(() => setBusy(false))
  }
  return h('div', { className: 'stoa-box' },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'baseline' } },
      h('span', { className: 'stoa-eyebrow' }, (intake.origin === 'proposal' ? 'Proposed · ' : 'Intake · ') + intake.source),
      open ? null : h(Chip, { tone: intake.status === 'applied' ? 'good' : '' }, intake.status)),
    h('div', { className: 'stoa-name', style: { marginTop: 4 } },
      [counts.new ? counts.new + ' new' : '', counts.update ? counts.update + ' to update' : '', counts.same ? counts.same + ' already there' : '',
        counts.unclear ? counts.unclear + ' unclear' : '', counts.problem ? counts.problem + ' unusable' : ''].filter(Boolean).join(' · ') || 'Nothing to add'),
    usable.length ? h('div', { className: 'stoa-list' }, usable.map(row => h('label', { key: row.n, className: 'stoa-pick', 'data-off': open && off[row.n] ? '1' : '0' },
      open ? h('input', { type: 'checkbox', checked: !off[row.n], disabled: busy, onChange: () => setOff({ ...off, [row.n]: !off[row.n] }) })
        : h('i', { className: 'stoa-dot', 'data-tone': row.applied ? 'good' : '', title: row.applied ? 'applied' : row.done === 'declined' ? 'declined' : 'left out' }),
      h('span', { className: 'stoa-grow' },
        h('span', { className: 'stoa-name' }, row.name),
        h('span', { className: 'stoa-sub' }, '  ' + [row.company !== row.name ? row.company : '', row.group].filter(Boolean).join(' · ')),
        h('span', { className: 'stoa-sub', style: { display: 'block' } },
          row.kind === 'new' ? 'New' + (row.stage ? ' at ' + row.stage : '') : row.changes.map(changeText).join(' · ')),
        row.case ? h('span', { className: 'stoa-sub', style: { display: 'block' } }, row.case) : null,
        (row.warnings || []).map((warning, index) => h('span', { key: index, className: 'stoa-sub', style: { display: 'block' } }, warning))),
      h(Chip, { tone: row.kind === 'new' ? 'good' : '' }, row.kind === 'new' ? 'new' : 'update')))) : null,
    rest_.length ? h('details', { className: 'stoa-rest' },
      h('summary', null, plural(rest_.length, 'row', 'rows') + ' left out'),
      rest_.map(row => h('p', { key: row.n }, (row.name || 'Row ' + row.n) + ': ' + row.why))) : null,
    open ? h('div', { className: 'stoa-btns' },
      h(Btn, { kind: 'primary', disabled: busy || !chosen.length, onClick: () => send('apply') }, 'Apply ' + chosen.length),
      h(Btn, { kind: 'quiet', disabled: busy, onClick: () => send('discard') }, 'Discard'),
      h('span', { className: 'stoa-sub', style: { alignSelf: 'center' } }, 'Nothing is written until you apply.'))
      : intake.status === 'applied' ? h('div', { className: 'stoa-sub', style: { marginTop: 10 } },
        (result.added || 0) + ' added · ' + (result.updated || 0) + ' updated' + (result.left ? ' · ' + result.left + ' left out' : '')) : null)
}

// -- narrowing what is shown ----------------------------------------------------------------------
function Filters({ state }) {
  const f = useValue($filter)
  const open = state.deals.filter(deal => deal.status === 'open')
  const owners = [...new Set(open.map(deal => deal.owner).filter(Boolean))].sort()
  const groups = [...new Set(open.map(deal => deal.group).filter(Boolean))].sort()
  const terms = state.playbook.terms
  return h('div', { className: 'stoa-filters' },
    h('input', { className: 'stoa-input', value: f.text, placeholder: 'Filter…', 'aria-label': 'Filter by text', onChange: event => setFilter({ text: event.target.value }) }),
    owners.length > 1 ? h('select', { className: 'stoa-select', value: f.owner, 'aria-label': 'Owner', onChange: event => setFilter({ owner: event.target.value }) },
      [h('option', { key: '', value: '' }, 'Everyone')].concat(owners.map(owner => h('option', { key: owner, value: owner }, owner)))) : null,
    groups.length > 1 ? h('select', { className: 'stoa-select', value: f.group, 'aria-label': cap(terms.group), onChange: event => setFilter({ group: event.target.value }) },
      [h('option', { key: '', value: '' }, 'All ' + terms.groups)].concat(groups.map(group => h('option', { key: group, value: group }, group)))) : null,
    h('button', { type: 'button', className: 'stoa-toggle', 'data-on': f.due ? '1' : '0', onClick: () => setFilter({ due: !f.due }) }, 'Due'),
    h('button', { type: 'button', className: 'stoa-toggle', 'data-on': f.quiet ? '1' : '0', onClick: () => setFilter({ quiet: !f.quiet }) }, 'Quiet'),
    f.stage && state.playbook.stages.some(stage => stage.id === f.stage) ? h('button', { type: 'button', className: 'stoa-toggle', 'data-on': '1',
      title: 'This stage is picked out on the board. Press to let go.', onClick: () => setFilter({ stage: '' }) },
    state.playbook.stages.find(stage => stage.id === f.stage).label + '  ×') : null,
    filtering(f) || f.stage ? h('button', { type: 'button', className: 'stoa-toggle', onClick: () => setFilter({ ...NO_FILTER }) }, 'Clear') : null)
}

// -- the funnel: how many reach each stage, and what share moves on ------------------------------
// Press a stage to pick out its column on the board (the others stay, dimmed); press it again to let go.
function Funnel({ state }) {
  const flow = state.flow, pb = state.playbook
  const filter = useValue($filter)
  if (!flow || !flow.total) return null
  const steps = flow.stages.map(stage => ({ id: stage.id, label: stage.label, reached: stage.reached, here: stage.here, lost: stage.lost, value: stage.value }))
    .concat([{ id: '', label: flow.won_label, reached: flow.won, won: true, value: flow.won_value }])
  const W = 1000, H = 196, mid = 102, tall = 100, col = W / steps.length, block = col * 0.5
  const height = step => Math.max(3, (tall * step.reached) / flow.total)
  const closed = flow.won + flow.lost
  const links = [], groups = []
  steps.forEach((step, index) => {
    const x = index * col, tallness = height(step), next = steps[index + 1]
    if (next) {
      const after = height(next), right = (index + 1) * col, centre = x + block + (col - block) / 2
      links.push(h('polygon', { key: 'l' + index, className: 'stoa-fn-link',
        points: [x + block, mid - tallness / 2, right, mid - after / 2, right, mid + after / 2, x + block, mid + tallness / 2].join(' ') }))
      links.push(h('rect', { key: 'q' + index, className: 'stoa-fn-pill', x: centre - 19, y: mid - 10, width: 38, height: 20, rx: 10 }))
      links.push(h('text', { key: 'p' + index, className: 'stoa-fn-pct', x: centre, y: mid + 4, textAnchor: 'middle' },
        step.reached ? Math.round((100 * next.reached) / step.reached) + '%' : '–'))
    }
    const on = Boolean(step.id) && filter.stage === step.id
    groups.push(h('g', { key: 'g' + index, className: 'stoa-fn-step', 'data-on': on ? '1' : '0', 'data-press': step.id ? '1' : '0',
      onClick: step.id ? () => setFilter({ stage: on ? '' : step.id }) : undefined },
      h('title', null, step.id ? (on ? 'Let go of ' + step.label : 'Pick out ' + step.label + ' on the board') : step.label),
      h('rect', { className: 'stoa-fn-hit', x: x - 4, y: 0, width: col - 2, height: H }),
      h('rect', { className: step.won ? 'stoa-fn-won' : 'stoa-fn-block', x, y: mid - tallness / 2, width: block, height: tallness, rx: 3,
        style: { animationDelay: index * 55 + 'ms' } }),
      h('text', { className: 'stoa-fn-label', x, y: 11 }, step.label.toUpperCase().slice(0, Math.max(6, Math.floor(col / 7.6)))),
      h('text', { className: 'stoa-fn-num', x, y: 31 }, String(step.reached)),
      h('text', { className: 'stoa-fn-sub', x, y: mid + tall / 2 + 22 },
        step.won ? (step.value ? money(step.value, pb.currency) : '') : step.here + ' here' + (step.value ? ' · ' + money(step.value, pb.currency) : '')),
      step.lost ? h('text', { className: 'stoa-fn-sub', x, y: mid + tall / 2 + 35 }, step.lost + ' lost here') : null))
  })
  return h('div', { className: 'stoa-funnel' },
    h('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': 'How many records reach each stage, and what share moves on' },
      h('defs', null,
        h('linearGradient', { id: 'stoa-fn-fill', x1: 0, y1: 0, x2: 0, y2: 1 },
          h('stop', { offset: '0%', style: { stopColor: 'var(--ui-text-primary)', stopOpacity: 0.62 } }),
          h('stop', { offset: '100%', style: { stopColor: 'var(--ui-text-primary)', stopOpacity: 0.36 } })),
        h('linearGradient', { id: 'stoa-fn-flow', x1: 0, y1: 0, x2: 1, y2: 0 },
          h('stop', { offset: '0%', style: { stopColor: 'var(--ui-text-primary)', stopOpacity: 0.2 } }),
          h('stop', { offset: '100%', style: { stopColor: 'var(--ui-text-primary)', stopOpacity: 0.08 } }))),
      links, groups),
    h('div', { className: 'stoa-funnel-foot' },
      h('span', null, h('b', null, closed ? Math.round((100 * flow.won) / closed) + '%' : '–'), ' win rate' + (closed ? ' (' + flow.won + ' of ' + closed + ' closed)' : '')),
      h('span', null, h('b', null, Math.round((100 * flow.won) / flow.total) + '%'), ' of everything that entered has been ' + flow.won_label.toLowerCase()),
      h('span', null, 'Percentages are the share that reached the next stage; ' + pb.terms.deals + ' still in a stage have not had their chance yet.')))
}

// -- the pane beside the conversation ------------------------------------------------------------
function Row({ deal, sub, right, tone, open, onClick }) {
  return h('button', { type: 'button', className: 'stoa-row', 'data-open': open ? '1' : '0', onClick },
    h('i', { className: 'stoa-dot', 'data-tone': tone || '' }),
    h('span', { className: 'stoa-grow' },
      h('span', { className: 'stoa-name stoa-clip', style: { display: 'block' } }, deal.name),
      sub ? h('span', { className: 'stoa-sub stoa-clip', style: { display: 'block' } }, sub) : null),
    right ? h('span', { className: 'stoa-num', 'data-tone': tone || '' }, right) : null)
}

function Pane() {
  const query = useDesk()
  const filter = useValue($filter)
  const [openId, setOpenId] = useState('')
  if (!query.data) return h(Unavailable, { error: query.error, loading: query.isLoading })
  const state = query.data, pb = state.playbook, totals = state.totals
  const everyone = state.deals.filter(deal => deal.status === 'open')
  const open = everyone.filter(deal => matches(deal, filter))
  const due = open.filter(deal => deal.due).sort((a, b) => (a.next_due < b.next_due ? -1 : 1))
  const quiet = open.filter(deal => deal.stale && !deal.due)
  const toggle = id => setOpenId(openId === id ? '' : id)
  const listed = (deal, key, sub, right, tone) => [
    h(Row, { key, deal, sub, right, tone, open: openId === key, onClick: () => toggle(key) }),
    openId === key ? h('div', { key: key + ':detail', className: 'stoa-detail' }, h(Record, { id: deal.id, state })) : null]
  const calls = (state.markets[0] ? state.markets[0].first_calls : []).filter(call => call.status === 'new').slice(0, 3)
  return h('div', { className: 'stoa stoa-pane' }, h(Style),
    h('div', { className: 'stoa-scroll' },
      h('div', { style: { padding: '0 8px' } },
        state.tour ? h('div', { className: 'stoa-tourline' }, 'Sample data · nothing here is real') : null,
        h('div', { className: 'stoa-eyebrow' }, 'Stoa · ' + pb.title),
        h('div', { className: 'stoa-figure' }, money(totals.open_value, pb.currency) || String(totals.open), h('small', null, totals.open_value ? 'in play' : pb.terms.deals + ' open')),
        h('div', { className: 'stoa-sub' }, [plural(totals.open, 'open', 'open'), totals.due ? totals.due + ' due' : '', totals.stale ? totals.stale + ' quiet' : '']
          .filter(Boolean).join(' · ')),
        h('div', { className: 'stoa-rule' }),
        state.deals.length ? h('div', { style: { marginTop: 10 } }, h(Filters, { state })) : null,
        filtering(filter) ? h('div', { className: 'stoa-sub', style: { marginTop: 6 } }, 'Showing ' + open.length + ' of ' + everyone.length) : null),
      (state.intakes || []).length ? h('div', { className: 'stoa-sec' },
        h('div', { className: 'stoa-sechead' }, h('span', { className: 'stoa-eyebrow' }, 'Waiting for your approval'), h('span', { className: 'stoa-num' }, state.intakes.length)),
        state.intakes.map(item => h('div', { key: item.id, style: { marginBottom: 8 } }, h(Review, { id: item.id })))) : null,
      !state.deals.length ? h('p', { className: 'stoa-sub', style: { padding: '8px' } },
        'Nothing on the desk yet. Tell Hermes about a ' + pb.terms.deal + ', send it a list, or ask it to map a market.') : null,
      due.length || quiet.length ? h('div', { className: 'stoa-sec' },
        h('div', { className: 'stoa-sechead' }, h('span', { className: 'stoa-eyebrow' }, 'Needs you'), h('span', { className: 'stoa-num' }, due.length + quiet.length)),
        due.map(deal => listed(deal, 'due:' + deal.id, deal.next_step || deal.stage_label, dueLabel(deal), dueTone(deal))),
        quiet.map(deal => listed(deal, 'quiet:' + deal.id, deal.stage_label, 'quiet ' + ago(deal.last_touch || deal.updated_at).replace(' ago', '')))) : null,
      pb.stages.map(stage => {
        const here = open.filter(deal => deal.stage === stage.id)
        if (!here.length) return null
        const value = here.reduce((sum, deal) => sum + (deal.value || 0), 0)
        return h('div', { className: 'stoa-sec', key: stage.id },
          h('div', { className: 'stoa-sechead' }, h('span', { className: 'stoa-eyebrow' }, stage.label),
            h('span', { className: 'stoa-num' }, here.length + (value ? ' · ' + money(value, pb.currency) : ''))),
          here.map(deal => listed(deal, stage.id + ':' + deal.id, [deal.company, deal.next_step].filter(Boolean).join(' · '), money(deal.value, pb.currency))))
      }),
      calls.length && !filtering(filter) ? h('div', { className: 'stoa-sec' },
        h('div', { className: 'stoa-sechead' }, h('span', { className: 'stoa-eyebrow' }, 'First calls'), h('span', { className: 'stoa-num' }, state.markets[0].name)),
        calls.map(call => h('div', { key: call.id, className: 'stoa-row', style: { cursor: 'default' } },
          h('span', { className: 'stoa-grow' },
            h('span', { className: 'stoa-name stoa-clip', style: { display: 'block' } }, call.company),
            h('span', { className: 'stoa-sub', style: { display: 'block' } }, call.why)),
          h(Btn, { onClick: () => track(call) }, 'Add')))) : null),
    h('div', { className: 'stoa-foot' },
      h(Btn, { onClick: () => host.navigate('/stoa') }, 'Open the desk'),
      h(Btn, { kind: 'quiet', onClick: () => toChat("What's on my plate today on the Stoa desk?") }, "What's on my plate?")))
}

// -- the dock at the foot of the page: say it, dictate it, or hand it a file ------------------------
const ICON = {
  clip: 'M21.4 11.1l-9.2 9.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5',
  mic: 'M12 2.5a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0v-6a3 3 0 0 0-3-3zM5.5 11a6.5 6.5 0 0 0 13 0M12 17.5v4',
  up: 'M12 19.5v-15M5.5 11L12 4.5 18.5 11',
  down: 'M6 9.5l6 6 6-6',
  x: 'M6.5 6.5l11 11M17.5 6.5l-11 11',
  back: 'M9 14.5L4 9.5l5-5M4 9.5h10.5a5.5 5.5 0 0 1 0 11H11'
}
const Icon = ({ name, size }) => h('svg', { width: size || 17, height: size || 17, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
  strokeWidth: 1.8, strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true }, h('path', { d: ICON[name] }))

/** A reply, set plainly: paragraphs, bullets and bold. Lines that only draw a card in a chat are left out. */
function Words({ text }) {
  const clean = String(text || '').replace(/^[ \t]*::stoa-[a-z]+\{[^}\n]*\}[ \t]*$/gm, '').trim()
  const bold = line => line.split(/(\*\*[^*]+\*\*)/g).map((part, index) => (/^\*\*[^*]+\*\*$/.test(part) ? h('b', { key: index }, part.slice(2, -2)) : part))
  return h('div', { className: 'stoa-reply' }, clean.split(/\n{2,}/).map((block, index) => h('p', { key: index },
    block.split('\n').map((line, at) => h('span', { key: at, style: { display: 'block' } }, bold(line.replace(/^\s*[-*]\s+/, '•  ').replace(/^#+\s+/, '')))))))
}

// Dictation is the browser's own speech recognition, where it has one (Chrome, Edge and Safari do;
// the Desktop app does not, and there the microphone in the full chat is the one to use).
const Recognition = typeof window !== 'undefined' ? window.SpeechRecognition || window.webkitSpeechRecognition : null

function Dock({ state }) {
  const talk = useValue($talk)
  const file = useValue($attached)
  const [text, setText] = useState('')
  const [listening, setListening] = useState(false)
  const area = useRef(null), picker = useRef(null), sheet = useRef(null), ear = useRef(null), before = useRef('')
  const draft = useValue($draft)
  useEffect(() => {
    if (!draft) return
    setText(draft)
    $draft.set('')
    const el = area.current
    if (el) { el.focus(); setTimeout(() => el.setSelectionRange(draft.length, draft.length), 0) }
  }, [draft])
  useEffect(() => {
    const el = area.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 132) + 'px'
  }, [text])
  useEffect(() => {
    if (sheet.current) sheet.current.scrollTop = sheet.current.scrollHeight
  }, [talk.turns, talk.open])
  useEffect(() => () => { try { if (ear.current) ear.current.abort() } catch { /* already stopped */ } }, [])
  const hush = () => { try { if (ear.current) ear.current.stop() } catch { /* already stopped */ } }
  const send = line => {
    const words = typeof line === 'string' ? line : text
    if ((!words.trim() && !file) || talk.busy) return
    hush()
    say(words, file)
    setText('')
    $attached.set(null)
  }
  const dictate = () => {
    if (listening) return hush()
    try {
      const hearing = new Recognition()
      hearing.lang = navigator.language || 'en-US'
      hearing.interimResults = true
      hearing.continuous = true
      before.current = text.trim() ? text.replace(/\s*$/, ' ') : ''
      hearing.onresult = event => {
        let said = ''
        for (let index = 0; index < event.results.length; index++) said += event.results[index][0].transcript
        setText(before.current + said)
      }
      hearing.onerror = event => {
        setListening(false)
        if (event.error === 'no-speech' || event.error === 'aborted') return
        host.notify({ kind: 'warning', message: event.error === 'not-allowed' || event.error === 'service-not-allowed'
          ? 'The browser did not allow the microphone.' : 'Dictation is not available here. The microphone in the full chat works everywhere.' })
      }
      hearing.onend = () => setListening(false)
      hearing.start()
      ear.current = hearing
      setListening(true)
    } catch {
      setListening(false)
    }
  }
  const hints = ['What needs me today?', 'Who has gone quiet?', 'What moved this week?']
  return h('div', { className: 'stoa-dockzone' }, h('div', { className: 'stoa-dock' },
    h(UndoBar),
    talk.turns.length && talk.open ? h('div', { className: 'stoa-glass stoa-sheet', ref: sheet },
      h('div', { className: 'stoa-sheethead' },
        h('span', { className: 'stoa-eyebrow' }, 'Stoa desk'),
        h('span', { style: { display: 'flex', gap: 2 } },
          h(Btn, { kind: 'quiet', onClick: openTalk }, 'Open the conversation'),
          h('button', { type: 'button', className: 'stoa-icon', 'aria-label': 'Hide the conversation', title: 'Hide', onClick: () => setTalk({ open: false }) }, h(Icon, { name: 'down' })))),
      talk.turns.map((turn, index) => h('div', { key: index, className: 'stoa-turn' },
        h('div', { className: 'stoa-youline' }, h('span', { className: 'stoa-you' }, turn.you, turn.file ? h('small', null, turn.file) : null)),
        turn.reply ? h(Words, { text: turn.reply }) : null,
        turn.note ? h('div', { className: 'stoa-sub stoa-working', style: { marginTop: 8 } }, turn.note) : null,
        turn.error ? h('div', { style: { marginTop: 8, color: 'var(--ui-red)' } }, turn.error) : null)))
      : talk.turns.length ? h('div', { className: 'stoa-hints' },
        h('button', { type: 'button', className: 'stoa-hint', onClick: () => setTalk({ open: true }) }, 'Show the conversation'))
        : !text && !file ? h('div', { className: 'stoa-hints' }, hints.map(hint =>
          h('button', { type: 'button', key: hint, className: 'stoa-hint', disabled: talk.busy, onClick: () => send(hint) }, hint))) : null,
    h('div', { className: 'stoa-glass stoa-composer' },
      file ? h('div', { className: 'stoa-filechip' }, h(Icon, { name: 'clip', size: 13 }), file.name,
        h('button', { type: 'button', className: 'stoa-icon', style: { width: 22, height: 22 }, 'aria-label': 'Remove the file', onClick: () => $attached.set(null) }, h(Icon, { name: 'x', size: 13 }))) : null,
      h('div', { className: 'stoa-composerrow' },
        h('button', { type: 'button', className: 'stoa-icon', title: 'Hand it a file: a CSV becomes a review, a text file goes with your message', 'aria-label': 'Attach a file',
          onClick: () => picker.current && picker.current.click() }, h(Icon, { name: 'clip' })),
        h('input', { ref: picker, type: 'file', hidden: true, accept: '.csv,.tsv,.txt,.md,.json,.vcf,text/*',
          onChange: event => { takeFile(event.target.files[0]); event.target.value = '' } }),
        h('textarea', { ref: area, rows: 1, value: text, disabled: talk.busy, 'aria-label': 'Talk to the desk',
          placeholder: listening ? 'Listening…' : 'Tell Stoa what happened, ask it anything, or drop a list on the page…',
          onChange: event => setText(event.target.value),
          onKeyDown: event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send() } } }),
        Recognition ? h('button', { type: 'button', className: 'stoa-icon', 'data-on': listening ? '1' : '0', disabled: talk.busy,
          title: listening ? 'Stop dictating' : 'Dictate (your browser\'s speech recognition)', 'aria-label': listening ? 'Stop dictating' : 'Dictate', onClick: dictate },
          h(Icon, { name: 'mic' })) : null,
        h('button', { type: 'button', className: 'stoa-icon', 'data-kind': 'send', disabled: talk.busy || (!text.trim() && !file), title: 'Send', 'aria-label': 'Send',
          onClick: () => send() }, h(Icon, { name: 'up' }))))))
}

// -- Today: what wants you, most pressing first, one move per row ---------------------------------
const tomorrow = () => { const d = new Date(Date.now() + 864e5); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0') }
const shortDate = iso => { const d = new Date(iso + 'T12:00:00'); return isNaN(d) ? iso : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) }
const asleep = deal => Boolean(deal.snoozed_until) && deal.snoozed_until > today()

/** What comes after a step is done, or the first step for a record that has none. Nothing is saved until the button. */
function NextStep({ deal, mode, close }) {
  const [step, setStep] = useState('')
  const [due, setDue] = useState('')
  const [busy, setBusy] = useState(false)
  const save = () => {
    if (busy || (mode === 'plan' && !step.trim())) return
    setBusy(true)
    act(deal.id, { verb: mode === 'done' ? 'done' : 'update', next_step: step.trim(), next_due: due }).then(result => { setBusy(false); if (result) close() })
  }
  return h('div', { className: 'stoa-nextform' },
    mode === 'done' ? h('div', { className: 'stoa-sub', style: { gridColumn: '1 / -1' } }, 'Done: ' + deal.next_step + '. What comes next?') : null,
    h('input', { className: 'stoa-field', autoFocus: true, value: step, 'aria-label': 'The next step',
      placeholder: mode === 'done' ? 'The next step (leave empty if nothing is planned)' : 'The next step',
      onChange: event => setStep(event.target.value),
      onKeyDown: event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) save(); if (event.key === 'Escape') close() } }),
    h('input', { className: 'stoa-field', type: 'date', value: due, min: today(), 'aria-label': 'Due', onChange: event => setDue(event.target.value) }),
    h(Btn, { kind: 'primary', disabled: busy || (mode === 'plan' && !step.trim()), onClick: save }, mode === 'done' ? 'Mark done' : 'Save'),
    h(Btn, { kind: 'quiet', disabled: busy, onClick: close }, 'Cancel'))
}

/** Put a record aside for a while, or until a date. Until then it is on no list that asks for attention. */
function Snooze({ deal, busy, run }) {
  const [picking, setPicking] = useState(false)
  const [date, setDate] = useState('')
  if (picking) {
    return h('span', { className: 'stoa-pickdate' },
      h('input', { className: 'stoa-field', type: 'date', autoFocus: true, min: tomorrow(), value: date, 'aria-label': 'Snooze until', onChange: event => setDate(event.target.value),
        onKeyDown: event => { if (event.key === 'Escape') setPicking(false) } }),
      h(Btn, { disabled: busy || !date || date <= today(), onClick: () => { run({ verb: 'snooze', until: date }); setPicking(false) } }, 'Snooze'),
      h(Btn, { kind: 'quiet', onClick: () => setPicking(false), 'aria-label': 'Cancel' }, '×'))
  }
  const off = asleep(deal)
  return h('select', { className: 'stoa-select', value: '', disabled: busy, 'aria-label': 'Snooze',
    title: 'Put it aside: it leaves Today, the due list and the quiet list until then',
    onChange: event => {
      const pick = event.target.value
      if (pick === 'date') setPicking(true)
      else if (pick === 'wake') run({ verb: 'snooze', days: 0 })
      else if (pick) run({ verb: 'snooze', days: Number(pick) })
    } },
  [h('option', { key: 'none', value: '' }, off ? 'Snoozed to ' + shortDate(deal.snoozed_until) : 'Snooze…')]
    .concat(off ? [h('option', { key: 'wake', value: 'wake' }, 'Wake it now')] : [])
    .concat([['1', 'Until tomorrow'], ['3', '3 days'], ['7', 'A week'], ['14', 'Two weeks'], ['30', 'A month']].map(pair => h('option', { key: pair[0], value: pair[0] }, pair[1])))
    .concat([h('option', { key: 'date', value: 'date' }, 'Pick a date…')]))
}

/** Hand a record's next step to Hermes from the page: it writes in the dock's conversation. */
function draftFor(deal, quiet) {
  if (quiet || !deal.next_step) return act(deal.id, { verb: 'draft' })
  toChat('On the Stoa desk, ' + deal.name + ' (record ' + deal.id + ') has this next step due: “' + deal.next_step + '”. Draft what it needs and show me.')
  return Promise.resolve(null)
}

const TODAY_LISTS = [
  { key: 'overdue', title: 'Overdue', tone: 'bad', help: 'The next step had a date, and the date has passed.' },
  { key: 'due_today', title: 'Due today', tone: 'warn', help: 'The next step is dated today.' },
  { key: 'drafts', title: 'Writing waiting for Hermes', tone: 'good', help: 'A button asked for a draft and nobody has picked it up yet.' },
  { key: 'invoices_overdue', title: 'Invoices overdue', tone: 'bad', help: 'Sent, not paid in full, and past the due date.' },
  { key: 'to_invoice', title: 'Won, not invoiced', tone: 'warn', help: 'Won in the last two months with no invoice on the record. Shown once the desk keeps invoices at all.' },
  { key: 'quiet', title: 'Gone quiet', tone: '', help: 'No call, email, note or move for longer than the stage allows. Your playbook sets the days for each stage.' },
  { key: 'new', title: 'New, nothing planned', tone: '', help: 'Added in the last week, with no next step yet.' }
]

function Today({ state, openId, setOpenId }) {
  const pb = state.playbook, day = state.today
  const [form, setForm] = useState(null) // the row with its "what comes next" form open: { id, mode }
  const [busy, setBusy] = useState('')
  const byId = new Map(state.deals.map(deal => [deal.id, deal]))
  const run = (id, body) => { setBusy(id); return act(id, body).then(() => setBusy('')) }
  const facts = {
    overdue: (deal, row) => [deal.next_step || deal.stage_label, row.days + 'd overdue'],
    due_today: deal => [deal.next_step || deal.stage_label, 'today'],
    drafts: deal => [deal.stage_label + ' · a draft is waiting to be written', ''],
    invoices_overdue: (deal, row) => ['Invoice' + (row.number ? ' #' + row.number : '') + ' · ' + exact(row.owed, pb.currency) + ' owed', row.days + 'd late'],
    to_invoice: (deal, row) => [exact(row.amount, pb.currency) + ' · ' + pb.won.label.toLowerCase() + (row.days ? ' ' + row.days + 'd ago' : ' today'), ''],
    quiet: (deal, row) => [deal.stage_label + ' · nothing for ' + row.days + 'd', 'nudge due after ' + row.expected + 'd'],
    new: deal => [[deal.company, 'added ' + ago(deal.created_at)].filter(Boolean).join(' · '), '']
  }
  const moves = {
    overdue: deal => [h(Btn, { key: 'done', kind: 'go', title: 'Log it as done and say what comes next', onClick: () => setForm({ id: deal.id, mode: 'done' }) }, '✓  Done'),
      h(Btn, { key: 'draft', title: 'Hermes drafts what this step needs', onClick: () => draftFor(deal) }, 'Draft it'),
      h(Snooze, { key: 'snooze', deal, busy: busy === deal.id, run: body => run(deal.id, body) })],
    invoices_overdue: (deal, row) => [
      h(Btn, { key: 'paid', kind: 'go', title: 'Everything still owed came in', onClick: () => bill(deal.id, { verb: 'pay', invoice: String(row.invoice) }) }, 'Paid'),
      h(Btn, { key: 'remind', title: 'Hermes drafts a payment reminder', onClick: () => toChat('On the Stoa desk, ' + deal.name + ' (record ' + deal.id + ') has an invoice'
        + (row.number ? ' #' + row.number : '') + ' with ' + exact(row.owed, pb.currency) + ' still owed, ' + row.days + ' days past due. Draft a short, polite payment reminder and show me.') }, 'Draft a reminder'),
      h(Snooze, { key: 'snooze', deal, busy: busy === deal.id, run: body => run(deal.id, body) })],
    to_invoice: deal => [
      h(Btn, { key: 'bill', kind: 'go', onClick: () => { $billing.set(deal.id); setOpenId(deal.id) } }, 'Add invoice'),
      h(Snooze, { key: 'snooze', deal, busy: busy === deal.id, run: body => run(deal.id, body) })],
    drafts: deal => [h(Btn, { key: 'write', kind: 'go', onClick: () => toChat('Do the writing task waiting on ' + deal.name + ' on the Stoa desk (record ' + deal.id + ').') }, 'Write it now')],
    quiet: deal => [h(Btn, { key: 'draft', kind: 'go', title: 'Hermes drafts a follow-up from the record and its log', onClick: () => draftFor(deal, true) }, 'Draft a nudge'),
      h(Snooze, { key: 'snooze', deal, busy: busy === deal.id, run: body => run(deal.id, body) })],
    new: deal => [h(Btn, { key: 'plan', kind: 'go', onClick: () => setForm({ id: deal.id, mode: 'plan' }) }, 'Plan the next step')],
  }
  moves.due_today = moves.overdue
  const rows = (key, tone) => (day[key] || []).map(row => {
    const deal = byId.get(row.id)
    if (!deal) return null
    const [sub, right] = facts[key](deal, row)
    const open = Boolean(form) && form.id === deal.id
    return h('div', { key: key + deal.id + ':' + (row.invoice || ''), className: 'stoa-trow', 'data-open': openId === deal.id ? '1' : '0' },
      h('button', { type: 'button', className: 'stoa-tmain', title: 'Open the record', onClick: () => setOpenId(openId === deal.id ? '' : deal.id) },
        h('i', { className: 'stoa-dot', 'data-tone': tone }),
        h('span', { className: 'stoa-grow' },
          h('span', { className: 'stoa-name stoa-clip', style: { display: 'block' } }, deal.name,
            deal.value ? h('span', { className: 'stoa-sub', style: { fontWeight: 400 } }, '  ' + money(deal.value, pb.currency)) : null),
          h('span', { className: 'stoa-sub stoa-clip', style: { display: 'block' } }, sub))),
      right ? h('span', { className: 'stoa-num', 'data-tone': tone }, right) : null,
      h('span', { className: 'stoa-tacts' }, moves[key](deal, row)),
      open ? h(NextStep, { deal, mode: form.mode, close: () => setForm(null) }) : null)
  })
  const counts = day.counts
  const tally = [[counts.overdue, 'overdue', 'bad'], [counts.due_today, 'due today', 'warn'], [counts.approvals, 'to approve', 'good'],
    [counts.invoices_overdue, counts.invoices_overdue === 1 ? 'invoice overdue' : 'invoices overdue', 'bad'], [counts.to_invoice, 'to invoice', 'warn'],
    [counts.drafts, counts.drafts === 1 ? 'draft waiting' : 'drafts waiting', 'good'], [counts.quiet, 'gone quiet', ''], [counts.new, 'new', '']].filter(item => item[0])
  return h('div', { className: 'stoa-today' },
    h('div', { className: 'stoa-todayhead' },
      h('div', { className: 'stoa-name', style: { fontSize: 16 } }, day.needs ? plural(day.needs, 'thing needs you', 'things need you') : 'Nothing needs you today'),
      tally.length ? h('div', { className: 'stoa-chips', style: { marginTop: 0 } }, tally.map(item => h(Chip, { key: item[1], tone: item[2] }, item[0] + ' ' + item[1])))
        : h('span', { className: 'stoa-sub' }, counts.coming ? 'What is coming up is on the right.' : state.deals.length ? 'No next step is due and nobody has gone quiet.' : '')),
    h(Approvals, { state, setOpenId }),
    TODAY_LISTS.map(list => (day[list.key] || []).length ? h('div', { key: list.key, className: 'stoa-tsec' },
      h('div', { className: 'stoa-sechead', style: { padding: '0 0 4px' } },
        h('span', { className: 'stoa-eyebrow', title: list.help }, list.key === 'to_invoice' ? pb.won.label + ', not invoiced' : list.title),
        h('span', { className: 'stoa-num' }, String(counts[list.key]))),
      rows(list.key, list.tone)) : null))
}

/** Beside Today while no record is open: what is coming, how the week is going, and what is put aside. */
function Ahead({ state, setOpenId }) {
  const day = state.today, totals = state.totals, pb = state.playbook
  const byId = new Map(state.deals.map(deal => [deal.id, deal]))
  const line = (row, right, extra) => {
    const deal = byId.get(row.id)
    return deal ? h('div', { key: row.id, className: 'stoa-aheadrow' },
      h('button', { type: 'button', className: 'stoa-row', style: { padding: '5px 6px' }, title: 'Open the record', onClick: () => setOpenId(deal.id) },
        h('span', { className: 'stoa-grow' },
          h('span', { className: 'stoa-name stoa-clip', style: { display: 'block' } }, deal.name),
          h('span', { className: 'stoa-sub stoa-clip', style: { display: 'block' } }, deal.next_step || deal.stage_label)),
        h('span', { className: 'stoa-num' }, right)), extra || null) : null
  }
  const coming = day.coming || [], snoozed = day.snoozed || []
  return h('div', { className: 'stoa-box stoa-ahead' },
    h('div', { className: 'stoa-sechead', style: { padding: 0 } }, h('span', { className: 'stoa-eyebrow' }, 'Coming up this week'), h('span', { className: 'stoa-num' }, String(coming.length))),
    coming.length ? coming.slice(0, 8).map(row => line(row, shortDate(row.due)))
      : h('div', { className: 'stoa-sub', style: { padding: '6px 0 2px' } }, 'No next step is dated in the next seven days.'),
    coming.length > 8 ? h('div', { className: 'stoa-sub', style: { padding: '4px 6px 0' } }, 'and ' + (coming.length - 8) + ' more') : null,
    h('div', { className: 'stoa-edit', style: { paddingTop: 10 } },
      h('div', { className: 'stoa-eyebrow' }, 'The last seven days'),
      h('div', { className: 'stoa-weekline' }, [[totals.new_week, 'new'], [totals.moves_week, totals.moves_week === 1 ? 'stage move' : 'stage moves']].map(pair =>
        h('span', { key: pair[1] }, h('b', null, String(pair[0])), ' ' + pair[1])))),
    snoozed.length ? h('div', { className: 'stoa-edit', style: { paddingTop: 10 } },
      h('div', { className: 'stoa-sechead', style: { padding: 0 } }, h('span', { className: 'stoa-eyebrow', title: 'Put aside until a date: on no list until then' }, 'Snoozed'),
        h('span', { className: 'stoa-num' }, String(snoozed.length))),
      snoozed.slice(0, 8).map(row => line(row, 'to ' + shortDate(row.until),
        h(Btn, { kind: 'quiet', title: 'Bring it back now', onClick: () => act(row.id, { verb: 'snooze', days: 0 }) }, 'Wake')))) : null)
}

// -- the way in: a desk nobody has set up yet --------------------------------------------------------
async function switchPlaybook(name, title) {
  try {
    const result = await rest('/playbook', { method: 'POST', body: { name } })
    refresh()
    if (result && result.ask) toChat(result.ask) // this page cannot save a setting: Hermes can
    else host.notify({ kind: result && result.warning ? 'warning' : 'success', message: (result && result.warning) || 'The desk now runs on ' + title + '.' })
    return true
  } catch (error) {
    host.notifyError(error, 'Stoa could not switch playbook')
    return false
  }
}

async function tour(action, example) {
  try {
    const result = await rest('/tour', { method: 'POST', body: { action, example: example || '' } })
    refresh()
    if (action === 'start') host.notify({ kind: 'info', message: 'Sample data: ' + result.title + '. Your own desk is untouched.' })
    else if (action === 'leave') host.notify({ kind: 'info', message: 'Back on your own desk.' })
    else host.notify({ kind: 'success', message: plural(result.removed || 0, 'sample record', 'sample records') + ' removed from your desk.' })
    return result
  } catch (error) {
    host.notifyError(error, 'Stoa could not do that')
    return null
  }
}

const an = word => (/^[aeiou]/i.test(word) ? 'an ' : 'a ') + word
const DOOR_ORDER = ['b2b-sales', 'agency', 'client-leads', 'recruiting', 'partnerships'] // the shipped ones, most general first; a team's own come after

function Doors({ playbooks, active, pick, busy }) {
  const place = item => { const at = DOOR_ORDER.indexOf(item.name); return at < 0 ? DOOR_ORDER.length : at }
  const doors = playbooks.filter(item => !item.error).sort((a, b) => place(a) - place(b) || a.name.localeCompare(b.name))
  return h('div', { className: 'stoa-doors' }, doors.map(item =>
    h('div', { key: item.name, className: 'stoa-box stoa-door', 'data-on': item.name === active ? '1' : '0' },
      h('div', { className: 'stoa-name', style: { fontSize: 14 } }, item.door || item.title),
      h('div', { className: 'stoa-sub', style: { marginTop: 2 } }, item.summary),
      h('div', { className: 'stoa-chain' }, item.stages.concat([item.won]).join('  →  ')),
      h('div', { className: 'stoa-sub' }, 'Each record is ' + an(item.words.split(', ')[0]) + ', grouped by ' + item.words.split(', ')[1] + '.'),
      h('div', { className: 'stoa-btns' }, item.name === active ? h(Chip, { tone: 'good' }, 'in use')
        : h(Btn, { kind: 'primary', disabled: busy, onClick: () => pick(item) }, 'Use this')))))
}

function Welcome({ skip }) {
  const query = useQuery({ queryKey: ['stoa', 'setup'], queryFn: () => rest('/setup'), staleTime: 3000 })
  const [busy, setBusy] = useState(false)
  const setup = query.data
  if (!setup) return h('p', { className: 'stoa-sub' }, query.error ? 'The desk is not answering yet (' + why(query.error) + '). Reload in a moment.' : 'Opening the desk…')
  const pick = item => { setBusy(true); switchPlaybook(item.name, item.title).then(ok => { setBusy(false); if (ok) skip() }) }
  return h('div', { className: 'stoa-welcome' },
    h('div', { className: 'stoa-eyebrow' }, 'Welcome to Stoa'),
    h('div', { className: 'stoa-name', style: { fontSize: 22, margin: '6px 0 4px', letterSpacing: '-.01em' } }, 'The CRM Hermes runs for you.'),
    h('p', { className: 'stoa-reading', style: { marginTop: 0 } },
      'You say what happened, in your own words. Hermes keeps the pipeline, logs your email and calls, drafts the follow-ups, and asks before anything changes stage. Start by choosing how you sell.'),
    h(Doors, { playbooks: setup.playbooks, active: '', pick, busy }),
    h('div', { className: 'stoa-box', style: { marginTop: 14 } },
      h('div', { className: 'stoa-name' }, 'Or look around first'),
      h('div', { className: 'stoa-sub' }, 'A full pipeline of made-up people and companies, kept in a file of its own. Your desk stays empty, and one press brings you back.'),
      h('div', { className: 'stoa-btns' }, setup.examples.map(example =>
        h(Btn, { key: example.id, disabled: busy, onClick: () => { setBusy(true); tour('start', example.id).then(() => setBusy(false)) } }, cap(example.title))),
        h(Btn, { kind: 'quiet', disabled: busy, onClick: skip }, 'Skip: I will set it up later'))))
}

// -- Setup: what is connected, the rules in force, and how to work the desk ------------------------
function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type: type || 'text/plain' }))
  const link = document.createElement('a')
  link.href = url
  link.download = name
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 4000)
}

const SAY = ['Add Priya Raman from Lumen Health, met at the conference, call her Friday', 'I spoke to Dana: she wants the proposal by Thursday',
  'Add their CFO, Lena Ortiz, to the Kestrel deal', 'Who do we know at Kestrel?', 'Undo that',
  'I invoiced Kestrel 12k today, net 30', 'Kestrel paid half of invoice 1042', 'How is cash looking, and what is likely to close this month?',
  'Move Marcus to the next stage and draft the email', 'Who has gone quiet, and what should I send each of them?', 'Here are my leads (then drop a CSV on the page)',
  'Map the market for us and add the three best fits', 'Write me a playbook for selling to dental practices']
const BY_HAND = ['Drag a card to another column to move it; drop it on the last column to close it as won.', 'Changed your mind? Undo appears above the box at the foot of the page, and on the record.',
  'Press a stage in the funnel to pick out its column on the board; press it again to let go.', 'Press a company name on a record, or Companies on the pipeline, for everything at that company.',
  'Press a card or a row to open the record: advance it, mark the step done, snooze it, or edit any fact.', 'Drop a CSV anywhere on the page. It comes back as a review; nothing is written until you apply.',
  'Use the microphone in the box at the foot of the page to dictate.']
const TYPED = [['/gtm', 'the board, in any Hermes chat'], ['/gtm today', 'what needs you, most pressing first'],
  ['/gtm cash', 'won, invoiced, collected, overdue, and the forecast'], ['/gtm brief', 'the morning brief'], ['/gtm <name>', 'one record as a card'],
  ['/gtm map', 'your market map'], ['!gtm …', 'the same in Slack, which keeps the slash for itself'], ['/blueprint', 'switch on an automation'], ['hermes stoa status', 'in a terminal: what is connected']]
const ALWAYS = ['Keeps one ledger: every change to a record or a person, what it replaced and what it left. Lines are never altered.',
  'Keeps each person once, however many records they are on.', 'Gives each record one next step and a date, and tells you when the date comes.',
  'Calls a record quiet when nothing has happened for longer than its stage allows.', 'Holds any list, and anything Hermes suggests, for your yes or no, with what it rests on.',
  'Remembers a no, and does not ask again.', 'Lets you take a change back, whoever made it, unless that would overwrite a later one.',
  'Takes a record off the desk without destroying it, so it can be put back.',
  'Logs what it finds in your email, calls and Slack on the right record, whoever on it sent it.', 'Posts cards in Slack whose buttons act at once, and only for the people allowed to press them.',
  'Keeps the invoices on a record in whole cents, and tells you what is late and what was won but never billed.',
  'Lets you snooze anything until a date, and export everything at any time.']

/** An error from the desk's backend, as a line a person can pass on: its message, and the status if there is one. */
function why(error) {
  if (!error) return ''
  const parts = [error.status || (error.response && error.response.status) || '', error.detail || (error.body && error.body.detail) || '', error.message || String(error)]
  return [...new Set(parts.map(part => String(part || '').trim()).filter(Boolean))].join(' · ').slice(0, 400) || 'No reason was given.'
}

function Lamp({ on }) {
  return h('i', { className: 'stoa-lamp', 'data-on': on === true ? '1' : on === false ? '0' : '', 'aria-label': on === true ? 'on' : on === false ? 'off' : 'not checked' })
}

/** The pipelines on this desk: each has its own records and its own playbook. Add one, rename one, remove an empty one. */
function Pipelines({ state, setup }) {
  const pipes = state.pipelines || []
  const [adding, setAdding] = useState(false)
  const [name, setName] = useState('')
  const [playbook, setPlaybook] = useState('')
  const [renaming, setRenaming] = useState('')
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const send = (body, said) => {
    setBusy(true)
    return rest('/pipeline', { method: 'POST', body })
      .then(result => { refresh(); if (said) host.notify({ kind: 'success', message: said }); return result })
      .catch(error => { host.notifyError(error, 'Stoa could not do that'); return null })
      .then(result => { setBusy(false); return result })
  }
  const add = () => {
    if (!name.trim() || busy) return
    send({ verb: 'add', name: name.trim(), playbook: playbook || (setup.playbooks[0] || {}).name }, name.trim() + ' is open. It is in the row of pipelines at the top of the page.')
      .then(result => { if (result) { setName(''); setAdding(false) } })
  }
  const rename = id => {
    if (!title.trim() || busy) return
    send({ verb: 'rename', id, name: title.trim() }).then(result => { if (result) setRenaming('') })
  }
  return h('div', { className: 'stoa-setbox', 'data-wide': '0' }, h('div', { className: 'stoa-box' },
    h('div', { className: 'stoa-sechead', style: { padding: '0 0 8px' } }, h('span', { className: 'stoa-eyebrow' }, 'Pipelines'),
      h('span', { className: 'stoa-sub' }, pipes.length > 1 ? pipes.length + ' side by side' : 'one')),
    pipes.map(item => h('div', { key: item.id, className: 'stoa-piperow' },
      renaming === item.id
        ? h('input', { className: 'stoa-field stoa-grow', autoFocus: true, value: title, 'aria-label': 'Name', onChange: event => setTitle(event.target.value),
          onKeyDown: event => { if (event.key === 'Enter') rename(item.id); if (event.key === 'Escape') setRenaming('') } })
        : h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name', style: { display: 'block' } }, item.name),
          h('span', { className: 'stoa-sub' }, item.title + ' · ' + item.open + ' open' + (item.id === state.pipeline ? ' · on screen' : ''))),
      renaming === item.id
        ? h(Btn, { kind: 'primary', disabled: busy || !title.trim(), onClick: () => rename(item.id) }, 'Save')
        : h(Btn, { kind: 'quiet', disabled: busy, onClick: () => { setRenaming(item.id); setTitle(item.name) } }, 'Rename'),
      !item.first && !item.open && renaming !== item.id ? h(Btn, { kind: 'quiet', disabled: busy, title: 'Only a pipeline with nothing in it can be removed',
        onClick: () => send({ verb: 'remove', id: item.id }, item.name + ' was removed.') }, 'Remove') : null)),
    h('p', { className: 'stoa-sub', style: { margin: '10px 0 0' } }, 'Each pipeline has its own records, its own playbook and its own autonomy setting: your own sales beside the work you do for clients, say. In a chat, name the pipeline; left out, Hermes means the first.'),
    adding ? h('div', null,
      h('div', { className: 'stoa-form', style: { marginTop: 10 } },
        h(Field, { label: 'Name', autoFocus: true, value: name, placeholder: 'My practice', onChange: event => setName(event.target.value), onKeyDown: event => { if (event.key === 'Enter') add() } }),
        h(Field, { label: 'Runs on' }, h('select', { className: 'stoa-select', style: { width: '100%' }, value: playbook || (setup.playbooks[0] || {}).name, onChange: event => setPlaybook(event.target.value) },
          setup.playbooks.map(item => h('option', { key: item.name, value: item.name }, item.door || item.title || item.name))))),
      h('div', { className: 'stoa-btns', style: { marginTop: 8 } }, h(Btn, { kind: 'primary', disabled: busy || !name.trim(), onClick: add }, 'Open it'),
        h(Btn, { kind: 'quiet', onClick: () => setAdding(false) }, 'Cancel')))
      : pipes.length < 6 ? h('div', { className: 'stoa-btns', style: { marginTop: 10 } }, h(Btn, { onClick: () => setAdding(true) }, '+  Pipeline')) : null))
}

function Setup({ state, go }) {
  const query = useQuery({ queryKey: ['stoa', 'setup'], queryFn: () => rest('/setup'), staleTime: 3000, refetchInterval: 15000 })
  const [busy, setBusy] = useState(false)
  const [switching, setSwitching] = useState(false)
  const setup = query.data
  if (!setup) {
    if (!query.error) return h('p', { className: 'stoa-sub' }, 'Loading…')
    // Say what actually went wrong: this is the tab people open when something is wrong.
    return h('div', { className: 'stoa-box', style: { maxWidth: 720 } },
      h('div', { className: 'stoa-name' }, 'Setup is not answering'),
      h('p', { className: 'stoa-sub', style: { margin: '6px 0 0', overflowWrap: 'anywhere' } }, why(query.error)),
      h('p', { className: 'stoa-sub', style: { margin: '6px 0 0' } }, 'The rest of the desk is not affected. If Stoa was just updated, restart the gateway (hermes gateway restart) and reload.'),
      h('div', { className: 'stoa-btns' }, h(Btn, { disabled: query.isFetching, onClick: () => query.refetch() }, query.isFetching ? 'Trying…' : 'Try again')))
  }
  const data = setup.data, sets = setup.playbook_sets, left = setup.checklist.filter(item => !item.done).length
  const wait = promise => { setBusy(true); return Promise.resolve(promise).then(() => setBusy(false)) }
  const button = item => {
    if (item.done) return null
    if (item.id === 'playbook') return h(Btn, { onClick: () => setSwitching(true) }, 'Choose')
    if (item.id === 'records') return setup.tour ? null : h(Btn, { onClick: () => go('add') }, '+  New ' + state.playbook.terms.deal)
    if (item.id === 'sweep') return h(Btn, { onClick: () => $draft.set('/blueprint') }, 'Put /blueprint in the box')
    return null
  }
  const exportAll = () => wait(rest('/export').then(file => { download(file.filename, file.csv, 'text/csv'); host.notify({ kind: 'success', message: plural(file.rows, 'record', 'records') + ' exported.' }) })
    .catch(error => host.notifyError(error, 'Stoa could not export')))
  const box = (title, right, children, wide) => h('div', { className: 'stoa-setbox', 'data-wide': wide ? '1' : '0' }, h('div', { className: 'stoa-box' },
    h('div', { className: 'stoa-sechead', style: { padding: '0 0 8px' } }, h('span', { className: 'stoa-eyebrow' }, title), right ? h('span', { className: 'stoa-sub' }, right) : null),
    children))
  return h('div', { className: 'stoa-setup' },
    (setup.problems || []).length ? h('div', { className: 'stoa-setbox', 'data-wide': '1' }, h('div', { className: 'stoa-box', style: { borderColor: 'var(--ui-orange)' } },
      h('div', { className: 'stoa-name' }, 'Some of this could not be read'),
      setup.problems.map((line, index) => h('p', { key: index, className: 'stoa-sub', style: { margin: '4px 0 0', overflowWrap: 'anywhere' } }, line)))) : null,
    box('Get started', left ? left + ' left' : 'all done', setup.checklist.map(item => h('div', { key: item.id, className: 'stoa-check', 'data-done': item.done ? '1' : '0' },
      h('i', { className: 'stoa-tick', 'aria-hidden': true }, item.done ? '✓' : ''),
      h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name', style: { display: 'block' } }, item.title), h('span', { className: 'stoa-sub' }, item.detail)),
      button(item))), true),
    box('Connected', '', setup.connections.map(item => h('div', { key: item.id, className: 'stoa-setrow' },
      h(Lamp, { on: item.on }), h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name', style: { display: 'block' } }, item.label), h('span', { className: 'stoa-sub' }, item.detail))))),
    box('What Hermes may do by itself', setup.autonomy.levels.find(level => level.id === setup.autonomy.level).label, h(Autonomy, { setup })),
    box('Rules in force', 'set in the plugin\'s settings', setup.rules.filter(rule => !rule.own).map(rule => h('div', { key: rule.label, className: 'stoa-setrow', style: { display: 'block' } },
      h('small', { className: 'stoa-eyebrow', style: { display: 'block' } }, rule.label), h('span', { style: { overflowWrap: 'anywhere' } }, rule.value)))),
    setup.tour ? null : h(Pipelines, { key: 'pipes', state, setup }),
    box('Your playbook', setup.playbook.origin === 'custom' ? 'your own' : 'built in', [
      h('div', { key: 'title', className: 'stoa-name', style: { fontSize: 14 } }, setup.playbook.title),
      h('div', { key: 'sum', className: 'stoa-sub' }, setup.playbook.summary),
      h('div', { key: 'chain', className: 'stoa-chain' }, sets.stages.join('  →  ')),
      h('div', { key: 'facts', className: 'stoa-facts', style: { marginTop: 10 } },
        [['The words', sets.words], ['A nudge is due after', sets.quiet.map(item => item.stage + ' ' + item.days + 'd').join(' · ')], ['Reasons for a loss', sets.lost_reasons.join(' · ')],
          ['Extra facts on a record', sets.fields.join(' · ') || 'none'], ['Hermes drafts when a record enters', sets.drafts.join(' · ') || 'nothing'],
          ['A lookup finds', sets.lookup === 'none' ? 'nothing (switched off)' : 'the ' + sets.lookup]].map(pair =>
          h('div', { key: pair[0], style: { gridColumn: '1 / -1' } }, h('small', { className: 'stoa-eyebrow' }, pair[0]), h('span', null, pair[1])))),
      h('div', { key: 'btns', className: 'stoa-btns' },
        h(Btn, { onClick: () => setSwitching(!switching) }, switching ? 'Close' : 'Switch playbook'),
        h(Btn, { kind: 'quiet', onClick: () => $draft.set('Write me a playbook for ') }, 'Have Hermes write one for you'))]),
    switching ? h('div', { className: 'stoa-setbox', 'data-wide': '1' },
      setup.tour ? h('p', { className: 'stoa-sub' }, 'You are looking at sample data, which brings its own playbook. A switch here applies to your own desk.') : null,
      h(Doors, { playbooks: setup.playbooks, active: setup.playbook.name, busy,
        pick: item => wait(switchPlaybook(item.name, item.title).then(ok => { if (ok) setSwitching(false) })) })) : null,
    box('Your data', data.records ? plural(data.records, 'record', 'records') : 'empty', [
      h('div', { key: 'counts', className: 'stoa-facts', style: { marginTop: 0, paddingTop: 0, borderTop: 0 } },
        [['Open', data.open], ['Won', data.won], ['Lost', data.lost], ['Log entries', data.activities], ['Market maps', data.maps], ['Lists waiting', data.lists_waiting],
          ['Changes on the ledger', data.ledger], ['Taken off the desk', data.archived]].map(pair =>
          h('div', { key: pair[0] }, h('small', { className: 'stoa-eyebrow' }, pair[0]), h('span', null, String(pair[1]))))),
      h('div', { key: 'where', className: 'stoa-sub', style: { marginTop: 10, overflowWrap: 'anywhere' } }, 'One SQLite file on the machine Hermes runs on: ' + data.path),
      h('div', { key: 'copies', className: 'stoa-sub', style: { marginTop: 4, overflowWrap: 'anywhere' } }, data.backups && data.backups.count
        ? 'A copy is kept each day, the last fourteen: ' + plural(data.backups.count, 'copy', 'copies') + ' so far, the newest from ' + data.backups.newest + ', in ' + data.backups.folder + '.'
        : 'A copy of the file is kept each day once there is something on the desk, the last fourteen, in a backups folder beside it.'),
      h('div', { key: 'btns', className: 'stoa-btns' }, h(Btn, { disabled: busy, onClick: exportAll }, setup.tour ? 'Export the sample data as CSV' : 'Export everything as CSV')),
      h('div', { key: 'tour', className: 'stoa-edit', style: { paddingTop: 10 } },
        h('div', { className: 'stoa-name' }, setup.tour ? 'Sample data is on show' : 'Sample data'),
        h('div', { className: 'stoa-sub' }, setup.tour ? cap(setup.tour.title) + ', kept in a file of its own. Your desk is untouched and comes back when you leave.'
          : 'See a full pipeline without touching yours: made-up people and companies in a file of their own.'),
        h('div', { className: 'stoa-btns' }, setup.tour ? h(Btn, { kind: 'primary', disabled: busy, onClick: () => wait(tour('leave')) }, 'Leave the tour')
          : setup.examples.map(example => h(Btn, { key: example.id, disabled: busy, onClick: () => wait(tour('start', example.id)) }, cap(example.title)))),
        data.samples && !setup.tour ? h('div', { style: { marginTop: 10 } },
          h('div', { className: 'stoa-sub' }, plural(data.samples, 'sample record is', 'sample records are') + ' mixed into your own desk (an earlier version of Stoa put them there).'),
          h('div', { className: 'stoa-btns', style: { marginTop: 6 } }, h(Btn, { disabled: busy, onClick: () => wait(tour('tidy')) }, 'Remove them from my desk'))) : null)]),
    box('Automations', 'type /blueprint in a chat to switch one on', setup.automations.map(item => h('div', { key: item.name, className: 'stoa-setrow', style: { display: 'block' } },
      h('span', { className: 'stoa-name', style: { display: 'block' } }, item.title), h('span', { className: 'stoa-sub' }, item.description)))),
    box('How to work the desk', '', h('div', { className: 'stoa-cols', style: { gap: 22 } },
      h('div', null, h('div', { className: 'stoa-name' }, 'Say it'), h('div', { className: 'stoa-sub', style: { marginBottom: 6 } }, 'Press one to put it in the box below.'),
        SAY.map(line => h('button', { type: 'button', key: line, className: 'stoa-sayline', onClick: () => $draft.set(line.replace(/ \(then.*\)$/, '')) }, '“' + line + '”'))),
      h('div', null, h('div', { className: 'stoa-name', style: { marginBottom: 6 } }, 'Or do it by hand'), BY_HAND.map(line => h('p', { key: line, className: 'stoa-tip' }, line))),
      h('div', null, h('div', { className: 'stoa-name', style: { marginBottom: 6 } }, 'Or type it'),
        TYPED.map(pair => h('p', { key: pair[0], className: 'stoa-tip' }, h('code', null, pair[0]), '  ' + pair[1])))), true),
    box('What the desk always does, and what your playbook decides', '', h('div', { className: 'stoa-cols', style: { gap: 22 } },
      h('div', null, h('div', { className: 'stoa-name', style: { marginBottom: 6 } }, 'Always, whatever you sell'), ALWAYS.map(line => h('p', { key: line, className: 'stoa-tip' }, line))),
      h('div', null, h('div', { className: 'stoa-name', style: { marginBottom: 6 } }, 'Decided by your playbook'),
        [['What a record is called', sets.words], ['The stages', sets.stages.join(' → ')], ['How long before a nudge is due', 'per stage, ' + Math.min(...sets.quiet.map(item => item.days)) + ' to ' + Math.max(...sets.quiet.map(item => item.days)) + ' days'],
          ['Why records are lost', sets.lost_reasons.length + ' reasons to pick from'], ['The extra facts worth keeping', sets.fields.length ? sets.fields.join(', ') : 'none'],
          ['What Hermes writes at each stage', sets.drafts.length ? plural(sets.drafts.length, 'stage has', 'stages have') + ' a draft' : 'nothing yet']].map(pair =>
          h('p', { key: pair[0], className: 'stoa-tip' }, h('b', null, pair[0] + ': '), pair[1])))), true),
    h('div', { className: 'stoa-sub stoa-setbox', 'data-wide': '1', style: { textAlign: 'center' } }, 'Stoa ' + setup.version))
}

// -- what Hermes proposes: one decision each, with what it rests on -------------------------------
const EVIDENCE = { email: 'Email', call: 'Call', slack: 'Slack', meeting: 'Meeting', note: 'Note', calendar: 'Calendar' }

async function decide(item, verb) {
  try {
    const result = await rest('/intake/' + encodeURIComponent(item.intake) + '/row', { method: 'POST', body: { n: item.n, verb } })
    refresh()
    if (result.last) offerUndo(result.last)
    else if (result.note) host.notify({ kind: 'info', message: result.note })
    return result
  } catch (error) {
    host.notifyError(error, 'Stoa could not do that')
    return null
  }
}

function Proposal({ item, busy, run, open, follow }) {
  const what = item.kind === 'new' ? 'New' + (item.stage ? ' at ' + item.stage : '') : cap(item.changes.map(changeText).join(' · '))
  const where = [item.company !== item.name ? item.company : '', item.group !== item.company ? item.group : ''].filter(Boolean).join(' · ')
  return h('div', { className: 'stoa-proposal', 'data-follow': follow ? '1' : '0' },
    h('div', { className: 'stoa-prophead' },
      follow ? h('span', { className: 'stoa-sub' }, 'Also for ' + item.name)
        : item.deal ? h('button', { type: 'button', className: 'stoa-proplink stoa-name', title: 'Open the record', onClick: open }, item.name) : h('span', { className: 'stoa-name' }, item.name),
      h('span', { className: 'stoa-sub stoa-clip stoa-grow' }, follow ? '' : where),
      h('span', { className: 'stoa-sub', style: { flex: 'none' } }, item.source + ' · ' + ago(item.at))),
    h('div', { className: 'stoa-propchange' }, what),
    item.case ? h('div', { className: 'stoa-sub', style: { marginTop: 2 } }, item.case) : null,
    (item.evidence || []).map((entry, index) => h('blockquote', { key: index, className: 'stoa-evidence' },
      h('small', { className: 'stoa-eyebrow' }, [EVIDENCE[entry.kind] || cap(entry.kind) || 'Evidence', entry.at, entry.who].filter(Boolean).join(' · ')),
      entry.text ? h('span', null, entry.text) : null)),
    (item.warnings || []).map((warning, index) => h('div', { key: 'w' + index, className: 'stoa-sub' }, warning)),
    h('div', { className: 'stoa-btns', style: { marginTop: 10 } },
      h(Btn, { kind: 'go', disabled: busy, onClick: () => run('approve') }, 'Approve'),
      h(Btn, { kind: 'quiet', disabled: busy, title: 'Leave the desk as it is. Hermes will not propose this again.', onClick: () => run('decline') }, 'Decline')))
}

/** What sort of change a proposal is, by the heaviest thing it would do. */
const KINDS = [['steps', 'next step', 'next steps'], ['people', 'person', 'people'], ['stage', 'stage change', 'stage changes'],
  ['money', 'money entry', 'money entries'], ['new', 'new record', 'new records']]
function kindOf(item) {
  if (item.kind === 'new') return 'new'
  const fields = (item.changes || []).map(change => change.field)
  if (fields.includes('Invoice') || fields.includes('Payment')) return 'money'
  if (fields.includes('Stage')) return 'stage'
  if (fields.includes('Person')) return 'people'
  return 'steps'
}

/** The top of Today: everything waiting on a yes or a no. Nothing here has been written. */
function Approvals({ state, setOpenId }) {
  const items = (state.today && state.today.approvals) || []
  const [busy, setBusy] = useState('')
  if (!items.length) return null
  const key = item => item.intake + ':' + item.n
  // Suggestions about one record sit together, in the order the first of them arrived.
  const order = {}
  items.forEach((item, index) => { const about = item.list ? 'list:' + item.intake : item.deal || key(item); if (!(about in order)) order[about] = index })
  const sorted = items.map((item, index) => ({ item, index, at: order[item.list ? 'list:' + item.intake : item.deal || key(item)] }))
    .sort((a, b) => a.at - b.at || a.index - b.index).map(entry => entry.item)
  const proposals = sorted.filter(item => !item.list)
  const run = (item, verb) => { setBusy(key(item)); return decide(item, verb).then(() => setBusy('')) }
  const many = async (which, label) => {
    setBusy(label)
    try {
      const result = await rest('/approvals', { method: 'POST', body: { verb: 'approve', items: which.map(item => ({ intake: item.intake, n: item.n })) } })
      refresh()
      host.notify({ kind: 'success', message: plural(result.applied, 'change', 'changes') + ' made.' + ((result.notes || []).length ? ' ' + result.notes[0] : '') })
    } catch (error) {
      host.notifyError(error, 'Stoa could not do that')
    }
    setBusy('')
  }
  const kinds = KINDS.map(kind => ({ id: kind[0], one: kind[1], several: kind[2], items: proposals.filter(item => kindOf(item) === kind[0]) })).filter(kind => kind.items.length > 1)
  return h('div', { className: 'stoa-tsec stoa-approvals' },
    h('div', { className: 'stoa-sechead', style: { padding: '0 0 6px', alignItems: 'center' } },
      h('span', { className: 'stoa-eyebrow', title: 'Changes Hermes suggests and lists you were sent. Nothing is written until you approve it.' }, 'Waiting for your approval'),
      h('span', { style: { display: 'flex', gap: 10, alignItems: 'center' } },
        proposals.length > 1 ? h(Btn, { kind: 'quiet', disabled: Boolean(busy), onClick: () => many(proposals, 'all') }, 'Approve all ' + proposals.length) : null,
        h('span', { className: 'stoa-num' }, String(items.length)))),
    kinds.length && kinds[0].items.length < proposals.length ? h('div', { className: 'stoa-kinds' }, kinds.map(kind =>
      h(Btn, { key: kind.id, disabled: Boolean(busy), title: 'Approve every waiting ' + kind.one + ' at once. Each can still be taken back by itself.',
        onClick: () => many(kind.items, kind.id) }, 'Approve ' + kind.items.length + ' ' + kind.several))) : null,
    sorted.map((item, index) => item.list
      ? h('div', { key: item.intake, style: { marginBottom: 10 } }, h(Review, { id: item.intake }))
      : h(Proposal, { key: key(item), item, follow: index > 0 && !sorted[index - 1].list && Boolean(item.deal) && sorted[index - 1].deal === item.deal,
        busy: busy === 'all' || busy === kindOf(item) || busy === key(item), run: verb => run(item, verb), open: () => setOpenId(item.deal) })))
}

// -- taking a change back ----------------------------------------------------------------------------
function UndoBar() {
  const last = useValue($last)
  if (!last) return null
  return h('div', { className: 'stoa-undobar', role: 'status' },
    h('span', { className: 'stoa-clip' }, h('b', null, last.name), '  ' + last.text),
    h(Btn, { onClick: () => undo(last.deal, last.op) }, 'Undo'),
    h('button', { type: 'button', className: 'stoa-icon', style: { width: 24, height: 24 }, 'aria-label': 'Dismiss', onClick: () => $last.set(null) }, h(Icon, { name: 'x', size: 12 })))
}

// -- the other people on a record ---------------------------------------------------------------------
function People({ deal, term }) {
  const blank = { name: '', email: '', title: '', role: '' }
  const [adding, setAdding] = useState(false)
  const [v, setV] = useState(blank)
  const [busy, setBusy] = useState(false)
  const set = key => event => setV({ ...v, [key]: event.target.value })
  const send = body => {
    setBusy(true)
    return rest('/deal/' + encodeURIComponent(deal.id) + '/person', { method: 'POST', body })
      .then(result => { refresh(); if (result.last) offerUndo(result.last); return result })
      .catch(error => { host.notifyError(error, 'Stoa could not save that'); return null })
      .then(result => { setBusy(false); return result })
  }
  const add = () => {
    if (!v.name.trim() || busy) return
    send({ verb: 'add', ...v }).then(result => {
      if (!result) return
      if (result.exists) host.notify({ kind: 'info', message: v.name.trim() + ' is already on this record.' })
      setV(blank)
      setAdding(false)
    })
  }
  const enter = event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) add() }
  const people = deal.people || []
  return h('div', { className: 'stoa-log' },
    h('div', { className: 'stoa-sechead', style: { padding: 0, alignItems: 'center' } },
      h('small', { className: 'stoa-eyebrow', title: 'Email, calls and messages from these people are filed on this record too' }, 'Also on this ' + term),
      h(Btn, { kind: 'quiet', onClick: () => setAdding(!adding) }, adding ? 'Close' : '+  Person')),
    people.map(person => h('div', { key: person.id, className: 'stoa-person' },
      h('span', { className: 'stoa-grow' },
        h('span', { className: 'stoa-name' }, person.name),
        person.role || person.title ? h('span', { className: 'stoa-sub' }, '  ' + [person.role, person.title].filter(Boolean).join(' · ')) : null,
        person.email || person.also_on ? h('span', { className: 'stoa-sub', style: { display: 'block', overflowWrap: 'anywhere' } },
          [person.email, person.also_on ? 'also on ' + plural(person.also_on, 'other record', 'other records') : ''].filter(Boolean).join(' · ')) : null),
      h('button', { type: 'button', className: 'stoa-icon', style: { width: 24, height: 24 }, disabled: busy, title: 'Take ' + person.name + ' off this record',
        'aria-label': 'Remove ' + person.name, onClick: () => send({ verb: 'remove', person: String(person.id) }) }, h(Icon, { name: 'x', size: 12 })))),
    !people.length && !adding ? h('div', { className: 'stoa-sub' }, 'Nobody else yet. Add the people around it, and what they send is filed here too.') : null,
    adding ? h('div', null,
      h('div', { className: 'stoa-form', style: { marginTop: 8 } },
        h(Field, { label: 'Name', autoFocus: true, value: v.name, onChange: set('name'), onKeyDown: enter }),
        h(Field, { label: 'Role here', value: v.role, placeholder: 'Decision maker, finance…', onChange: set('role'), onKeyDown: enter }),
        h(Field, { label: 'Title', value: v.title, onChange: set('title'), onKeyDown: enter }),
        h(Field, { label: 'Email', type: 'email', value: v.email, onChange: set('email'), onKeyDown: enter })),
      h('div', { className: 'stoa-btns', style: { marginTop: 8 } }, h(Btn, { kind: 'primary', disabled: busy || !v.name.trim(), onClick: add }, 'Add'))) : null)
}

// -- a company: its records, everyone known there, and the notes kept on it ------------------------
function CompanyPanel({ name, state, close, openRecord }) {
  const query = useQuery({ queryKey: ['stoa', 'company', name], queryFn: () => rest('/company', { method: 'POST', body: { name } }), staleTime: 1500 })
  const [note, setNote] = useState(null) // null until edited
  const [domain, setDomain] = useState(null)
  const [busy, setBusy] = useState(false)
  const data = query.data
  if (query.error) return h('div', { className: 'stoa-box stoa-sub' }, 'Nothing on the desk is at that company any more.', h('div', { className: 'stoa-btns' }, h(Btn, { onClick: close }, 'Close')))
  if (!data) return h('div', { className: 'stoa-box stoa-sub' }, 'Loading…')
  const pb = state.playbook, company = data.company
  const changed = (note !== null && note !== company.note) || (domain !== null && domain.trim().toLowerCase() !== company.domain)
  const save = () => {
    setBusy(true)
    rest('/company', { method: 'POST', body: { name: company.name, note: note === null ? company.note : note, domain: domain === null ? company.domain : domain } })
      .then(() => { refresh(); setNote(null); setDomain(null); host.notify({ kind: 'success', message: 'Saved on ' + company.name + '.' }) })
      .catch(error => host.notifyError(error, 'Stoa could not save that'))
      .then(() => setBusy(false))
  }
  const stats = [['Open', data.open ? data.open + (data.open_value ? ' · ' + money(data.open_value, pb.currency) : '') : '0'],
    [pb.won.label, data.won ? data.won + (data.won_value ? ' · ' + money(data.won_value, pb.currency) : '') : '0'],
    [pb.lost.label, String(data.lost)], ['Last touch', ago(data.last_touch) || '—']]
  return h('div', { className: 'stoa-box' },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'flex-start' } },
      h('div', { className: 'stoa-grow' },
        h('div', { className: 'stoa-eyebrow' }, pb.terms.company),
        h('div', { className: 'stoa-name', style: { fontSize: 15, marginTop: 2 } }, company.name),
        h('div', { className: 'stoa-sub' }, company.domain || 'no email domain known yet')),
      h(Btn, { kind: 'quiet', onClick: close, 'aria-label': 'Close' }, '×')),
    h('div', { className: 'stoa-facts' }, stats.map(pair => h('div', { key: pair[0] }, h('small', { className: 'stoa-eyebrow' }, pair[0]), h('span', null, pair[1])))),
    h('div', { className: 'stoa-log' },
      h('small', { className: 'stoa-eyebrow' }, cap(pb.terms.deals) + ' here'),
      data.records.map(record => h('button', { type: 'button', key: record.id, className: 'stoa-row', style: { padding: '5px 6px', margin: '0 -6px', width: 'calc(100% + 12px)', opacity: record.status === 'open' ? 1 : 0.6 },
        onClick: () => openRecord(record.id) },
      h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name stoa-clip', style: { display: 'block' } }, record.name),
        h('span', { className: 'stoa-sub stoa-clip', style: { display: 'block' } }, record.stage_label + (record.next_step && record.status === 'open' ? ' · ' + record.next_step : ''))),
      record.value ? h('span', { className: 'stoa-num' }, money(record.value, pb.currency)) : null))),
    h('div', { className: 'stoa-log' },
      h('small', { className: 'stoa-eyebrow' }, 'People'),
      data.people.map((person, index) => h('div', { key: index, className: 'stoa-person' }, h('span', { className: 'stoa-grow' },
        h('span', { className: 'stoa-name' }, person.name),
        person.role || person.title ? h('span', { className: 'stoa-sub' }, '  ' + [person.role, person.title].filter(Boolean).join(' · ')) : null,
        h('span', { className: 'stoa-sub', style: { display: 'block', overflowWrap: 'anywhere' } },
          [person.email, person.primary ? '' : 'on ' + person.record_name].filter(Boolean).join(' · ')))))),
    h('div', { className: 'stoa-log' },
      h('small', { className: 'stoa-eyebrow' }, 'Notes on the company'),
      h('textarea', { className: 'stoa-field stoa-notes', rows: 3, value: note === null ? company.note : note, placeholder: 'What is worth remembering about them: how they buy, who decides, when their year ends…',
        onChange: event => setNote(event.target.value) }),
      h('div', { className: 'stoa-form', style: { marginTop: 8 } },
        h(Field, { label: 'Email domain', wide: true, value: domain === null ? company.domain : domain, placeholder: 'kestrel.com',
          title: 'Mail from anyone at this domain is recognised as coming from this company', onChange: event => setDomain(event.target.value) })),
      changed ? h('div', { className: 'stoa-btns', style: { marginTop: 8 } }, h(Btn, { kind: 'primary', disabled: busy, onClick: save }, 'Save'),
        h(Btn, { kind: 'quiet', disabled: busy, onClick: () => { setNote(null); setDomain(null) } }, 'Cancel')) : null),
    data.activity.length ? h('div', { className: 'stoa-log' }, h('small', { className: 'stoa-eyebrow', style: { display: 'block', marginBottom: 4 } }, 'Lately'),
      data.activity.slice(0, 5).map((entry, index) => h('p', { key: index }, h('b', { style: { fontWeight: 550 } }, entry.name), '  ' + entry.text,
        h('span', { className: 'stoa-sub' }, '  ' + ago(entry.at))))) : null)
}

/** Every company on the desk, biggest open value first. Press one for the whole picture. */
function Companies({ state, current, openCompany }) {
  const filter = useValue($filter)
  const pb = state.playbook
  const text = filter.text.toLowerCase()
  const rows = (state.companies || []).filter(row => !text || row.name.toLowerCase().includes(text))
  if (!rows.length) return h('p', { className: 'stoa-sub' }, text ? 'No company matches “' + filter.text + '”.' : 'No company is named on any record yet.')
  return h('div', { className: 'stoa-companies' },
    h('div', { className: 'stoa-crow stoa-chead' }, ['Company', 'Open', pb.won.label, 'People', 'Last touch'].map(label => h('span', { key: label, className: 'stoa-eyebrow' }, label))),
    rows.map(row => h('button', { type: 'button', key: row.key, className: 'stoa-crow', 'data-open': current && current.toLowerCase() === row.name.toLowerCase() ? '1' : '0', onClick: () => openCompany(row.name) },
      h('span', { className: 'stoa-name stoa-clip' }, row.name),
      h('span', { className: 'stoa-num' }, row.open ? row.open + (row.open_value ? ' · ' + money(row.open_value, pb.currency) : '') : '—'),
      h('span', { className: 'stoa-num' }, row.won ? row.won + (row.won_value ? ' · ' + money(row.won_value, pb.currency) : '') : '—'),
      h('span', { className: 'stoa-num' }, String(row.people)),
      h('span', { className: 'stoa-num' }, ago(row.last_touch) || '—'))))
}

// -- a group: a search, an account, a client. Who it is for, where it stands, its terms, who is on their side ----
const GROUP_STATUS = [['open', 'Open'], ['hold', 'On hold'], ['done', 'Done'], ['closed', 'Closed']]

function GroupPanel({ name, state, close, openRecord, onRenamed }) {
  const query = useQuery({ queryKey: ['stoa', 'group', name], queryFn: () => rest('/group', { method: 'POST', body: { name } }), staleTime: 1500 })
  const blank = { name: '', email: '', title: '', role: '' }
  const [draft, setDraft] = useState(null) // null until edited
  const [busy, setBusy] = useState(false)
  const [adding, setAdding] = useState(false)
  const [v, setV] = useState(blank)
  const data = query.data
  if (query.error) return h('div', { className: 'stoa-box stoa-sub' }, 'That one is no longer on the desk.', h('div', { className: 'stoa-btns' }, h(Btn, { onClick: close }, 'Close')))
  if (!data) return h('div', { className: 'stoa-box stoa-sub' }, 'Loading…')
  const pb = state.playbook, group = data.group, term = pb.terms.group
  const was = { title: group.name, client: group.client, status: group.status, value: group.value ? String(group.value) : '', note: group.note, fields: {} }
  const now = draft || was
  const set = patch => setDraft({ ...now, ...patch })
  const fact = item => (item.key in now.fields ? now.fields[item.key] : item.value)
  const send = (path, body, said) => {
    setBusy(true)
    return rest(path, { method: 'POST', body })
      .then(result => { refresh(); if (said) host.notify({ kind: 'success', message: said }); return result })
      .catch(error => { host.notifyError(error, 'Stoa could not save that'); return null })
      .then(result => { setBusy(false); return result })
  }
  const save = async () => {
    const body = { name: group.name, client: now.client, status: now.status, note: now.note, fields: now.fields }
    if (now.value.trim() !== was.value) body.value = now.value.trim() === '' ? 0 : now.value.trim()
    const saved = await send('/group', body, 'Saved on ' + group.name + '.')
    if (!saved) return
    const title = now.title.trim()
    setDraft(null)
    if (title && title !== group.name) {
      const moved = await send('/group', { name: group.name, to: title }, 'Renamed to ' + title + '.')
      if (moved && onRenamed) onRenamed(moved.group.name)
    }
  }
  const addPerson = () => {
    if (!v.name.trim() || busy) return
    send('/group/person', { verb: 'add', group: group.name, ...v }).then(result => {
      if (!result) return
      if (result.exists) host.notify({ kind: 'info', message: v.name.trim() + ' is already here.' })
      setV(blank)
      setAdding(false)
    })
  }
  const enter = event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) addPerson() }
  const field = key => event => setV({ ...v, [key]: event.target.value })
  const exportIt = share => send('/export', { group: group.name, share }).then(file => {
    if (!file) return
    download(file.filename, file.csv, 'text/csv')
    host.notify({ kind: 'success', message: plural(file.rows, pb.terms.deal, pb.terms.deals) + ' exported: only ' + group.name + (share ? ', without your own columns.' : '.') })
  })
  const stats = [['Open', data.open ? data.open + (data.open_value ? ' · ' + money(data.open_value, pb.currency) : '') : '0'],
    [pb.won.label, data.won ? data.won + (data.won_value ? ' · ' + money(data.won_value, pb.currency) : '') : '0'],
    [pb.lost.label, String(data.lost)], ['Last touch', ago(data.last_touch) || '—']]
  return h('div', { className: 'stoa-box' },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'flex-start' } },
      h('div', { className: 'stoa-grow' },
        h('div', { className: 'stoa-eyebrow' }, cap(term)),
        h('div', { className: 'stoa-name', style: { fontSize: 15, marginTop: 2 } }, group.name),
        h('div', { className: 'stoa-sub' }, [group.client ? 'for ' + group.client : 'no client named yet', group.status !== 'open' ? group.status_label.toLowerCase() : ''].filter(Boolean).join(' · '))),
      h(Btn, { kind: 'quiet', onClick: close, 'aria-label': 'Close' }, '×')),
    h('div', { className: 'stoa-facts' }, stats.map(pair => h('div', { key: pair[0] }, h('small', { className: 'stoa-eyebrow' }, pair[0]), h('span', null, pair[1])))),
    h('div', { className: 'stoa-log' },
      h('div', { className: 'stoa-sechead', style: { padding: 0, alignItems: 'center' } },
        h('small', { className: 'stoa-eyebrow' }, cap(pb.terms.deals) + ' in it'),
        data.records.length ? h('span', { style: { display: 'flex', gap: 2 } },
          h(Btn, { kind: 'quiet', disabled: busy, title: 'A CSV of the ' + pb.terms.deals + ' in this ' + term + ' and nothing else, with every column',
            onClick: () => exportIt(false) }, 'Export'),
          h(Btn, { kind: 'quiet', disabled: busy, title: 'The copy to hand the client: no value, owner, next steps, dates or sources',
            onClick: () => exportIt(true) }, 'Copy to share')) : null),
      data.records.length ? data.records.map(record => h('button', { type: 'button', key: record.id, className: 'stoa-row', style: { padding: '5px 6px', margin: '0 -6px', width: 'calc(100% + 12px)', opacity: record.status === 'open' ? 1 : 0.6 },
        onClick: () => openRecord(record.id) },
      h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name stoa-clip', style: { display: 'block' } }, record.name),
        h('span', { className: 'stoa-sub stoa-clip', style: { display: 'block' } }, record.stage_label + (record.next_step && record.status === 'open' ? ' · ' + record.next_step : ''))),
      record.value ? h('span', { className: 'stoa-num' }, money(record.value, pb.currency)) : null))
        : h('div', { className: 'stoa-sub' }, 'None yet. Add a ' + pb.terms.deal + ' and name this ' + term + ' on it.')),
    h('div', { className: 'stoa-log' },
      h('div', { className: 'stoa-sechead', style: { padding: 0, alignItems: 'center' } },
        h('small', { className: 'stoa-eyebrow', title: 'The people on the client\'s side: mail from them is recognised as about this ' + term }, 'On their side'),
        h(Btn, { kind: 'quiet', onClick: () => setAdding(!adding) }, adding ? 'Close' : '+  Person')),
      data.people.map(person => h('div', { key: person.on + person.id, className: 'stoa-person' },
        h('span', { className: 'stoa-grow' },
          h('span', { className: 'stoa-name' }, person.name),
          person.role || person.title ? h('span', { className: 'stoa-sub' }, '  ' + [person.role, person.title].filter(Boolean).join(' · ')) : null,
          person.email || person.on === 'record' ? h('span', { className: 'stoa-sub', style: { display: 'block', overflowWrap: 'anywhere' } },
            [person.email, person.on === 'record' ? 'on ' + person.record_name : ''].filter(Boolean).join(' · ')) : null),
        person.on === 'group' ? h('button', { type: 'button', className: 'stoa-icon', style: { width: 24, height: 24 }, disabled: busy, title: 'Take ' + person.name + ' off this ' + term,
          'aria-label': 'Remove ' + person.name, onClick: () => send('/group/person', { verb: 'remove', group: group.name, person: String(person.id) }) }, h(Icon, { name: 'x', size: 12 })) : null)),
      !data.people.length && !adding ? h('div', { className: 'stoa-sub' }, 'Nobody yet. Add the people you deal with there, and what they send is recognised.') : null,
      adding ? h('div', null,
        h('div', { className: 'stoa-form', style: { marginTop: 8 } },
          h(Field, { label: 'Name', autoFocus: true, value: v.name, onChange: field('name'), onKeyDown: enter }),
          h(Field, { label: 'Role here', value: v.role, placeholder: 'Hiring manager, sponsor…', onChange: field('role'), onKeyDown: enter }),
          h(Field, { label: 'Title', value: v.title, onChange: field('title'), onKeyDown: enter }),
          h(Field, { label: 'Email', type: 'email', value: v.email, onChange: field('email'), onKeyDown: enter })),
        h('div', { className: 'stoa-btns', style: { marginTop: 8 } }, h(Btn, { kind: 'primary', disabled: busy || !v.name.trim(), onClick: addPerson }, 'Add'))) : null),
    h('div', { className: 'stoa-log' },
      h('small', { className: 'stoa-eyebrow' }, 'About this ' + term),
      h('div', { className: 'stoa-form', style: { marginTop: 8 } },
        h(Field, { label: 'Name', wide: true, value: now.title, onChange: event => set({ title: event.target.value }) }),
        h(Field, { label: 'Client', value: now.client, placeholder: 'The company it is for', title: 'The company that pays: not always the company on a ' + pb.terms.deal,
          onChange: event => set({ client: event.target.value }) }),
        h(Field, { label: 'Status' },
          h('select', { className: 'stoa-select', style: { width: '100%' }, value: now.status, onChange: event => set({ status: event.target.value }) },
            GROUP_STATUS.map(pair => h('option', { key: pair[0], value: pair[0] }, pair[1])))),
        h(Field, { label: pb.terms.value + ' for each ' + pb.terms.deal, value: now.value, placeholder: 'Left blank: each says its own',
          title: 'A new ' + pb.terms.deal + ' here takes this unless it brings its own', onChange: event => set({ value: event.target.value }) }),
        (group.facts || []).map(item => h(Field, { key: item.key, label: item.label, value: fact(item), onChange: event => set({ fields: { ...now.fields, [item.key]: event.target.value } }) }))),
      h('textarea', { className: 'stoa-field stoa-notes', rows: 3, style: { marginTop: 8 }, value: now.note, placeholder: 'What is worth remembering: how they decide, what was agreed, what to avoid…',
        onChange: event => set({ note: event.target.value }) }),
      draft ? h('div', { className: 'stoa-btns', style: { marginTop: 8 } }, h(Btn, { kind: 'primary', disabled: busy || !now.title.trim(), onClick: save }, 'Save'),
        h(Btn, { kind: 'quiet', disabled: busy, onClick: () => setDraft(null) }, 'Cancel')) : null),
    data.activity.length ? h('div', { className: 'stoa-log' }, h('small', { className: 'stoa-eyebrow', style: { display: 'block', marginBottom: 4 } }, 'Lately'),
      data.activity.slice(0, 5).map((entry, index) => h('p', { key: index }, h('b', { style: { fontWeight: 550 } }, entry.name), '  ' + entry.text,
        h('span', { className: 'stoa-sub' }, '  ' + ago(entry.at))))) : null)
}

/** Every group on the desk: who it is for, where it stands, what is open in it. Open ones first. */
function Groups({ state, current, openGroup }) {
  const filter = useValue($filter)
  const pb = state.playbook, term = pb.terms.group
  const [name, setName] = useState('')
  const [client, setClient] = useState('')
  const [busy, setBusy] = useState(false)
  const text = filter.text.toLowerCase()
  const rows = (state.groups || []).filter(row => !text || (row.name + ' ' + row.client).toLowerCase().includes(text))
  const open = () => {
    if (!name.trim() || busy) return
    setBusy(true)
    rest('/group', { method: 'POST', body: { name: name.trim(), new: true, client: client.trim() } })
      .then(result => { refresh(); setName(''); setClient(''); openGroup(result.group.name) })
      .catch(error => host.notifyError(error, 'Stoa could not open that'))
      .then(() => setBusy(false))
  }
  const enter = event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) open() }
  return h('div', { className: 'stoa-companies' },
    h('div', { className: 'stoa-newgroup' },
      h(Field, { label: 'New ' + term, value: name, placeholder: 'Its name', onChange: event => setName(event.target.value), onKeyDown: enter }),
      h(Field, { label: 'Client', value: client, placeholder: 'The company it is for', onChange: event => setClient(event.target.value), onKeyDown: enter }),
      h(Btn, { disabled: busy || !name.trim(), onClick: open }, 'Open it')),
    !rows.length ? h('p', { className: 'stoa-sub' }, text ? 'No ' + term + ' matches “' + filter.text + '”.' : 'No ' + term + ' yet. Open one above, or name one on a ' + pb.terms.deal + '.') : null,
    rows.length ? h('div', { className: 'stoa-crow stoa-chead', 'data-cols': '7' }, [cap(term), 'Client', 'Status', 'Open', pb.won.label, 'People', 'Last touch'].map(label =>
      h('span', { key: label, className: 'stoa-eyebrow' }, label))) : null,
    rows.map(row => h('button', { type: 'button', key: row.key, className: 'stoa-crow', 'data-cols': '7', 'data-dim': row.status === 'open' ? '0' : '1',
      'data-open': current && current.toLowerCase() === row.name.toLowerCase() ? '1' : '0', onClick: () => openGroup(row.name) },
    h('span', { className: 'stoa-name stoa-clip' }, row.name),
    h('span', { className: 'stoa-clip' }, row.client || '—'),
    h('span', { className: 'stoa-num' }, row.status_label),
    h('span', { className: 'stoa-num' }, row.open ? row.open + (row.open_value ? ' · ' + money(row.open_value, pb.currency) : '') : '—'),
    h('span', { className: 'stoa-num' }, row.won ? row.won + (row.won_value ? ' · ' + money(row.won_value, pb.currency) : '') : '—'),
    h('span', { className: 'stoa-num' }, String(row.people)),
    h('span', { className: 'stoa-num' }, ago(row.last_touch) || '—'))))
}

// -- how much a scheduled job may change by itself ----------------------------------------------------
function Autonomy({ setup }) {
  const [busy, setBusy] = useState(false)
  const auto = setup.autonomy
  const pick = level => {
    if (level === auto.level || busy) return
    setBusy(true)
    rest('/autonomy', { method: 'POST', body: { level } })
      .then(result => { refresh(); host.notify({ kind: 'success', message: 'Scheduled jobs: ' + result.label + '.' }) })
      .catch(error => host.notifyError(error, 'Stoa could not change that'))
      .then(() => setBusy(false))
  }
  const forget = () => {
    setBusy(true)
    rest('/declined/forget', { method: 'POST', body: {} })
      .then(result => { refresh(); host.notify({ kind: 'info', message: plural(result.forgotten, 'declined proposal', 'declined proposals') + ' forgotten.' }) })
      .catch(error => host.notifyError(error, 'Stoa could not do that'))
      .then(() => setBusy(false))
  }
  return h('div', null,
    h('div', { className: 'stoa-sub', style: { marginBottom: 8 } }, 'In a chat, Hermes does what you ask. This is for the jobs that run when you are not there. Only you can change it here: Hermes has no way to.'),
    auto.levels.map(level => h('button', { type: 'button', key: level.id, className: 'stoa-level', 'data-on': level.id === auto.level ? '1' : '0', disabled: busy,
      'aria-pressed': level.id === auto.level, onClick: () => pick(level.id) },
    h(Lamp, { on: level.id === auto.level }),
    h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name', style: { display: 'block' } }, level.label), h('span', { className: 'stoa-sub' }, level.summary)))),
    h('div', { className: 'stoa-edit', style: { paddingTop: 10 } },
      h('div', { className: 'stoa-sub' }, setup.declined ? plural(setup.declined, 'proposal you declined is', 'proposals you declined are') + ' remembered for 90 days and not made again.'
        : 'Anything it may not do is held on Today for your yes or no. A no is remembered for 90 days and not asked again.'),
      setup.declined ? h('div', { className: 'stoa-btns', style: { marginTop: 6 } }, h(Btn, { disabled: busy, onClick: forget }, 'Forget them')) : null))
}

// -- money: invoices on a record, where won work stands, and what is likely to come in ---------------
// Stoa sends no invoices and moves no money. It keeps the account of them, so the desk can say what
// is owed, what is late, and what was won but never billed.
const $billing = atom('') // the record whose "new invoice" form should be open when its panel appears
const BILL_TONE = { overdue: 'bad', partial: 'warn', paid: 'good' }
const BILL_WORD = { overdue: 'overdue', partial: 'part paid', sent: 'sent', draft: 'draft', paid: 'paid', void: 'void' }

/** An amount in full: an invoice is not rounded to the nearest thousand. */
function exact(value, currency) {
  const number = Number(value || 0)
  const text = number.toLocaleString(undefined, { minimumFractionDigits: Number.isInteger(number) ? 0 : 2, maximumFractionDigits: 2 })
  const sign = SYMBOL[currency] ?? ''
  return sign ? sign + text : text + ' ' + (currency || '')
}

const inDays = days => { const d = new Date(Date.now() + days * 864e5); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0') }

async function bill(dealId, body) {
  try {
    const result = await rest('/deal/' + encodeURIComponent(dealId) + '/invoice', { method: 'POST', body })
    refresh()
    if (result.last) offerUndo(result.last)
    return result
  } catch (error) {
    host.notifyError(error, 'Stoa could not save that')
    return null
  }
}

/** One invoice, with the moves that fit where it stands. */
function Invoice({ invoice, dealId, currency, name, onOpen }) {
  const [busy, setBusy] = useState(false)
  const [part, setPart] = useState(null) // null, or the amount being typed for a part payment
  const run = body => { setBusy(true); return bill(dealId, { invoice: String(invoice.id), ...body }).then(result => { setBusy(false); return result }) }
  const owing = invoice.state === 'sent' || invoice.state === 'partial' || invoice.state === 'overdue'
  const takePart = () => { const amount = Number(part); if (amount > 0) run({ verb: 'pay', amount }).then(result => { if (result) setPart(null) }) }
  const when = invoice.state === 'paid' ? (invoice.paid_on ? 'paid ' + shortDate(invoice.paid_on) : 'paid')
    : invoice.state === 'overdue' ? invoice.days_overdue + 'd late'
      : invoice.due_on && owing ? 'due ' + shortDate(invoice.due_on) : ''
  return h('div', { className: 'stoa-irow', 'data-state': invoice.state },
    h('span', { className: 'stoa-grow', style: { minWidth: 0 } },
      name ? h('button', { type: 'button', className: 'stoa-proplink stoa-name stoa-clip', style: { display: 'block', maxWidth: '100%' }, title: 'Open the record', onClick: onOpen }, name) : null,
      h('span', { className: name ? 'stoa-sub' : 'stoa-name' }, (invoice.number ? '#' + invoice.number + ' · ' : '') + exact(invoice.amount, currency)),
      invoice.state === 'partial' || (invoice.state === 'overdue' && invoice.paid) ? h('span', { className: 'stoa-sub' }, ' · ' + exact(invoice.owed, currency) + ' still owed') : null,
      invoice.note ? h('span', { className: 'stoa-sub', style: { display: 'block' } }, invoice.note) : null),
    when ? h('span', { className: 'stoa-num', 'data-tone': BILL_TONE[invoice.state] || '' }, when) : null,
    h(Chip, { tone: BILL_TONE[invoice.state] || '' }, BILL_WORD[invoice.state] || invoice.state),
    h('span', { className: 'stoa-tacts' },
      part !== null ? [
        h('input', { key: 'amount', className: 'stoa-field', type: 'number', min: 0, step: 'any', autoFocus: true, value: part, placeholder: 'Amount received', 'aria-label': 'Amount received',
          style: { width: 124, marginTop: 0 }, onChange: event => setPart(event.target.value),
          onKeyDown: event => { if (event.key === 'Enter') takePart(); if (event.key === 'Escape') setPart(null) } }),
        h(Btn, { key: 'ok', kind: 'go', disabled: busy || !(Number(part) > 0), onClick: takePart }, 'Record'),
        h(Btn, { key: 'no', kind: 'quiet', onClick: () => setPart(null), 'aria-label': 'Cancel' }, '×')
      ] : [
        owing ? h(Btn, { key: 'paid', kind: 'go', disabled: busy, title: 'Everything still owed came in', onClick: () => run({ verb: 'pay' }) }, 'Paid') : null,
        owing ? h(Btn, { key: 'part', disabled: busy, title: 'Part of it came in', onClick: () => setPart('') }, 'Part') : null,
        invoice.state === 'draft' ? h(Btn, { key: 'send', kind: 'go', disabled: busy, onClick: () => run({ verb: 'send' }) }, 'Mark sent') : null,
        h('select', { key: 'more', className: 'stoa-select', value: '', disabled: busy, 'aria-label': 'More', style: { maxWidth: 74 },
          onChange: event => { const verb = event.target.value; if (verb) run({ verb }) } },
        [h('option', { key: '', value: '' }, 'More…')].concat(invoice.state !== 'void' && invoice.state !== 'paid' ? [h('option', { key: 'void', value: 'void' }, 'Void it')] : [],
          [h('option', { key: 'remove', value: 'remove' }, 'Remove (entered by mistake)')]))
      ]))
}

/** The money on one record: what was billed, what came in, and a form to add an invoice. */
function Money({ deal, pb }) {
  const billing = useValue($billing)
  const money = deal.money || { invoiced: 0, paid: 0, owed: 0, uninvoiced: 0 }
  const invoices = deal.invoices || []
  const suggested = deal.status === 'won' ? money.uninvoiced : Math.max(0, (deal.value || 0) - money.invoiced)
  const [adding, setAdding] = useState(false)
  const [v, setV] = useState({ amount: '', number: '', due: inDays(30), draft: false })
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (billing !== deal.id) return
    setAdding(true)
    $billing.set('')
  }, [billing, deal.id])
  useEffect(() => { if (adding) setV(now => ({ ...now, amount: now.amount || (suggested ? String(suggested) : '') })) }, [adding])
  const set = key => event => setV({ ...v, [key]: event.target.value })
  const add = () => {
    if (!(Number(v.amount) > 0) || busy) return
    setBusy(true)
    bill(deal.id, { verb: 'add', amount: Number(v.amount), number: v.number.trim(), due: v.due, status: v.draft ? 'draft' : 'sent' }).then(result => {
      setBusy(false)
      if (result) { setAdding(false); setV({ amount: '', number: '', due: inDays(30), draft: false }) }
    })
  }
  if (!invoices.length && deal.status !== 'won' && !adding) {
    return h('div', { className: 'stoa-log' }, h('div', { className: 'stoa-sechead', style: { padding: 0, alignItems: 'center' } },
      h('small', { className: 'stoa-eyebrow' }, 'Money'), h(Btn, { kind: 'quiet', onClick: () => setAdding(true) }, '+  Invoice')))
  }
  const line = [money.invoiced ? 'invoiced ' + exact(money.invoiced, pb.currency) : '', money.paid ? 'paid ' + exact(money.paid, pb.currency) : '',
    money.owed ? 'owed ' + exact(money.owed, pb.currency) : '', deal.status === 'won' && money.uninvoiced ? exact(money.uninvoiced, pb.currency) + ' not invoiced' : '']
    .filter(Boolean).join(' · ')
  return h('div', { className: 'stoa-log' },
    h('div', { className: 'stoa-sechead', style: { padding: 0, alignItems: 'center' } },
      h('small', { className: 'stoa-eyebrow' }, 'Money'), h(Btn, { kind: 'quiet', onClick: () => setAdding(!adding) }, adding ? 'Close' : '+  Invoice')),
    line ? h('div', { className: 'stoa-sub', style: { marginBottom: 2 } }, cap(line)) : null,
    deal.value ? h(BillBar, { value: deal.value, invoiced: money.invoiced, paid: money.paid }) : null,
    invoices.map(invoice => h(Invoice, { key: invoice.id, invoice, dealId: deal.id, currency: pb.currency })),
    adding ? h('div', null,
      h('div', { className: 'stoa-form', style: { marginTop: 8 } },
        h(Field, { label: 'Amount', type: 'number', min: 0, step: 'any', autoFocus: true, value: v.amount, onChange: set('amount') }),
        h(Field, { label: 'Number', value: v.number, placeholder: '1042', onChange: set('number') }),
        h(Field, { label: 'Due', type: 'date', value: v.due, onChange: set('due') }),
        h('label', { style: { alignSelf: 'end', display: 'flex', gap: 6, alignItems: 'center', height: 28, fontSize: 12 } },
          h('input', { type: 'checkbox', checked: v.draft, onChange: event => setV({ ...v, draft: event.target.checked }) }), 'Not sent yet')),
      h('div', { className: 'stoa-btns', style: { marginTop: 8 } },
        h(Btn, { kind: 'primary', disabled: busy || !(Number(v.amount) > 0), onClick: add }, 'Add invoice'),
        h('span', { className: 'stoa-sub', style: { alignSelf: 'center' } }, 'Stoa keeps the record. It does not send the invoice.'))) : null)
}

/** How much of a record's value has been billed, and how much of that has come in. */
function BillBar({ value, invoiced, paid }) {
  const of = Math.max(value || 0, invoiced || 0, 1)
  return h('div', { className: 'stoa-billbar', title: 'Paid, then invoiced, out of the full amount' },
    h('i', { 'data-kind': 'invoiced', style: { width: Math.min(100, (100 * (invoiced || 0)) / of) + '%' } }),
    h('i', { 'data-kind': 'paid', style: { width: Math.min(100, (100 * (paid || 0)) / of) + '%' } }))
}

const BASIS = {
  history: 'Each stage counts for how often records there have gone on to close on this desk.',
  playbook: 'Each stage counts for the odds your playbook gives it. Your own history takes over once twelve have closed.',
  even: 'Each stage counts for an even step toward closing. Your own history takes over once twelve have closed.'
}

/** The Cash tab: what came in and what is owed, what the open pipeline is likely to bring, and where won work stands. */
function Cash({ state, openId, setOpenId }) {
  const cash = state.cash, pb = state.playbook
  const [over, setOver] = useState('')
  if (!cash) return h('p', { className: 'stoa-sub' }, 'The money side needs the gateway restarted after this update (hermes gateway restart).')
  const strip = cash.strip, forecast = cash.forecast, lane = cash.delivery
  const tiles = [
    [pb.won.label + ' this month', strip.won_month.value, plural(strip.won_month.count, pb.terms.deal, pb.terms.deals), '', 'What was closed as ' + pb.won.label.toLowerCase() + ' since the first of the month.'],
    ['Collected this month', strip.collected_month.value, 'payments recorded', 'good', 'Every payment recorded on an invoice this month.'],
    ['Invoiced, not yet paid', strip.owed.value, plural(strip.owed.count, 'invoice', 'invoices'), '', 'What is still owed on invoices that have been sent.'],
    ['Overdue', strip.overdue.value, plural(strip.overdue.count, 'invoice', 'invoices') + ' past due', strip.overdue.count ? 'bad' : '', 'Owed on invoices whose due date has passed.'],
    [pb.won.label + ', not invoiced', strip.uninvoiced.value, plural(strip.uninvoiced.count, pb.terms.deal, pb.terms.deals), strip.uninvoiced.count ? 'warn' : '', 'Won work whose invoices add up to less than its value.']]
  const most = Math.max(1, ...forecast.buckets.map(bucket => bucket.value))
  const shown = forecast.buckets.filter(bucket => bucket.count)
  const stages = lane.stages.length ? lane.stages : [{ id: '', label: pb.won.label }]
  const owing = cash.invoices.filter(invoice => invoice.state !== 'paid')
  const settled = cash.invoices.filter(invoice => invoice.state === 'paid')
  const drop = (event, stage) => {
    const id = event.dataTransfer.getData(DRAG)
    setOver('')
    if (!id || !stage.id) return
    event.preventDefault()
    const record = lane.records.find(item => item.id === id)
    if (record && record.delivery !== stage.id) act(id, { verb: 'update', delivery: stage.id })
  }
  return h('div', { className: 'stoa-cash' },
    h('div', { className: 'stoa-strip' }, tiles.map(tile => h('div', { key: tile[0], className: 'stoa-tile', 'data-tone': tile[3], title: tile[4] },
      h('span', { className: 'stoa-eyebrow' }, tile[0]), h('b', null, money(tile[1], pb.currency) || '0'), h('span', { className: 'stoa-sub' }, tile[2])))),
    h('div', { className: 'stoa-h2' }, 'Likely to come in'),
    h('div', { className: 'stoa-box' },
      h('div', { style: { display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', gap: '4px 14px' } },
        h('span', { className: 'stoa-name', style: { fontSize: 18 } }, money(forecast.weighted, pb.currency) || '0'),
        h('span', { className: 'stoa-sub' }, 'weighted, of ' + (money(forecast.value, pb.currency) || '0') + ' open')),
      shown.length ? h('div', { style: { marginTop: 10 } }, shown.map(bucket => h('div', { key: bucket.key, className: 'stoa-fc', 'data-key': bucket.key },
        h('span', { className: 'stoa-clip' }, bucket.label),
        h('u', null, h('i', { style: { width: (100 * bucket.value) / most + '%' } }), h('b', { style: { width: (100 * bucket.weighted) / most + '%' } })),
        h('span', { className: 'stoa-num' }, (money(bucket.weighted, pb.currency) || '0') + ' of ' + (money(bucket.value, pb.currency) || '0') + ' · ' + bucket.count))))
        : h('div', { className: 'stoa-sub', style: { marginTop: 6 } }, 'Nothing is open.'),
      h('div', { className: 'stoa-sub', style: { marginTop: 10 } }, BASIS[forecast.basis] + ' A ' + pb.terms.deal + ' goes in a month by its expected close date (Edit on the record).'),
      h('div', { className: 'stoa-chain', style: { margin: '6px 0 0' } }, forecast.odds.map(item => item.label + ' ' + item.odds + '%').join('  ·  '))),
    h('div', { className: 'stoa-h2' }, 'After the win'),
    lane.records.length ? h('div', { className: 'stoa-board', style: { gridAutoColumns: 'minmax(200px, 1fr)' } }, stages.map(stage => {
      const here = lane.records.filter(record => (record.delivery || '') === stage.id)
      return h('div', { key: stage.id || 'won', className: 'stoa-col', 'data-over': over === stage.id && stage.id ? '1' : '0',
        onDragOver: event => { if (dragging(event) && stage.id) { event.preventDefault(); if (over !== stage.id) setOver(stage.id) } },
        onDragLeave: event => { if (!event.currentTarget.contains(event.relatedTarget)) setOver('') },
        onDrop: event => drop(event, stage) },
      h('div', { className: 'stoa-colhead' }, h('span', { className: 'stoa-eyebrow stoa-clip' }, stage.label), h('span', { className: 'stoa-num' }, String(here.length))),
      h('div', { className: 'stoa-weight' }),
      here.length ? here.map(record => h('button', { type: 'button', key: record.id, className: 'stoa-card', 'data-open': openId === record.id ? '1' : '0',
        draggable: Boolean(stage.id), title: stage.id ? 'Drag to another column as the work moves on' : undefined,
        onDragStart: event => { event.dataTransfer.setData(DRAG, record.id); event.dataTransfer.effectAllowed = 'move' }, onDragEnd: () => setOver(''),
        onClick: () => setOpenId(openId === record.id ? '' : record.id) },
      h('h5', null, h('span', { className: 'stoa-clip' }, record.name), record.value ? h('span', { className: 'stoa-num' }, money(record.value, pb.currency)) : null),
      h('div', { className: 'stoa-sub stoa-clip' }, record.company || ' '),
      record.value ? h(BillBar, { value: record.value, invoiced: record.invoiced, paid: record.paid }) : null,
      h('div', { className: 'stoa-chips', style: { marginTop: 6 } },
        record.overdue ? h(Chip, { tone: 'bad' }, 'invoice overdue') : null,
        !record.invoiced && record.value ? h(Chip, { tone: 'warn' }, 'not invoiced')
          : record.uninvoiced ? h(Chip, null, (money(record.uninvoiced, pb.currency) || '') + ' to bill') : null,
        record.value && record.paid >= record.value ? h(Chip, { tone: 'good' }, 'paid') : record.owed && !record.overdue ? h(Chip, null, 'awaiting payment') : null)))
        : h('div', { className: 'stoa-empty' }, over === stage.id && stage.id ? 'Drop it here' : '—'))
    })) : h('p', { className: 'stoa-sub' }, 'Nothing has been ' + pb.won.label.toLowerCase() + ' yet. When something is, it lands here: where the work stands, and whether it has been billed.'),
    h('div', { className: 'stoa-h2' }, 'Invoices'),
    owing.length || settled.length ? h('div', { style: { maxWidth: 980 } },
      owing.map(invoice => h(Invoice, { key: invoice.id, invoice, dealId: invoice.deal, currency: pb.currency, name: invoice.name, onOpen: () => setOpenId(invoice.deal) })),
      settled.length ? h('div', { className: 'stoa-eyebrow', style: { margin: '14px 0 2px' } }, 'Lately paid') : null,
      settled.map(invoice => h(Invoice, { key: invoice.id, invoice, dealId: invoice.deal, currency: pb.currency, name: invoice.name, onOpen: () => setOpenId(invoice.deal) })))
      : h('p', { className: 'stoa-sub' }, 'No invoices yet. Open a record and press + Invoice, or tell Stoa: “I invoiced Kestrel 12k today, net 30.”'))
}

// -- the full page -------------------------------------------------------------------------------
// The board is worked by hand as well as by talking: drag a card to another column to move it.
const DRAG = 'text/stoa-deal'
const dragging = event => Boolean(event.dataTransfer) && [...event.dataTransfer.types].includes(DRAG)

/** How long records have sat in a column, beside how long the playbook allows before a nudge is due. */
function Pace({ column }) {
  const days = column.here.map(deal => (Date.now() - Date.parse(deal.stage_at || '')) / 864e5).filter(value => value >= 0)
  if (!days.length || !column.stale_days) return h('div', { className: 'stoa-pace' }, column.stale_days ? 'quiet after ' + column.stale_days + 'd' : ' ')
  const average = Math.round(days.reduce((sum, value) => sum + value, 0) / days.length)
  return h('div', { className: 'stoa-pace',
    title: 'Records here have been in this stage ' + average + ' days on average. A record counts as quiet after ' + column.stale_days + ' days with nothing logged (your playbook sets that).' },
  'avg ' + average + 'd here · quiet after ' + column.stale_days + 'd')
}

function Board({ state, openId, setOpenId }) {
  const pb = state.playbook
  const filter = useValue($filter)
  const [over, setOver] = useState('')
  const board = useRef(null)
  // A stage picked out in the funnel: bring its column into view. Every column keeps its cards.
  useEffect(() => {
    const el = board.current
    const column = el && filter.stage ? el.querySelector('[data-spot="1"]') : null
    if (column) el.scrollTo({ left: Math.max(0, column.offsetLeft - el.offsetLeft - (el.clientWidth - column.clientWidth) / 2), behavior: 'smooth' })
  }, [filter.stage])
  const open = state.deals.filter(deal => deal.status === 'open' && matches(deal, filter))
  const won = state.deals.filter(deal => deal.status === 'won' && matches(deal, { ...filter, due: false, quiet: false }))
    .sort((a, b) => (a.stage_at < b.stage_at ? 1 : -1))
  const known = new Set(pb.stages.map(stage => stage.id))
  const strays = open.filter(deal => !known.has(deal.stage))
  const columns = pb.stages.map(stage => ({ ...stage, here: open.filter(deal => deal.stage === stage.id) }))
    .concat(strays.length ? [{ id: '~other', label: 'Other stages', other: true, here: strays }] : [])
    .concat([{ id: '~won', label: pb.won.label, won: true, here: won.slice(0, 8) }])
    .map(column => ({ ...column, value: column.here.reduce((sum, deal) => sum + (deal.value || 0), 0) }))
  const most = Math.max(1, ...columns.filter(column => !column.won).map(column => column.value))
  const drop = (event, column) => {
    const id = event.dataTransfer.getData(DRAG)
    setOver('')
    if (!id || column.other) return
    event.preventDefault()
    const deal = state.deals.find(item => item.id === id)
    const target = column.won ? pb.won.id : column.id
    if (deal && deal.stage !== target) act(id, { verb: 'move', stage: target })
  }
  const picked = filter.stage && known.has(filter.stage) ? filter.stage : ''       // a stage from another playbook picks out nothing
  return h('div', { className: 'stoa-board', ref: board }, columns.map(column =>
    h('div', { key: column.id, className: 'stoa-col', 'data-over': over === column.id ? '1' : '0', 'data-dim': picked && column.id !== picked ? '1' : '0',
      'data-spot': picked && column.id === picked ? '1' : '0',
      onDragOver: event => { if (dragging(event) && !column.other) { event.preventDefault(); if (over !== column.id) setOver(column.id) } },
      onDragLeave: event => { if (!event.currentTarget.contains(event.relatedTarget)) setOver('') },
      onDrop: event => drop(event, column) },
      h('div', { className: 'stoa-colhead' }, h('span', { className: 'stoa-eyebrow stoa-clip' }, column.label),
        h('span', { className: 'stoa-num' }, column.here.length + (column.value ? ' · ' + money(column.value, pb.currency) : ''))),
      h('div', { className: 'stoa-weight', 'data-won': column.won ? '1' : '0' }, h('i', { style: { width: (column.won ? 100 : Math.round((100 * column.value) / most)) + '%' } })),
      column.won || column.other ? null : h(Pace, { column }),
      column.here.length ? column.here.map(deal => h('button', { type: 'button', key: deal.id, className: 'stoa-card', 'data-open': openId === deal.id ? '1' : '0',
        draggable: !column.won, title: column.won ? undefined : 'Drag to another column to move it',
        onDragStart: event => { event.dataTransfer.setData(DRAG, deal.id); event.dataTransfer.effectAllowed = 'move' },
        onDragEnd: () => setOver(''),
        onClick: () => setOpenId(openId === deal.id ? '' : deal.id) },
        h('h5', null, h('span', { className: 'stoa-clip' }, deal.name), deal.value ? h('span', { className: 'stoa-num' }, money(deal.value, pb.currency)) : null),
        h('div', { className: 'stoa-cardfoot', style: { marginTop: 0 } },
          h('span', { className: 'stoa-sub stoa-clip' }, [deal.company, deal.group && deal.group !== deal.company ? deal.group : ''].filter(Boolean).join(' · ') || ' '),
          deal.owner ? h('em', null, deal.owner) : null),
        deal.next_step && !column.won ? h('div', { className: 'stoa-clip', style: { marginTop: 6, fontSize: 12 } }, deal.next_step) : null,
        deal.due || deal.stale || deal.pending || column.other ? h('div', { className: 'stoa-chips' },
          column.other ? h(Chip, null, deal.stage_label) : null,
          deal.due ? h(Chip, { tone: dueTone(deal) }, deal.next_due < today() ? 'overdue' : 'due today') : null,
          deal.stale && !deal.due ? h(Chip, null, 'quiet') : null,
          deal.pending ? h(Chip, { tone: 'good' }, 'draft waiting') : null) : null))
        : h('div', { className: 'stoa-empty' }, over === column.id ? 'Drop it here' : '—'))))
}

function Company({ company, sessionId }) {
  const [busy, setBusy] = useState(false)
  const buyer = (company.people || [])[0]
  return h('div', { className: 'stoa-company' },
    h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'baseline' } },
      h('span', { className: 'stoa-name stoa-clip' }, company.company), h(Fit, { value: company.fit })),
    h('div', { className: 'stoa-sub' }, [company.size, company.location, company.uses ? 'uses ' + company.uses : ''].filter(Boolean).join(' · ')),
    company.why ? h('div', { style: { marginTop: 4 } }, company.why) : null,
    buyer ? h('div', { className: 'stoa-sub', style: { marginTop: 4 } }, buyer.name + (buyer.title ? ', ' + buyer.title : '')) : null,
    h('div', { className: 'stoa-chips', style: { alignItems: 'center' } },
      company.status === 'tracked' ? h(Chip, { tone: 'good' }, 'in the pipeline')
        : company.status === 'dismissed' ? h(Chip, null, 'dismissed')
          : h(Btn, { disabled: busy, onClick: () => { setBusy(true); track(company, sessionId).then(() => setBusy(false)) } }, 'Add to pipeline'),
      company.signal ? h(Chip, null, company.signal) : null))
}

function Market({ market, sessionId, compact }) {
  const numbers = market.numbers
  return h('div', null,
    h('div', { className: 'stoa-name', style: { fontSize: compact ? 14 : 16 } }, market.name),
    h('div', { className: 'stoa-sub' }, [plural(numbers.companies, 'company', 'companies'), numbers.in_pipeline + ' in the pipeline',
      numbers.with_signal ? numbers.with_signal + ' with a signal' : '', numbers.buyers_named ? plural(numbers.buyers_named, 'buyer named', 'buyers named') : '']
      .filter(Boolean).join(' · ')),
    market.reading && !compact ? h('p', { className: 'stoa-reading' }, market.reading) : null,
    (market.first_calls || []).length ? h('div', null, h('div', { className: 'stoa-h2' }, 'First calls'),
      market.first_calls.slice(0, compact ? 3 : 7).map(call => h('div', { key: call.id, className: 'stoa-row', style: { cursor: 'default', padding: '6px 0' } },
        h(Fit, { value: call.fit }),
        h('span', { className: 'stoa-grow' }, h('span', { className: 'stoa-name' }, call.company),
          h('span', { className: 'stoa-sub' }, '  ' + [call.segment, call.buyer].filter(Boolean).join(' · ')),
          h('span', { style: { display: 'block' } }, call.why)),
        call.status === 'tracked' ? h(Chip, { tone: 'good' }, 'in the pipeline') : h(Btn, { onClick: () => track(call, sessionId) }, 'Add')))) : null,
    h('div', { className: 'stoa-h2' }, 'The map'),
    h('div', { className: 'stoa-cols' }, market.segments.map(segment => h('div', { key: segment.name, className: 'stoa-box' },
      h('div', { style: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'baseline' } },
        h('span', { className: 'stoa-eyebrow' }, segment.name), h('span', { className: 'stoa-num' }, segment.count + ' · fit ' + segment.avg_fit)),
      segment.description && !compact ? h('div', { className: 'stoa-sub', style: { marginTop: 2 } }, segment.description) : null,
      segment.companies.slice(0, compact ? 4 : 30).map(company => h(Company, { key: company.id, company, sessionId }))))),
    compact ? null : h('div', { className: 'stoa-cols', style: { marginTop: 6 } },
      (market.rates || []).length ? h('div', null, h('div', { className: 'stoa-h2' }, 'How rare each requirement is'),
        market.rates.map(rate => h('div', { key: rate.label, className: 'stoa-rate' },
          h('span', { className: 'stoa-clip' }, rate.label), h('u', null, h('i', { style: { width: rate.rate + '%' } })),
          h('span', { className: 'stoa-num' }, rate.rate + '%')))) : null,
      (market.holds || []).length ? h('div', null, h('div', { className: 'stoa-h2' }, 'Who holds this market today'),
        market.holds.map(hold => h('div', { key: hold.name, className: 'stoa-rate' },
          h('span', { className: 'stoa-clip' }, hold.name), h('u', null, h('i', { style: { width: hold.share + '%' } })),
          h('span', { className: 'stoa-num' }, hold.share + '%')))) : null))
}

function Page() {
  const query = useDesk()
  const tab = useValue($tab)
  const [openId, setOpenId] = useState('')
  const [adding, setAdding] = useState(false)
  const [marketId, setMarketId] = useState('')
  const [companyName, setCompanyName] = useState('')
  const [groupName, setGroupName] = useState('')
  const view = useValue($view)
  const [dropping, setDropping] = useState(false)
  const [welcomed, setWelcomed] = useState(() => Boolean(storage && storage.get('welcomed', false)))
  const split = useRef(null)
  // While the page is on screen, a click that calls for writing goes to its own dock.
  useEffect(() => { onPage = true; return () => { onPage = false; $last.set(null) } }, [])
  // A record or the form opens beside the list: bring it clear of the dock if it would sit under it.
  useEffect(() => {
    if (!adding && !openId && !companyName && !groupName) return undefined
    const timer = setTimeout(() => {
      const box = split.current && split.current.querySelector(':scope > .stoa-box')
      const page = split.current && split.current.closest('.stoa-page')
      if (!box || !page) return
      const frame = page.getBoundingClientRect(), rect = box.getBoundingClientRect()
      const hidden = rect.bottom - (frame.bottom - 150)
      if (hidden > 0) page.scrollBy({ top: Math.min(hidden, Math.max(0, rect.top - frame.top - 12)), behavior: 'smooth' })
    }, 260)
    return () => clearTimeout(timer)
  }, [adding, openId, companyName, groupName, tab])
  if (!query.data) return h('div', { className: 'stoa stoa-page' }, h(Unavailable, { error: query.error, loading: query.isLoading }))
  const state = query.data, pb = state.playbook, totals = state.totals
  const day = state.today || { counts: {}, needs: 0 }
  const market = state.markets.find(item => item.id === marketId) || state.markets[0]
  const setTab = name => { $tab.set(name); setOpenId(''); setAdding(false); setCompanyName(''); setGroupName('') }
  const add = () => { if (tab !== 'today' && tab !== 'pipeline') $tab.set('pipeline'); setOpenId(''); setCompanyName(''); setGroupName(''); setAdding(true) }
  const openRecord = id => { setAdding(false); setCompanyName(''); setGroupName(''); setOpenId(id) }
  const openCompany = name => { setAdding(false); setOpenId(''); setGroupName(''); setCompanyName(name) }
  const openGroup = name => { setAdding(false); setOpenId(''); setCompanyName(''); setGroupName(name) }
  const groups = state.groups || []
  const pipes = state.pipelines || []
  const showPipe = id => {
    if (id === state.pipeline) return
    $pipe.set(id === 'main' ? '' : id)
    if (storage) storage.set('pipe', id === 'main' ? '' : id)
    setOpenId(''); setAdding(false); setCompanyName(''); setGroupName('')
    refresh()
  }
  const waiting = (day.counts && day.counts.approvals) || 0
  const welcome = Boolean(state.fresh) && !welcomed
  const skip = () => { setWelcomed(true); if (storage) storage.set('welcomed', true) }
  // Each number says what it counts when you rest on it; the ones that are lists take you there.
  const stats = [
    ['Open', totals.open, cap(pb.terms.deals) + ' still in play: not ' + pb.won.label.toLowerCase() + ', not ' + pb.lost.label.toLowerCase() + '.', 'pipeline'],
    ['Due', totals.due, 'Next steps dated today or earlier. Snoozed ' + pb.terms.deals + ' are left out. Press to see them.', 'today'],
    ['Quiet', totals.stale, 'No call, email, note or move for longer than the stage allows. Your playbook sets the days for each stage. Press to see them.', 'today'],
    ['Mapped', totals.mapped, 'Companies on your market maps.', 'market'],
    ['From the map', totals.tracked, cap(pb.terms.deals) + ' that started on a market map.', ''],
    ['Won', totals.won + (totals.won_value ? ' · ' + money(totals.won_value, pb.currency) : ''), 'Closed as ' + pb.won.label.toLowerCase() + ', all time.', '']]
  const side = adding ? h(NewRecord, { state, close: () => setAdding(false), opened: id => { setAdding(false); setOpenId(id) } })
    : openId ? h(Record, { key: openId, id: openId, state, close: () => setOpenId(''), onCompany: openCompany, onGroup: openGroup })
      : companyName ? h(CompanyPanel, { key: companyName, name: companyName, state, close: () => setCompanyName(''), openRecord })
        : groupName ? h(GroupPanel, { key: groupName, name: groupName, state, close: () => setGroupName(''), openRecord, onRenamed: setGroupName }) : null
  const nothing = h('div', { className: 'stoa-sub' },
    h('p', { style: { marginTop: 0 } }, 'Nothing on the desk yet. Add a ' + pb.terms.deal + ' with the button above, tell Stoa about one below, drop a list on the page, or ask it to map a market.'),
    h(Btn, { onClick: () => setTab('setup') }, 'See how it works, or look around with sample data'))
  // A file dragged in from the computer lands on the desk, wherever on the page it is let go.
  const carrying = event => Boolean(event.dataTransfer) && [...event.dataTransfer.types].includes('Files')
  return h('div', { className: 'stoa stoa-page', 'data-drop': dropping ? '1' : '0',
    onDragOver: event => { if (carrying(event)) { event.preventDefault(); if (!dropping) setDropping(true) } },
    onDragLeave: event => { if (!event.currentTarget.contains(event.relatedTarget)) setDropping(false) },
    onDrop: event => { if (!carrying(event)) return; event.preventDefault(); setDropping(false); takeFile(event.dataTransfer.files[0]) } },
  h(Style),
  dropping ? h('div', { className: 'stoa-dropnote' }, 'Drop it on the desk: a CSV becomes a review to approve') : null,
  h('div', { className: 'stoa-wrap' },
    state.tour ? h('div', { className: 'stoa-tour', role: 'status' },
      h('span', null, h('b', null, 'Sample data'), ' · ' + state.tour.title + '. Everyone on it is made up, and your own desk is untouched.'),
      h(Btn, { onClick: () => { setOpenId(''); tour('leave') } }, 'Leave the tour')) : null,
    pipes.length > 1 ? h('div', { className: 'stoa-pipes', role: 'tablist', 'aria-label': 'Pipelines' }, pipes.map(item =>
      h('button', { type: 'button', key: item.id, role: 'tab', 'aria-selected': item.id === state.pipeline, className: 'stoa-pipe', 'data-on': item.id === state.pipeline ? '1' : '0',
        title: item.title + ': its own ' + 'records and its own playbook', onClick: () => showPipe(item.id) }, item.name, h('small', null, String(item.open))))) : null,
    h('div', { className: 'stoa-head' },
      h('div', null,
        h('div', { className: 'stoa-eyebrow' }, 'Stoa · ' + pb.title),
        h('div', { className: 'stoa-figure' }, money(totals.open_value, pb.currency) || String(totals.open), h('small', null, totals.open_value ? 'in play' : pb.terms.deals + ' open'))),
      h('div', { className: 'stoa-headstats' }, stats.map(item => h('button', { type: 'button', key: item[0], className: 'stoa-stat', title: item[2], 'data-press': item[3] ? '1' : '0',
        onClick: item[3] ? () => setTab(item[3]) : undefined }, h('span', { className: 'stoa-eyebrow' }, item[0]), h('b', null, String(item[1])))))),
    h('div', { className: 'stoa-rule' }),
    h('div', { className: 'stoa-tabsrow' },
      h('div', { className: 'stoa-tabs', role: 'tablist' }, [['today', 'Today', day.needs], ['pipeline', 'Pipeline', totals.open],
        ['cash', 'Cash', state.cash ? state.cash.strip.overdue.count : 0], ['market', 'Market map', totals.mapped],
        ['activity', 'Activity', 0], ['setup', 'Setup', 0]].map(item =>
        h('button', { type: 'button', key: item[0], role: 'tab', 'aria-selected': tab === item[0], className: 'stoa-tab', 'data-on': tab === item[0] ? '1' : '0',
          onClick: () => setTab(item[0]) }, item[1], item[2] ? h('small', null, String(item[2])) : null))),
      (tab === 'today' || tab === 'pipeline') && !welcome ? h(Btn, { onClick: add }, '+  New ' + pb.terms.deal) : null),
    waiting && tab !== 'today' && tab !== 'setup' ? h('button', { type: 'button', className: 'stoa-waiting', onClick: () => setTab('today') },
      h('b', null, String(waiting)), ' waiting for your approval', h('span', { className: 'stoa-sub' }, '  ·  see Today  ›')) : null,
    (tab === 'today' || tab === 'pipeline') && welcome && !waiting ? h(Welcome, { skip }) : null,
    tab === 'today' && (!welcome || waiting) ? h('div', { className: 'stoa-split', ref: split, 'data-side': side || state.deals.length ? '1' : '0' },
      state.deals.length || waiting ? h(Today, { state, openId, setOpenId: openRecord }) : nothing,
      side || (state.deals.length ? h(Ahead, { state, setOpenId: openRecord }) : null)) : null,
    tab === 'pipeline' && (!welcome || waiting) ? h('div', null,
      state.deals.length && view === 'board' ? h(Funnel, { state }) : null,
      state.deals.length || groups.length ? h('div', { className: 'stoa-viewrow' },
        h('div', { className: 'stoa-grow', style: { maxWidth: 760 } }, h(Filters, { state })),
        h('div', { className: 'stoa-tabs', style: { margin: 0 }, role: 'group', 'aria-label': 'How the pipeline is shown' },
          [['board', 'Board'], ['groups', cap(pb.terms.groups || 'groups')], ['companies', 'Companies']].map(item => h('button', { type: 'button', key: item[0], className: 'stoa-tab', style: { height: 26, padding: '0 11px', fontSize: 12 },
            'data-on': view === item[0] ? '1' : '0', 'aria-pressed': view === item[0], onClick: () => $view.set(item[0]) }, item[1])))) : null,
      h('div', { className: 'stoa-split', ref: split, 'data-side': side ? '1' : '0' },
        view === 'groups' && (state.deals.length || groups.length) ? h(Groups, { state, current: groupName, openGroup })
          : !state.deals.length ? nothing : view === 'companies' ? h(Companies, { state, current: companyName, openCompany })
            : h(Board, { state, openId, setOpenId: openRecord }), side)) : null,
    tab === 'cash' ? h('div', { className: 'stoa-split', ref: split, 'data-side': side ? '1' : '0' },
      h(Cash, { state, openId, setOpenId: openRecord }), side) : null,
    tab === 'market' ? (market
      ? h('div', null,
        state.markets.length > 1 ? h('div', { className: 'stoa-tabs', style: { marginTop: 0 } }, state.markets.map(item =>
          h('button', { type: 'button', key: item.id, className: 'stoa-tab', 'data-on': item.id === market.id ? '1' : '0', onClick: () => setMarketId(item.id) }, item.name))) : null,
        h(Market, { market }))
      : h('p', { className: 'stoa-sub' }, 'No market map yet. Ask Stoa below: “Map the market for us.”')) : null,
    tab === 'activity' ? h('div', { style: { maxWidth: 800 } }, state.activity.length ? state.activity.map(entry => h('div', { key: entry.id, className: 'stoa-row', style: { cursor: 'default', alignItems: 'center' } },
      h('i', { className: 'stoa-dot', style: { marginTop: 0 }, 'data-tone': entry.kind === 'stage' && !entry.undone ? 'good' : '' }),
      h('span', { className: 'stoa-grow', style: entry.undone ? { opacity: 0.55 } : undefined },
        h('span', { className: 'stoa-name' }, entry.name), h('span', { style: entry.undone ? { textDecoration: 'line-through' } : undefined }, '  ' + entry.text),
        h('span', { className: 'stoa-sub', style: { display: 'block' } }, ago(entry.at) + ' · ' + (entry.undone ? 'taken back' : entry.kind) + (entry.by ? ' · ' + entry.by : ''))),
      entry.can_undo ? h(Btn, { kind: 'quiet', title: 'This is the last change on ' + entry.name + ': put it back as it was', onClick: () => undo(entry.deal, entry.op) },
        h('span', { style: { display: 'inline-flex', alignItems: 'center', gap: 6 } }, h(Icon, { name: 'back', size: 13 }), 'Undo')) : null,
      entry.can_restore ? h(Btn, { kind: 'quiet', title: entry.name + ' was taken off the desk, not destroyed', onClick: () => putBack(entry.deal) }, 'Put back') : null))
      : h('p', { className: 'stoa-sub' }, 'Everything that happens on the desk is logged here: who did it, when, and what changed.')) : null,
    tab === 'setup' ? h(Setup, { state, go: what => { if (what === 'add') add() } }) : null),
  h(Dock, { state }))
}

// -- drawn inside a reply: ::stoa-card{id="…"} and ::stoa-map{market="…"} ------------------------
// Attributes are model output: checked, and nothing is drawn when they do not name something real.
function CardInChat({ attrs, sessionId }) {
  const query = useDesk()
  const id = String((attrs && attrs.id) || '').trim()
  if (!id || id.length > 80 || /[\s/\\]/.test(id) || !query.data) return null
  return h('div', { className: 'stoa stoa-inchat' }, h(Style), h(Record, { id, state: query.data, sessionId: sessionId ?? null }))
}

function MapInChat({ attrs, sessionId }) {
  const query = useDesk()
  const want = String((attrs && attrs.market) || '').trim().toLowerCase()
  if (!query.data || want.length > 120) return null
  const markets = query.data.markets
  const market = markets.find(item => item.id === want || item.name.toLowerCase() === want) || (want ? null : markets[0])
  if (!market) return null
  return h('div', { className: 'stoa stoa-box', style: { margin: '6px 0', maxWidth: 760 } }, h(Style),
    h(Market, { market, sessionId: sessionId ?? null, compact: true }))
}

function ReviewInChat({ attrs }) {
  const id = String((attrs && attrs.id) || '').trim()
  if (!/^in-[0-9a-f]{8}$/.test(id)) return null
  return h('div', { className: 'stoa', style: { margin: '6px 0', maxWidth: 620 } }, h(Style), h(Review, { id }))
}

export default {
  id: 'stoa',
  name: 'Stoa',
  register(ctx) {
    storage = ctx.storage
    try { $pipe.set(String((storage && storage.get('pipe', '')) || '')) } catch { /* no storage: the first pipeline */ }
    // Every request names the pipeline the page is showing (an opaque id such as p2, never its name).
    rest = (path, options) => {
      const pipe = $pipe.get()
      return ctx.rest(pipe && pipe !== 'main' ? path + (path.includes('?') ? '&' : '?') + 'pipeline=' + encodeURIComponent(pipe) : path, options)
    }
    const kept = ctx.storage.get('filter', {}) || {}      // an older version kept every filter, a picked-out stage included: take only these two
    $filter.set({ ...NO_FILTER, owner: typeof kept.owner === 'string' ? kept.owner : '', group: typeof kept.group === 'string' ? kept.group : '' })
    // While the app is open the desk knows it can draw cards and maps in the conversation here.
    const hello = () => { ctx.rest('/hello', { method: 'POST', body: {} }).catch(() => {}) }
    hello()
    ctx.setInterval(hello, 30000)
    // The agent, a Slack click or another window changed the desk: redraw now, not at the next poll.
    ctx.onEvent('plugin.stoa.changed', refresh)
    // The dock on the page: hear its own conversation, and nobody else's.
    const mine = event => Boolean(talkSession.runtime) && $talk.get().busy
      && String((event && (event.session_id || event.sessionId)) || '') === talkSession.runtime
    ctx.onEvent('message.delta', event => {
      const text = event && event.payload && event.payload.text
      if (mine(event) && typeof text === 'string') setTurn(last => ({ reply: last.reply + text, note: '' }))
    })
    ctx.onEvent('tool.start', event => {
      if (mine(event)) setTurn(last => ({ note: 'Working on the desk…', reply: last.reply && !last.reply.endsWith('\n') ? last.reply + '\n\n' : last.reply }))
    })
    ctx.onEvent('message.complete', event => {
      if (!mine(event)) return
      const payload = (event && event.payload) || {}
      const failed = typeof payload.error === 'string' ? payload.error : ''
      setTurn(last => {
        const final = [payload.text, payload.rendered].find(value => typeof value === 'string' && value.trim()) || last.reply
        return { note: '', reply: final || (failed ? '' : 'Done.'), error: failed }
      })
      setTalk({ busy: false })
      refresh()
    })
    ctx.onEvent('error', event => {
      if (!mine(event)) return
      const payload = (event && event.payload) || {}
      setTurn({ note: '', error: String(payload.message || payload.error || 'Hermes could not answer that.') })
      setTalk({ busy: false })
    })
    ctx.registerMany([
      { id: 'pane', area: 'panes', title: 'Stoa', data: { placement: 'right', width: '340px' }, render: () => h(Pane) },
      { id: 'page', area: 'routes', data: { path: '/stoa' }, render: () => h(Page) },
      { id: 'nav', area: 'sidebar.nav', data: { path: '/stoa', label: 'Stoa', codicon: 'graph' } },
      { id: 'card', area: 'transcript.directives', data: { name: 'stoa-card', render: props => h(CardInChat, props) } },
      { id: 'map', area: 'transcript.directives', data: { name: 'stoa-map', render: props => h(MapInChat, props) } },
      { id: 'intake', area: 'transcript.directives', data: { name: 'stoa-intake', render: props => h(ReviewInChat, props) } },
      { id: 'open', area: 'palette', data: { id: 'stoa.open', label: 'Stoa: open the desk', keywords: ['pipeline', 'gtm', 'market map'], run: () => host.navigate('/stoa') } }
    ])
  }
}
