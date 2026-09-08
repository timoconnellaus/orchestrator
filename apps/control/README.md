# Orchestrator control

Node **22.13+** HTTP control module implementing [`../../docs/contract.md`](../../docs/contract.md). Uses Node's built-in SQLite (experimental warning on Node 22), WAL + `synchronous=FULL`, durable operation/message/event transactions, one serialized routing queue, and a single persistent Codex app-server thread. Module-local dependencies; no global CLI configuration writes.

## Install and run

```sh
cd apps/control
npm ci
npm run build
# Deterministic local UI flow; explicitly allow an existing project directory:
ORCHESTRATOR_MODE=mock ORCHESTRATOR_PROJECT_ROOTS=/absolute/project npm start
# Live is the default. Run only when intentionally authorizing new worker/model work:
ORCHESTRATOR_PROJECT_ROOTS=/absolute/project npm start
```

Default listener: `127.0.0.1:8787`; emulator accesses `10.0.2.2:8787`. Set `ORCHESTRATOR_HOST` explicitly for a Tailscale interface. **No application user authentication:** trusted local/tailnet access and Tailscale ACLs are the security boundary. Never expose publicly. Browser Origin/fetch-metadata requests are rejected; no CORS. Worker bearer credentials are rejected on non-worker routes, but a worker with general network access could omit the header and access the unauthenticated user API; scoped credentials are not network isolation.

`npm test`, `npm run typecheck`, `npm run build` run entirely against temporary databases, mock adapters, and executable fake JSONL/Herdr fixtures. They do not contact models, LiveKit servers, or a real Herdr server. Fake provisioning tests run `git init` only in their temporary directory. No persistent test servers remain.

## Configuration

