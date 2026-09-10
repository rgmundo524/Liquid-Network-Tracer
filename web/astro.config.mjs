import { defineConfig } from "astro/config";

// The Python loopback server serves dist/ and the API on the same origin.
export default defineConfig({
  output: "static",
  devToolbar: { enabled: false },
  vite: { build: { sourcemap: false } },
});
