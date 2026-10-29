"""The settings page: thresholds, sound alerts, and a browser for the pictures.

    /settings                               the page
    /api/settings          GET, POST        every setting, or change some
    /api/settings/reset    POST             back to the defaults
    /api/alerts            GET              sound alert status and history
    /api/alerts/test       POST {event}     play one alert on the Pi now
    /sounds/<event>.wav                     the sound, to preview in the browser
    /api/snapshot          POST             save the current picture
    /api/snapshots         GET              saved pictures, newest first
    /snapshots/<name>.jpg                   one saved picture
    /api/recordings        GET              recorded training sessions
    /recordings/<label>/<session>/<n>.jpg   frame n of a session, with boxes

There is no login: like the live view, anyone who can reach the Pi on the
network can open it. Every value is range-checked before it is used.
"""

from __future__ import annotations

from flask import Flask, Response, abort, jsonify, render_template_string, request, send_file

from alerts import EVENTS, SOUND_DIR
from dataset_recorder import load_session
from settings_store import settings


def add_settings_routes(app: Flask, *, store, recorder, alerts, snapshots, render) -> None:
    """render(frame) turns one (24, 32) temperature frame into a JPEG."""

    @app.route("/settings")
    def settings_page():
        return render_template_string(SETTINGS_PAGE)

    @app.route("/api/settings", methods=["GET", "POST"])
    def api_settings():
        if request.method == "POST":
            changes = request.get_json(silent=True)
            if not isinstance(changes, dict):
                return jsonify({"error": "send a JSON object of name: value"}), 400
            applied, errors = settings.update(changes)
            return jsonify({"applied": applied, "errors": errors,
                            "settings": settings.schema()}), (400 if errors else 200)
        return jsonify({"settings": settings.schema()})

    @app.route("/api/settings/reset", methods=["POST"])
    def api_settings_reset():
        group = (request.get_json(silent=True) or {}).get("group")
        settings.reset(str(group) if group else None)
        return jsonify({"settings": settings.schema()})

    @app.route("/api/alerts")
    def api_alerts():
        return jsonify(alerts.status())

    @app.route("/api/alerts/test", methods=["POST"])
    def api_alerts_test():
        event = str((request.get_json(silent=True) or {}).get("event", ""))
        if event not in EVENTS:
            return jsonify({"error": f"unknown alert '{event}'"}), 400
        alerts.trigger(event, force=True, reason="test from the settings page")
        return jsonify({"queued": event})

    @app.route("/sounds/<event>.wav")
    def sound_file(event: str):
        if event not in EVENTS or not (SOUND_DIR / f"{event}.wav").exists():
            abort(404)
        return send_file(SOUND_DIR / f"{event}.wav", mimetype="audio/wav")

    @app.route("/api/snapshot", methods=["POST"])
    def api_snapshot():
        jpeg, frame, stats = store.latest()
        name = snapshots.save(jpeg, frame, "manual", stats)
        if name is None:
            return jsonify({"error": "no picture yet"}), 503
        return jsonify({"saved": name})

    @app.route("/api/snapshots")
    def api_snapshots():
        return jsonify({"snapshots": snapshots.list(), "folder": str(snapshots.root)})

    @app.route("/snapshots/<name>.jpg")
    def snapshot_file(name: str):
        path = snapshots.path(name)
        if path is None:
            abort(404)
        return send_file(path, mimetype="image/jpeg")

    @app.route("/api/recordings")
    def api_recordings():
        return jsonify({"sessions": recorder.sessions(), "folder": str(recorder.root)})

    cache: dict = {}

    @app.route("/recordings/<label>/<session>/<int:index>.jpg")
    def recording_frame(label: str, session: str, index: int):
        folder = recorder.session_folder(label, session)
        if folder is None:
            abort(404)
        if cache.get("folder") != folder:              # keep one session in memory
            _, frames, _ = load_session(folder)
            cache.update(folder=folder, frames=frames)
        frames = cache["frames"]
        if not 0 <= index < len(frames):
            abort(404)
        return Response(render(frames[index]), mimetype="image/jpeg",
                        headers={"Cache-Control": "max-age=3600"})


