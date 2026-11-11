# Locked risk ceilings

The immutable ceilings live in `src/config.py`; `load_settings` rejects a
configuration that exceeds them before bot startup. Strategies, learning
artifacts and Telegram commands cannot alter the loaded risk settings. Config
may tighten each bound, including reducing risk, position count, margin use or
cost allowance. The minimum reward/risk floor cannot be reduced. Tests cover
both tighter settings and attempted increases.

The ceilings currently lock risk per trade at 0.25%, daily loss at 3%, weekly
loss at 8%, 20 positions total, 6 per group, margin use at 50%, total open
stop risk at 5%, maximum cost/target at 30%, and reward/risk at no less than
1.2. Any future authorized adjustment requires a reviewed code change.

## DEMO vs REAL profiles (2026-11-11)

The account type is read from MT5 (`account_info().trade_mode`), never from
`config.yaml`. An unknown account type is treated as REAL.

- **REAL**: the locked ceilings above apply unchanged (0.25% per trade, 3% daily,
  8% weekly). Brain dynamic risk on a REAL account is bounded to 0.1–0.25%.
- **DEMO** (`demo_risk` in `config.yaml`, ceilings in `src/risk_profile.py`):
  1% per trade in every mode, 10% daily, 25% weekly. Brain dynamic risk 1–2%.
  Hard DEMO ceilings: 2% per trade, 10% daily, 25% weekly.

Brain dynamic risk is used only when the brain is live (enabled, not shadow,
`gate.json` PASS, live drift guard not triggered), the situation passed the OOD
guard and fewer than `brain.loss_streak_base_risk` consecutive losses occurred.
Scanner, bot and executor all re-clamp the value to the account's bounds.

### Small balances (minimum lot fallback, DEMO only)

If the fixed DEMO risk (1%) is smaller than the broker's minimum lot risk, the bot
trades exactly the minimum lot when that lot risks at most
`demo_risk.min_lot_max_risk_pct` (default 3%, hard ceiling 5%) of equity, re-checked
with stressed costs before sending. Otherwise the signal is skipped with the exact
percentage in the reason. REAL accounts never use this fallback; for a small real
balance use an XM Micro account (1 lot = 1,000 units).
