# XM MT5 Trading Bot

The active runtime is local Windows MT5. Linux/Wine deployment files are optional and currently inactive; see [docs/SERVER.md](docs/SERVER.md) for the preserved deployment notes.
The optional server build refreshes history in small rotating batches over the
bot's existing MT5 session; no second terminal connection is opened.

This is a single-owner XM trading bot. Python connects to the locally running MetaTrader 5 terminal through the official `MetaTrader5` package. It scans the broker's discovered symbols, records decisions and trades in SQLite, and provides a localhost dashboard. It never withdraws money. Validate on DEMO before considering any real account.

## Install and connect

1. On Windows, install Python 3.11 or later and the XM MetaTrader 5 terminal. Sign in to the XM **demo** account, enable **Allow algorithmic trading**, and set **Tools > Options > Charts > Max bars in chart** to **Unlimited** for history downloads.
2. From this project folder, create and activate a virtual environment, then install the packages:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and set `MT5_LOGIN`, `MT5_PASSWORD` and `MT5_SERVER`. `MT5_PATH` is optional when MT5 is installed in a standard location. Keep `.env` private; it is ignored by Git. Real accounts are refused unless the account mode is `real` in `config.yaml` and `ALLOW_REAL=true` is set in `.env`.
4. Run `pytest`, then `python scripts/check_connection.py`. Review the account type, margin mode, symbols and spreads. For a read-only local status summary later, run `python scripts/health_check.py`.
5. Run `python scripts/test_order_demo.py` for a read-only dry run. The `--confirm` option places a DEMO test order, waits five seconds and closes it; never use it on a real account.

## Emergency stop and ownership

Create an empty file named `STOP` (or run `pause_bot.bat` / `./pause_bot.sh`) to block new entries while the bot continues managing its positions. A `STOP` file containing `HALT` (written by `stop_bot.bat` / `./stop_bot.sh`) stops the bot completely and is never removed by automatic start; use `start_bot.bat force` to resume. See [docs/UNIVERSAL.md](docs/UNIVERSAL.md) for Windows and Linux commands. The dashboard Pause button creates this file through an expiring command; Resume removes it. The dashboard Stop bot command exits cleanly and leaves broker SL/TP in place. Close all & stop requires confirmation and closes only positions with this bot's `magic_number`. Every close or stop modification checks ownership. Netting accounts are detected and a new order is refused if that symbol already has a position.

## Risk settings in `config.yaml`

- `risk_per_trade_pct`: maximum equity budget at the stop for one trade. Volume rounds down to the broker step; if minimum volume exceeds the budget, the trade is refused.
- `daily_loss_limit_pct` and `weekly_loss_limit_pct`: stop new entries at the configured loss, including floating P/L from open positions.
- `max_trades_per_day`: maximum number of new bot entries during one UTC day. Entries accepted earlier in the same scan cycle count immediately.
- `max_open_positions`: total position limit. `max_positions_per_group`: correlation limit for each asset group. `max_positions_per_currency` limits net FX/metals exposure per currency. `max_positions_per_underlying_group` limits correlated index exposure.
- `max_margin_usage_pct`: maximum projected used margin as a share of equity. Leverage is only a margin constraint; it never sets position size.
- `first_target_r` and `scale_out_pct`: close part of a position at the first favorable R target. If the broker's minimum volume prevents a split, close the full position there.

## Short-horizon trade rules

The bot normally holds a trade for at most `risk.max_hold_minutes` (20 minutes). Its ATR move estimate uses `risk.move_horizon_minutes` (10 minutes), independently of the hold limit. Signals are filtered so M1, M5 and M15 agree, confidence is at least 0.6, and current spread is no more than 1.5 times the symbol's 24-hour median. SL/TP distances use `ATR(M1) * sqrt(risk.move_horizon_minutes)`: `risk.sl_move_mult` (0.8) sets the stop and `risk.tp_move_mult` (1.2) sets the target. The target must retain at least 1.3 times the stop risk after estimated costs, and spread plus commission may use at most 25% of target value.

