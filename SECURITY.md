# Security Policy — Sovereign Claw

## Reporting Vulnerabilities

Report security vulnerabilities to the maintainers via private disclosure. Do not open public issues for security bugs.

## Security Architecture

### Access Control
- **Allowlist/denylist modes**: Control which users can interact with the system
- **DM pairing**: Cryptographic verification for direct message channels
- **Rate limiting**: Token bucket algorithm prevents abuse
- **Reputation tracking**: Byzantine-tolerant scoring for user trust

### Secret Protection
- **Real-time secret detection**: Scans all messages for API keys, tokens, passwords
- **Automatic redaction**: Detected secrets are replaced with `[REDACTED]` before logging
- **Pattern coverage**: OpenAI, Anthropic, GitHub PAT, Slack, AWS, private keys, JWT, generic passwords

### Audit Trail
- **Proof Vault**: Append-only WORM ledger with SHA-256 chained steps
- **Event Stream**: Immutable JSONL log of all system events
- **Trace IDs**: Every message carries a trace_id for end-to-end audit

### Agent Containment (God File AG-01 through AG-07)
- **Repository-bound intelligence**: Agents cannot persist identity across repos
- **Evaluation before authority**: No output without passing eval harness
- **Role isolation**: No agent can plan + execute + validate simultaneously
- **Agent mortality**: All agent versions have declared termination conditions
- **Tool sovereignty**: Every tool must be explicitly declared and sandboxed
- **Non-proliferation**: Restricted repos must include misuse probes

### Governance
- **PolicyEngine**: All inbound messages pass through governance evaluation
- **ELFE convergence**: Fixed-time stability prevents runaway execution
- **Drift tracking**: Real-time divergence monitoring with automatic correction
- **Silence as containment**: Failed or ambiguous states produce silence, not guesses

## Dependencies

- No external services required for core operation
- All backends are sandboxed with circuit breakers
- Provider credentials are never logged or persisted in plain text

## Docker Security

- Non-root container execution
- Read-only config mounts
- Isolated sandbox profile for untrusted execution
