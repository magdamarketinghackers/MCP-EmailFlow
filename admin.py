"""
Admin panel: /admin — single-password auth, multi-connection clients.

UX:
  • Top: logo, app name, description, Figma integration card
  • List of clients (just names + connection count). Click → modal with full detail.
  • Detail modal lists every ESP connection (edit / delete / set primary)
    plus a "+ Add another connection" button.
  • "+ Add new client" opens a 2-step wizard: connect API → pick defaults.
"""
import os
import json as jsonlib
import html as _html
from typing import Optional

from starlette.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.routing import Route
from itsdangerous import URLSafeSerializer, BadSignature

import store
from esp import SUPPORTED, get_esp

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
_SECRET = os.environ.get("MASTER_KEY", "dev-secret")
_signer = URLSafeSerializer(_SECRET, salt="admin-session")
COOKIE = "ef_admin"


# ── auth ──────────────────────────────────────────────────────────────────────

def _is_auth(request) -> bool:
    tok = request.cookies.get(COOKIE)
    if not tok:
        return False
    try:
        return _signer.loads(tok) == "ok"
    except BadSignature:
        return False


def _require_auth(request, json_response=False):
    if _is_auth(request):
        return None
    if json_response:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return RedirectResponse("/admin/login", status_code=303)


# ── layout primitives ────────────────────────────────────────────────────────

LOGO_SVG = (
    '<svg viewBox="0 0 32 32" width="40" height="40" aria-hidden="true">'
    '<rect x="2" y="6" width="28" height="20" rx="3" fill="none" stroke="#1a1a1a" stroke-width="2"/>'
    '<path d="M3 8l13 10L29 8" fill="none" stroke="#1a1a1a" stroke-width="2"/>'
    '</svg>'
)

CSS = """
*{box-sizing:border-box}
body{font-family:-apple-system,system-ui,sans-serif;max-width:780px;margin:40px auto;padding:0 20px;color:#1a1a1a}
header{display:flex;align-items:center;gap:14px;margin-bottom:8px}
header h1{font-size:1.55rem;font-weight:700;margin:0}
header .sub{color:#777;font-size:.85rem;margin-top:2px}
.intro{color:#444;font-size:.95rem;line-height:1.5;margin:8px 0 24px}
.card{border:1px solid #e6e6e6;border-radius:10px;padding:14px 18px;margin:14px 0;background:#fafafa}
.card h3{margin:0 0 6px;font-size:.95rem;color:#444}
.card .body{color:#555;font-size:.88rem;line-height:1.5}
.card .kv{font-family:ui-monospace,monospace;font-size:.82rem;color:#222;margin-top:4px}
.list{border:1px solid #e6e6e6;border-radius:10px;overflow:hidden;margin-top:8px}
.list button.row{width:100%;text-align:left;background:#fff;border:0;border-bottom:1px solid #f0f0f0;padding:14px 18px;font-size:15px;cursor:pointer;display:flex;justify-content:space-between;align-items:center}
.list button.row:last-child{border-bottom:0}
.list button.row:hover{background:#fafafa}
.tag{font-size:.72rem;background:#eef;color:#345;padding:2px 8px;border-radius:10px;margin-left:6px}
.tag.primary{background:#e6f4ea;color:#1e6b32}
.empty{padding:18px;color:#888;text-align:center}
.add{display:block;width:100%;margin:20px 0 0;padding:14px;background:#1a1a1a;color:#fff;border:0;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer}
.add:hover{background:#000}
.add-sm{background:#1a1a1a;color:#fff;border:0;border-radius:8px;padding:8px 14px;font-size:13px;font-weight:500;cursor:pointer;margin-top:10px}
dialog{border:0;border-radius:14px;padding:0;max-width:540px;width:92%;box-shadow:0 30px 80px rgba(0,0,0,.22)}
dialog::backdrop{background:rgba(0,0,0,.45)}
.dlg{padding:24px 26px}
.dlg h2{margin:0 0 4px;font-size:1.15rem}
.dlg .desc{color:#777;font-size:.85rem;margin-bottom:16px}
.dlg label{display:block;font-size:12px;color:#555;font-weight:600;margin-top:12px}
.dlg input,.dlg select{font-size:14px;padding:9px 11px;margin-top:4px;width:100%;border:1px solid #d4d4d4;border-radius:8px;background:#fff}
.dlg input:focus,.dlg select:focus{outline:0;border-color:#1a1a1a}
.actions{display:flex;justify-content:flex-end;gap:8px;margin-top:20px;flex-wrap:wrap}
.btn{padding:10px 16px;border-radius:8px;border:0;font-size:14px;cursor:pointer;font-weight:500}
.btn.primary{background:#1a1a1a;color:#fff}
.btn.primary:hover{background:#000}
.btn.ghost{background:#fff;color:#444;border:1px solid #ddd}
.btn.ghost:hover{background:#f7f7f7}
.btn.danger{background:#fff;color:#c0392b;border:1px solid #f0c4be}
.btn.danger:hover{background:#fff5f3}
.conn-row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid #f0f0f0}
.conn-row:last-child{border-bottom:0}
.conn-row .meta{font-size:.85rem;color:#555}
.conn-row .meta b{color:#222}
.conn-row .actions{margin:0}
.conn-row .btn{padding:5px 10px;font-size:12px}
.mask{font-family:ui-monospace,monospace;color:#666;font-size:.82rem}
.err{font-size:.85rem;color:#c0392b;margin-top:10px}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.logout{margin-left:auto;font-size:.85rem;color:#888;text-decoration:none}
.logout:hover{color:#1a1a1a}
"""


