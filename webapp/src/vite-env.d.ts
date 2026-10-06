/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Dev only: initData to send when the app runs outside Telegram (default "dev", accepted by the mock API). */
  readonly VITE_DEV_INIT_DATA?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
