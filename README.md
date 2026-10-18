# Website to APK Builder

A small Bengali web app that accepts an uploaded site or a website URL and builds a debug Android APK with a WebView.

## What visitors can upload

- A single `index.html` file. This is best for a one-file page.
- A ZIP archive with `index.html` at the ZIP root and the site's CSS, JavaScript, images, and other assets beside it or in subfolders.
- A website URL beginning with `https://` or `http://`. The APK opens that live website, so it needs an internet connection and reflects changes made to the website after the APK is built.

The generated APK is a **debug APK**. It can be installed for testing, but publishing to Google Play requires a signed release build and store-specific preparation.

## Requirements on the build server

- Python 3.10 or newer
- JDK 17
- Gradle 8.11.1
- Android SDK Platform 35 and Android SDK Build-Tools 35.0.0
- Network access on the first build so Gradle can fetch the Android Gradle Plugin and dependencies

This starter pins Android Gradle Plugin 8.9.2 and AndroidX WebKit 1.15.0. Android's compatibility table lists Gradle 8.11.1 and JDK 17 for the AGP 8.9 line, and SDK Build-Tools 35.0.0 for that release. WebKit 1.15.0 supports Android API 23 and newer. The generated app uses `WebViewAssetLoader` to serve packaged files under an HTTPS origin, which supports local CSS, JavaScript, images, and same-origin web APIs. See the [official AGP 8.9 release notes](https://developer.android.com/build/releases/agp-8-9-0-release-notes), [AGP/Gradle compatibility table](https://developer.android.com/build/releases/about-agp), [WebKit release notes](https://developer.android.com/jetpack/androidx/releases/webkit), and [Android local-content WebView guide](https://developer.android.com/develop/ui/views/layout/webapps/load-local-content).

Install the Android SDK using Android Studio or the Android command-line tools, and accept the SDK licenses. Set `ANDROID_HOME` (or `ANDROID_SDK_ROOT`) so Gradle can find it. Make sure `java -version` and `gradle --version` show the versions above.

## Run locally on Windows

Open PowerShell in this folder and run:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:ANDROID_HOME = "$env:LOCALAPPDATA\Android\Sdk"
uvicorn app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000> in a browser. Set `ANDROID_HOME` to your actual Android SDK folder if it is installed elsewhere.

For a Linux server, create and activate a Python virtual environment, install `requirements.txt`, configure `ANDROID_HOME`, then run:

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

Place a reverse proxy such as Nginx or Caddy in front of the app to provide HTTPS and request body limits. The builder has default limits of 50 MB uploaded, 100 MB unpacked, 1,000 ZIP entries, one concurrent build, and a 10-minute build timeout. These can be changed with `MAX_UPLOAD_MB`, `MAX_EXTRACTED_MB`, and `BUILD_TIMEOUT_SECONDS`.

## Before putting this on the public internet

This is a starter, not a hardened public build service. Add user authentication or quotas, per-IP rate limiting, job retention/cleanup, monitoring, and isolated build workers with CPU and memory limits. Keep the Android SDK and Gradle up to date. The service generates debug APKs only.

Uploaded ZIP paths are checked before extraction, and uploaded site files are copied into Android assets rather than executed on the build server. URL mode places the validated address in the generated WebView app; the build server does not fetch the URL. The resulting app can execute the visitor's HTML/JavaScript when installed, so only install APKs from people you trust.