SETTINGS_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Settings</title>
<style>
  :root { --bg:#12141a; --panel:#1b1e26; --line:#2b303b; --text:#e8eaf0;
          --muted:#9aa3b2; --ok:#2e9e6b; --alarm:#e5484d; --warn:#f5a524; --accent:#3b82f6; }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px 16px; background:var(--bg); color:var(--text);
         font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  .wrap { max-width: 900px; margin: 0 auto; }
  header { display:flex; justify-content:space-between; align-items:baseline; gap:12px; flex-wrap:wrap; }
  h1 { font-size: 1.25rem; margin: 0 0 4px; font-weight: 650; }
  h2 { font-size: 1rem; margin: 0 0 12px; font-weight: 650; }
  a { color: var(--accent); }
  p.sub { margin: 0 0 18px; color: var(--muted); font-size: .9rem; }
  .tabs { display:flex; gap:6px; margin-bottom:14px; flex-wrap:wrap; }
  .tabs button { background: var(--panel); color: var(--muted); border:1px solid var(--line);
                 padding: 8px 14px; border-radius: 8px; font: inherit; cursor: pointer; }
  .tabs button.on { color: var(--text); border-color: var(--accent); }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
          padding: 16px; margin-bottom: 14px; }
  .row { display:grid; grid-template-columns: minmax(0,1fr) 150px; gap: 4px 14px;
         padding: 10px 0; border-top: 1px solid var(--line); align-items:center; }
  .row:first-of-type { border-top: 0; }
  .row label { font-size: .92rem; }
  .row .help { grid-column: 1; color: var(--muted); font-size: .78rem; }
  .row .changed { color: var(--warn); }
  input[type=number], input[type=text], select { width: 100%; padding: 8px 10px; border-radius: 8px;
         border: 1px solid var(--line); background: var(--bg); color: var(--text); font: inherit; }
  input[type=checkbox] { width: 20px; height: 20px; justify-self: start; }
  .actions { display:flex; gap:8px; margin-top: 12px; flex-wrap: wrap; align-items:center; }
  button.main { padding: 9px 18px; border-radius: 8px; border: 0; background: var(--ok);
                color: #fff; font: inherit; font-weight: 650; cursor: pointer; }
  button.plain { padding: 8px 14px; border-radius: 8px; border: 1px solid var(--line);
                 background: transparent; color: var(--text); font: inherit; cursor: pointer; }
  .msg { font-size: .85rem; color: var(--muted); }
  .msg.err { color: var(--alarm); }
  .event { display:flex; align-items:center; gap: 10px; padding: 9px 0;
           border-top: 1px solid var(--line); flex-wrap: wrap; }
  .event:first-child { border-top: 0; }
  .event .t { flex: 1 1 200px; }
  .pill { font-size: .7rem; text-transform: uppercase; letter-spacing: .05em; padding: 2px 8px;
          border-radius: 999px; border: 1px solid var(--line); color: var(--muted); }
  .pill.critical { color: #fff; background: var(--alarm); border-color: var(--alarm); }
  .pill.warning { color: #1b1300; background: var(--warn); border-color: var(--warn); }
  .history { font-size: .85rem; color: var(--muted); margin: 0; padding-left: 18px; }
  .gallery { display:grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
  .gallery figure { margin:0; background: var(--bg); border:1px solid var(--line); border-radius:8px; overflow:hidden; }
  .gallery img { width:100%; display:block; cursor: zoom-in; }
  .gallery figcaption { padding: 6px 8px; font-size: .75rem; color: var(--muted); }
  .viewer img { width: 100%; max-width: 640px; display:block; border-radius: 8px; background:#000; }
  .viewer input[type=range] { width: 100%; max-width: 640px; margin-top: 10px; }
  .empty { color: var(--muted); font-size: .9rem; }
  .big { position: fixed; inset: 0; background: rgba(0,0,0,.85); display:none;
         align-items:center; justify-content:center; padding: 16px; }
  .big img { max-width: 100%; max-height: 100%; border-radius: 8px; }
  .big.on { display:flex; }
  @media (max-width: 560px) { .row { grid-template-columns: 1fr; } .row .help { grid-column: auto; } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Settings</h1>
      <p class="sub">Changes apply at once and are kept after a restart.</p>
    </div>
    <a href="/">&larr; Live view</a>
  </header>

  <div class="tabs" role="tablist">
    <button data-tab="thresholds" class="on">Thresholds</button>
    <button data-tab="sounds">Sound alerts</button>
    <button data-tab="images">Pictures</button>
    <button data-tab="recordings">Recordings</button>
  </div>

  <section id="tab-thresholds"><div id="groups"></div></section>

  <section id="tab-sounds" hidden>
    <div class="card">
      <h2>Alerts</h2>
      <p class="msg" id="player"></p>
      <div id="events"></div>
    </div>
    <div class="card">
      <h2>Recently played</h2>
      <ul class="history" id="history"></ul>
    </div>
  </section>

  <section id="tab-images" hidden>
    <div class="card">
      <div class="actions" style="margin:0 0 12px">
        <button class="main" id="snap">Take picture now</button>
        <span class="msg" id="snap-msg"></span>
      </div>
      <div class="gallery" id="gallery"></div>
    </div>
  </section>

  <section id="tab-recordings" hidden>
    <div class="card">
      <h2>Recorded sessions</h2>
      <select id="session"></select>
      <div class="viewer" style="margin-top:12px">
        <img id="frame" alt="Recorded thermal frame">
        <input type="range" id="slider" min="0" value="0" aria-label="Frame">
        <div class="msg" id="frame-msg"></div>
      </div>
    </div>
  </section>
</div>
<div class="big" id="big"><img alt="Saved thermal picture"></div>

<script>
const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...kids) => {
  const node = Object.assign(document.createElement(tag), props);
  kids.forEach(k => node.append(k));
  return node;
};
const when = (t) => t ? new Date(t * 1000).toLocaleString() : '';

// ---- tabs ----
document.querySelectorAll('.tabs button').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.tabs button').forEach(x => x.classList.toggle('on', x === b));
  document.querySelectorAll('section').forEach(s => s.hidden = s.id !== 'tab-' + b.dataset.tab);
  ({ sounds: loadAlerts, images: loadSnapshots, recordings: loadRecordings })[b.dataset.tab]?.();
}));

// ---- thresholds ----
let schema = [];
function input(s) {
  if (s.kind === 'bool') return el('input', { type: 'checkbox', checked: !!s.value, id: 'f-' + s.name });
  if (s.kind === 'str') return el('input', { type: 'text', value: s.value ?? '', id: 'f-' + s.name, maxLength: 100 });
  const i = el('input', { type: 'number', value: s.value, id: 'f-' + s.name });
  if (s.min !== null) i.min = s.min;
  if (s.max !== null) i.max = s.max;
  i.step = s.step ?? 'any';
  return i;
}
function readInput(s) {
  const i = $('f-' + s.name);
  return s.kind === 'bool' ? i.checked : i.value;
}
function renderSettings() {
  const groups = {};
  schema.filter(s => s.group !== 'Sound alerts: which ones')
        .forEach(s => (groups[s.group] ||= []).push(s));
  const box = $('groups');
  box.replaceChildren();
  Object.entries(groups).forEach(([name, items]) => {
    const card = el('div', { className: 'card' }, el('h2', { textContent: name }));
    items.forEach(s => {
      const changed = String(s.value) !== String(s.default);
      const help = el('div', { className: 'help' },
        (s.help ? s.help + ' ' : '') + 'Default ' + s.default + '.');
      if (changed) help.append(' ', el('span', { className: 'changed', textContent: 'Changed.' }));
      card.append(el('div', { className: 'row' },
        el('label', { htmlFor: 'f-' + s.name, textContent: s.label }), input(s), help));
    });
    const msg = el('span', { className: 'msg' });
    const save = el('button', { className: 'main', textContent: 'Save' });
    const reset = el('button', { className: 'plain', textContent: 'Reset to defaults' });
    save.onclick = async () => {
      const body = {};
      items.forEach(s => body[s.name] = readInput(s));
      const r = await fetch('/api/settings', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const d = await r.json();
      schema = d.settings;
      renderSettings();
      flash(name, Object.keys(d.errors || {}).length
        ? Object.values(d.errors).join('. ') : 'Saved.', !r.ok);
    };
    reset.onclick = async () => {
      const r = await fetch('/api/settings/reset', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ group: name }) });
      schema = (await r.json()).settings;
      renderSettings();
      flash(name, 'Back to defaults.');
    };
    msg.dataset.group = name;
    card.append(el('div', { className: 'actions' }, save, reset, msg));
    box.append(card);
  });
}
function flash(group, text, error) {
  const m = document.querySelector('.msg[data-group="' + group + '"]');
  if (!m) return;
  m.textContent = text;
  m.classList.toggle('err', !!error);
}
async function loadSettings() {
  schema = (await (await fetch('/api/settings')).json()).settings;
  renderSettings();
}

