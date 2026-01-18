# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

YB-Core (Yield Basis) is a Vyper smart contract project for DeFi yield optimization and leveraged trading on Ethereum. It integrates with Curve Finance infrastructure.

## Commands

### Setup
```bash
poetry install
```

### Testing
```bash
pytest                          # Run all tests
pytest tests/amm/               # Run AMM tests
pytest tests/lt/                # Run leveraged trading tests
pytest tests/dao/               # Run DAO/governance tests
pytest tests_forked/            # Run fork-based integration tests
pytest -n auto                  # Run tests in parallel
pytest tests/amm/test_unitary.py::test_name  # Run single test
```

### Linting
```bash
flake8                          # Uses .flake8 config (max-line-length=160)
```

## Architecture

### Core Contracts (`contracts/`)

- **AMM.vy** - Automated Market Maker with constant leverage mechanism
- **LT.vy** - Leveraged liquidity token implementation
- **Factory.vy** - Creates new LT markets
- **HybridVault.vy** - Combines YB yield with scrvUSD, ERC4626 compatible
- **HybridVaultFactory.vy** - Factory for deploying hybrid vaults
- **VirtualPool.vy** - Virtual pool for price calculations
- **CryptopoolLPOracle.vy** - Price oracle for Curve crypto pool LP tokens

### DAO Contracts (`contracts/dao/`)

- **YB.vy** - Governance token with emission schedule
- **VotingEscrow.vy** - veYB token for voting power (time-locked)
- **GaugeController.vy** - Controls gauge weights for emissions
- **LiquidityGauge.vy** - Distributes rewards to liquidity providers
- **FeeDistributor.vy** - Distributes protocol fees to veYB holders

### Testing (`tests/`)

Uses **Titanoboa** for Vyper contract testing with **Hypothesis** for property-based stateful tests.

Key patterns:
- Session-scoped fixtures in `conftest.py` for expensive contract deployments
- `boa.env.prank(address)` for impersonating accounts
- `boa.load('contracts/X.vy')` to deploy contracts
- `boa.reverts('message')` to assert reverts
- Mock contracts in `contracts/testing/` for isolated tests

Fork tests in `tests_forked/` use `boa.fork(NETWORK)` with RPC URLs from `tests_forked/networks.py`.

## Code Style

- Vyper 0.4.3 syntax
- No blank lines between imports
- flake8 with relaxed line length (160 chars)
