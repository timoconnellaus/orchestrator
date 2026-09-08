# Worker messaging adapters

Two tools, one scoped HTTP client:

- `read_inbox`: non-destructive recent messages (tool output capped at 40 KiB).
- `reply_to_orchestrator`: durable progress/question/result/error with an explicit stable reply id. Use `replyTo` from the inbox for a specific answer. Retry a lost acknowledgement with the **same id and payload**.

The control module handles worker wakeup and busy-session delivery. Merely installing these tools does not wake a worker or automatically publish terminal output.

## Install and test

```sh
npm ci
npm run typecheck
npm test
```

## Codex and Claude Code

The stdio MCP launcher is `bin/mcp`. Supply per-process MCP configuration, rather than editing global harness configuration:

```json
{
  "mcpServers": {
    "orchestrator": {
      "command": "/absolute/path/orchestrator/packages/worker-tools/bin/mcp",
      "args": ["--credential-file", "/private/path/worker-token", "--url", "http://127.0.0.1:8787"]
    }
  }
}
```

This is Claude's MCP configuration shape. Codex uses `mcp_servers.orchestrator.command` and `mcp_servers.orchestrator.args` in per-process `-c` overrides. The control module creates these arguments for newly managed workers.

## Pi

```sh
pi -e /absolute/path/orchestrator/packages/worker-tools/pi-extension.ts \
  --orchestrator-credential-file /private/path/worker-token \
  --orchestrator-url http://127.0.0.1:8787
```

The extension registers only for explicitly provisioned sessions. It does not change existing Pi settings, overwrite tools, poll an inbox, or transmit general session history. It supports TUI, RPC, and print modes; notifications are guarded by `hasUI`.

## Credentials

The backend issues the scoped credential. The credential file must belong to the current user, be a regular non-symlink file, have no group/other permissions, and be at most 8 KiB. Contents may be raw token text or JSON `{ "token": "...", "url": "http://127.0.0.1:8787" }`. Keep it outside repositories in a private runtime directory.

For direct launches, `ORCHESTRATOR_WORKER_CREDENTIAL_FILE`, `ORCHESTRATOR_WORKER_TOKEN`, and `ORCHESTRATOR_URL` are supported. Prefer a file: raw credentials should not appear in CLI arguments. Explicit URL flags override environment and file URL.

Redirects are rejected so credentials cannot follow a redirect. Requests have a 15-second deadline, response bodies are capped at 512 KiB, and failure messages do not echo server bodies or credentials. Cancellation does not undo a reply that the backend already accepted.
