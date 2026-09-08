# Worker tools

The Pi extension uses per-launch flags/environment only; absence of a worker credential disables its tools. This deliberately replaces the usual global ConfigLoader/enabled setting so unrelated Pi sessions are untouched.

Keep the Pi extension independent of the MCP runtime: it imports the shared HTTP client, not the MCP server. Follow the installed `@earendil-works/pi-coding-agent` documentation; older `@mariozechner` examples have incompatible signatures.

When changing message semantics, read `../../docs/contract.md`. Replies need stable idempotency keys; cancellation or network failure does not prove a POST was rejected. Test through the client and MCP interfaces without contacting live workers.
