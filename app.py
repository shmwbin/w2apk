from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Annotated
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "50")) * 1024 * 1024
MAX_EXTRACTED_BYTES = int(os.getenv("MAX_EXTRACTED_MB", "100")) * 1024 * 1024
MAX_ARCHIVE_FILES = 1000
BUILD_TIMEOUT_SECONDS = int(os.getenv("BUILD_TIMEOUT_SECONDS", "600"))
PACKAGE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){1,7}$")
BUILD_LOCK = asyncio.Semaphore(1)

app = FastAPI(title="Website to APK Builder")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def home() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


def _write_android_project(project_dir: Path, app_name: str, package_name: str, start_url: str | None = None) -> None:
    package_path = Path(*package_name.split("."))
    source_dir = project_dir / "app" / "src" / "main" / "java" / package_path
    assets_dir = project_dir / "app" / "src" / "main" / "assets"
    values_dir = project_dir / "app" / "src" / "main" / "res" / "values"
    source_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)
    values_dir.mkdir(parents=True, exist_ok=True)

    (project_dir / "settings.gradle").write_text(
        """pluginManagement {
    repositories { google(); mavenCentral(); gradlePluginPortal() }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories { google(); mavenCentral() }
}
rootProject.name = 'WebsiteApk'
include ':app'
""",
        encoding="utf-8",
    )
    (project_dir / "build.gradle").write_text(
        "plugins { id 'com.android.application' version '8.9.2' apply false }\n",
        encoding="utf-8",
    )
    (project_dir / "gradle.properties").write_text(
        "org.gradle.jvmargs=-Xmx2g -Dfile.encoding=UTF-8\n"
        "android.useAndroidX=true\n",
        encoding="utf-8",
    )
    (project_dir / "app" / "build.gradle").write_text(
        """plugins { id 'com.android.application' }

android {
    namespace '%(package)s'
    compileSdk 35

    defaultConfig {
        applicationId '%(package)s'
        minSdk 23
        targetSdk 35
        versionCode 1
        versionName '1.0'
    }
}

dependencies {
    implementation 'androidx.webkit:webkit:1.15.0'
}
""" % {"package": package_name},
        encoding="utf-8",
    )
    (project_dir / "app" / "src" / "main" / "AndroidManifest.xml").write_text(
        """<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <uses-permission android:name="android.permission.INTERNET" />
    <application
        android:label="@string/app_name"
        android:theme="@android:style/Theme.Material.Light.NoActionBar"
        android:usesCleartextTraffic="true">
        <activity
            android:name=".MainActivity"
            android:configChanges="orientation|screenSize|keyboardHidden"
            android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
    </application>
</manifest>
""",
        encoding="utf-8",
    )
    (values_dir / "strings.xml").write_text(
        f'<resources><string name="app_name">{escape(app_name)}</string></resources>\n',
        encoding="utf-8",
    )
    if start_url is None:
        initial_load = 'webView.loadUrl("https://appassets.androidplatform.net/assets/index.html");'
    else:
        encoded_url = base64.b64encode(start_url.encode("utf-8")).decode("ascii")
        initial_load = (
            'webView.loadUrl(new String(android.util.Base64.decode("'
            + encoded_url
            + '", android.util.Base64.DEFAULT), java.nio.charset.StandardCharsets.UTF_8));'
        )

    (source_dir / "MainActivity.java").write_text(
        f"""package {package_name};

import android.app.Activity;
import android.os.Bundle;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.net.Uri;
import androidx.webkit.WebViewAssetLoader;
import androidx.webkit.WebViewClientCompat;

public class MainActivity extends Activity {{
    private WebView webView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {{
        super.onCreate(savedInstanceState);
        webView = new WebView(this);
        setContentView(webView);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setAllowFileAccessFromFileURLs(false);
        settings.setAllowUniversalAccessFromFileURLs(false);
        final WebViewAssetLoader assetLoader = new WebViewAssetLoader.Builder()
                .addPathHandler("/assets/", new WebViewAssetLoader.AssetsPathHandler(this))
                .build();
        webView.setWebViewClient(new WebViewClientCompat() {{
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {{
                return assetLoader.shouldInterceptRequest(request.getUrl());
            }}

            @Override
            @SuppressWarnings("deprecation")
            public WebResourceResponse shouldInterceptRequest(WebView view, String url) {{
                return assetLoader.shouldInterceptRequest(Uri.parse(url));
            }}
        }});
        webView.setWebChromeClient(new WebChromeClient());
        {initial_load}
    }}

    @Override
    public void onBackPressed() {{
        if (webView != null && webView.canGoBack()) {{
            webView.goBack();
        }} else {{
            super.onBackPressed();
        }}
    }}
}}
""",
        encoding="utf-8",
    )


async def _save_upload(upload: UploadFile, destination: Path) -> int:
    size = 0
    with destination.open("wb") as output:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"Upload is too large. Maximum is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
            output.write(chunk)
    if size == 0:
        raise HTTPException(400, "The uploaded file is empty.")
    return size


