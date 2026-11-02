# LedgerGuard review screen

Next.js review of synthetic invoices, with local and Cognito-authenticated AWS modes. Node 24 is required.

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
`127.0.0.1`. Do not expose either service to a network. Use the authenticated AWS mode for deployment. Approval records a decision and causes no financial or graph action.

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

## AWS sign-in

Set `LEDGERGUARD_ENV=aws` and supply these server-only origins/identifiers:
`APP_ORIGIN`, `REVIEW_API_URL`, `COGNITO_DOMAIN`, `COGNITO_USER_POOL_ID`, and
`COGNITO_CLIENT_ID`. Origins must be exact HTTPS origins without paths, user
information, queries or fragments. Unset `REVIEW_API_TOKEN` entirely: even an
empty value is forbidden in AWS mode. Invalid configuration returns 503;
`/auth/health` remains an independent, unauthenticated readiness check.

Login uses authorization code with S256 PKCE, state and nonce. The callback is
always the configured `APP_ORIGIN` plus `/auth/callback`. The server verifies
Cognito's ID-token signature, issuer, audience, use, expiry, verified email and
login nonce. Only the ID token is retained, in an HttpOnly/Secure/SameSite=Lax
host-only cookie that expires with it. No refresh token is stored. Sign in again
when the one-hour token expires. The API receives that verified ID token;
reviewer identity comes from its verified Cognito email. Browser code does not
handle tokens. Logout requires a same-origin POST and clears the local session.

`NEXT_OUTPUT=standalone` enables Lambda packaging; ordinary local builds keep
the existing output. The ZIP build copies only standalone output, static assets,
optional public assets and the executable `run.sh`. Keep local environment files
out of `web/` before packaging. Build deployment artifacts on Linux x86_64 so
native runtime libraries match Lambda; CI uses Linux. On a Mac, `make web-zip`
uses Docker with the official Node 24 image and a curated temporary source
workspace. `WEB_BUILD_IMAGE` can select an already prepared Node 24 Linux image
when the default registry is unavailable; no host credentials are mounted. Terraform prepares the web
Lambda and its separate HTTPS API; applying it requires explicit `apply 8b`
approval after reviewing the saved plan.