- `risk.risk_per_trade_pct` (0.25): maximum equity risk at the stop.
- `risk.max_hold_minutes` (20): normal hard holding limit. Positions may extend only under the protected-profit conditions below.
- `risk.max_hold_extended_minutes` (60), `risk.extend_step_minutes` (10), and `scanner.extend_min_score` (0.7): extension ceiling, recheck interval and minimum continuation score.
- `risk.first_target_r` (0.8), `risk.scale_out_pct` (50), `risk.trail_atr_mult` (1.0), `risk.stagnation_minutes` (6), and `risk.stagnation_min_r` (0.2): scale-out, ATR trail and stagnation exit.
- `risk.early_exit_*`: adverse M1 EMA, swing, momentum and tick-volume thresholds. Early exit is considered only between -0.2R and -0.7R.
- `scanner.require_mtf_agreement`, `scanner.mtf_ema_fast`, `scanner.mtf_ema_slow`, and `scanner.min_confidence`: direction and confidence entry filters.
- `scanner.max_spread_vs_median`, `scanner.spread_median_lookback_hours`, and `scanner.spread_median_cache_seconds`: spread anomaly threshold, sample horizon and cache duration.
- `risk.symbol_cooldown_minutes` (5) and `risk.max_trades_per_symbol_per_day` (20): symbol renewal limits.
- `risk.gating_window` (20) and `risk.gating_disable_hours` (24): disable strategies or asset groups whose latest closed-trade expectancy is negative. Active gates appear in the dashboard details.

Extension requires an open profit, completed scale-out, stop at breakeven plus estimated costs or better, M1/M5/M15 continuation agreement and improving M1 momentum. At each configured extension interval the conditions are checked again. A position that fails any condition closes at its time limit; all positions close at the extended ceiling.
- `breakeven_cost_multiplier`: after scale-out, move the stop to breakeven plus estimated round-trip costs.
- `trail_atr_mult`: trail the remaining volume by ATR on its entry timeframe. Stops only move in the protective direction.
- `momentum_lookback_bars`: closed-bar lookback for detecting momentum reversal. `stagnation_minutes` and `stagnation_min_r` close positions that have not progressed enough. `max_hold_minutes` is the normal hard time stop; only a protected winner can extend it, up to `max_hold_extended_minutes`.
- `no_entry_before_close_minutes` and `max_session_gap_hours`: infer recurring symbol session closes from the broker's H1 history; avoid new entries shortly before a close and exit owned positions before longer breaks. All comparisons use UTC. The MT5 Python package does not provide the session schedule table, so the bot infers recurring closes from observed broker bars; gaps without a repeating history pattern cannot be forecast.
- `symbol_cooldown_minutes` and `max_trades_per_symbol_per_day`: limit repeated entries per instrument.
- `min_risk_reward`: minimum take-profit distance divided by stop distance.
- `max_cost_to_target_ratio`: reject a setup when estimated round-trip spread and commission are too large relative to target value.
- `max_spread_points`: optional per-symbol spread ceilings. An empty mapping means no configured spread ceiling.
- `executor.deviation_points`: maximum price deviation sent with an order.

The scanner considers the configured asset groups across the broker's discovered symbol list. It first checks ticks and session/spread conditions, then ranks recent movement and fully analyzes at most `scanner.top_movers` symbols. `universe.refresh_minutes` controls broker symbol/cache refresh, while `scanner.max_seconds_per_cycle` bounds the rotating universe scan; open positions remain included for management. The dashboard's scan panel shows totals, movers, signals, duration and rejection/strategy diagnostics. The bot runs one cycle at a time and subtracts work time from `loop.interval_seconds`, so slow scans do not overlap or add an extra full interval of delay.

`universe.symbol_aliases` contains search patterns, not fixed XM names. The terminal symbol list is always discovered at runtime. XM symbols and symbol suffixes vary by server/account; margin mode is read from `account_info()` and supports hedging and netting safeguards.

