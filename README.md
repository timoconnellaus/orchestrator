# Orchestrator

A personal Android voice/chat interface for coordinating Pi, Codex, and Claude Code sessions in Herdr.

- Flutter Android client over Tailscale
- Self-hosted LiveKit and Python voice worker (OpenAI speech)
- TypeScript durable control module (SQLite)
- Codex app-server reasoning with existing Codex authentication
- Worker MCP tools and Pi extension

Implementation contract: [docs/contract.md](docs/contract.md).

This is under active implementation. Live audio and eight-hour screen-off endurance require explicit integration testing; no production-readiness claim is made.
