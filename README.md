# SAVE-public

Public-safe snapshot of a production assistant / lead-qualification agent.

This repo is intentionally de-identified. It keeps the engineering proof and removes live phone numbers, bot handles, deployment names, private profile files, memory, logs, tokens, and customer data.

## What it demonstrates

- Tiered identity handling: stranger, approved user, operator.
- Prompt-injection and output-cleanup guardrails.
- Per-user memory isolation.
- Cost gate / approval parsing before paid or risky work.
- Webhook signature verification.
- Self-deploy lock primitives for high-risk operations.
- A runnable synthetic safety test suite.

## Architecture

```text
Message webhook
  ↓
HMAC/auth proxy
  ↓
Tier gate: stranger / user / operator
  ↓
Hermes-style agent loop
  ↓
Safety plugins: output cleanup, recall isolation, cost gate
  ↓
Draft response
  ↓
Synthetic tests prove the guardrails
```

## What is not included

- No live deployment config.
- No real phone numbers.
- No private profile or memory files.
- No credentials.
- No production logs.
- No customer or prospect data.

## Run tests

```bash
python3 -m pytest -q
```

Expected public snapshot result: `158 passed, 8 skipped`.

## Why this is useful proof

Most assistant demos only show happy-path chat. This snapshot shows the production parts that matter when an agent touches real users: identity boundaries, memory isolation, approval gates, webhook trust, output cleanup, and failure containment.
