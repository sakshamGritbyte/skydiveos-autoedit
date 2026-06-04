/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Override the API origin; defaults to same-origin (proxied to the backend). */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
