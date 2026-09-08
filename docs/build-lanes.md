# Initial implementation lanes

Shared interface: [contract.md](contract.md). All lanes target `/Users/tim/repos/orchestrator` and base `ac5ab50`. Writers may edit and locally commit only their claimed files; no pushes, live worker launches, global configuration edits, physical-device installs, or provider-paid tests. Parent integrates commits and performs explicit smoke tests.

| Lane | Isolation cwd | Claimed files / decision | Next gate | Handoff | Independence |
| --- | --- | --- | --- | --- | --- |
| Control | `/tmp/orchestrator-build.HP9z4E/control` | `apps/control/**`: durable HTTP, SQLite, Codex and Herdr adapters | Typecheck + deterministic tests + fresh read-only review | Local commit and report | Owns control implementation behind contract |
| Voice | `/tmp/orchestrator-build.HP9z4E/voice` | `apps/voice/**`: LiveKit speech bridge | Python lint + mock tests + fresh read-only review | Local commit and report | Consumes HTTP contract only |
| Mobile | `/tmp/orchestrator-build.HP9z4E/mobile` | `apps/mobile/**`: Flutter and Android lifecycle | Flutter analyze + tests + APK build if possible + fresh read-only review | Local commit and report | Consumes HTTP/LiveKit contract only |
| Parent | `/Users/tim/repos/orchestrator` | `packages/worker-tools/**`, root docs/scripts/deployment | Adapter tests + integrated emulator verification | Integrated repository | Does not edit active lane files |
