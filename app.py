import base64
import json
import os
import re
import secrets
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import parseaddr
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent
TOKEN_FILE = BASE / ".gmail_token.json"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
OPENAI_API = "https://api.openai.com/v1/responses"
SCOPE = "https://www.googleapis.com/auth/gmail.modify"
REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "https://khalid-ai-agent.onrender.com/oauth/callback")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")

app = FastAPI(title="AI Agent Khalid", version="0.5.3")

SETTINGS = {
    "classify": True,
    "suggest": True,
    "autosend": False,
    "alerts": True,
    "review_spam": True,
    "approval_required": True,
}

OAUTH_STATES = set()
CACHE = {"messages": [], "updated_at": None, "email": None, "ai_mode": "rules"}

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
    review_spam: bool = True


class ActionPayload(BaseModel):
    action: str
    draft_body: Optional[str] = None


def client_configured() -> bool:
    return bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET"))


def ai_configured() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def load_token() -> Optional[dict]:
    env_refresh = os.getenv("GOOGLE_REFRESH_TOKEN", "").strip()
    token = None
    if TOKEN_FILE.exists():
        try:
            token = json.loads(TOKEN_FILE.read_text())
        except Exception:
            token = None
    if token is None and env_refresh:
        token = {"refresh_token": env_refresh}
    elif token is not None and env_refresh and not token.get("refresh_token"):
        token["refresh_token"] = env_refresh
    return token


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
    if not token:
        raise HTTPException(401, "Gmail not connected")
    if not token.get("access_token") and token.get("refresh_token"):
        token = await refresh_access_token(token)
    if not token.get("access_token"):
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


def direct_reply_signal(text: str) -> bool:
    """Return True when the sender is clearly waiting for an answer/decision.

    This rule has priority over generic follow-up keywords such as appointment/invoice.
    """
    text = (text or "").lower()
    request_words = [
        "?", "can you", "could you", "would you", "will you", "please confirm", "please let me know",
        "do you want", "would you like", "are you available", "what time", "which time",
        "book for you", "schedule for you", "appointment for you", "need your approval", "need your confirmation",
        "هل", "هل يمكن", "هل تريد", "هل ترغب", "هل يناسب", "هل تفضل", "ممكن", "ممكن أحجز", "ممكن احجز",
        "احجز لك", "أحجز لك", "موعد لك", "أي وقت", "اي وقت", "متى يناسب", "أكد", "تأكيدك", "موافقتك",
        "يرجى التأكيد", "الرجاء التأكيد", "أفدني", "افدني", "ردك", "تحتاج رد",
    ]
    return any(w in text for w in request_words)


def classify_message(sender: str, subject: str, snippet: str) -> str:
    text = f"{sender} {subject} {snippet}".lower()
    # Highest priority: a direct question/request means the sender is waiting for a reply.
    if direct_reply_signal(text):
        return "reply"
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
    return "follow"


def fallback_spam_legit(sender: str, subject: str, snippet: str) -> bool:
    text = f"{sender} {subject} {snippet}".lower()
    spammy = ["unsubscribe", "casino", "lottery", "crypto giveaway", "viagra", "winner", "marketing", "newsletter"]
    human_signal = ["?", "hello", "hi ", "مرحبا", "السلام", "أحتاج", "احتاج", "ممكن", "please", "meeting", "موعد"]
    return not any(x in text for x in spammy) and any(x in text for x in human_signal)


def decision_text(kind: str, source: str = "inbox") -> str:
    prefix = "هذه الرسالة وصلت إلى Spam/Junk لكنها تبدو مهمة. " if source == "spam" else ""
    if kind == "reply":
        return prefix + "تحتاج ردًا. يمكن للوكيل تجهيز مسودة، ولن يتم إرسال أي شيء تلقائيًا."
    if kind == "follow":
        return prefix + "لا يوجد رد تلقائي. الرسالة تحتاج مراجعتك أو متابعة منك."
    return prefix + "لا تبدو الرسالة بحاجة إلى رد أو متابعة. يمكن تركها أو أرشفتها."


def summary_text(sender: str, subject: str, snippet: str) -> str:
    clean = re.sub(r"\s+", " ", snippet or "").strip()
    if len(clean) > 220:
        clean = clean[:217] + "..."
    return f"{subject or 'بدون عنوان'} — {clean}" if clean else f"رسالة من {sender}."


