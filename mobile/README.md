# NEKEZA Mobile (Capacitor)

APK ya native — notification inaonyesha **NEKEZA**, si Chrome.

## Mahitaji
1. Firebase → Add app → **Android**
2. Package name: `com.nekeza.app`
3. Download `google-services.json` → weka kwenye `mobile/android/app/google-services.json`
   (GitHub Actions: secret `GOOGLE_SERVICES_JSON`)

## Build (GitHub Actions)
Actions → **Build Capacitor APK** → Run workflow

## Badilisha URL
`capacitor.config.json` → `server.url`