## Part 2: analysis, data and backtests

Pure pandas/numpy indicators live in `src/indicators.py`; `src/features.py` builds causal latest-bar values and a coarse regime. The four configured strategies are `trend_pullback`, `mean_reversion`, `breakout` and `volatility_scalp`. Strategy parameters and allowed asset groups are in `config.yaml`.

Download candles from the logged-in local MT5 terminal (read-only):

```powershell
python scripts/download_history.py --symbols liquid
python scripts/download_history.py --symbols universe --from 2024-01-01 --to 2026-11-05
```

MT5 history availability is constrained by **Max bars in chart**. In MT5, open **Tools > Options > Charts** and set **Max bars in chart** to **Unlimited** before downloading long ranges. Cached candles are written as Parquet under `data/history/`; `symbol_specs.json` captures the broker tick and volume specifications used by the simulator.

Run the read-only live scanner or a cached-data backtest:

```powershell
python scripts/run_scan_once.py
python scripts/run_backtest.py --strategies all --symbols liquid --equity 1000 --walk-forward
python scripts/run_backtest.py --strategies all --symbols liquid --equity 10
```

The scanner ranks signals and prints the risk-manager decision; it never submits orders. The backtester enters at the next bar open, applies spread, slippage and configured per-side commission, assumes the stop is hit first when both stop and target fall inside one bar, and reuses the live position sizing and risk gates. Reports are saved under `reports/<UTC timestamp>/` with summary, trade, equity and grouped metric files. Historical and simulated results do not guarantee future performance.

## Part 3: live loop and dashboard

Install the latest dependencies with `python -m pip install -r requirements.txt`. Start the bot and dashboard in separate terminals:

```powershell
.\start_bot.bat
.\start_dashboard.bat
```

The dashboard binds to `127.0.0.1:8501` and reads bot state only from SQLite; it does not open an MT5 connection. `DASHBOARD_PASSWORD` is optional. Set it in `.env` or the Windows environment used to launch the dashboard to require a password. Keep the service on localhost or behind a separately secured tunnel.

The dashboard has **Əsas**, **Hesabat**, **Hazırlıq** and **Ətraflı** tabs. Reports show today, the last seven days and all recorded trades split by strategy, asset group and close reason. The readiness checklist uses the last seven days. Its small-account simulation uses the broker specifications cached under `history.data_dir`; without those cached specifications it reports signals whose size could not be checked. New trades store their estimated spread cost and reports add it to broker-recorded commissions. Older trades pre-dating that field can show commissions only.

`live.mode: shadow` records decisions without sending orders. `live.mode: trade` submits DEMO trades that pass every risk gate. The checked-in configuration selects `trade` and lists all four strategies; change it to `shadow` when you want observation only. Real trading also requires `account.mode: real` and `ALLOW_REAL=true` in `.env`; `ALLOW_REAL=false` refuses it. Keep `.env` private and never commit it.

Open the dashboard at **http://127.0.0.1:8501**. Start launches a detached supervisor, which starts the `.venv` bot and restarts unexpected exits after 10, 30, then 60 seconds. It stops after five restarts in a rolling hour and records a CRITICAL event. Stop bot exits after the current management step and leaves open positions protected by broker SL/TP; the supervisor sees the stop marker and does not restart it. Close all & stop needs the confirmation checkbox and also stops the supervisor. Commands expire after `live.command_ttl_seconds` (60 seconds by default), and duplicate pending commands are rejected. The dashboard shows bot, supervisor, heartbeat and MT5 state plus today's restart count.

The bot uses `data/bot.lock` to refuse a second instance. The supervisor PID is recorded separately in `data/supervisor.pid`. If a process was force-terminated, confirm it is no longer running before removing stale lock files. Repeated cycle errors at `live.max_cycle_errors` exit the child so the supervisor can restart it. Detached stdout/stderr are written to `logs/bot_stdout.log`; uncaught main/thread exceptions and faulthandler tracebacks go to `logs/crash.log`, while regular bot logs remain in `logs/bot.log`.