// ---- sound alerts ----
async function loadAlerts() {
  const s = await (await fetch('/api/alerts', { cache: 'no-store' })).json();
  $('player').textContent = (s.enabled ? 'Sound alerts are ON' : 'Sound alerts are OFF (see Thresholds)') +
    ' - playing on the Pi through ' + (s.player || 'nothing: no audio player found') +
    (s.last_error ? '. Last error: ' + s.last_error : '.');
  const box = $('events');
  box.replaceChildren();
  s.events.forEach(e => {
    const toggle = el('input', { type: 'checkbox', checked: e.enabled, title: 'Play this alert' });
    toggle.onchange = async () => {
      await fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ['SOUND_' + e.name.toUpperCase()]: toggle.checked }) });
    };
    const test = el('button', { className: 'plain', textContent: 'Play on Pi', disabled: !e.has_sound });
    test.onclick = () => fetch('/api/alerts/test', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ event: e.name }) });
    const here = el('button', { className: 'plain', textContent: 'Play here', disabled: !e.has_sound });
    here.onclick = () => new Audio('/sounds/' + e.name + '.wav').play();
    box.append(el('div', { className: 'event' }, toggle,
      el('span', { className: 'pill ' + e.level, textContent: e.level }),
      el('span', { className: 't', textContent: e.title + (e.has_sound ? '' : ' (no sound file)') }),
      here, test));
  });
  const list = $('history');
  list.replaceChildren(...(s.history.length ? s.history.map(h =>
    el('li', { textContent: when(h.time) + ' - ' + h.title + (h.reason ? ' (' + h.reason + ')' : '') }))
    : [el('li', { textContent: 'Nothing yet.' })]));
}

