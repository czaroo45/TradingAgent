# Repository Guidelines

## Project Structure

- `tradingagent/` contains the Python package: configuration and shared models, CCXT market data access, SQLite storage, deterministic analysis, and CLI entry point.
- `tests/` contains pytest unit tests for analysis, market-data validation, and storage.
- `docs/ROADMAP.md` describes planned implementation stages; `README.md` has setup and usage instructions.
- `.env.example` documents supported environment settings. Runtime database files belong in `data/` and are git-ignored.

## Build, Test, and Development Commands

Use Python 3.11 or newer. From the repository root:

```bash
python -m venv .venv
# Activate the environment, then:
python -m pip install -e '.[dev]'
python -m pytest
tradingagent
tradingagent --offline
tradingagent --json
```

The CLI fetches public OHLCV data by default, stores it in SQLite, and prints analyses. `--offline` analyzes previously stored candles. `--json` emits machine-readable output. Windows PowerShell activation is `.\.venv\Scripts\Activate.ps1`; on Linux/Fedora use `source .venv/bin/activate`.

## Coding Style

Follow standard Python conventions: four-space indentation, `snake_case` for modules/functions/variables, `PascalCase` for classes, and type annotations on public interfaces. Keep market data, computed indicators, strategy rules, and risk assumptions separate. Prefer small deterministic functions and immutable domain models. No formatter or linter is configured; keep changes readable and consistent with nearby code.

## Testing Guidelines

Use pytest. Name test files `test_<area>.py` and test functions `test_<behavior>`. Cover normal results and invalid, missing, or insufficient data. Keep unit tests independent of network access; mock exchange behavior when testing fetch/retry paths. Run `python -m pytest` after changes to application logic or storage. Do not claim tests passed unless they ran successfully.

## Security and Trading Boundaries

Never commit API credentials, `.env` files, or generated market databases. Current behavior is analysis-only: do not add live order submission or real-funds management as an implicit side effect. Treat signals as conditional scenarios. Keep exchange rate limiting enabled, validate external data, and enforce risk controls in ordinary code rather than relying on an LLM.

## Commits and Pull Requests

This checkout has no available Git history, so no existing commit-message convention can be confirmed. Use a short imperative summary, optionally scoped (for example, `analysis: validate candle inputs`). Pull requests should explain behavior and configuration changes, list tests actually run, mention known limitations, and link related issues when available. Include sample output only when it helps review a user-visible change.
