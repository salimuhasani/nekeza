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


## Firebase Push (FCM) — notification hata app imefungwa

1. Unda project kwenye [Firebase Console](https://console.firebase.google.com)
2. Ongeza **Web app** → nakili config
3. Project Settings → **Cloud Messaging** → Web Push certificates → Generate key pair (VAPID)
4. Project Settings → **Service accounts** → Generate new private key (JSON)
5. Weka env kwenye Vercel:

```
FIREBASE_API_KEY=
FIREBASE_AUTH_DOMAIN=
FIREBASE_PROJECT_ID=
FIREBASE_MESSAGING_SENDER_ID=
FIREBASE_APP_ID=
FIREBASE_VAPID_KEY=
FIREBASE_CREDENTIALS_JSON={"type":"service_account",...}
```

`FIREBASE_CREDENTIALS_JSON` = JSON yote ya service account **kama string moja**.

Web Push (VAPID) bado inafanya kazi kama backup.

## APK (GitHub Actions)

1. Push code kwenye GitHub
2. Actions → **Build APK** → Run workflow
3. Weka URL ya app (mf. `https://your-app.vercel.app`)
4. Download artifact **nekeza-apk**

Kwa TWA kamili (bila Chrome address bar), weka SHA256 ya signing key:

```
TWA_SHA256_CERT_FINGERPRINTS=AA:BB:...
TWA_PACKAGE_ID=app.nekeza.twa
```

Route `/.well-known/assetlinks.json` inatumika kiotomatiki.
