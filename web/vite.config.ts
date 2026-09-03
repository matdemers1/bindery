/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// This file runs in Node. Declared rather than pulling in @types/node for
// one lookup.
declare const process: { env: Record<string, string | undefined> };

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: true,
    // Dev-only. In the stack, nginx proxies /api to the api container.
    //
    // The default targets the compose service name, which only resolves from
    // inside the network. Running vite on the host instead — which you have to
    // do, because node_modules is built for the host's platform — needs a
    // different target, so it is overridable:
    //
    //   docker run -d --name bindery-apiproxy --network infra_default \
    //     -p 127.0.0.1:8000:8000 alpine/socat \
    //     tcp-listen:8000,fork,reuseaddr tcp-connect:api:8000
    //   BINDERY_API=http://127.0.0.1:8000 npm run dev
    proxy: { "/api": process.env.BINDERY_API ?? "http://api:8000" },
  },
  test: {
    // Only `*.test.*` under src. `e2e/` is Playwright's, and letting vitest
    // collect a spec there would run a browser test in jsdom and fail in a way
    // that looks like the code rather than the runner.
    include: ["src/**/*.test.{ts,tsx}"],
    environment: "jsdom",
    // Imported explicitly from "vitest" instead, so `src` keeps the one
    // ambient type list it has and a test global cannot be reached for from
    // application code by accident.
    globals: false,
    restoreMocks: true,
  },
});
