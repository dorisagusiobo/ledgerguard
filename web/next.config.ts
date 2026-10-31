import type { NextConfig } from 'next';

// The proxy adds a fresh nonce for Next's inline hydration on every page.
const config: NextConfig = {
  poweredByHeader: false,
  async headers() {
    return [{source: '/(.*)', headers: [
      {key: 'Content-Security-Policy', value: "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"},
      {key: 'X-Frame-Options', value: 'DENY'},
      {key: 'Referrer-Policy', value: 'no-referrer'},
      {key: 'X-Content-Type-Options', value: 'nosniff'},
    ]}];
  },
};
export default config;