def _page(body: str, title: str = "Email Flow") -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{CSS}</style></head><body>{body}</body></html>"""


# ── login ─────────────────────────────────────────────────────────────────────

async def login_get(request):
    if not ADMIN_PASSWORD:
        return HTMLResponse(_page(f"""
<header>{LOGO_SVG}<div><h1>Email Flow</h1><div class="sub">Admin disabled</div></div></header>
<p>Set <code>ADMIN_PASSWORD</code> env var on Railway to enable the panel.</p>"""))
    err = '<div class="err">Wrong password</div>' if request.query_params.get("e") else ""
    return HTMLResponse(_page(f"""
<header>{LOGO_SVG}<div><h1>Email Flow</h1><div class="sub">Admin panel</div></div></header>
<form method="post" action="/admin/login" class="dlg" style="border:1px solid #e6e6e6;border-radius:14px;max-width:380px;margin-top:20px">
  <h2>Log in</h2><div class="desc">Use the admin password.</div>
  <label>Password</label>
  <input type="password" name="password" autofocus>
  {err}
  <div class="actions"><button type="submit" class="btn primary">Log in</button></div>
</form>"""))


async def login_post(request):
    form = await request.form()
    if ADMIN_PASSWORD and form.get("password") == ADMIN_PASSWORD:
        resp = RedirectResponse("/admin", status_code=303)
        resp.set_cookie(COOKIE, _signer.dumps("ok"), httponly=True,
                        samesite="lax", max_age=86400 * 7)
        return resp
    return RedirectResponse("/admin/login?e=1", status_code=303)


async def logout(request):
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ── dashboard ────────────────────────────────────────────────────────────────

INTRO = ("System to przenoszenia designu z Figma do gotowych draftów w aplikacjach "
         "email marketingowych (GetResponse, Klaviyo) — z automatycznym uploadem obrazków "
         "na CDN klienta i tworzeniem draftów przez API.")


async def dashboard(request):
    if (r := _require_auth(request)): return r
    if not store.available():
        return HTMLResponse(_page("<h1>Store not ready</h1>"
                                  "<p>Set DATABASE_URL and MASTER_KEY on Railway.</p>"))
    clients = store.list_clients()

    rows = ""
    for c in clients:
        n = len(c["connections"])
        conns_summary = " · ".join(
            f"{cn['label']}" + (" (primary)" if cn['is_primary'] and n > 1 else "")
            for cn in c["connections"]
        )
        rows += (f'<button class="row" data-slug="{_html.escape(c["slug"])}" type="button">'
                 f'<span><b>{_html.escape(c["name"])}</b>'
                 f'<span class="tag">{n} connection{"s" if n != 1 else ""}</span></span>'
                 f'<span class="mask">{_html.escape(conns_summary)}</span>'
                 f'</button>')
    if not rows:
        rows = '<div class="empty">No clients yet — add your first below.</div>'

    esp_opts = "".join(f'<option value="{e}">{e}</option>' for e in SUPPORTED)

    body = f"""
