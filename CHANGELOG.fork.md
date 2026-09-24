# Fork changelog

Changes made in the ehd-themis fork on top of upstream Open WebUI. Upstream's release notes stay in
[CHANGELOG.md](CHANGELOG.md), which each upstream sync replaces; fork entries live here so they don't conflict with it.

## [Unreleased]

### Changed

- 🏷️ **IDE autocomplete is forwarded as a task.** `/api/completions` requests that carry no `metadata.task` are now marked `task: "autocomplete"`, so a connection header templated with `{{TASK}}` (e.g. `X-Gateway-Task`) arrives as `autocomplete` instead of empty and gateways can tell autocomplete from interactive chat. The chat-completions fallback for providers without a native `/completions` route is marked the same way. A task set by the caller is kept.
