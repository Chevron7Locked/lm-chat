// eslint.config.js — ESLint 9 flat config
import js from "@eslint/js";
import tsParser from "@typescript-eslint/parser";
import tsPlugin from "@typescript-eslint/eslint-plugin";
import reactHooksPlugin from "eslint-plugin-react-hooks";
import globals from "globals";

// Custom rule: require `/* SPDX-License-Identifier: Apache-2.0 */` as the
// first token in every .ts/.tsx file. Implemented inline; eslint-plugin-header
// v3 is incompatible with ESLint 9 flat config (missing schema definition).
const SPDX_TEXT = " SPDX-License-Identifier: Apache-2.0 ";
const spdxHeaderRule = {
  meta: {
    type: "layout",
    fixable: "code",
    schema: [],
    messages: {
      missingSpdx:
        "File must begin with /* SPDX-License-Identifier: Apache-2.0 */",
    },
  },
  create(context) {
    return {
      Program(node) {
        const src = context.sourceCode;
        const comments = src.getAllComments();
        const first = comments[0];
        if (
          !first ||
          first.type !== "Block" ||
          first.value !== SPDX_TEXT ||
          first.range[0] !== 0
        ) {
          context.report({
            node,
            messageId: "missingSpdx",
            fix(fixer) {
              // Insert at offset 0, before any existing content.
              return fixer.insertTextAfterRange([0, 0], `/*${SPDX_TEXT}*/\n`);
            },
          });
        }
      },
    };
  },
};