<header>
  {LOGO_SVG}
  <div><h1>Email Flow</h1><div class="sub">Marketing Hackers · client manager</div></div>
  <a href="/admin/logout" class="logout">log out</a>
</header>

<div class="intro">{INTRO}</div>

<div class="card">
  <h3>Figma integration</h3>
  <div class="body">
    Figma używamy przez OAuth każdego użytkownika Claude — to nie jest jedno wspólne konto.
    Każdy członek zespołu łączy własny Figma w
    <a href="https://claude.ai/settings/integrations" target="_blank">claude.ai → Settings → Integrations → Figma</a>,
    a serwer MCP pobiera obrazki przez ten OAuth użytkownika.<br>
    Aby konto miało dostęp do pliku Figma klienta, musi być członkiem teamu/projektu w figma.com.
  </div>
</div>

<h3 style="margin-top:28px;font-size:.95rem;color:#444;font-weight:600">Clients</h3>
<div class="list">{rows}</div>
<button class="add" onclick="openWizard()">+ Add new client</button>

<!-- ── Detail modal ─────────────────────────────────────────── -->
<dialog id="detail">
  <form method="dialog" class="dlg">
    <h2 id="d-name"></h2>
    <div class="desc">slug: <code id="d-slug"></code></div>
    <h3 style="margin:14px 0 0;font-size:.85rem;color:#666">Connections</h3>
    <div id="d-connections"></div>
    <button type="button" class="add-sm" onclick="addConnection()">+ Add another connection</button>
    <div class="actions">
      <button type="button" class="btn danger" onclick="deleteClient()">Delete client</button>
      <button class="btn primary" value="close">Close</button>
    </div>
  </form>
</dialog>

<!-- ── Wizard step 1: connect ────────────────────────────────── -->
<dialog id="wiz1">
  <form class="dlg" onsubmit="connect(event)">
    <h2 id="w1-title">Add new client</h2>
    <div class="desc" id="w1-desc">Step 1 — connect to the email platform</div>
    <input type="hidden" id="w1-mode" value="create">
    <input type="hidden" id="w1-orig-slug">
    <input type="hidden" id="w1-orig-label">
    <div id="w1-client-fields" class="row2">
      <div><label>Slug (id, no spaces)</label><input id="w1-slug" pattern="[a-z0-9-]+" placeholder="iveresse"></div>
      <div><label>Client name</label><input id="w1-name" placeholder="Iveresse"></div>
    </div>
    <label>Connection label</label>
    <input id="w1-label" required pattern="[a-z0-9-]+" placeholder="e.g. getresponse · klaviyo · klaviyo-test">
    <label>ESP</label>
    <select id="w1-esp" onchange="toggleEspFields()">{esp_opts}</select>
    <label>API key <span style="color:#aaa;font-weight:400" id="w1-key-hint">(GR: API key, Klaviyo: pk_…)</span></label>
    <input id="w1-key" placeholder="paste API key here">
    <div id="w1-gr-base">
      <label>GR API base <span style="color:#aaa;font-weight:400">(only for GR360)</span></label>
      <input id="w1-base" placeholder="https://api.getresponse.com/v3">
    </div>
    <div id="w1-err" class="err"></div>
    <div class="actions">
      <button type="button" class="btn ghost" onclick="document.getElementById('wiz1').close()">Cancel</button>
      <button type="submit" class="btn primary" id="w1-submit">Connect →</button>
    </div>
  </form>
</dialog>