def decode_b64url(data: str) -> str:
    if not data:
        return ""
    try:
        pad = "=" * (-len(data) % 4)
        raw = base64.urlsafe_b64decode(data + pad)
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def extract_message_text(payload: dict) -> str:
    texts = []
    def walk(part: dict):
        mime = (part.get("mimeType") or "").lower()
        body = part.get("body") or {}
        data = body.get("data")
        if data and mime in {"text/plain", "text/html"}:
            text = decode_b64url(data)
            if mime == "text/html":
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"&nbsp;", " ", text, flags=re.I)
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                texts.append(text)
        for child in part.get("parts", []) or []:
            walk(child)
    walk(payload or {})
    if not texts:
        return ""
    # Prefer the first plain-text-like content and avoid repeating multipart alternatives.
    return max(texts, key=lambda x: min(len(x), 12000))[:12000]


def extract_output_text(data: dict) -> str:
    for item in data.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return content.get("text", "")
    return data.get("output_text", "") or ""


async def ai_analyze(sender: str, subject: str, snippet: str, body_text: str, source: str) -> Optional[dict]:
    if not ai_configured():
        return None
    prompt = f"""You are a careful executive email assistant for one private Gmail inbox.
Return ONLY valid compact JSON with keys: classification, summary_ar, draft_reply, surface_from_spam, language, tone.
classification must be one of reply, follow, none.
summary_ar must be a short factual Arabic summary.
language must be the main language of the sender's message such as ar or en.
tone must be one of: formal, neutral, friendly.
draft_reply must be a polished ready-to-send reply ONLY when classification=reply; otherwise empty.

Classification priority (IMPORTANT):
- If the sender asks a direct question, asks for approval/confirmation/decision, asks whether to book or schedule an appointment, asks availability, or otherwise clearly waits for an answer, classification MUST be reply.
- A message about an appointment is follow ONLY when it is merely an informational reminder/status and does not ask the user to answer.
- Security alerts, invoices, notices, or status updates that need the user's review but no direct answer are follow.
- Promotions/automated notices that require nothing are none.

Reply rules:
- Reply in the same language as the sender unless the message clearly asks for another language.
- Match the sender's tone while staying professional and concise.
- Answer the actual request directly when the available message content allows it.
- Do not use generic filler like 'I will review and get back to you' unless the message truly requires later review.
- Never invent facts, prices, dates, approvals, attachments, actions already taken, or commitments not supported by the message.
- If key information is missing, ask one concise clarifying question instead of guessing.
- Do not claim that an email was sent, a payment was made, a file was attached, or a task was completed.
- Do not include a subject line in draft_reply.
- Keep normal replies roughly 2-6 sentences unless the incoming email clearly requires more detail.

surface_from_spam must be true only if a message found in spam appears legitimate or personally relevant; otherwise false.
Do not send anything.

Source folder: {source}
From: {sender}
Subject: {subject}
Snippet: {snippet[:1800]}
Body:
{body_text[:6000]}
"""
    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}", "Content-Type": "application/json"}
    body = {"model": OPENAI_MODEL, "input": prompt}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(OPENAI_API, headers=headers, json=body)
        if r.status_code >= 400:
            return None
        text = extract_output_text(r.json()).strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.I | re.M).strip()
        data = json.loads(text)
        if data.get("classification") not in {"reply", "follow", "none"}:
            return None
        # Deterministic safety net: direct questions/approval/booking requests always need a reply.
        source_text = f"{subject} {snippet} {body_text}"
        if direct_reply_signal(source_text) and data.get("classification") != "reply":
            data["classification"] = "reply"
            if not (data.get("draft_reply") or "").strip():
                data["draft_reply"] = "شكرًا على رسالتك. نعم، يرجى إرسال الخيارات أو المواعيد المتاحة لأؤكد الأنسب منها."
        return data
    except Exception:
        return None


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


async def list_folder(query: str, limit: int) -> list:
    data = await gmail_request("GET", "/messages", params={"q": query, "maxResults": limit})
    return data.get("messages", [])


