---
name: Don't normalize room URLs
description: Room URLs must not be modified by the server; the game is responsible for sending consistent URLs
type: feedback
---

Don't strip trailing slashes or query strings from room URLs at write or read time. The game owns the URL format and is expected to send the same URL consistently on every request.

**Why:** User explicitly rejected a server-side normalization approach — the contract is that the game sends the same URL it registered with.

**How to apply:** If a URL lookup fails, the bug is in the caller sending an inconsistent URL, not in missing server-side normalization. Don't add `.rstrip("/")`, `.split("?")[0]`, or any other transforms to URLs before storing or querying rooms. Use `body.source` as-is.