<!-- ── Wizard step 2: defaults ───────────────────────────────── -->
<dialog id="wiz2">
  <form class="dlg" onsubmit="saveConn(event)">
    <h2>Defaults</h2>
    <div class="desc">Step 2 — pick the default sender and list for this connection</div>
    <label>Default sender</label>
    <select id="w2-sender"></select>
    <input id="w2-sender-text" style="display:none" placeholder="from@example.com">
    <div id="w2-fl-wrap" style="display:none">
      <label>From label (display name)</label>
      <input id="w2-from-label" placeholder="Brand name">
    </div>
    <label>Default audience / list</label>
    <select id="w2-audience"></select>
    <div id="w2-err" class="err"></div>
    <div class="actions">
      <button type="button" class="btn ghost" onclick="backToStep1()">← Back</button>
      <button type="submit" class="btn primary">Save</button>
    </div>
  </form>
</dialog>

<script>
let wiz = {{}};
let currentDetail = null;

function toggleEspFields() {{
  const esp = document.getElementById('w1-esp').value;
  document.getElementById('w1-gr-base').style.display = esp === 'getresponse' ? '' : 'none';
  document.getElementById('w1-key-hint').textContent =
    esp === 'klaviyo' ? '(pk_… private key)' : '(GR API key)';
}}

function openWizard() {{
  document.getElementById('w1-mode').value = 'create';
  document.getElementById('w1-title').textContent = 'Add new client';
  document.getElementById('w1-desc').textContent = 'Step 1 — connect to the email platform';
  ['w1-slug','w1-name','w1-label','w1-key','w1-base'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('w1-client-fields').style.display = '';
  document.getElementById('w1-slug').required = true;
  document.getElementById('w1-name').required = true;
  document.getElementById('w1-slug').disabled = false;
  document.getElementById('w1-name').disabled = false;
  document.getElementById('w1-err').textContent = '';
  toggleEspFields();
  document.getElementById('wiz1').showModal();
}}

function addConnection() {{
  if (!currentDetail) return;
  document.getElementById('detail').close();
  document.getElementById('w1-mode').value = 'add-connection';
  document.getElementById('w1-title').textContent = 'Add connection · ' + currentDetail.name;
  document.getElementById('w1-desc').textContent = 'Step 1 — connect to the email platform';
  document.getElementById('w1-orig-slug').value = currentDetail.slug;
  document.getElementById('w1-slug').value = currentDetail.slug;
  document.getElementById('w1-name').value = currentDetail.name;
  document.getElementById('w1-slug').disabled = true;
  document.getElementById('w1-name').disabled = true;
  document.getElementById('w1-slug').required = false;
  document.getElementById('w1-name').required = false;
  document.getElementById('w1-label').value = '';
  document.getElementById('w1-key').value = '';
  document.getElementById('w1-key').placeholder = 'paste API key here';
  document.getElementById('w1-base').value = '';
  document.getElementById('w1-err').textContent = '';
  toggleEspFields();
  document.getElementById('wiz1').showModal();
}}

function editConnection(label) {{
  const cn = currentDetail.connections.find(x => x.label === label);
  if (!cn) return;
  document.getElementById('detail').close();
  document.getElementById('w1-mode').value = 'edit';
  document.getElementById('w1-title').textContent = 'Edit connection · ' + label;
  document.getElementById('w1-desc').textContent = 'Leave API key blank to keep current';
  document.getElementById('w1-orig-slug').value = currentDetail.slug;
  document.getElementById('w1-orig-label').value = label;
  document.getElementById('w1-slug').value = currentDetail.slug;
  document.getElementById('w1-name').value = currentDetail.name;
  document.getElementById('w1-slug').disabled = true;
  document.getElementById('w1-name').disabled = true;
  document.getElementById('w1-slug').required = false;
  document.getElementById('w1-name').required = false;
  document.getElementById('w1-label').value = label;
  document.getElementById('w1-esp').value = cn.esp_type;
  document.getElementById('w1-key').value = '';
  document.getElementById('w1-key').placeholder = 'leave blank to keep current key';
  document.getElementById('w1-base').value = '';
  document.getElementById('w1-err').textContent = '';
  toggleEspFields();
  document.getElementById('wiz1').showModal();
}}

async function connect(ev) {{
  ev.preventDefault();
  const btn = document.getElementById('w1-submit');
  const err = document.getElementById('w1-err');
  err.textContent = '';
  const apiKey = document.getElementById('w1-key').value.trim();
  const mode = document.getElementById('w1-mode').value;

  if (!apiKey && mode === 'edit') {{
    // edit without changing key: skip the live check, jump to step 2 with empty dropdowns
    wiz = {{
      slug: document.getElementById('w1-orig-slug').value,
      name: document.getElementById('w1-name').value.trim(),
      label: document.getElementById('w1-label').value.trim(),
      esp_type: document.getElementById('w1-esp').value,
      api_key: '', base: '', mode: 'edit',
      orig_label: document.getElementById('w1-orig-label').value,
    }};
    populateStep2([], []);  // user can leave defaults as-is
    document.getElementById('wiz1').close();
    document.getElementById('wiz2').showModal();
    return;
  }}

  btn.disabled = true; btn.textContent = 'Connecting…';
  const payload = {{
    esp_type: document.getElementById('w1-esp').value,
    api_key:  apiKey,
    base:     document.getElementById('w1-base').value.trim(),
  }};
  try {{
    const r = await fetch('/admin/test-connection', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(payload)
    }});
    const data = await r.json();
    if (!data.ok) {{ err.textContent = data.error || 'Connection failed'; return; }}
    wiz = {{
      slug: (document.getElementById('w1-orig-slug').value
             || document.getElementById('w1-slug').value.trim()),
      name: document.getElementById('w1-name').value.trim(),
      label: document.getElementById('w1-label').value.trim(),
      esp_type: payload.esp_type, api_key: payload.api_key, base: payload.base,
      mode, orig_label: document.getElementById('w1-orig-label').value,
    }};
    populateStep2(data.senders || [], data.audiences || []);
    document.getElementById('wiz1').close();
    document.getElementById('wiz2').showModal();
  }} catch (e) {{ err.textContent = 'Network: ' + e.message; }}
  finally {{ btn.disabled = false; btn.textContent = 'Connect →'; }}
}}

