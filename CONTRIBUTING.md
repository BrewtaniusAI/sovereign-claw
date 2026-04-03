# Contributing to Sovereign Claw

## Getting Started

```bash
git clone https://github.com/BrewtaniusAI/sovereign-claw.git
cd sovereign-claw
pip install -e ".[dev]"
sovereign onboard
sovereign doctor
```

## Development Workflow

1. Create a feature branch from `main`
2. Make your changes
3. Run the full check suite:
   ```bash
   make lint        # ruff check
   make typecheck   # mypy strict
   make test        # pytest
   make coverage    # ≥85% required
   ```
4. Open a pull request

## Code Standards

- **Type hints everywhere** — mypy strict mode is enforced
- **Dataclasses over dicts** — Use `@dataclass` for structured data
- **Async where appropriate** — Browser, voice, MCP, channels use asyncio
- **No Any types** — Understand the type, don't escape it
- **Imports at top** — Never import inside functions (except lazy module loading in `__init__.py`)

## Governance Requirements

Every new module must satisfy the Isomorphic Closure Invariant:

1. **Fixed-time convergence** — Include `t_max` bounds, no asymptotic settling
2. **Drift tracking** — Integrate with drift measurement where applicable
3. **Policy gating** — Route through PolicyEngine for all inbound
4. **Proof Vault logging** — Record decisions to the immutable ledger
5. **Refusal pathways** — Include tested refusal behavior (AG-07)
6. **Evaluation harness** — New agents/skills must pass eval before activation (AG-02)

## Adding a New Channel

1. Create a class in `channels/connectors.py` inheriting from `Channel` (in `channels/base.py`)
2. Implement `connect()`, `disconnect()`, `send()`, `on_message()`
3. Register in `CHANNEL_REGISTRY`
4. Add config dataclass in `config.py`
5. Write tests in `tests/test_platform_v3.py`

## Adding a New Skill

1. Define `SkillSpec` with purpose, tools_provided, forbidden_actions
2. Add to bundled skills in `skills.py` or create as managed/workspace skill
3. Skill must pass evaluation harness (AG-02) before activation
4. Include refusal test, adversarial test, and timeout test

## Tests

- Tests live in `tests/`
- Use pytest with the existing conftest
- No external services needed for unit tests
- Coverage must stay ≥85%

## License

By contributing, you agree that your contributions will be licensed under Apache-2.0.
