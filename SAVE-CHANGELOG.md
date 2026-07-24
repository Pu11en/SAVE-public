# Changelog

Public-safe snapshot of the SAVE production-agent pattern.
De-identified: no live phone numbers, deployment config, memory, logs, tokens,
or customer data.

## Roadmap

- Pluggable notifier interface (beyond the Inkbox adapter)
- Rate-limit / backoff policy around the cost-gate approval loop
- Additional synthetic red-team fixtures for cross-tier leakage
- Optional Redis-backed approval nonce store

## 0.1.0 — 2026-07-21

### Added

- Tier gate (stranger / user / operator) **failing closed** to least privilege
- Per-user recall isolation with phone-keyed boundaries
- Cost gate with single-use, time-limited Crockford-base32 nonces
- Webhook HMAC signature verification
- Output-cleanup guardrails + self-deploy lock primitives
- Prompt-injection hardening in prompt assembly
- Synthetic test suite — 158 passed, 8 skipped

### Removed (for public release)

- Live phone numbers, bot handles, deployment config, private profile/memory
  files, logs, tokens, and customer/prospect data.