function populateStep2(senders, audiences) {{
  const ss = document.getElementById('w2-sender');
  const st = document.getElementById('w2-sender-text');
  const fl = document.getElementById('w2-fl-wrap');
  if (senders.length === 0) {{
    ss.style.display = 'none'; ss.innerHTML = '';
    st.style.display = '';
    fl.style.display = wiz.esp_type === 'klaviyo' ? '' : 'none';
  }} else {{
    ss.style.display = '';
    st.style.display = 'none';
    fl.style.display = wiz.esp_type === 'klaviyo' ? '' : 'none';
    ss.innerHTML = senders.map(s =>
      `<option value="${{s.id}}">${{s.name || s.id}}${{s.email ? ' (' + s.email + ')' : ''}}</option>`).join('');
  }}
  const aa = document.getElementById('w2-audience');
  aa.innerHTML = audiences.map(a =>
    `<option value="${{a.id}}">${{a.name || a.id}}</option>`).join('') ||
    '<option value="">— none —</option>';
}}

function backToStep1() {{
  document.getElementById('wiz2').close();
  document.getElementById('wiz1').showModal();
}}

async function saveConn(ev) {{
  ev.preventDefault();
  const err = document.getElementById('w2-err'); err.textContent = '';
  const senderSel = document.getElementById('w2-sender');
  const senderTxt = document.getElementById('w2-sender-text');
  const sender = senderSel.style.display === 'none' ? senderTxt.value.trim() : senderSel.value;
  const fl = document.getElementById('w2-from-label').value.trim();
  const body = new URLSearchParams({{
    slug: wiz.slug, name: wiz.name, label: wiz.label, esp_type: wiz.esp_type,
    api_key: wiz.api_key, base: wiz.base,
    sender, audience: document.getElementById('w2-audience').value,
    from_label: fl,
  }});
  let url = '/admin/create';
  if (wiz.mode === 'edit') url = '/admin/update/' + wiz.slug + '/' + wiz.orig_label;
  const r = await fetch(url, {{ method: 'POST', body }});
  if (r.redirected || r.ok) location.href = '/admin';
  else err.textContent = 'Save failed (HTTP ' + r.status + ')';
}}

