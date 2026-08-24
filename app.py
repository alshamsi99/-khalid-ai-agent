import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent
TOKEN_FILE = BASE / ".gmail_token.json"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE = "https://www.googleapis.com/auth/gmail.modify"
REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "https://khalid-ai-agent.onrender.com/oauth/callback")

app = FastAPI(title="AI Agent Khalid", version="0.4.1")

SETTINGS = {
    "classify": True,
    "suggest": True,
    "autosend": False,
    "alerts": True,
    "approval_required": True,
}

OAUTH_STATES = set()
CACHE = {"messages": [], "updated_at": None, "email": None}

LABEL_NAMES = {
    "reply": "AI/Needs Reply",
    "follow": "AI/Follow Up",
    "none": "AI/No Action",
}


class SettingsPayload(BaseModel):
    classify: bool = True
    suggest: bool = True
    autosend: bool = False
    alerts: bool = True


class ActionPayload(BaseModel):
    action: str


def client_configured() -> bool:
    return bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET"))


def load_token() -> Optional[dict]:
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text())
    except Exception:
        return None


def save_token(token: dict) -> None:
    TOKEN_FILE.write_text(json.dumps(token))


async def refresh_access_token(token: dict) -> dict:
    if not token.get("refresh_token"):
        raise HTTPException(401, "Gmail needs reconnect")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "refresh_token": token["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
    if r.status_code >= 400:
        raise HTTPException(401, "Gmail token refresh failed")
    new = r.json()
    token.update(new)
    save_token(token)
    return token


async def gmail_request(method: str, path: str, *, params=None, json_body=None) -> dict:
    token = load_token()
    if not token or not token.get("access_token"):
        raise HTTPException(401, "Gmail not connected")
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.request(method, GMAIL_API + path, headers=headers, params=params, json=json_body)
        if r.status_code == 401:
            token = await refresh_access_token(token)
            headers = {"Authorization": f"Bearer {token['access_token']}"}
            r = await c.request(method, GMAIL_API + path, headers=headers, params=params, json=json_body)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"Gmail API error: {r.text[:300]}")
    return r.json() if r.content else {}


def get_header(payload: dict, name: str) -> str:
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def classify_message(sender: str, subject: str, snippet: str) -> str:
    text = f"{sender} {subject} {snippet}".lower()
    # Important account/security/financial notices should be surfaced for human review.
    follow_words = [
        "security alert", "تنبيه أمني", "action required", "مطلوب إجراء", "verify", "verification",
        "invoice", "فاتورة", "payment", "دفع", "overdue", "موعد", "appointment", "account access",
        "password", "كلمة المرور", "suspicious", "unusual activity",
    ]
    if any(w in text for w in follow_words):
        return "follow"

    no_reply_sender = any(w in text for w in ["no-reply", "noreply", "notifications@", "newsletter", "marketing"])
    promo_words = ["credits", "daily credits", "newsletter", "unsubscribe", "promotion", "offer", "sale", "خصم", "عرض"]
    if no_reply_sender or any(w in text for w in promo_words):
        return "none"

    request_words = [
        "?", "can you", "could you", "please", "need your", "reply", "response", "let me know",
        "هل", "ممكن", "يرجى", "الرجاء", "أحتاج", "احتاج", "رد", "أفدني", "تأكيد",
    ]
    if any(w in text for w in request_words):
        return "reply"
    return "follow"


def decision_text(kind: str) -> str:
    if kind == "reply":
        return "يبدو أن الرسالة تحتاج ردًا. سيتم تجهيز مسودة لاحقًا، ولن يتم إرسال أي شيء تلقائيًا."
    if kind == "follow":
        return "لا يوجد رد تلقائي. الرسالة تحتاج مراجعتك أو متابعة منك."
    return "لا تبدو الرسالة بحاجة إلى رد أو متابعة. يمكن تركها أو أرشفتها."


def summary_text(sender: str, subject: str, snippet: str) -> str:
    clean = re.sub(r"\s+", " ", snippet or "").strip()
    if len(clean) > 220:
        clean = clean[:217] + "..."
    return f"{subject or 'بدون عنوان'} — {clean}" if clean else f"رسالة من {sender}."


async def ensure_labels() -> dict:
    data = await gmail_request("GET", "/labels")
    existing = {x.get("name"): x.get("id") for x in data.get("labels", [])}
    out = {}
    for kind, name in LABEL_NAMES.items():
        if name in existing:
            out[kind] = existing[name]
            continue
        created = await gmail_request(
            "POST",
            "/labels",
            json_body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
        )
        out[kind] = created["id"]
    return out


