# AI Agent Khalid — v5

المرحلة الخامسة من وكيل البريد.

## الجديد
- قراءة Inbox ومراجعة Spam/Junk وعرض الرسائل التي تبدو حقيقية فقط.
- تصنيف: يحتاج رد / متابعة / لا إجراء.
- دعم OpenAI اختياري عبر `OPENAI_API_KEY` لتحسين التصنيف وتوليد ردود مقترحة.
- إنشاء **مسودة Gmail فقط** للرسائل التي تحتاج ردًا. لا يوجد إرسال تلقائي.
- زر لنقل الرسالة الحقيقية من Spam/Junk إلى Inbox.

## متغيرات Render الموجودة
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REDIRECT_URI=https://khalid-ai-agent.onrender.com/oauth/callback`

## لإضافة الذكاء الاصطناعي
أضف في Render:
- `OPENAI_API_KEY` = مفتاح OpenAI API الخاص بك
- اختياري: `OPENAI_MODEL=gpt-5.6-luna`

إذا لم تضف مفتاح OpenAI سيعمل التطبيق بقواعد ذكية احتياطية، لكن لن تكون جودة التصنيف وصياغة الردود بنفس المستوى.

## الأمان
الإرسال التلقائي مقفل. زر "إنشاء مسودة" ينشئ Draft داخل Gmail ولا يرسل الرسالة.
