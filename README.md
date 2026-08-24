# AI Agent Khalid — v3

نسخة Web App + Backend تجريبي.

## تشغيل محلي

```bash
python -m pip install -r requirements.txt
./start.sh
```

ثم افتح: http://localhost:8000

## الحالة الحالية

- الواجهة تعمل.
- API داخلي يعمل.
- وضع الأمان يمنع الإرسال التلقائي.
- بيانات البريد الحالية Demo فقط.
- Gmail الحقيقي يحتاج Google OAuth لموقع مستقل.

## ربط Gmail الحقيقي لاحقًا

1. إنشاء مشروع Google Cloud.
2. تفعيل Gmail API.
3. إنشاء OAuth 2.0 Client ID من نوع Web application.
4. إضافة Redirect URI الخاص بالموقع المنشور.
5. استخدام scopes مناسبة للقراءة/التصنيف/إنشاء المسودات.
6. تخزين الرموز Tokens على الخادم بشكل آمن.

لا تضع Client Secret أو Refresh Token داخل index.html أو JavaScript في المتصفح.
