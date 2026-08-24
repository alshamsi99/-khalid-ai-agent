from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pathlib import Path
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parent
app = FastAPI(title='AI Agent Khalid', version='0.3.0')

MESSAGES = [
    {
        'id':'google-security','provider':'gmail','sender':'Google <no-reply@accounts.google.com>',
        'subject':'تنبيه أمني','snippet':'تم منح OpenAI الإذن بالوصول إلى بعض بيانات حساب Google.',
        'classification':'follow','classification_label':'متابعة','received':'اليوم','important':True,
        'summary':'تنبيه من Google يفيد بمنح OpenAI صلاحية للوصول إلى بعض بيانات الحساب.',
        'decision':'لا يحتاج رد. يحتاج فقط مراجعة الصلاحيات والتأكد أن الربط تم بموافقتك.'
    },
    {
        'id':'replit-credits','provider':'gmail','sender':'Replit <notifications@replit.com>',
        'subject':'You just got new daily credits','snippet':'Good news—you get fresh credits every day on your Starter plan.',
        'classification':'none','classification_label':'لا إجراء','received':'اليوم','important':False,
        'summary':'إشعار خدمي/ترويجي عن رصيد يومي جديد في خطة Starter.',
        'decision':'لا يحتاج رد أو متابعة. يمكن تركه أو أرشفته.'
    }
]

SETTINGS = {
    'classify': True,
    'suggest': True,
    'autosend': False,
    'alerts': True,
    'approval_required': True
}

class SettingsPayload(BaseModel):
    classify: bool = True
    suggest: bool = True
    autosend: bool = False
    alerts: bool = True

class ActionPayload(BaseModel):
    action: str

@app.get('/api/status')
def status():
    counts = {'all':len(MESSAGES),'reply':0,'follow':0,'none':0}
    for m in MESSAGES:
        counts[m['classification']] += 1
    return {
        'ok': True,
        'mode': 'demo',
        'gmail': {'connected': False, 'label': 'تجريبي — OAuth غير مربوط بعد'},
        'whatsapp': {'connected': False},
        'instagram': {'connected': False},
        'counts': counts,
        'updated_at': datetime.now(timezone.utc).isoformat()
    }

@app.get('/api/messages')
def messages(classification: str | None = None, q: str | None = None):
    items = MESSAGES
    if classification and classification != 'all':
        items = [m for m in items if m['classification'] == classification]
    if q:
        ql = q.lower()
        items = [m for m in items if ql in (m['sender']+' '+m['subject']+' '+m['snippet']).lower()]
    return {'items': items}

@app.get('/api/messages/{message_id}')
def message(message_id: str):
    for m in MESSAGES:
        if m['id'] == message_id:
            return m
    raise HTTPException(404, 'Message not found')

@app.post('/api/messages/{message_id}/action')
def message_action(message_id: str, payload: ActionPayload):
    m = next((x for x in MESSAGES if x['id'] == message_id), None)
    if not m:
        raise HTTPException(404, 'Message not found')
    allowed = {'follow','no_action','archive','approve_draft'}
    if payload.action not in allowed:
        raise HTTPException(400, 'Unsupported action')
    if payload.action == 'approve_draft':
        return {'ok': False, 'requires_live_gmail': True, 'message':'الإرسال غير مفعل في الوضع التجريبي.'}
    return {'ok': True, 'action': payload.action, 'message_id': message_id}

@app.get('/api/settings')
def get_settings():
    return SETTINGS

@app.post('/api/settings')
def save_settings(payload: SettingsPayload):
    SETTINGS.update(payload.model_dump())
    # Safety rail for v3: autosend stays disabled even if UI tries to enable it.
    SETTINGS['autosend'] = False
    SETTINGS['approval_required'] = True
    return SETTINGS

@app.get('/api/integrations')
def integrations():
    return {
        'gmail': {
            'state':'needs_oauth',
            'requirements':['Google Cloud project','Gmail API enabled','OAuth client ID','Authorized redirect URI'],
            'scopes':['gmail.readonly','gmail.modify','gmail.compose']
        },
        'whatsapp': {'state':'planned'},
        'instagram': {'state':'planned'}
    }

@app.get('/')
def root():
    return FileResponse(BASE / 'index.html')

app.mount('/', StaticFiles(directory=BASE, html=True), name='static')
