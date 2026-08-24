# AI Agent Khalid — v4

نسخة ويب مهيأة لربط Gmail الحقيقي عبر Google OAuth.

## Render Environment Variables
أضف داخل Render > Environment:

- `GOOGLE_CLIENT_ID` = OAuth Client ID من Google Cloud
- `GOOGLE_CLIENT_SECRET` = OAuth Client Secret من Google Cloud
- `GOOGLE_REDIRECT_URI` = `https://khalid-ai-agent.onrender.com/oauth/callback`

لا تضع أي سر داخل GitHub.

## Google Cloud
- Gmail API enabled
- OAuth app in Testing
- Scope: `https://www.googleapis.com/auth/gmail.modify`
- Authorized redirect URI: `https://khalid-ai-agent.onrender.com/oauth/callback`
- أضف حساب Gmail كـ Test user أثناء وضع Testing.

## التشغيل
Build command:
`pip install -r requirements.txt`

Start command:
`uvicorn app:app --host 0.0.0.0 --port $PORT`

## الأمان
- الإرسال التلقائي معطل.
- v4 يجلب البريد ويصنف الرسائل ويطبق التصنيفات داخل Gmail.
- التخزين الحالي لرمز OAuth في ملف محلي مناسب للنسخة التجريبية؛ قد تحتاج لإعادة الربط بعد إعادة تشغيل/إعادة نشر Render Free.
