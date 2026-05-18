package com.jacyhung.mici;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.graphics.Bitmap;
import android.net.http.SslError;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.webkit.ConsoleMessage;
import android.webkit.PermissionRequest;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
import android.webkit.HttpAuthHandler;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.ProgressBar;

public class MainActivity extends Activity {

    private static final String TAG = "Mici";
    private static final String DASHCAM_URL = "https://mici.jacyhung.com";
    private static final String USERNAME = "comma";
    private static final String PASSWORD = "comma";

    private WebView webView;
    private ProgressBar loading;
    private Handler handler = new Handler(Looper.getMainLooper());

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        webView = findViewById(R.id.webview);
        loading = findViewById(R.id.loading);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setUseWideViewPort(true);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        settings.setSupportZoom(false);
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);

        // Allow mixed content for webrtcd localhost connection
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);

        webView.setWebChromeClient(new DashcamWebChromeClient());
        webView.setWebViewClient(new DashcamWebViewClient());

        webView.loadUrl(DASHCAM_URL);
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            hideSystemBars();
        }
    }

    private void hideSystemBars() {
        View decorView = getWindow().getDecorView();
        WindowInsetsController controller = decorView.getWindowInsetsController();
        if (controller != null) {
            controller.hide(WindowInsets.Type.systemBars());
            controller.setSystemBarsBehavior(
                WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            );
        }
    }

    private void autoLogin() {
        String js = "(function() {" +
            "  var u = document.querySelector('input[type=\\'text\\'], input:not([type])');" +
            "  var p = document.querySelector('input[type=\\'password\\']');" +
            "  var b = document.querySelector('button[type=\\'submit\\'], input[type=\\'submit\\']');" +
            "  if (u) { u.value = '" + USERNAME + "'; u.dispatchEvent(new Event('input', {bubbles:true})); }" +
            "  if (p) { p.value = '" + PASSWORD + "'; p.dispatchEvent(new Event('input', {bubbles:true})); }" +
            "  if (b) { b.click(); } else if (u && p) {" +
            "    var f = u.closest('form'); if (f) f.submit();" +
            "  }" +
            "  return {user:!!u, pass:!!p, btn:!!b};" +
            "})();";
        webView.evaluateJavascript(js, result -> Log.d(TAG, "Auto-login result: " + result));
    }

    private void injectViewportScale() {
        // Force mobile/portrait layout regardless of 1600px width
        String js = "(function() {" +
            "  var meta = document.querySelector('meta[name=viewport]');" +
            "  if (!meta) {" +
            "    meta = document.createElement('meta');" +
            "    meta.name = 'viewport';" +
            "    document.head.appendChild(meta);" +
            "  }" +
            "  meta.content = 'width=640, initial-scale=1.0, minimum-scale=1.0, maximum-scale=1.0, user-scalable=no';" +
            "  var style = document.getElementById('dashcam-scale-style');" +
            "  if (!style) {" +
            "    style = document.createElement('style');" +
            "    style.id = 'dashcam-scale-style';" +
            "    style.textContent = " +
            "      /* Force mobile layout by overriding desktop media query */" +
            "      '#body { flex-direction: column !important; }' +" +
            "      '#player-area { flex: 0 0 auto !important; min-width: auto !important; }' +" +
            "      '#list-drawer { flex: 1 !important; border-left: none !important; border-top: 1px solid #27272a !important; min-width: auto !important; }' +" +
            "      '#video-wrap { aspect-ratio: 16/9 !important; width: 100% !important; max-height: 42dvh !important; min-height: 180px !important; flex: none !important; }' +" +
            "      /* Typography scaling */" +
            "      'html, body { font-size: 24px !important; height: 100% !important; margin: 0 !important; overflow: hidden !important; }' +" +
            "      '#app { display: flex !important; flex-direction: column !important; height: 100dvh !important; min-height: 0 !important; }' +" +
            "      '#header { flex-shrink: 0 !important; padding: 16px 20px !important; height: auto !important; }' +" +
            "      '#header span { font-size: 36px !important; }' +" +
            "      '.tab-btn, .cam-btn { padding: 12px 24px !important; font-size: 22px !important; border-radius: 10px !important; }' +" +
            "      '#live-status { font-size: 22px !important; }' +" +
            "      'video { width: 100% !important; height: 100% !important; object-fit: contain !important; display: block !important; }' +" +
            "      '#live-video-wrap { flex: 1 !important; min-height: 0 !important; }' +" +
            "      '#route-list { padding: 12px !important; flex: 1 !important; overflow-y: auto !important; }' +" +
            "      '.route-card { padding: 20px !important; margin-bottom: 12px !important; border-radius: 16px !important; }' +" +
            "      '.route-card .rc-date { font-size: 28px !important; }' +" +
            "      '.route-card .rc-time { font-size: 22px !important; }' +" +
            "      '#search-input { padding: 16px 20px !important; font-size: 24px !important; border-radius: 12px !important; }' +" +
            "      '.seg-chip { padding: 10px 20px !important; font-size: 20px !important; border-radius: 10px !important; }' +" +
            "      '.nav-btn { width: 56px !important; height: 56px !important; font-size: 28px !important; border-radius: 14px !important; }' +" +
            "      '#refresh-btn { padding: 12px 20px !important; font-size: 22px !important; border-radius: 12px !important; }' +" +
            "      '.bm-btn { padding: 8px 16px !important; font-size: 20px !important; }' +" +
            "      '#timeline-row { padding: 8px 12px !important; }' +" +
            "      'input[type=range] { height: 40px !important; }' +" +
            "      '#bookmark-panel { font-size: 22px !important; }';" +
            "    document.head.appendChild(style);" +
            "  }" +
            "})();";
        webView.evaluateJavascript(js, null);
    }

    private class DashcamWebChromeClient extends WebChromeClient {
        @Override
        public boolean onConsoleMessage(ConsoleMessage consoleMessage) {
            Log.d(TAG, "JS: " + consoleMessage.message() + " -- From line " +
                consoleMessage.lineNumber() + " of " + consoleMessage.sourceId());
            return true;
        }

        @Override
        public void onPermissionRequest(PermissionRequest request) {
            // Auto-grant WebRTC permissions
            request.grant(request.getResources());
        }
    }

    private class DashcamWebViewClient extends WebViewClient {
        @Override
        public void onPageStarted(WebView view, String url, Bitmap favicon) {
            loading.setVisibility(View.VISIBLE);
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            loading.setVisibility(View.GONE);
            injectViewportScale();

            // Auto-login after a short delay so form elements exist
            handler.postDelayed(() -> {
                autoLogin();
                // Retry once more in case of slow load
                handler.postDelayed(() -> autoLogin(), 1500);
            }, 800);
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            Log.e(TAG, "WebView error: " + error.getDescription());
        }

        @Override
        public void onReceivedHttpAuthRequest(WebView view, HttpAuthHandler handler, String host, String realm) {
            handler.proceed(USERNAME, PASSWORD);
        }

        @Override
        public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
            // Allow self-signed / Cloudflare origin certificates
            handler.proceed();
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        webView.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        webView.onResume();
        hideSystemBars();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        webView.destroy();
    }
}
