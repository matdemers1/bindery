/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// This file runs in Node. Declared rather than pulling in @types/node for
// one lookup.
declare const process: { env: Record<string, string | undefined> };

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    // @d3cloud/ui installs from a release tarball, so this is not needed for a
    // normal install. It is kept for the case that does need it: linking the
    // library locally (`npm link`, or a `file:` path) to work on both at once.
    // Then its Radix dependencies resolve `react` from *its* node_modules
    // rather than this app's — two copies of React in one tree, and every hook
    // inside a Radix component throws "Cannot read properties of null
    // (reading 'useContext')".
    //
    // It stayed hidden until Tabs, because Tabs is the first Radix-backed
    // component this app uses; Button and Alert are plain React and resolve
    // fine either way. Modal, Select, Checkbox and Tooltip would all have hit
    // it the moment they were adopted.
    //
    // This is enough for the dev server and the build — checked, not assumed:
    // a `--sourcemap` build lists every react and react-dom module as coming
    // from this app's node_modules. The test run needs more; see `test.server`.
    //
    // Like `server.fs.allow` below, this is a consequence of the symlink and
    // not of the library: installing from a git tag copies the package in and
    // npm dedupes React. Harmless to keep either way.
    dedupe: ["react", "react-dom"],
  },
  server: {
    host: true,
    fs: {
      // Only needed while the library is linked from a sibling directory: Vite
      // refuses to serve files outside the project root, so the design system's
      // fonts 403 silently and the app drops to a system fallback — which looks
      // like a styling bug rather than a filesystem one.
      //
      // Installing from the release tarball puts the package inside
      // node_modules, where it needs no exception.
      allow: ["..", "../../d3-design-system/d3-ui"],
    },
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
    server: {
      // Force the linked package and its Radix dependencies through Vite's
      // own transform, which is where `resolve.dedupe` applies. Left external,
      // they are resolved by Node from inside d3-ui and pick up *its* React.
      deps: { inline: [/@d3cloud\/ui/, /@radix-ui/] },
    },
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