The active scanner universe is limited to the ten logical weekday symbols in `universe.focus_symbols`; XM broker names are resolved from `universe.symbol_aliases`. On Saturday and Sunday it switches to `universe.weekend_symbols` and adds indices only when their session and quote indicate an open market. Open bot positions are still monitored even if their symbol is outside the active entry universe. History targets are configured by timeframe in `history.ranges_months`; a configured range is a requested target and does not mean the broker has supplied the full range. Check logged actual ranges before pruning old history.

`health.stale_ticks_seconds` controls stale-price reconnect checks for owned open symbols. Three failed checks block new entries and send a critical notice; entries resume after the terminal and tick stream recover. `terminal_info().connected` and `trade_allowed` are checked each cycle. The weekly equity drawdown stop closes bot positions and sets the supervisor stop marker. Telegram settings are optional: `notify.big_loss_usd`, `notify.daily_summary_utc` and `notify.rate_limit_minutes` control notifications. Data pruning, online SQLite backups and idle weekly VACUUM are set by the `db` section. `paths.data_dir` and `paths.logs_dir` choose relative project folders; optional `.env` overrides are `BOT_DATA_DIR` and `BOT_LOG_DIR`.

### Telegram bildirişləri

Bildirişləri qoşmaq üçün Telegram-da **@BotFather** hesabına `/newbot` göndərin və yaratdığı bot tokenini `.env` faylındakı `TELEGRAM_BOT_TOKEN` sətrinə yazın. Öz chat ID-nizi öyrənmək üçün yeni botla söhbətə `/start` göndərin, sonra Telegram Bot API-nin `getUpdates` nəticəsində `message.chat.id` dəyərini tapıb `.env`-də `TELEGRAM_CHAT_ID` kimi yazın. Tokeni heç kimlə paylaşmayın. Hər iki dəyər boşdursa, bildirişlər bağlı qalır. Bağlantını yoxlamaq üçün `python scripts/test_telegram.py` işlədin.

Close reasons in the trades table are `tp`, `partial_tp`, `trail`, `momentum_lost`, `stagnant`, `sl`, `time`, `session_close`, `manual`, and `kill`. Each scale-out is separately recorded in `partial_closes` and shown in the dashboard.

For a VPS, use Windows Task Scheduler to start `start_bot.bat` at system startup, with **Start in** set to this project directory and the same Windows account/environment that can access MT5 and the credentials. Create a separate startup task for `start_dashboard.bat` only if the local dashboard is needed. Keep the dashboard bound to localhost; do not expose its port directly to the internet.

### Serverə köçürmə

1. Serverə kodu Git ilə köçürün; `.env` faylını Git-dən kənar təhlükəsiz üsulla ayrıca yerləşdirin. Heç vaxt `.env`-i repoya əlavə etməyin.
2. DEMO üçün Windows server/VPS istifadə edin, çünki rəsmi MT5 Python paketi yerli Windows terminalı ilə işləyir. MT5 terminalını quraşdırın, DEMO hesabına daxil olun və alqoritmik ticarətə icazə verin.
3. Python 3.11+, `pip install -r requirements.txt`, sonra `.env.example` əsasında `.env` yaradın. `ALLOW_REAL=false` saxlayın.
4. Serverdə layihə qovluğunda `start_bot.bat` başladın. Panel lazımdırsa ayrıca `start_dashboard.bat` başladın. `python scripts/health_check.py` ilə son heartbeat və vəziyyəti yoxlayın.
5. Panelə SSH tuneli ilə təhlükəsiz qoşulun: öz kompüterinizdə `ssh -L 8501:127.0.0.1:8501 WINDOWS_USER@SERVER` işlədin, sonra brauzerdə `http://127.0.0.1:8501` açın. Dashboard portunu internetə açmayın.

`python scripts/health_check.py` serverdəki son qeyd olunmuş MT5 vəziyyətini SQLite-dan oxuyur; MT5-ə yeni bağlantı açmır.
