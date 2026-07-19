# AvianVisitors private internet gateway

This directory publishes the local read-only archive site to invited viewers without exposing the Mac, the detector Pi, the shared Caddy router, or any router port.

## Live endpoint

- URL: <https://avian-visitors.prue-starfield-cloudflare.workers.dev>
- Authentication: Cloudflare Access, one-time PIN, established private-viewer allowlist
- Origin: `http://127.0.0.1:9137`

Unauthenticated requests are intercepted by Cloudflare Access before the Worker executes. A direct no-follow request currently receives HTTP 302 to the Access login; clients that follow the login chain may finish on an Access 403. Any unauthenticated 2xx response is a security failure.

## Architecture

```text
viewer
  -> Cloudflare Access
  -> avian-visitors Worker (GET/HEAD only)
  -> Workers VPC service (HTTP 127.0.0.1:9137 only)
  -> named Cloudflare Tunnel
  -> local archive_site server
```

The Worker forwards only these request headers:

- `Accept`
- `Accept-Language`
- `If-Modified-Since`
- `If-None-Match`
- `If-Range`
- `Range`
- `User-Agent`

Cookies, `Authorization`, Cloudflare Access JWTs, forwarding/IP trust headers, `Origin`, `Referer`, and arbitrary client headers are not forwarded to the local service. Responses use a separate explicit allowlist for representation, Range, validator, CSP, and frame-protection headers; arbitrary backend metadata, origin cookies, implementation headers, and `Location` cannot cross the boundary. Origin redirects (301, 302, 303, 307, and 308) fail closed as a generic 502. Public responses are marked `no-store` and `noindex`.

## Cloud resources

These identifiers are non-secret and make the deployment auditable:

| Resource | Identifier |
|---|---|
| Account | `dcd3474c07647ae62bf05f6215610ef3` |
| Worker | `avian-visitors` |
| Tunnel | `cbf8ec21-3b37-44ae-8555-e0855e1958bb` |
| VPC service | `019f7acf-4661-7533-9d1d-fa166484b4b7` |
| Access app | `20ffa77e-e645-4d55-a14c-37cb49904866` |
| Access allow policy | `d4256e9a-6153-4c2e-8d39-1ac2b793dd98` |

The tunnel connector token lives at `~/.cloudflared/avian-visitors.token`, mode `0600`. It must never be committed, copied into a plist, or placed in process arguments.

## Local supervision

The installed LaunchAgent is:

```text
~/Library/LaunchAgents/com.prue.avian-tunnel.plist
```

The committed copy under `launchd/` matches the active definition. It reads the token with cloudflared's `--token-file` option, exports readiness metrics only on `127.0.0.1:49312`, and is configured with `RunAtLoad` and `KeepAlive`.

Useful checks:

```bash
launchctl print gui/$(id -u)/com.prue.avian-tunnel
curl --fail http://127.0.0.1:49312/ready
wrangler vpc service get 019f7acf-4661-7533-9d1d-fa166484b4b7
status="$(curl -o /dev/null -sS -w '%{http_code}' \
  https://avian-visitors.prue-starfield-cloudflare.workers.dev/)"
test "$status" = 302 || test "$status" = 403
```

The final check deliberately does not follow redirects. It accepts the configured Access denial behaviours and, critically, fails on any unauthenticated 2xx response.

## Development and deployment

```bash
npm install
npm test
npx wrangler deploy --dry-run
npx wrangler deploy
```

`wrangler.jsonc` binds `env.AVIAN_ORIGIN` to the VPC service. Do not replace it with a direct public origin or a binding to the shared Caddy router.

## Verified deployment properties

On 2026-07-19 the production deployment was exercised end-to-end:

- unauthenticated page, API, and audio requests: intercepted by Access (direct HTTP 302 login redirect; no origin 2xx);
- authenticated root page: HTTP 200 with the Listening Garden title;
- authenticated `/api/today`: HTTP 200 JSON;
- authenticated audio Range request: HTTP 206 and exactly the requested bytes;
- authenticated POST: HTTP 405 with `Allow: GET, HEAD`;
- temporary verification policy and service token deleted after testing;
- forced connector termination: launchd assigned a new PID, readiness recovered, and the tunnel reconnected.

## Failure isolation

The publication stack is downstream of ingestion and archiving. A Worker, Access, VPC, Tunnel, or internet outage can make the website unavailable, but must not interrupt detection, analysis, audio forwarding, archive ingestion, or the local tailnet site.
