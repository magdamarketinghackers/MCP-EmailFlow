"""
Admin panel: /admin — single-password auth, CRUD over clients.

UX flow:
  1. List of clients (names only). Click a name → detail modal with edit/delete.
  2. "Add new client" button → wizard step 1 (name + ESP + API key + Connect).
  3. "Connect" tests the API live, fetches senders + audiences, opens step 2.
  4. Step 2: pick default sender + default list from real dropdowns → Save.
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


# ── layout ────────────────────────────────────────────────────────────────────

LOGO_SVG = (
    '<svg viewBox="0 0 32 32" width="36" height="36" aria-hidden="true">'
    '<rect x="2" y="6" width="28" height="20" rx="3" fill="none" stroke="#1a1a1a" stroke-width="2"/>'
    '<path d="M3 8l13 10L29 8" fill="none" stroke="#1a1a1a" stroke-width="2"/>'
    '</svg>'
)

CSS = """
*{box-sizing:border-box}
body{font-family:-apple-system,system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#1a1a1a}
header{display:flex;align-items:center;gap:14px;margin-bottom:32px}
header h1{font-size:1.5rem;font-weight:700;margin:0}
header .sub{color:#777;font-size:.85rem;margin-top:2px}
.list{border:1px solid #e6e6e6;border-radius:10px;overflow:hidden}
.list button.row{width:100%;text-align:left;background:#fff;border:0;border-bottom:1px solid #f0f0f0;padding:14px 18px;font-size:15px;cursor:pointer;display:flex;justify-content:space-between;align-items:center}
.list button.row:last-child{border-bottom:0}
.list button.row:hover{background:#fafafa}
.list .tag{font-size:.75rem;background:#eef;color:#345;padding:2px 8px;border-radius:10px;margin-left:8px}
.empty{padding:18px;color:#888;text-align:center}
.add{display:block;width:100%;margin:20px 0 0;padding:14px;background:#1a1a1a;color:#fff;border:0;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer}
.add:hover{background:#000}
dialog{border:0;border-radius:14px;padding:0;max-width:520px;width:92%;box-shadow:0 30px 80px rgba(0,0,0,.2)}
dialog::backdrop{background:rgba(0,0,0,.45)}
.dlg{padding:24px 26px}
.dlg h2{margin:0 0 4px;font-size:1.15rem}
.dlg .desc{color:#777;font-size:.85rem;margin-bottom:18px}
.dlg label{display:block;font-size:12px;color:#555;font-weight:600;margin-top:12px}
.dlg input,.dlg select{font-size:14px;padding:9px 11px;margin-top:4px;width:100%;border:1px solid #d4d4d4;border-radius:8px;background:#fff}
.dlg input:focus,.dlg select:focus{outline:0;border-color:#1a1a1a}
.actions{display:flex;justify-content:flex-end;gap:8px;margin-top:20px}
.btn{padding:10px 16px;border-radius:8px;border:0;font-size:14px;cursor:pointer;font-weight:500}
.btn.primary{background:#1a1a1a;color:#fff}
.btn.primary:hover{background:#000}
.btn.ghost{background:#fff;color:#444;border:1px solid #ddd}
.btn.ghost:hover{background:#f7f7f7}
.btn.danger{background:#fff;color:#c0392b;border:1px solid #f0c4be}
.btn.danger:hover{background:#fff5f3}
.detail dl{margin:0;display:grid;grid-template-columns:140px 1fr;gap:8px 12px}
.detail dt{color:#888;font-size:13px}
.detail dd{margin:0;font-size:14px;word-break:break-all}
.mask{font-family:ui-monospace,monospace;color:#666;font-size:.85rem}
.note{font-size:.8rem;color:#666;background:#f7f7f0;border-left:3px solid #d8c860;padding:8px 12px;border-radius:4px;margin-top:10px}
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
<form method="post" action="/admin/login" class="dlg" style="border:1px solid #e6e6e6;border-radius:14px;max-width:380px">
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


# ── dashboard ─────────────────────────────────────────────────────────────────

async def dashboard(request):
    if (r := _require_auth(request)): return r
    if not store.available():
        return HTMLResponse(_page("<h1>Store not ready</h1>"
                                  "<p>Set DATABASE_URL and MASTER_KEY on Railway.</p>"))
    clients = store.list_clients()
    rows = "".join(
        f'<button class="row" data-slug="{_html.escape(c["slug"])}" type="button">'
        f'<span>{_html.escape(c["name"])}</span>'
        f'<span class="tag">{_html.escape(c["esp_type"])}</span>'
        f'</button>'
        for c in clients
    ) or '<div class="empty">No clients yet — add your first below.</div>'

    esp_opts = "".join(f'<option value="{e}">{e}</option>' for e in SUPPORTED)

    body = f"""
<header>
  {LOGO_SVG}
  <div><h1>Email Flow</h1><div class="sub">Marketing Hackers · client manager</div></div>
  <a href="/admin/logout" class="logout">log out</a>
</header>

<div class="list">{rows}</div>
<button class="add" onclick="openWizard()">+ Add new client</button>

<!-- ── Detail modal ─────────────────────────────────────────── -->
<dialog id="detail">
  <form method="dialog" class="dlg detail">
    <h2 id="d-name"></h2>
    <div class="desc"><span id="d-esp"></span> · <code id="d-slug"></code></div>
    <dl>
      <dt>API key</dt><dd class="mask" id="d-key"></dd>
      <dt>Default sender</dt><dd id="d-sender"></dd>
      <dt>Default audience</dt><dd id="d-audience"></dd>
      <dt id="d-fl-l" style="display:none">From label</dt><dd id="d-fl" style="display:none"></dd>
    </dl>
    <div class="actions">
      <button type="button" class="btn danger" onclick="deleteClient()">Delete</button>
      <button type="button" class="btn ghost" onclick="editClient()">Edit</button>
      <button class="btn primary" value="close">Close</button>
    </div>
  </form>
</dialog>

<!-- ── Wizard step 1: connect ────────────────────────────────── -->
<dialog id="wiz1">
  <form class="dlg" onsubmit="connect(event)">
    <h2 id="w1-title">Add new client</h2>
    <div class="desc">Step 1 — connect to the email platform</div>
    <input type="hidden" id="w1-mode" value="create">
    <input type="hidden" id="w1-orig-slug">
    <div class="row2">
      <div><label>Slug (id, no spaces)</label><input id="w1-slug" required pattern="[a-z0-9-]+" placeholder="iveresse"></div>
      <div><label>Client name</label><input id="w1-name" required placeholder="Iveresse"></div>
    </div>
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
  <form class="dlg" onsubmit="saveClient(event)">
    <h2>Defaults</h2>
    <div class="desc">Step 2 — pick the default sender and list for this client</div>
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
      <button type="submit" class="btn primary">Save client</button>
    </div>
  </form>
</dialog>

<script>
// ── shared state from wizard step 1 → step 2 ──
let wiz = {{}};

function toggleEspFields() {{
  const esp = document.getElementById('w1-esp').value;
  document.getElementById('w1-gr-base').style.display = esp === 'getresponse' ? '' : 'none';
  document.getElementById('w1-key-hint').textContent =
    esp === 'klaviyo' ? '(pk_… private key)' : '(GR API key)';
}}

function openWizard() {{
  document.getElementById('w1-mode').value = 'create';
  document.getElementById('w1-title').textContent = 'Add new client';
  ['w1-slug','w1-name','w1-key','w1-base'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('w1-slug').disabled = false;
  document.getElementById('w1-err').textContent = '';
  toggleEspFields();
  document.getElementById('wiz1').showModal();
}}

async function connect(ev) {{
  ev.preventDefault();
  const btn = document.getElementById('w1-submit');
  const err = document.getElementById('w1-err');
  err.textContent = '';
  btn.disabled = true; btn.textContent = 'Connecting…';
  const payload = {{
    esp_type: document.getElementById('w1-esp').value,
    api_key:  document.getElementById('w1-key').value.trim(),
    base:     document.getElementById('w1-base').value.trim(),
  }};
  try {{
    const r = await fetch('/admin/test-connection', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(payload)
    }});
    const data = await r.json();
    if (!data.ok) {{
      err.textContent = data.error || 'Connection failed';
      return;
    }}
    // stash for step 2 + open
    wiz = {{
      slug:     document.getElementById('w1-slug').value.trim(),
      name:     document.getElementById('w1-name').value.trim(),
      esp_type: payload.esp_type, api_key: payload.api_key, base: payload.base,
      mode:     document.getElementById('w1-mode').value,
      origSlug: document.getElementById('w1-orig-slug').value,
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
    // Klaviyo path: free-text from_email + from_label
    ss.style.display = 'none'; ss.innerHTML = '';
    st.style.display = '';
    fl.style.display = '';
  }} else {{
    ss.style.display = ''; st.style.display = ''; st.style.display = 'none';
    fl.style.display = wiz.esp_type === 'klaviyo' ? '' : 'none';
    ss.innerHTML = senders.map(s =>
      `<option value="${{s.id}}">${{s.name || s.id}}${{s.email ? ' (' + s.email + ')' : ''}}</option>`).join('');
  }}
  const aa = document.getElementById('w2-audience');
  aa.innerHTML = audiences.map(a =>
    `<option value="${{a.id}}">${{a.name || a.id}}</option>`).join('') ||
    '<option value="">— no audiences found —</option>';
}}

function backToStep1() {{
  document.getElementById('wiz2').close();
  document.getElementById('wiz1').showModal();
}}

async function saveClient(ev) {{
  ev.preventDefault();
  const err = document.getElementById('w2-err'); err.textContent = '';
  const senderSel = document.getElementById('w2-sender');
  const senderTxt = document.getElementById('w2-sender-text');
  const sender = senderSel.style.display === 'none' ? senderTxt.value.trim() : senderSel.value;
  const fl = document.getElementById('w2-from-label').value.trim();
  const body = new URLSearchParams({{
    slug: wiz.slug, name: wiz.name, esp_type: wiz.esp_type,
    api_key: wiz.api_key, base: wiz.base,
    sender, audience: document.getElementById('w2-audience').value,
    from_label: fl,
  }});
  const url = wiz.mode === 'edit' ? '/admin/update/' + wiz.origSlug : '/admin/create';
  const r = await fetch(url, {{ method: 'POST', body }});
  if (r.redirected || r.ok) location.href = '/admin';
  else err.textContent = 'Save failed (HTTP ' + r.status + ')';
}}

async function openClient(slug) {{
  const r = await fetch('/admin/client/' + slug + '/json');
  if (!r.ok) return alert('Failed to load');
  const c = await r.json();
  document.getElementById('d-name').textContent = c.name;
  document.getElementById('d-esp').textContent = c.esp_type;
  document.getElementById('d-slug').textContent = c.slug;
  const key = c.credentials_masked.api_key || '—';
  document.getElementById('d-key').textContent = key;
  document.getElementById('d-sender').textContent = c.defaults.sender || '—';
  document.getElementById('d-audience').textContent = c.defaults.audience || '—';
  if (c.defaults.from_label) {{
    document.getElementById('d-fl-l').style.display = '';
    document.getElementById('d-fl').style.display = '';
    document.getElementById('d-fl').textContent = c.defaults.from_label;
  }} else {{
    document.getElementById('d-fl-l').style.display = 'none';
    document.getElementById('d-fl').style.display = 'none';
  }}
  document.getElementById('detail').dataset.slug = slug;
  document.getElementById('detail').dataset.full = JSON.stringify(c);
  document.getElementById('detail').showModal();
}}

async function deleteClient() {{
  const slug = document.getElementById('detail').dataset.slug;
  if (!confirm('Delete ' + slug + '?')) return;
  const r = await fetch('/admin/delete/' + slug, {{ method: 'POST' }});
  if (r.redirected || r.ok) location.href = '/admin';
}}

document.addEventListener('click', e => {{
  const row = e.target.closest('button.row[data-slug]');
  if (row) openClient(row.dataset.slug);
}});

function editClient() {{
  const c = JSON.parse(document.getElementById('detail').dataset.full);
  document.getElementById('detail').close();
  document.getElementById('w1-mode').value = 'edit';
  document.getElementById('w1-orig-slug').value = c.slug;
  document.getElementById('w1-title').textContent = 'Edit: ' + c.name;
  document.getElementById('w1-slug').value = c.slug;
  document.getElementById('w1-slug').disabled = true;
  document.getElementById('w1-name').value = c.name;
  document.getElementById('w1-esp').value = c.esp_type;
  document.getElementById('w1-key').value = '';
  document.getElementById('w1-key').placeholder = 'leave blank to keep current key';
  document.getElementById('w1-base').value = '';
  document.getElementById('w1-err').textContent = '';
  toggleEspFields();
  document.getElementById('wiz1').showModal();
}}
</script>
"""
    return HTMLResponse(_page(body))


# ── JSON endpoints ────────────────────────────────────────────────────────────

async def client_json(request):
    if (r := _require_auth(request, json_response=True)): return r
    slug = request.path_params["slug"]
    clients = store.list_clients()
    c = next((x for x in clients if x["slug"] == slug), None)
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

    # Build a transient ESP instance just for the live test
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

def _form_to_args(form):
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
    if (r := _require_auth(request)): return r
    form = await request.form()
    creds, defaults = _form_to_args(form)
    slug = (form.get("slug") or "").strip().lower().replace(" ", "-")
    if not slug or "api_key" not in creds:
        return RedirectResponse("/admin", status_code=303)
    store.create_client(slug, form.get("name", slug),
                        form.get("esp_type", "getresponse"), creds, defaults)
    return RedirectResponse("/admin", status_code=303)


async def update(request):
    if (r := _require_auth(request)): return r
    slug = request.path_params["slug"]
    form = await request.form()
    creds, defaults = _form_to_args(form)
    store.update_client(slug, form.get("name", slug),
                        form.get("esp_type", "getresponse"),
                        creds if "api_key" in creds else None, defaults)
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
    Route("/admin/update/{slug}", update, methods=["POST"]),
    Route("/admin/delete/{slug}", delete, methods=["POST"]),
]
