# AI Agent Khalid — v5.1

نسخة تثبيت اتصال Gmail على Render.

## الجديد
- يدعم `GOOGLE_REFRESH_TOKEN` كمتغير بيئة دائم.
- بعد ربط Gmail لأول مرة، إذا لم يكن المتغير موجودًا، تظهر صفحة تعرض Refresh Token لتنسخه إلى Render.
- بعد حفظه في Render، يبقى Gmail متصلًا بعد Deploy أو Restart.
- يستمر دعم Inbox + Spam/Junk والتصنيف والمسودات من v5.

## Environment Variables
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REDIRECT_URI=https://khalid-ai-agent.onrender.com/oauth/callback`
- `GOOGLE_REFRESH_TOKEN` ← يضاف بعد الربط الأول في v5.1
- `OPENAI_API_KEY` (اختياري)
- `OPENAI_MODEL` (اختياري)

لا تضع أي Secret في GitHub.