def _copy_website(upload_path: Path, original_name: str, assets_dir: Path) -> None:
    assets_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(original_name).suffix.lower()
    if suffix in {".html", ".htm"}:
        shutil.copyfile(upload_path, assets_dir / "index.html")
        return
    if suffix != ".zip" or not zipfile.is_zipfile(upload_path):
        raise HTTPException(400, "Upload one .html file or a .zip containing index.html and its website assets.")

    try:
        with zipfile.ZipFile(upload_path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_FILES:
                raise HTTPException(400, f"ZIP has too many files (maximum {MAX_ARCHIVE_FILES}).")

            total_uncompressed = 0
            safe_entries: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
            for info in entries:
                raw_name = info.filename.replace("\\", "/")
                relative = PurePosixPath(raw_name)
                mode = (info.external_attr >> 16) & 0o170000
                if relative.is_absolute() or ".." in relative.parts or mode == 0o120000:
                    raise HTTPException(400, "ZIP contains an unsafe path or symbolic link.")
                if info.is_dir():
                    continue
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_EXTRACTED_BYTES:
                    raise HTTPException(413, f"Unpacked website is too large. Maximum is {MAX_EXTRACTED_BYTES // (1024 * 1024)} MB.")
                safe_entries.append((info, relative))

            if not any(path.as_posix() == "index.html" for _, path in safe_entries):
                raise HTTPException(400, "ZIP must contain index.html at its root, alongside any CSS, JS, and image files.")

            for info, relative in safe_entries:
                target = (assets_dir / Path(*relative.parts)).resolve()
                if assets_dir.resolve() not in target.parents:
                    raise HTTPException(400, "ZIP contains an unsafe path.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    except zipfile.BadZipFile as exc:
        raise HTTPException(400, "That ZIP file could not be opened.") from exc


def _validate_website_url(raw_url: str) -> str:
    value = raw_url.strip()
    if not value or len(value) > 2048 or any(ord(char) < 32 for char in value) or "\\" in value:
        raise HTTPException(400, "Enter a valid website address beginning with https:// or http://.")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port  # Accessing this validates the port number.
    except ValueError as exc:
        raise HTTPException(400, "Enter a valid website address.") from exc
    if parsed.scheme.lower() not in {"https", "http"} or not hostname or parsed.username or parsed.password:
        raise HTTPException(400, "Website address must use http:// or https:// and cannot contain a username or password.")
    try:
        hostname.encode("idna")
    except UnicodeError as exc:
        raise HTTPException(400, "Website address has an invalid domain name.") from exc
    return value


@app.post("/api/build")
async def build_apk(
    app_name: Annotated[str, Form(min_length=1, max_length=40)],
    package_name: Annotated[str, Form(min_length=3, max_length=180)],
    source_type: Annotated[str, Form()] = "upload",
    website: Annotated[UploadFile | None, File(description="A single HTML file or a ZIP website")] = None,
    website_url: Annotated[str | None, Form()] = None,
) -> FileResponse:
    app_name = app_name.strip()
    package_name = package_name.strip()
    if not app_name:
        raise HTTPException(400, "Enter an app name.")
    if not PACKAGE_PATTERN.fullmatch(package_name):
        raise HTTPException(400, "Package ID must look like com.example.myapp and use letters, numbers, or underscores.")
    if source_type not in {"upload", "url"}:
        raise HTTPException(400, "Choose either file upload or website URL.")
    start_url: str | None = None
    if source_type == "upload" and website is None:
        raise HTTPException(400, "Choose an HTML file or website ZIP to upload.")
    if source_type == "url":
        if not website_url:
            raise HTTPException(400, "Enter the website address.")
        start_url = _validate_website_url(website_url)
    if shutil.which("gradle") is None:
        raise HTTPException(503, "Android build tools are not installed on this server. See README.md for setup.")

    await BUILD_LOCK.acquire()
    job_dir: Path | None = None
    try:
        job_dir = Path(tempfile.mkdtemp(prefix="website-apk-"))
        project_dir = job_dir / "android"
        _write_android_project(project_dir, app_name, package_name, start_url=start_url)
        if source_type == "upload":
            assert website is not None
            upload_path = job_dir / "upload.bin"
            await _save_upload(website, upload_path)
            _copy_website(upload_path, website.filename or "", project_dir / "app" / "src" / "main" / "assets")

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["gradle", "--no-daemon", "--stacktrace", "assembleDebug"],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=BUILD_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(504, "APK build timed out. Try a smaller website or increase BUILD_TIMEOUT_SECONDS.") from exc

        apk_path = project_dir / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
        if result.returncode != 0 or not apk_path.exists():
            details = (result.stderr or result.stdout or "Unknown Gradle error")[-5000:]
            raise HTTPException(500, "APK build failed. Check that JDK 17 and Android SDK Platform 35 are installed.\n\n" + details)

        filename = re.sub(r"[^A-Za-z0-9_-]+", "-", app_name).strip("-") or "website-app"
        return FileResponse(
            apk_path,
            media_type="application/vnd.android.package-archive",
            filename=f"{filename}.apk",
            background=BackgroundTask(shutil.rmtree, job_dir, ignore_errors=True),
        )
    except Exception:
        if job_dir is not None:
            shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        BUILD_LOCK.release()