async def sync_gmail(limit: int = 30) -> list:
    profile = await gmail_request("GET", "/profile")
    CACHE["email"] = profile.get("emailAddress")
    labels = await ensure_labels()

    inbox_rows = await list_folder("in:inbox -in:trash", limit)
    spam_rows = await list_folder("in:spam -in:trash", min(limit, 20)) if SETTINGS.get("review_spam") else []
    rows = [(r, "inbox") for r in inbox_rows] + [(r, "spam") for r in spam_rows]
    seen = set()
    items = []
    ai_label_ids = list(labels.values())
    used_ai = False

    for row, source in rows:
        mid = row["id"]
        if mid in seen:
            continue
        seen.add(mid)
        m = await gmail_request(
            "GET",
            f"/messages/{mid}",
            params={"format": "full"},
        )
        payload = m.get("payload", {})
        sender = get_header(payload, "From")
        subject = get_header(payload, "Subject") or "(بدون عنوان)"
        snippet = m.get("snippet", "")
        body_text = extract_message_text(payload) or snippet

        ai = await ai_analyze(sender, subject, snippet, body_text, source) if SETTINGS["classify"] else None
        if ai:
            used_ai = True
            kind = ai["classification"]
            summary = ai.get("summary_ar") or summary_text(sender, subject, snippet)
            draft = ai.get("draft_reply", "") if kind == "reply" else ""
            surface_spam = bool(ai.get("surface_from_spam", False))
            language = ai.get("language", "")
            tone = ai.get("tone", "")
        else:
            kind = classify_message(sender, subject, snippet) if SETTINGS["classify"] else "follow"
            summary = summary_text(sender, subject, snippet)
            draft = ""
            surface_spam = fallback_spam_legit(sender, subject, snippet)
            language = ""
            tone = ""

        # Do not clutter the dashboard with obvious spam; surface only messages that look legitimate.
        if source == "spam" and not surface_spam:
            continue

        current_labels = set(m.get("labelIds", []))
        remove_ids = [x for x in ai_label_ids if x in current_labels and x != labels[kind]]
        add_ids = [] if labels[kind] in current_labels else [labels[kind]]
        if remove_ids or add_ids:
            await gmail_request("POST", f"/messages/{mid}/modify", json_body={"addLabelIds": add_ids, "removeLabelIds": remove_ids})

        ts = m.get("internalDate")
        received = "حديثًا"
        sort_ts = 0
        if ts:
            try:
                sort_ts = int(ts)
                dt = datetime.fromtimestamp(sort_ts / 1000, tz=timezone.utc)
                received = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass

        items.append({
            "id": mid,
            "thread_id": m.get("threadId"),
            "provider": "gmail",
            "source": source,
            "source_label": "Spam/Junk" if source == "spam" else "Inbox",
            "sender": sender,
            "reply_to": get_header(payload, "Reply-To") or sender,
            "message_id_header": get_header(payload, "Message-ID"),
            "subject": subject,
            "snippet": snippet,
            "classification": kind,
            "classification_label": {"reply": "يحتاج رد", "follow": "متابعة", "none": "لا إجراء"}[kind],
            "received": received,
            "sort_ts": sort_ts,
            "important": kind == "follow",
            "summary": summary,
            "decision": decision_text(kind, source),
            "draft_reply": draft,
            "language": language,
            "tone": tone,
            "ai_analyzed": bool(ai),
        })

    items.sort(key=lambda x: x.get("sort_ts", 0), reverse=True)
    CACHE["messages"] = items
    CACHE["updated_at"] = datetime.now(timezone.utc).isoformat()
    CACHE["ai_mode"] = "openai" if used_ai else "rules"
    return items


def find_cached(message_id: str) -> dict:
    for m in CACHE["messages"]:
        if m["id"] == message_id:
            return m
    raise HTTPException(404, "Message not found")


