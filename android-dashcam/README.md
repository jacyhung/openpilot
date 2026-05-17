# Mici Android Automotive App

An Android Automotive WebView wrapper for the openpilot dashcam server.

## Features
- Auto-launches your cloudflared tunnel (`https://mici.jacyhung.com`)
- Auto-enters basic auth credentials (`comma` / `comma`)
- Scales UI elements 2x for large automotive displays (1600x2560)
- Full-screen immersive mode with hidden system bars
- Auto-grants WebRTC camera/microphone permissions
- Handles SSL errors for Cloudflare origin certificates

## Build

```bash
cd android-dashcam
./gradlew assembleDebug
```

Install the APK:
```bash
adb install app/build/outputs/apk/debug/app-debug.apk
```

## Configuration

To change the target URL or credentials, edit:
- `app/src/main/java/com/jacyhung/mici/MainActivity.java`
  - `DASHCAM_URL`
  - `USERNAME`
  - `PASSWORD`

To adjust UI scale, modify the CSS in `injectViewportScale()`.
