# ADR 0004: Local live-room HLS gateway

- Status: Accepted
- Note: ADR numbers are duplicated — `0004-signed-client-updates.md` shares the number 0004; both files are retained under their names to avoid breaking links (see A48-P3-1).
- Date: 2026-07-28
- Scope: private desktop client only

## Context

CourseLens needs to show the live state of an already-authorized iCourse course
and let the same course player enter an active room. The implementation must not
send iCourse credentials to the public Worker or expose upstream media URLs,
cookies, bearer values, `Origin`, or `Referer` to the browser.

`SeleiXi/fudan-icourse-liveroom` has no license. It was used only to identify the
existence of the product problem. No source, structure, protocol detail, text,
asset, or test was copied or translated from that repository.

## Decision

Add an independent `LiveRoomService` in the local client. It receives three
narrow adapters from the application composition root:

1. an identity-scoped authorization check;
2. a platform observer that returns a structured live observation and an
   authenticated request factory;
3. a monotonic clock and resolver/transport interfaces that are replaceable in
   security tests.

The public state set is closed: `live`, `upcoming`, `ended`, `denied`,
`offline`, `stale`, and `unknown`. `loading` exists only in browser memory.
Only a fresh platform observation can produce `live`.

Entering a live room consumes a short-lived, one-time grant and sets an
`HttpOnly`, `SameSite=Strict` cookie restricted to the session manifest path.
The browser receives only a local manifest URL. The gateway maps every upstream
HLS object to a short-lived opaque resource ID and rewrites master, media,
segment, initialization-map, and key URIs.

Every upstream hop must be HTTPS on port 443 with no userinfo, resolve only to
global addresses, preserve TLS hostname/SNI validation, and pass the same policy
after DNS resolution and redirects. Redirects are bounded and same-origin by
default. Only an explicit request-header allowlist is forwarded; sensitive
headers are stripped on any redirect-origin change. Responses enforce HLS/media
content types, bounded sizes, and a single bounded byte range.

FLV, unknown formats, downloads, recording, archives, Blob export, permanent
URLs, and authorization bypasses are rejected. This is access minimization, not
DRM.

## Consequences

- Live playback remains local and does not change wire protocol v2.
- A browser developer can still inspect bytes delivered to their own browser;
  the product makes no DRM claim.
- Real platform verification remains `unknown` until a user supplies credentials
  locally during an available live window.
- Rollback is one feature commit sequence: remove the live-room service/routes,
  UI module and CSS hook; no database migration or credential deletion is
  required.