| Variable | Default / meaning |
| --- | --- |
| `ORCHESTRATOR_MODE` | `live`; `mock` is explicit |
| `ORCHESTRATOR_HOST`, `ORCHESTRATOR_PORT` | `127.0.0.1`, `8787` |
| `ORCHESTRATOR_DB` | `.data/orchestrator.sqlite`, relative to process cwd |
| `ORCHESTRATOR_PROJECT_ROOTS` | Empty denies all creation; OS path-delimited existing roots (`:` on macOS). Canonical-directory checks reject symlink escapes and sibling-prefix matches. |
| `HERDR_BIN_PATH`, `HERDR_SOCKET_PATH` | `herdr`, inherited socket selection unless explicitly set |
| `CODEX_BIN`, `CODEX_MODEL` | `codex`, existing CLI default model |
| `ORCHESTRATOR_WORKER_COMMAND` | `/Users/tim/repos/orchestrator/packages/worker-tools/bin/mcp` |
| `ORCHESTRATOR_PI_EXTENSION` | `/Users/tim/repos/orchestrator/packages/worker-tools/pi-extension.ts` |
| `ORCHESTRATOR_URL` | Worker callback URL, derived from host/port; wildcard bind maps to loopback. Override if needed. |
| `LIVEKIT_URL` | Phone-reachable `ws(s)` URL |
| `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | Server-side signing credentials |
| `LIVEKIT_INTERNAL_URL` | Not consumed by control: token signing is local; internal connectivity belongs to voice worker. |

Database/credential directories are private (0700), DB and worker credential files 0600. Store `.data` on a local filesystem, not a network share. Keep DB, WAL, SHM, credentials, and existing Codex rollout history together when backing up; use SQLite's backup tooling or stop control before copying. **Run only one control process per database.** No multi-process lease or journal fanout is implemented. Event history is retained indefinitely; disk retention/archival is an operator responsibility. Recent message endpoints return the latest 200, oldest first.

## HTTP / app integration

All successful JSON is a direct object; errors are `{error:{code,message}}`. JSON bodies are strict, bounded at 64 KiB; text fields 32,000 characters. Client IDs: 1–160 characters, `[A-Za-z0-9][A-Za-z0-9_.:-]*`. Reuse the **same** ID and payload after a lost response; differing payload under the same ID is 409. A new ID explicitly authorizes new work.

- `GET /health`: live Codex status is initially `unavailable` because connection is lazy. No model turn, CLI login, or worker mutation occurs on health checks. It becomes `ready` after successful protocol/thread initialization on an accepted chat, and `unavailable` after a process failure.
- `GET/POST /v1/sessions`; `GET /v1/sessions/:id`; `POST /v1/sessions/:id/messages`.
- `POST /v1/chat`: `{id,text,conversationId:"main",source?:"text"|"voice"}` returns 202 `{operationId}` after persistence. Source defaults to text. `GET /v1/operations/:id` polls terminal states. Chat success output is `{text,messageId}`.
- `GET /v1/messages?conversationId=main` or `session:<sessionId>`.
- `GET /v1/events?after=<seq>`: synchronous replay-then-subscribe prevents a replay/follow gap. `Last-Event-ID` takes precedence. Each SSE data field is the complete Event, not just its data. Heartbeats every 15 seconds; slow consumers disconnect above 1 MiB queued output and must reconnect from their last consumed cursor. Maximum 100 streams. No deltas are emitted: persisted assistant Messages are authoritative.
- `POST /v1/voice/token` with `{conversationId:"main"}` signs an hour-long, unique phone identity, room-scoped token for `orchestrator-main`, dispatching `orchestrator-voice`. Missing URL/key/secret returns 503 `voice_not_configured`. Tokens are never journaled. Control makes no LiveKit network request.

Mock mode supports create/list/detail, queued chat and messages, worker-thread replies and main notifications, SSE, and operation polling. Mock chat echoes `Mock orchestrator: <text>`; mock workers emit deterministic `Mock <agent> result: <text>` replies. Mock completion is generated explicitly, not inferred from idle. Mock workers do not create real worktrees or invoke providers. Voice remains honestly unavailable without actual configuration.

## Queue, replies, and recovery

The queue preserves SQLite insertion order across text, voice, and routing tools. A busy/blocked/unknown/offline worker's sends stay **queued**, allowing unrelated eligible operations to proceed; same-worker sends remain FIFO. Reasoning turns never overlap. Dynamic `create_session` / `send_message` calls durably enqueue and immediately return an operation ID, rather than waiting behind the active turn (which would deadlock). Their IDs are derived from `(threadId, turnId, callId)`, so repeated calls cannot duplicate effects. The model is instructed that queue acceptance is not completion.

A successful creation means a dedicated worker is provisioned, **not that its task finished**; initial instructions are a separate durable send operation. A successful send means delivery acknowledged, not task success. Only explicit worker replies convey results. Worker replies create a worker-thread Message and an atomic main-conversation notification, with no autonomous response loop. Recent notifications enter the next user-requested Codex turn as untrusted context.

On restart, every persisted running operation becomes **uncertain**, never replayed automatically; queued operations remain queued. Subprocess exits, timeouts, malformed side-effect acknowledgements, and post-delivery identity changes become uncertain. Explicit turn/provider rejections become failed. There is no automatic retry/cancel/recover endpoint: inspect an uncertain outcome manually before deciding whether a new client ID is appropriate. A partially created worktree may remain in Herdr and a credential file/session row may remain for diagnosis; control does not delete worktrees or unrelated sessions.

## Worker credentials and per-process launch

Control generates one random scoped token and persists only its SHA-256 hash in SQLite. Raw token is stored as a newline-terminated file at `<DB directory>/worker-credentials/worker-<id>.token`, mode 0600. Token/hash is absent from Session/Operation/Event payloads. Credential **paths** and callback URLs are nonsecret and may be passed as argv; secret values are never passed in argv.

Installed Herdr `worktree create` does **not** accept `--env`, though `workspace create` does. Therefore launch uses the parent-supplied file-argument interfaces without creating a redundant workspace or injecting an `export` shell string:

- Pi: `herdr agent start <name> --kind pi --pane <id> --timeout 30000 -- -e <extension> --orchestrator-credential-file <path> --orchestrator-url <url>`.
- Codex worker: per-process `-c mcp_servers.orchestrator.command=<JSON string>` and `-c mcp_servers.orchestrator.args=["--credential-file",<path>,"--url",<url>]`.
- Claude worker: `--strict-mcp-config --mcp-config <JSON>` with `mcpServers.orchestrator.command` and the same file/URL args.

The parent MCP launcher must accept `--credential-file`/`--url`; Pi extension must register `--orchestrator-credential-file`/`--orchestrator-url`. These complement the parent adapters' environment/file support. No global MCP/extension registration is performed.

Worker interface:

- `POST /v1/worker/register`, bearer token, `{}` → `{sessionId}`; identity is derived solely from token and messaging becomes connected.
- `GET /v1/worker/inbox`, bearer token → bounded non-destructive outbound user messages for this thread. Polling alone does not wake a worker; Herdr remains the delivery adapter.
- `POST /v1/worker/replies`, bearer token, `{id,text,kind:"progress"|"question"|"result"|"error",replyTo?}` → `{message}`. IDs are scoped per worker. `replyTo` must name a message in that exact worker thread, not main or another worker. No caller-supplied session identity accepted. Reply kinds do not override native terminal readiness.

Unmanaged observed Pi/Codex/Claude sessions appear as `messaging:"limited"`; no credentials are retroactively injected and no existing session is reconfigured. A native identity is mandatory for delivery even to limited sessions.

## Herdr safety and known limitations

Inspected installed CLI help and bundled **protocol 20** API schema, plus the existing `herdr-quick-session` startup implementation read-only. New creation uses only argument arrays:

`herdr worktree create --cwd <allowlisted repo> --branch orchestrator/<stable hash> --label <name> --no-focus`

Parses actual `result.workspace.workspace_id`, `result.root_pane.pane_id`, and `result.worktree.path`. Non-Git directories fail before mutation; there is no shared-cwd fallback. Native identity comes from `agent_session.{agent,kind,value}`, not pane ID/name alone. If a workspace layout hook already launched an agent, or the returned shell is unavailable, startup fails closed/uncertain; **no command is typed into that existing agent**. Readiness and native-session reporting depend on installed Herdr integrations. Parent-owned live validation is still required.

**CLI TOCTOU limitation:** Herdr `agent prompt` has no expected-native-session/CAS argument. Control checks pane, workspace, agent, native identity, and idle/done immediately before submission, then checks the returned agent and fresh observation afterward. Any missing/mismatched post-send identity makes the operation uncertain with no retry. These checks cannot eliminate a replacement between the two CLI calls; this is explicitly not atomic identity enforcement. Managed sessions are never silently rebound. Prompt uses `--wait` with a bounded timeout and explicit states to require an observed submission/state change, not stale pre-submission idle. Idle is never taken as proof of task completion.

## Codex safety and protocol

Protocol implementation is pinned/tested against generated experimental schemas from installed **0.153.4**. Uses existing CLI ChatGPT login; no new authentication flow or provider SDK calls. JSONL initialize/initialized, `experimentalApi:true`, thread/start with function dynamic tools, persisted thread/resume ID, turn/start, completed agent items/turns, `item/tool/call` responses are implemented. Delta notifications are intentionally non-authoritative and not forwarded.

Only four dynamic tools: `list_sessions`, `get_session`, `create_session`, `send_message`. Per-process shell_tool/unified_exec and additional execution/browser/plugin/hook/agent features are disabled; inherited MCP servers are disabled through per-thread overrides discovered via config/read. Read-only sandbox, approvalPolicy never, web search disabled, isolated control-owned cwd, no turn environments. Command/file approvals are declined; permission grants empty; all unsupported server requests fail closed. No unrestricted orchestrator shell. This experimental CLI surface needs revalidation on upgrades; do not silently upgrade the protocol assumption.

JSONL frames are bounded at 4 MiB, pending RPCs at 64, concurrent inbound calls at 32. RPC timeout 30 seconds, whole turn 5 minutes, Herdr readiness 30 seconds plus bounded CLI overhead. Process failures kill the owned app-server child and settle pending promises. No automatic replay of an accepted ambiguous turn. HTTP/SSE disconnect or voice playback cancellation never cancels accepted durable work.
