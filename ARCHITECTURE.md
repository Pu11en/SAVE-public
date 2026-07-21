# Architecture

SAVE is a production-agent pattern for a personal assistant / lead qualifier.

The public snapshot focuses on safety and reliability, not private deployment details.

## Runtime shape

```text
Inbound message
  ↓
Webhook proxy
  - verifies signed requests
  - normalizes payloads
  - exposes health checks
  ↓
Tier gate
  - unknown sender = stranger flow
  - approved sender = assistant flow
  - operator = admin flow
  ↓
Agent prompt assembly
  - loads only the context allowed for that tier
  - blocks cross-user memory leaks
  ↓
Tool/safety layer
  - cost gate before expensive work
  - output cleanup before user-visible text
  - recall isolation by user key
  - self-deploy lock for dangerous operations
  ↓
Reply
```

## Public-safe design choices

- Synthetic phone numbers only.
- No live service names.
- No private memory.
- No secrets or token-shaped examples.
- Tests run without external services.

## Production lessons shown here

1. Agents need identity boundaries before they need more tools.
2. Memory must be scoped before it is useful.
3. Costly or irreversible actions need explicit approval gates.
4. User-visible output needs cleanup because models leak implementation language.
5. Webhooks need signature checks because public endpoints are hostile by default.