async function openClient(slug) {{
  const r = await fetch('/admin/client/' + slug + '/json');
  if (!r.ok) return alert('Failed to load');
  const c = await r.json();
  currentDetail = c;
  document.getElementById('d-name').textContent = c.name;
  document.getElementById('d-slug').textContent = c.slug;
  const container = document.getElementById('d-connections');
  container.innerHTML = c.connections.map(cn => {{
    const fl = cn.defaults.from_label ? '· from_label=<b>' + escapeHtml(cn.defaults.from_label) + '</b>' : '';
    const isPrimary = cn.is_primary;
    const primaryTag = isPrimary ? '<span class="tag primary">primary</span>' : '';
    const setPrimaryBtn = isPrimary || c.connections.length === 1
      ? ''
      : `<button type="button" class="btn ghost" onclick="setPrimary('${{escapeAttr(cn.label)}}')">make primary</button>`;
    return `<div class="conn-row">
      <div class="meta">
        <b>${{escapeHtml(cn.label)}}</b> <span class="tag">${{escapeHtml(cn.esp_type)}}</span>${{primaryTag}}
        <div class="mask">key=${{escapeHtml(cn.credentials_masked.api_key || '—')}}
          · sender=<b>${{escapeHtml(cn.defaults.sender || '—')}}</b>
          · audience=<b>${{escapeHtml(cn.defaults.audience || '—')}}</b>
          ${{fl}}</div>
      </div>
      <div class="actions">
        ${{setPrimaryBtn}}
        <button type="button" class="btn ghost" onclick="editConnection('${{escapeAttr(cn.label)}}')">edit</button>
        <button type="button" class="btn danger" onclick="deleteConnection('${{escapeAttr(cn.label)}}')">delete</button>
      </div>
    </div>`;
  }}).join('');
  document.getElementById('detail').showModal();
}}

