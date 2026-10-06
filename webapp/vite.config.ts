/// <reference types="vitest/config" />
/**
 * Vite config of the Mini App.
 *
 * - `npm run dev`: the app with the in-memory mock API (mock/), no bot needed. `VITE_API_PROXY=http://127.0.0.1:8080`
 *   sends /api to a running bot instead (set `VITE_DEV_INIT_DATA` to a real initData then).
 * - `npm run build`: dist/ (index.html + assets/), served by the bot from WEBAPP_STATIC_DIR (/app/webapp).
 *   The built index.html gets a Content-Security-Policy: own files, Telegram's script, blob: pictures.
 */
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv, type Plugin } from "vite";
import { mockApi } from "./mock/plugin.ts";

const CSP = [
  "default-src 'self'",
  "script-src 'self' https://telegram.org",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' blob: data:",
  "connect-src 'self'",
  "font-src 'self'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
].join("; ");

function contentSecurityPolicy(): Plugin {
  return {
    name: "dumpbot-csp",
    apply: "build",
    transformIndexHtml(html) {
      return html.replace("<!--csp-->", `<meta http-equiv="Content-Security-Policy" content="${CSP}" />`);
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const proxy = env.VITE_API_PROXY;
  return {
    plugins: [react(), contentSecurityPolicy(), ...(proxy ? [] : [mockApi()])],
    server: {
      host: "127.0.0.1",
      port: 5173,
      proxy: proxy ? { "/api": { target: proxy, changeOrigin: true } } : undefined,
    },
    build: {
      // Telegram's in-app browsers: iOS WKWebView (Safari 15+) and Android System WebView.
      target: ["es2020", "safari15", "chrome90"],
      cssTarget: ["safari15", "chrome90"],
      assetsInlineLimit: 0,
      sourcemap: false,
    },
    test: {
      environment: "jsdom",
      setupFiles: ["src/test/setup.ts"],
      include: ["src/**/*.test.{ts,tsx}", "mock/**/*.test.ts"],
      restoreMocks: true,
    },
  };
});
