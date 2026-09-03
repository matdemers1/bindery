// Lint for the web app.
//
// The rule that earns its keep here is `react-hooks/exhaustive-deps`. This
// codebase has already shipped a dependency-array feedback loop that reached
// sixty requests a second, and a screen whose polling timer fought its own
// state. Neither was visible in review; both are exactly what this rule says.
//
// Deliberately not type-aware (`recommendedTypeChecked`): `tsc --noEmit` runs
// as its own CI gate and already knows the types. Two tools disagreeing about
// the same question is a way to end up ignoring both.
import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "public"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // Off, deliberately. It is a dev-server ergonomics rule: a file exporting
      // both a component and something else loses fast refresh. `LiveProvider`
      // exports its provider beside the `useLive` and `useLiveQuery` hooks that
      // read it, which is the ordinary shape for that file and not worth
      // splitting for a hot-reload nicety. Left installed so the rule can be
      // turned back on if the tradeoff ever changes.
      "react-refresh/only-export-components": "off",
      // An unused argument named `_` is usually a deliberate placeholder in a
      // callback whose shape is fixed by someone else's API.
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
  {
    // No screen owns a polling timer. That rule is stated in CLAUDE.md and was
    // broken anyway, because the seam had nothing to offer a screen watching
    // work the server does not announce — so the exception now lives in
    // `useLiveQuery` (`unannouncedMs`), where there is one implementation to
    // review, and the rest of the app cannot grow another one unnoticed.
    files: ["src/**/*.{ts,tsx}"],
    ignores: ["src/live/**"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: "CallExpression[callee.name='setInterval']",
          message:
            "Screens do not poll. Use useLiveQuery(topics, load) — and if the server has no topic for what you are watching, useLiveQuery's `unannouncedMs`, which is the one place a repeating timer is allowed to live.",
        },
        {
          selector:
            "CallExpression[callee.property.name='setInterval']",
          message:
            "Screens do not poll. Use useLiveQuery(topics, load) — and if the server has no topic for what you are watching, useLiveQuery's `unannouncedMs`, which is the one place a repeating timer is allowed to live.",
        },
      ],
    },
  },
  {
    // End-to-end specs are Playwright, not React. `test.extend({ use })` is a
    // fixture callback, and `rules-of-hooks` reads the bare name `use` as
    // React's hook — a false positive that would otherwise be silenced with a
    // disable comment on every fixture anyone writes.
    files: ["e2e/**/*.ts"],
    languageOptions: { globals: globals.node },
    rules: {
      "react-hooks/rules-of-hooks": "off",
    },
  },
);
