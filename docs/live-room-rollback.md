# Live-room rollback

Live-room delivery has no database migration and writes no durable media.

## Code rollback

Revert the documentation, mobile header, frontend/player, and backend commits
in reverse order. Keep independently merged client auto-update changes. Remove
only the live-room runtime and hooks, `/api/v3/live-room/*`, the live UI module
and card, local `hls.js`, and live-room tests/docs.

Restart the client. In-memory grants, resource maps, and playback cookies then
expire or disappear with the process/browser session.

## Data safety

Do not delete `runtime/data`, the DPAPI credential store, course catalog,
learning records, Worker configuration, or update configuration. Rollback does
not require credential rotation because this feature persists no live
credentials.
