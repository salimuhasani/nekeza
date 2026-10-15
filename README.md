# NEKEZA — Vercel + Supabase

## Features
- Mauzo, stock, wafanyakazi (logic ya asili)
- **Thamani ya duka** (cost / retail / profit) kwenye Stock
- **PWA + Web Push**
- **Live Chat** (logged-in) + **Customer Care** (guest)
- **Muda wa akaunti** (admin ongeza siku / set date; boss anaona siku zilizobaki)
- Privacy, Terms, Futa akaunti
- UI ya kisasa (simu + PC)

## Env (Vercel)
```
DATABASE_URL
SECRET_KEY
ADMIN_PASSWORD
COOKIE_SECURE=true
```
Optional: `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `APP_NAME`

## Deploy
GitHub → Vercel import → env vars → Deploy
Login: admin / ADMIN_PASSWORD