async def sync_gmail(limit: int = 30) -> list:
    profile = await gmail_request("GET", "/profile")
    CACHE["email"] = profile.get("emailAddress")
    labels = await ensure_labels()

    listed = await gmail_request("GET", "/messages", params={"q": "in:inbox -in:spam -in:trash", "maxResults": limit})
    items = []
    ai_label_ids = list(labels.values())
    for row in listed.get("messages", []):
        mid = row["id"]
        m = await gmail_request(
            "GET",
            f"/messages/{mid}",
            params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
        )
        payload = m.get("payload", {})
        sender = get_header(payload, "From")
        subject = get_header(payload, "Subject") or "(بدون عنوان)"
        snippet = m.get("snippet", "")
        kind = classify_message(sender, subject, snippet) if SETTINGS["classify"] else "follow"

        current_labels = set(m.get("labelIds", []))
        remove_ids = [x for x in ai_label_ids if x in current_labels and x != labels[kind]]
        add_ids = [] if labels[kind] in current_labels else [labels[kind]]
        if remove_ids or add_ids:
            await gmail_request(
                "POST",
                f"/messages/{mid}/modify",
                json_body={"addLabelIds": add_ids, "removeLabelIds": remove_ids},
            )

        ts = m.get("internalDate")
        received = "حديثًا"
        if ts:
            try:
                dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
                received = dt.strftime("%Y-%m-%d")
            except Exception:
                pass

        items.append(
            {
                "id": mid,
                "provider": "gmail",
                "sender": sender,
                "subject": subject,
                "snippet": snippet,
                "classification": kind,
                "classification_label": {"reply": "يحتاج رد", "follow": "متابعة", "none": "لا إجراء"}[kind],
                "received": received,
                "important": kind == "follow",
                "summary": summary_text(sender, subject, snippet),
                "decision": decision_text(kind),
            }
        )
    CACHE["messages"] = items
    CACHE["updated_at"] = datetime.now(timezone.utc).isoformat()
    return items


@app.get("/connect/gmail")
def connect_gmail():
    if not client_configured():
        raise HTTPException(503, "Google OAuth is not configured on Render yet")
    state = secrets.token_urlsafe(24)
    OAUTH_STATES.add(state)
    params = {
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return RedirectResponse(GOOGLE_AUTH_URL + "?" + urlencode(params))


@app.get("/oauth/callback")
async def oauth_callback(code: str, state: str):
    if state not in OAUTH_STATES:
        raise HTTPException(400, "Invalid OAuth state")
    OAUTH_STATES.discard(state)
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
    if r.status_code >= 400:
        raise HTTPException(400, f"OAuth token exchange failed: {r.text[:300]}")
    token = r.json()
    old = load_token() or {}
    if not token.get("refresh_token") and old.get("refresh_token"):
        token["refresh_token"] = old["refresh_token"]
    save_token(token)
    try:
        await sync_gmail()
    except Exception:
        pass
    return RedirectResponse("/?gmail=connected")


@app.get("/api/status")
async def status():
    connected = bool(load_token())
    if connected and not CACHE["messages"]:
        try:
            await sync_gmail()
        except Exception:
            connected = False
    items = CACHE["messages"] if connected else []
    counts = {"all": len(items), "reply": 0, "follow": 0, "none": 0}
    for m in items:
        counts[m["classification"]] += 1
    return {
        "ok": True,
        "mode": "live" if connected else "setup",
        "gmail": {
            "connected": connected,
            "configured": client_configured(),
            "email": CACHE.get("email"),
            "label": "متصل" if connected else ("جاهز للربط" if client_configured() else "يحتاج مفاتيح OAuth"),
        },
        "whatsapp": {"connected": False},
        "instagram": {"connected": False},
        "counts": counts,
        "updated_at": CACHE.get("updated_at"),
    }


@app.post("/api/sync")
async def sync_now():
    items = await sync_gmail()
    return {"ok": True, "count": len(items), "updated_at": CACHE["updated_at"]}


@app.get("/api/messages")
async def messages(classification: str | None = None, q: str | None = None):
    if not load_token():
        return {"items": []}
    if not CACHE["messages"]:
        await sync_gmail()
    items = CACHE["messages"]
    if classification and classification != "all":
        items = [m for m in items if m["classification"] == classification]
    if q:
        ql = q.lower()
        items = [m for m in items if ql in (m["sender"] + " " + m["subject"] + " " + m["snippet"]).lower()]
    return {"items": items}


@app.get("/api/messages/{message_id}")
async def message(message_id: str):
    if not CACHE["messages"]:
        await sync_gmail()
    for m in CACHE["messages"]:
        if m["id"] == message_id:
            return m
    raise HTTPException(404, "Message not found")


@app.post("/api/messages/{message_id}/action")
async def message_action(message_id: str, payload: ActionPayload):
    if payload.action == "archive":
        await gmail_request("POST", f"/messages/{message_id}/modify", json_body={"removeLabelIds": ["INBOX"]})
        CACHE["messages"] = [m for m in CACHE["messages"] if m["id"] != message_id]
        return {"ok": True, "message": "تمت أرشفة الرسالة."}
    if payload.action in {"follow", "no_action"}:
        labels = await ensure_labels()
        kind = "follow" if payload.action == "follow" else "none"
        await gmail_request("POST", f"/messages/{message_id}/modify", json_body={"addLabelIds": [labels[kind]]})
        return {"ok": True, "message": "تم تحديث التصنيف."}
    if payload.action == "approve_draft":
        return {"ok": False, "message": "إنشاء/إرسال الردود غير مفعّل بعد. سيبقى الإرسال بموافقتك فقط."}
    raise HTTPException(400, "Unsupported action")


@app.get("/api/settings")
def get_settings():
    return SETTINGS


@app.post("/api/settings")
def save_settings(payload: SettingsPayload):
    SETTINGS.update(payload.model_dump())
    SETTINGS["autosend"] = False
    SETTINGS["approval_required"] = True
    return SETTINGS


@app.get("/api/integrations")
def integrations():
    return {
        "gmail": {
            "state": "connected" if load_token() else "ready_to_connect",
            "scope": SCOPE,
            "redirect_uri": REDIRECT_URI,
        },
        "whatsapp": {"state": "planned"},
        "instagram": {"state": "planned"},
    }


@app.get("/")
def root():
    return FileResponse(BASE / "index.html")


app.mount("/", StaticFiles(directory=BASE, html=True), name="static")
