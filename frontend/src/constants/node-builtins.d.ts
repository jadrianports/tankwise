// Minimal ambient shims for the handful of Node builtin functions used by
// presets.warm.test.ts (a vitest spec that reads .github/workflows/
// keep-warm.yml off disk). This project has no @types/node dependency --
// see this plan's threat register (T-3bv-SC): it deliberately installs no
// packages, so a full @types/node install is out of scope here. These
// declarations cover only the functions actually imported; if a future
// spec needs a fuller Node surface, install @types/node properly instead
// of growing this file.
declare module 'node:fs' {
  export function readFileSync(path: string, encoding: 'utf-8'): string;
}

declare module 'node:url' {
  export function fileURLToPath(url: string): string;
}

declare module 'node:path' {
  export function dirname(path: string): string;
  export function resolve(...paths: string[]): string;
}

interface ImportMeta {
  url: string;
}
