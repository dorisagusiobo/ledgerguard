# LedgerGuard review screen

Local Next.js review of synthetic invoices. Node 24 is required.

```sh
npm ci
npm run dev
```

The dev/start scripts bind to `127.0.0.1:3000`. Run the Python review API on
`127.0.0.1:8000` (see `../docs/DATABASE.md`). Supply `REVIEW_API_TOKEN` and
optionally `REVIEW_API_URL` to the Next.js server environment; the URL defaults to
`http://127.0.0.1:8000` and is restricted to loopback. The token must match the
API's `REVIEW_API_TOKEN`. The API server also needs its app-role database URL and
`REVIEW_REVIEWER`. Configure these through your environment manager; never commit
credentials. No browser-visible environment setting contains API credentials or
its URL. Only server components and server actions call the API.

**Version 1 access limitation:** anyone who can reach the Next.js app can act as
`REVIEW_REVIEWER`. This is acceptable only because both services run on
`127.0.0.1`. Do not expose either service to a network. Proper sign-in comes in
step 8. Approval records a decision and causes no financial or graph action.

The finding view presents source page numbers and exact text, not PDF images.
Vendor content and AI notes render as plain text, including markup. Amounts and
unit prices remain the exact strings supplied by the API. Required response fields
are checked at runtime; incompatible responses produce a visible contract error.
A failed decision request leaves the draft note in the form. A successful server
action invalidates the review pages and redirects back to the finding.

Security headers are configured in `next.config.ts`. `proxy.ts` adds a fresh CSP
nonce for Next.js hydration on each request. No inline scripts are generally
allowed in production; local development additionally permits eval for Next's
debugger and inline styles for hot reload. There are no external fonts, scripts,
images or analytics. All pages render dynamically with uncached API requests.

```sh
npm run lint
npm run typecheck
npm test
npm run build
npm start
```

Tests cover literal rendering, exact money strings, form validation and draft
retention, runtime contracts, server credential handling and source boundaries.
Source scans exclude installed dependencies and generated build output. CI runs
all checks and a production build without requiring API credentials or a live API.

The current npm audit reports 16 moderate development-only advisories in the
ESLint/minimatch dependency tree. The production dependency audit is clean. The
lint plugins currently require ESLint 9, so an incompatible forced upgrade is
not applied; review their upstream updates before deployment.