async def create_gmail_draft(m: dict, body_override: Optional[str] = None) -> dict:
    if m.get("classification") != "reply":
        raise HTTPException(400, "This message is not classified as needing a reply")
    body = (body_override if body_override is not None else (m.get("draft_reply") or "")).strip()
    if not body:
        # Fallback safe draft when no OpenAI key exists.
        body = "مرحبًا،\n\nشكرًا على رسالتك. استلمت رسالتك وسأراجع الموضوع وأعود إليك قريبًا.\n\nتحياتي"
    recipient = parseaddr(m.get("reply_to") or m.get("sender") or "")[1]
    if not recipient:
        raise HTTPException(400, "Could not determine reply recipient")
    subject = m.get("subject", "")
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = recipient
    msg["Subject"] = subject
    if m.get("message_id_header"):
        msg["In-Reply-To"] = m["message_id_header"]
        msg["References"] = m["message_id_header"]
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
    payload = {"message": {"raw": raw}}
    if m.get("thread_id"):
        payload["message"]["threadId"] = m["thread_id"]
    return await gmail_request("POST", "/drafts", json_body=payload)


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

    if token.get("refresh_token") and not os.getenv("GOOGLE_REFRESH_TOKEN"):
        rt = token["refresh_token"]
        safe = (rt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                  .replace(chr(34), "&quot;").replace(chr(39), "&#39;"))
        page = f"""<!doctype html><html lang='ar' dir='rtl'><meta name='viewport' content='width=device-width,initial-scale=1'><title>تثبيت اتصال Gmail</title><style>body{{font-family:-apple-system,BlinkMacSystemFont,Arial;background:#07111f;color:#fff;padding:24px;line-height:1.7}}.card{{max-width:720px;margin:auto;background:#101b2d;border:1px solid #2b3b55;border-radius:22px;padding:22px}}code,input{{direction:ltr}}input{{width:100%;box-sizing:border-box;padding:14px;border-radius:12px;border:1px solid #40516d;background:#081321;color:#fff;font-size:14px}}button,a{{display:inline-block;margin-top:14px;padding:12px 16px;border-radius:12px;background:#377cf6;color:#fff;border:0;text-decoration:none;font-weight:700}}.warn{{color:#f6c85f}}</style><div class='card'><h2>تم ربط Gmail ✅</h2><p>باقي خطوة واحدة حتى يبقى Gmail متصلًا بعد أي تحديث أو إعادة تشغيل في Render.</p><p>في Render أضف متغيرًا جديدًا:</p><p><b>KEY</b><br><code>GOOGLE_REFRESH_TOKEN</code></p><p><b>VALUE</b> — انسخ القيمة التالية:</p><input id='rt' type='password' readonly value='{safe}'><button onclick="navigator.clipboard.writeText(document.getElementById('rt').value);this.textContent='تم النسخ ✓'">نسخ Refresh Token</button><p class='warn'>هذه قيمة سرية. لا ترسلها في المحادثة ولا تضعها في GitHub.</p><p>بعد إضافتها في Render اختر <b>Save, rebuild, and deploy</b>. بعدها لن تحتاج لإعادة ربط Gmail عند كل نشر.</p><a href='/'>العودة إلى AI Agent</a></div></html>"""
        return HTMLResponse(page)
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
    counts = {"all": len(items), "reply": 0, "follow": 0, "none": 0, "spam_review": 0}
    for m in items:
        counts[m["classification"]] += 1
        if m.get("source") == "spam":
            counts["spam_review"] += 1
    return {
        "ok": True,
        "mode": "live" if connected else "setup",
        "gmail": {"connected": connected, "configured": client_configured(), "email": CACHE.get("email")},
        "ai": {"configured": ai_configured(), "mode": CACHE.get("ai_mode", "rules"), "model": OPENAI_MODEL if ai_configured() else None},
        "whatsapp": {"connected": False},
        "instagram": {"connected": False},
        "counts": counts,
        "updated_at": CACHE.get("updated_at"),
    }


@app.post("/api/sync")
async def sync_now():
    items = await sync_gmail()
    return {"ok": True, "count": len(items), "updated_at": CACHE["updated_at"], "ai_mode": CACHE["ai_mode"]}


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
    return find_cached(message_id)


@app.post("/api/messages/{message_id}/action")
async def message_action(message_id: str, payload: ActionPayload):
    m = find_cached(message_id)
    if payload.action == "archive":
        await gmail_request("POST", f"/messages/{message_id}/modify", json_body={"removeLabelIds": ["INBOX"]})
        CACHE["messages"] = [x for x in CACHE["messages"] if x["id"] != message_id]
        return {"ok": True, "message": "تمت أرشفة الرسالة."}
    if payload.action == "move_to_inbox":
        await gmail_request("POST", f"/messages/{message_id}/modify", json_body={"addLabelIds": ["INBOX"], "removeLabelIds": ["SPAM"]})
        m["source"] = "inbox"; m["source_label"] = "Inbox"
        return {"ok": True, "message": "تم نقل الرسالة من Spam/Junk إلى الوارد."}
    if payload.action in {"follow", "no_action"}:
        labels = await ensure_labels()
        kind = "follow" if payload.action == "follow" else "none"
        await gmail_request("POST", f"/messages/{message_id}/modify", json_body={"addLabelIds": [labels[kind]]})
        m["classification"] = kind
        return {"ok": True, "message": "تم تحديث التصنيف."}
    if payload.action == "create_draft":
        d = await create_gmail_draft(m, payload.draft_body)
        return {"ok": True, "message": "تم إنشاء مسودة رد داخل Gmail. لم يتم إرسالها.", "draft_id": d.get("id")}
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
        "gmail": {"state": "connected" if load_token() else "ready_to_connect", "scope": SCOPE, "redirect_uri": REDIRECT_URI},
        "openai": {"state": "connected" if ai_configured() else "optional", "model": OPENAI_MODEL},
        "whatsapp": {"state": "planned"},
        "instagram": {"state": "planned"},
    }


@app.get("/")
def root():
    return FileResponse(BASE / "index.html")


app.mount("/", StaticFiles(directory=BASE, html=True), name="static")