export default [
  // Ignore generated and built files.
  {
    ignores: [
      "src/types/api.ts",
      "dist/**",
      "node_modules/**",
      // Gitignored ad-hoc manual probe scripts (web/.gitignore) — not
      // tracked source, not wired into any playwright config. Present only
      // on a local dev's disk; excluded from tsconfig.tests.json too.
      "tests/_manual-live/**",
    ],
  },

  // Base JS recommended rules.
  js.configs.recommended,

  // SPDX header enforcement — must be first token in every .ts/.tsx file.
  {
    files: ["src/**/*.{ts,tsx}"],
    plugins: {
      local: { rules: { "spdx-header": spdxHeaderRule } },
    },
    rules: {
      "local/spdx-header": "error",
    },
  },

  // TypeScript source files.
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        project: "./tsconfig.app.json",
        ecmaVersion: "latest",
        sourceType: "module",
      },
      // Browser + ES2024 globals.
      globals: {
        ...globals.browser,
        ...globals.es2024,
      },
    },
    plugins: {
      "@typescript-eslint": tsPlugin,
    },
    rules: {
      // TypeScript strict rules.
      ...tsPlugin.configs["strict-type-checked"].rules,
      ...tsPlugin.configs["stylistic-type-checked"].rules,

      // Allow void-return assertions for event handlers (onSubmit, etc.).
      "@typescript-eslint/no-misused-promises": [
        "error",
        { checksVoidReturn: { attributes: false } },
      ],

      // Allow explicit void for floating promise acknowledgement.
      "@typescript-eslint/no-floating-promises": ["error", { ignoreVoid: true }],

      // Allow spread on specific types (headers object spread is valid).
      "@typescript-eslint/no-misused-spread": "off",

      // React 19 JSX transform — no React import needed.
      // FormEvent is deprecated in @types/react 19; use SyntheticEvent or
      // the event's native type. Warn rather than error for now.
      "@typescript-eslint/no-deprecated": "warn",

      // Permit empty interfaces for future extension.
      "@typescript-eslint/no-empty-object-type": "off",
    },
  },

  // FE-STATE work-stream (2026-07-17): react-hooks/exhaustive-deps enabled
  // as an error, scoped to the files under remediation. Deliberately NOT
  // project-wide — the rest of the codebase has pre-existing effects that
  // would need their own audit before turning this on everywhere. Once a
  // dep array is genuinely exhaustive, a future omission needs an explicit,
  // reviewed eslint-disable rather than a prose comment.
  {
    files: [
      "src/pages/Chat.tsx",
      "src/hooks/useSubSession.ts",
      "src/hooks/useStoppedStreamReconciliation.ts",
      "src/components/Composer.tsx",
    ],
    plugins: {
      "react-hooks": reactHooksPlugin,
    },
    rules: {
      "react-hooks/exhaustive-deps": "error",
    },
  },

  // Scripts / config files that run under Node.
  {
    files: [
      "scripts/**/*.ts",
      "vite.config.ts",
      "vitest.config.ts",
      "playwright.config.ts",
      "playwright.live.config.ts",
      "playwright.dogfood.config.ts",
    ],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        project: "./tsconfig.node.json",
        ecmaVersion: "latest",
        sourceType: "module",
      },
      globals: {
        ...globals.node,
        ...globals.es2024,
      },
    },
    plugins: {
      "@typescript-eslint": tsPlugin,
    },
    rules: {
      ...tsPlugin.configs["strict-type-checked"].rules,
      ...tsPlugin.configs["stylistic-type-checked"].rules,
      "@typescript-eslint/no-floating-promises": ["error", { ignoreVoid: true }],
    },
  },

  // Test files get relaxed rules.
  {
    files: ["tests/**/*.{ts,tsx}"],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        // NOT tsconfig.app.json — that project's `include` is `["src"]`
        // only, which made this block parse-error on every file it
        // matched (tests/** was never actually linted; `lint` = `eslint
        // src` never triggered this block, so the mismatch went unnoticed).
        project: "./tsconfig.tests.json",
        ecmaVersion: "latest",
        sourceType: "module",
      },
      // browser: DOM (jsdom unit tests + Playwright page context).
      // node: Playwright specs/helpers import real Node builtins (fs, path,
      // process, Buffer) and a couple of vitest files use the bare `global`
      // object (tsconfig.tests.json's `types` carries the matching TS-side
      // fix). vitest: `globals: true` in vitest.config.ts already makes
      // vi/describe/it/expect/etc. real runtime globals for these files —
      // this just tells the linter the same thing tsconfig.tests.json's
      // `vitest/globals` entry tells the type-checker.
      globals: {
        ...globals.browser,
        ...globals.node,
        ...globals.vitest,
        ...globals.es2024,
        // @types/react and @types/node both ship an `export as namespace X`
        // UMD global declaration — `React.KeyboardEvent<...>`,
        // `NodeJS.ProcessEnv`, etc. are valid TYPE references with no value
        // import required. no-undef (a core, non-type-aware rule) doesn't
        // know that, and neither `globals.browser` nor `globals.node` (the
        // `globals` npm package's presets) carries these names. Declared
        // read-only, not imported as values — nothing in the test tree
        // reaches for React/NodeJS at runtime, only in type position.
        React: "readonly",
        NodeJS: "readonly",
      },
    },
    plugins: {
      "@typescript-eslint": tsPlugin,
    },
    rules: {
      ...tsPlugin.configs["strict-type-checked"].rules,
      // Tests commonly use `any` for mocks.
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/no-unsafe-assignment": "off",
      "@typescript-eslint/no-unsafe-member-access": "off",
      "@typescript-eslint/no-unsafe-call": "off",

      // no-unnecessary-type-assertion contradicts tsc (6.0.3, inside
      // typescript-eslint's own declared >=4.8.4 <6.1.0 support range — not
      // version skew) on a generic type parameter with a default. Repro:
      //   declare function q<T extends HTMLElement = HTMLElement>(id: string): T;
      //   const el = q("x") as HTMLInputElement;  // eslint: "unnecessary"
      //   const el2 = q("x");                      // tsc, with the cast gone:
      //   el2.value;                                // Property 'value' does not
      //                                              // exist on type 'HTMLElement'
      // Mechanism: for a type query on an overloaded/defaulted generic, the
      // rule's type-equality check resolves T to its default (HTMLElement)
      // instead of the call-site-inferred type, so it can't see that the
      // assertion is real narrowing. screen.getByTestId/getByLabelText and
      // container.querySelector all return exactly this generic-with-default
      // shape, so every DOM-element cast on their result is a false
      // positive. typesToIgnore matches the asserted type's exact source
      // text (see @typescript-eslint/eslint-plugin's
      // no-unnecessary-type-assertion.js), so this lists only the DOM
      // element types actually cast to in tests/** — do not widen it beyond
      // that. Unrelated assertions (Request, RequestInit, literal-narrowing
      // casts like `false as false`, double casts through `unknown`, etc.)
      // are genuine judgement calls and must keep firing.
      "@typescript-eslint/no-unnecessary-type-assertion": [
        "error",
        {
          typesToIgnore: [
            "HTMLInputElement",
            "HTMLInputElement | null",
            "HTMLSelectElement",
            "HTMLTextAreaElement",
            "HTMLButtonElement",
            "HTMLButtonElement | null",
            "HTMLElement",
            "HTMLOptionElement",
            "HTMLAnchorElement",
          ],
        },
      ],
    },
  },
];
