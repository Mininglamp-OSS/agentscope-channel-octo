# Changelog

## 0.1.0 — 2026-10-16

Initial release.

- `OctoChannel`: WuKongIM v4 long connection (X25519 + AES-128-CBC), REST
  outbound, DM / group / thread, @mention gating, quoted replies, images and
  files in both directions, typing indicator, online heartbeat.
- Tool approvals as `octo/v2` interactive cards; clicks are polled from
  `POST /v1/bot/events` and resume the parked run.
- Platform tools for the agent: `ListOctoGroups`, `SendOctoMessage`.
- Built for AgentScope's channel-worker split (`run_channel_worker` +
  `create_app(enable_channel_worker=False)`): every REST method works on an
  instance that never listened.