// ---- pictures ----
async function loadSnapshots() {
  const d = await (await fetch('/api/snapshots', { cache: 'no-store' })).json();
  const box = $('gallery');
  if (!d.snapshots.length) {
    box.replaceChildren(el('p', { className: 'empty',
      textContent: 'No pictures yet. They are saved in ' + d.folder }));
    return;
  }
  box.replaceChildren(...d.snapshots.map(s => {
    const img = el('img', { src: '/snapshots/' + s.name + '.jpg', loading: 'lazy', alt: s.reason });
    img.onclick = () => { $('big').querySelector('img').src = img.src; $('big').classList.add('on'); };
    const extra = (s.human_count ? s.human_count + ' person(s), ' : '') + (s.max_c != null ? 'max ' + s.max_c + ' C' : '');
    return el('figure', {}, img, el('figcaption', { textContent:
      s.reason.replace(/_/g, ' ') + ' - ' + when(s.time) + (extra ? ' - ' + extra : '') }));
  }));
}
$('snap').onclick = async () => {
  const r = await fetch('/api/snapshot', { method: 'POST' });
  const d = await r.json();
  $('snap-msg').textContent = r.ok ? 'Saved ' + d.saved : d.error;
  loadSnapshots();
};
$('big').onclick = () => $('big').classList.remove('on');

// ---- recordings ----
let sessions = [];
async function loadRecordings() {
  sessions = (await (await fetch('/api/recordings', { cache: 'no-store' })).json()).sessions;
  const pick = $('session');
  if (!sessions.length) {
    pick.replaceChildren(el('option', { textContent: 'No recordings yet: use the panel on the live view' }));
    $('frame').removeAttribute('src');
    return;
  }
  pick.replaceChildren(...sessions.map((s, i) => el('option', { value: i,
    textContent: s.label.replace(/_/g, ' ') + ' - ' + when(s.started_at) + ' - ' + s.frames + ' frames' +
                 (s.note ? ' - ' + s.note : '') })));
  showSession();
}
function showSession() {
  const s = sessions[$('session').value];
  if (!s) return;
  $('slider').max = Math.max(s.frames - 1, 0);
  $('slider').value = 0;
  showFrame();
}
function showFrame() {
  const s = sessions[$('session').value];
  const i = $('slider').value;
  $('frame').src = '/recordings/' + s.label + '/' + s.session + '/' + i + '.jpg';
  $('frame-msg').textContent = 'Frame ' + (Number(i) + 1) + ' of ' + s.frames +
    ' (boxes are what the detector finds with the current settings)';
}
$('session').onchange = showSession;
$('slider').oninput = showFrame;

loadSettings();
</script>
</body>
</html>
"""