function escapeHtml(s) {{
  return String(s).replace(/[&<>"']/g, m => ({{
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[m]));
}}
function escapeAttr(s) {{ return String(s).replace(/'/g, "\\\\'"); }}

async function deleteClient() {{
  if (!currentDetail) return;
  if (!confirm('Delete client "' + currentDetail.name + '" and ALL its connections?')) return;
  const r = await fetch('/admin/delete/' + currentDetail.slug, {{ method: 'POST' }});
  if (r.redirected || r.ok) location.href = '/admin';
}}

async function deleteConnection(label) {{
  if (!confirm('Delete connection "' + label + '"?')) return;
  const r = await fetch('/admin/delete-connection/' + currentDetail.slug + '/' + label, {{ method: 'POST' }});
  if (r.redirected || r.ok) location.href = '/admin';
}}

async function setPrimary(label) {{
  const r = await fetch('/admin/set-primary/' + currentDetail.slug + '/' + label, {{ method: 'POST' }});
  if (r.redirected || r.ok) location.href = '/admin';
}}

document.addEventListener('click', e => {{
  const row = e.target.closest('button.row[data-slug]');
  if (row) openClient(row.dataset.slug);
}});
</script>
"""
    return HTMLResponse(_page(body))


# ── JSON endpoints ────────────────────────────────────────────────────────────

async def client_json(request):
    if (r := _require_auth(request, json_response=True)): return r
    slug = request.path_params["slug"]
    c = next((x for x in store.list_clients() if x["slug"] == slug), None)
    if not c:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(c)


async def test_connection(request):
    if (r := _require_auth(request, json_response=True)): return r
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    esp_type = data.get("esp_type", "")
    api_key = (data.get("api_key") or "").strip()
    base = (data.get("base") or "").strip()
    if esp_type not in SUPPORTED:
        return JSONResponse({"ok": False, "error": f"esp_type must be one of {SUPPORTED}"})
    if not api_key:
        return JSONResponse({"ok": False, "error": "API key is required"})
    creds = {"api_key": api_key}
    if base:
        creds["base"] = base
    try:
        esp = get_esp({"esp_type": esp_type, "credentials": creds, "defaults": {}})
        senders_resp = esp.list_senders()
        audiences_resp = esp.list_audiences()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    if "error" in senders_resp:
        return JSONResponse({"ok": False, "error": senders_resp["error"]})
    if "error" in audiences_resp:
        return JSONResponse({"ok": False, "error": audiences_resp["error"]})
    return JSONResponse({
        "ok": True,
        "senders":   senders_resp.get("senders", []),
        "audiences": audiences_resp.get("audiences", []),
    })


# ── CRUD ──────────────────────────────────────────────────────────────────────

def _form_args(form):
    creds = {}
    api_key = (form.get("api_key") or "").strip()
    if api_key:
        creds["api_key"] = api_key
    base = (form.get("base") or "").strip()
    if base:
        creds["base"] = base
    defaults = {
        "sender":   (form.get("sender") or "").strip(),
        "audience": (form.get("audience") or "").strip(),
    }
    fl = (form.get("from_label") or "").strip()
    if fl:
        defaults["from_label"] = fl
    return creds, defaults


async def create(request):
    """Add a connection. Creates the client row if it doesn't exist."""
    if (r := _require_auth(request)): return r
    form = await request.form()
    creds, defaults = _form_args(form)
    slug = (form.get("slug") or "").strip().lower().replace(" ", "-")
    name = (form.get("name") or slug).strip()
    label = (form.get("label") or "").strip().lower().replace(" ", "-")
    esp_type = form.get("esp_type", "getresponse")
    if not slug or not label or "api_key" not in creds:
        return RedirectResponse("/admin", status_code=303)
    store.add_connection(slug, name, label, esp_type, creds, defaults)
    return RedirectResponse("/admin", status_code=303)


async def update(request):
    """Update specific connection. Can rename label via 'label' form field."""
    if (r := _require_auth(request)): return r
    slug = request.path_params["slug"]
    orig_label = request.path_params["label"]
    form = await request.form()
    creds, defaults = _form_args(form)
    new_label = (form.get("label") or orig_label).strip().lower().replace(" ", "-")
    esp_type = form.get("esp_type", "getresponse")
    name = (form.get("name") or "").strip()
    if name:
        store.rename_client(slug, name)
    store.update_connection(slug, orig_label, esp_type,
                            creds if "api_key" in creds else None,
                            defaults, new_label=new_label)
    return RedirectResponse("/admin", status_code=303)


async def delete_connection(request):
    if (r := _require_auth(request)): return r
    store.delete_connection(request.path_params["slug"], request.path_params["label"])
    return RedirectResponse("/admin", status_code=303)


async def set_primary(request):
    if (r := _require_auth(request)): return r
    store.set_primary(request.path_params["slug"], request.path_params["label"])
    return RedirectResponse("/admin", status_code=303)


async def delete(request):
    if (r := _require_auth(request)): return r
    store.delete_client(request.path_params["slug"])
    return RedirectResponse("/admin", status_code=303)


routes = [
    Route("/admin", dashboard),
    Route("/admin/login", login_get),
    Route("/admin/login", login_post, methods=["POST"]),
    Route("/admin/logout", logout),
    Route("/admin/client/{slug}/json", client_json),
    Route("/admin/test-connection", test_connection, methods=["POST"]),
    Route("/admin/create", create, methods=["POST"]),
    Route("/admin/update/{slug}/{label}", update, methods=["POST"]),
    Route("/admin/delete-connection/{slug}/{label}", delete_connection, methods=["POST"]),
    Route("/admin/set-primary/{slug}/{label}", set_primary, methods=["POST"]),
    Route("/admin/delete/{slug}", delete, methods=["POST"]),
]
