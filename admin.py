"""
Admin panel: /admin — single-password auth, CRUD over clients.
API keys are entered here, encrypted by store.py, never shown in full again.
"""
import os
import html as _html
from typing import Optional

from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route
from itsdangerous import URLSafeSerializer, BadSignature

import store
from esp import SUPPORTED

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
_SECRET = os.environ.get("MASTER_KEY", "dev-secret")
_signer = URLSafeSerializer(_SECRET, salt="admin-session")
COOKIE = "ef_admin"


def _is_auth(request) -> bool:
    tok = request.cookies.get(COOKIE)
    if not tok:
        return False
    try:
        return _signer.loads(tok) == "ok"
    except BadSignature:
        return False


def _page(body: str, title: str = "Email Flow — Admin") -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{font-family:system-ui,-apple-system,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#1a1a1a}}
h1{{font-size:1.4rem}} h2{{font-size:1.1rem;margin-top:32px}}
input,select{{font-size:14px;padding:8px;margin:4px 0;width:100%;box-sizing:border-box;border:1px solid #ccc;border-radius:6px}}
label{{font-size:13px;color:#555;font-weight:600;display:block;margin-top:8px}}
button{{background:#1a1a1a;color:#fff;border:0;padding:10px 18px;border-radius:6px;font-size:14px;cursor:pointer;margin-top:12px}}
button.danger{{background:#c0392b}}
.card{{border:1px solid #e0e0e0;border-radius:10px;padding:16px 20px;margin:12px 0}}
.tag{{display:inline-block;padding:2px 10px;border-radius:20px;font-size:.8rem;background:#eef;margin-left:8px}}
.mask{{font-family:monospace;color:#777;font-size:.85rem}}
a{{color:#2563eb}} .row{{display:flex;gap:12px}} .row>div{{flex:1}}
hr{{border:0;border-top:1px solid #eee;margin:24px 0}}
</style></head><body>{body}</body></html>"""


async def login_get(request):
    if not ADMIN_PASSWORD:
        return HTMLResponse(_page("<h1>Admin disabled</h1><p>Set <code>ADMIN_PASSWORD</code> "
                                  "env var on Railway to enable the panel.</p>"))
    err = "<p style='color:#c0392b'>Wrong password</p>" if request.query_params.get("e") else ""
    return HTMLResponse(_page(f"""
      <h1>Email Flow — Admin</h1>{err}
      <form method="post" action="/admin/login">
        <label>Password</label>
        <input type="password" name="password" autofocus>
        <button type="submit">Log in</button>
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


def _client_form(c: Optional[dict] = None) -> str:
    is_edit = c is not None
    slug = c["slug"] if is_edit else ""
    name = c["name"] if is_edit else ""
    esp = c["esp_type"] if is_edit else "getresponse"
    defaults = c["defaults"] if is_edit else {}
    sender = defaults.get("sender", "")
    audience = defaults.get("audience", "")
    from_label = defaults.get("from_label", "")
    base = (c.get("credentials_masked", {}).get("base", "")) if is_edit else ""
    esp_opts = "".join(
        f'<option value="{e}"{" selected" if e == esp else ""}>{e}</option>' for e in SUPPORTED)
    action = f"/admin/update/{slug}" if is_edit else "/admin/create"
    key_hint = ("leave blank to keep current key" if is_edit else "GR: API key · Klaviyo: pk_…")
    slug_field = (f'<input type="hidden" name="slug" value="{slug}">'
                  if is_edit else
                  '<label>Slug (id, no spaces)</label><input name="slug" required '
                  'placeholder="iveresse">')
    return f"""
      <form method="post" action="{action}">
        {slug_field}
        <label>Client name</label><input name="name" value="{_html.escape(name)}" required>
        <label>ESP</label><select name="esp_type">{esp_opts}</select>
        <label>API key ({key_hint})</label><input name="api_key" placeholder="{key_hint}">
        <label>GR API base (optional — GR360 only)</label>
        <input name="base" placeholder="https://api.getresponse.com/v3">
        <div class="row">
          <div><label>Default sender</label>
            <input name="sender" value="{_html.escape(sender)}"
             placeholder="GR fromFieldId / Klaviyo from_email"></div>
          <div><label>Default audience</label>
            <input name="audience" value="{_html.escape(audience)}"
             placeholder="GR campaignId / Klaviyo list id"></div>
        </div>
        <label>From label (Klaviyo display name)</label>
        <input name="from_label" value="{_html.escape(from_label)}" placeholder="Brand name">
        <button type="submit">{"Save changes" if is_edit else "Add client"}</button>
      </form>"""


async def dashboard(request):
    if not _is_auth(request):
        return RedirectResponse("/admin/login", status_code=303)
    if not store.available():
        return HTMLResponse(_page("<h1>Store not ready</h1><p>Add Postgres (DATABASE_URL) "
                                  "and set MASTER_KEY on Railway.</p>"))

    edit_slug = request.query_params.get("edit")
    clients = store.list_clients()

    cards = ""
    for c in clients:
        masked = " · ".join(f"{k}={v}" for k, v in c["credentials_masked"].items())
        d = c["defaults"]
        cards += f"""
        <div class="card">
          <b>{_html.escape(c['name'])}</b><span class="tag">{c['esp_type']}</span>
          <span class="tag">{c['slug']}</span>
          <div class="mask">{_html.escape(masked)}</div>
          <div class="mask">sender={_html.escape(str(d.get('sender','')))} ·
               audience={_html.escape(str(d.get('audience','')))}
               {('· from_label='+_html.escape(str(d.get('from_label')))) if d.get('from_label') else ''}</div>
          <a href="/admin?edit={c['slug']}">edit</a> ·
          <form method="post" action="/admin/delete/{c['slug']}" style="display:inline"
                onsubmit="return confirm('Delete {c['slug']}?')">
            <button class="danger" style="padding:2px 10px;font-size:12px;margin:4px 0">delete</button>
          </form>
        </div>"""

    if edit_slug:
        target = next((c for c in clients if c["slug"] == edit_slug), None)
        form_html = f"<h2>Edit: {edit_slug}</h2>" + _client_form(target) if target else "<p>Not found</p>"
    else:
        form_html = "<h2>Add new client</h2>" + _client_form()

    body = f"""
      <h1>Email Flow — Clients</h1>
      <a href="/admin/logout">log out</a>
      {cards or '<p>No clients yet.</p>'}
      <hr>{form_html}"""
    return HTMLResponse(_page(body))


def _form_to_args(form):
    creds = {}
    api_key = (form.get("api_key") or "").strip()
    if api_key:
        creds["api_key"] = api_key
    base = (form.get("base") or "").strip()
    if base:
        creds["base"] = base
    defaults = {
        "sender": (form.get("sender") or "").strip(),
        "audience": (form.get("audience") or "").strip(),
    }
    fl = (form.get("from_label") or "").strip()
    if fl:
        defaults["from_label"] = fl
    return creds, defaults


async def create(request):
    if not _is_auth(request):
        return RedirectResponse("/admin/login", status_code=303)
    form = await request.form()
    creds, defaults = _form_to_args(form)
    slug = (form.get("slug") or "").strip().lower().replace(" ", "-")
    if not slug or "api_key" not in creds:
        return RedirectResponse("/admin", status_code=303)
    store.create_client(slug, form.get("name", slug), form.get("esp_type", "getresponse"),
                        creds, defaults)
    return RedirectResponse("/admin", status_code=303)


async def update(request):
    if not _is_auth(request):
        return RedirectResponse("/admin/login", status_code=303)
    slug = request.path_params["slug"]
    form = await request.form()
    creds, defaults = _form_to_args(form)
    # blank key → keep existing
    store.update_client(slug, form.get("name", slug), form.get("esp_type", "getresponse"),
                        creds if "api_key" in creds else None, defaults)
    return RedirectResponse("/admin", status_code=303)


async def delete(request):
    if not _is_auth(request):
        return RedirectResponse("/admin/login", status_code=303)
    store.delete_client(request.path_params["slug"])
    return RedirectResponse("/admin", status_code=303)


routes = [
    Route("/admin", dashboard),
    Route("/admin/login", login_get),
    Route("/admin/login", login_post, methods=["POST"]),
    Route("/admin/logout", logout),
    Route("/admin/create", create, methods=["POST"]),
    Route("/admin/update/{slug}", update, methods=["POST"]),
    Route("/admin/delete/{slug}", delete, methods=["POST"]),
]
