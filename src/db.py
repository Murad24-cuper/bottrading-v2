"""SQLite persistence with WAL enabled for concurrent dashboard readers."""
from __future__ import annotations
from datetime import datetime,timezone,date,timedelta
from pathlib import Path
import re
import sqlite3
import time

_IDENT_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')
ALLOWED_TABLES = {
    'trades', 'signals', 'daily_stats', 'equity_snapshots', 'bot_events',
    'bot_status', 'commands', 'telegram_updates', 'adaptive_gates',
    'weekend_control', 'partial_closes', 'spread_samples', 'scan_funnel',
    'maintenance_runs', 'daily_report', 'weekly_limits', 'schema_migrations',
    'external_positions'
}

def _validate_identifier(name: str, kind: str = "column") -> str:
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(f"Invalid {kind} identifier: {name!r}")
    return name

def _validate_table(table: str) -> str:
    if table not in ALLOWED_TABLES:
        raise ValueError(f"Invalid table name: {table!r}")
    return table

class _RetryConnection(sqlite3.Connection):
    """Retry transient SQLite lock errors with exponential backoff (up to 4 attempts)."""
    _RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4)  # seconds: 50ms, 100ms, 200ms, 400ms

    @staticmethod
    def _locked(exc):
        return isinstance(exc, sqlite3.OperationalError) and 'locked' in str(exc).lower()

    def execute(self, *args, **kwargs):
        last_exc = None
        for delay in (None,) + self._RETRY_DELAYS:
            if delay is not None:
                time.sleep(delay)
            try:
                return super().execute(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not self._locked(exc):
                    raise
                last_exc = exc
        raise last_exc

    def executemany(self, *args, **kwargs):
        last_exc = None
        for delay in (None,) + self._RETRY_DELAYS:
            if delay is not None:
                time.sleep(delay)
            try:
                return super().executemany(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not self._locked(exc):
                    raise
                last_exc = exc
        raise last_exc

SCHEMA='''
CREATE TABLE IF NOT EXISTS trades(
 id INTEGER PRIMARY KEY, ticket INTEGER UNIQUE, symbol TEXT, asset_group TEXT, direction TEXT,
 volume REAL, open_time TEXT, close_time TEXT, open_price REAL, close_price REAL,
 sl REAL, tp REAL, profit REAL, commission REAL, swap REAL, margin_used REAL,
 strategy TEXT, reason TEXT, magic INTEGER, status TEXT, close_reason TEXT,
 timeframe TEXT, initial_risk REAL, initial_volume REAL, scale_out_done INTEGER DEFAULT 0, peak_favorable_r REAL DEFAULT 0,
 extension_until TEXT, extended_minutes INTEGER DEFAULT 0, estimated_spread_cost REAL DEFAULT 0,
 estimated_commission_cost REAL DEFAULT 0, features TEXT NOT NULL DEFAULT '{}', mode TEXT NOT NULL DEFAULT 'main',
 account_id TEXT);
CREATE TABLE IF NOT EXISTS partial_closes(
 id INTEGER PRIMARY KEY, deal_ticket INTEGER, ticket INTEGER, symbol TEXT, time TEXT, volume REAL,
 price REAL, profit REAL, commission REAL, swap REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS signals(
 id INTEGER PRIMARY KEY, time TEXT, symbol TEXT, strategy TEXT, direction TEXT,
 timeframe TEXT, confidence REAL, entry REAL, sl REAL, tp REAL, expected_cost REAL, score REAL DEFAULT 0,
 taken INTEGER, skip_reason TEXT, features TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS scan_funnel(
 id INTEGER PRIMARY KEY, time TEXT NOT NULL, symbols_scanned INTEGER NOT NULL DEFAULT 0,
 filters_passed INTEGER NOT NULL DEFAULT 0, signals INTEGER NOT NULL DEFAULT 0,
 passed_risk INTEGER NOT NULL DEFAULT 0, orders INTEGER NOT NULL DEFAULT 0,
 filter_rejections TEXT NOT NULL DEFAULT '{}', signal_rejections TEXT NOT NULL DEFAULT '{}',
 risk_rejections TEXT NOT NULL DEFAULT '{}', order_rejections TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS idx_scan_funnel_time ON scan_funnel(time);
CREATE TABLE IF NOT EXISTS spread_samples(
 time TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
 hour_utc INTEGER NOT NULL, spread_price REAL NOT NULL,
 PRIMARY KEY(time,symbol,timeframe));
CREATE INDEX IF NOT EXISTS idx_spread_cell ON spread_samples(symbol,timeframe,hour_utc,time);
CREATE TABLE IF NOT EXISTS daily_stats(
 date TEXT PRIMARY KEY, start_balance REAL, end_balance REAL, pnl REAL, trades_count INTEGER,
 wins INTEGER, losses INTEGER, max_drawdown REAL, invested_margin_peak REAL);
CREATE TABLE IF NOT EXISTS daily_report(date TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS weekly_limits(week_start TEXT PRIMARY KEY, start_balance REAL NOT NULL, stopped INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS maintenance_runs(task TEXT NOT NULL, run_key TEXT NOT NULL, completed_at TEXT NOT NULL, PRIMARY KEY(task,run_key));
CREATE TABLE IF NOT EXISTS equity_snapshots(
 time TEXT, balance REAL, equity REAL, margin REAL, free_margin REAL, open_positions INTEGER);
CREATE TABLE IF NOT EXISTS bot_events(time TEXT, level TEXT, source TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS bot_status(
 id INTEGER PRIMARY KEY CHECK(id=1), time TEXT, cycle INTEGER, mode TEXT, status TEXT,
 account_type TEXT, equity REAL, last_error TEXT, mt5_connected INTEGER DEFAULT 0, scan_total INTEGER,
 scan_scanned INTEGER, scan_complete INTEGER, scan_stage1_passed INTEGER, scan_top_n INTEGER, scan_signals INTEGER,
 scan_seconds REAL, scan_rejections TEXT, strategy_diagnostics TEXT, trade_allowed INTEGER DEFAULT 0, scan_groups_open TEXT);
CREATE TABLE IF NOT EXISTS commands(
 id INTEGER PRIMARY KEY, time TEXT, created_at TEXT, expires_at TEXT, command TEXT, status TEXT, message TEXT, processed_time TEXT);
CREATE TABLE IF NOT EXISTS telegram_updates(update_id INTEGER PRIMARY KEY, received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS account_context(id INTEGER PRIMARY KEY CHECK(id=1),account_id TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS external_positions(
 account_id TEXT NOT NULL, ticket INTEGER NOT NULL, symbol TEXT NOT NULL,
 magic INTEGER NOT NULL, direction TEXT NOT NULL, volume REAL NOT NULL,
 open_time TEXT, open_price REAL, sl REAL, tp REAL, profit REAL, swap REAL,
 comment TEXT, status TEXT NOT NULL DEFAULT 'open', last_seen TEXT NOT NULL,
 PRIMARY KEY(account_id,ticket));
CREATE INDEX IF NOT EXISTS idx_external_positions_open ON external_positions(account_id,status,open_time);
CREATE TABLE IF NOT EXISTS adaptive_gates(
 scope TEXT NOT NULL, name TEXT NOT NULL, disabled_until TEXT NOT NULL, expectancy REAL NOT NULL,
 sample_size INTEGER NOT NULL, reason TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(scope,name));
CREATE TABLE IF NOT EXISTS weekend_control(id INTEGER PRIMARY KEY CHECK(id=1),disabled_until TEXT,expectancy REAL,sample_size INTEGER,updated_at TEXT);
'''

def backup_database(source,folder,keep_days=7,today=None):
    """Consistent copy of the SQLite database (online backup API), one file per UTC day.

    Returns the backup path, or None when today's backup already exists. Backups older
    than keep_days are removed."""
    from datetime import date
    source=Path(source); folder=Path(folder); folder.mkdir(parents=True,exist_ok=True)
    day=(today or datetime.now(timezone.utc).date()).isoformat()
    target=folder/f'{source.stem}-{day}.db'
    if target.exists(): return None
    tmp=target.with_suffix('.tmp')
    # Close both connections before renaming: Windows refuses to move a file that is still open.
    src=sqlite3.connect(source,timeout=10)
    try:
        dst=sqlite3.connect(tmp)
        try: src.backup(dst)
        finally: dst.close()
    finally: src.close()
    tmp.replace(target)
    for old in sorted(folder.glob(f'{source.stem}-*.db'))[:-max(1,int(keep_days))]:
        try: old.unlink()
        except OSError: pass
    return target


class Database:
    def __init__(self,path='data/bot.db'):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True); self.init()
    def connect(self):
        connection=sqlite3.connect(self.path,timeout=5,factory=_RetryConnection)
        connection.row_factory=sqlite3.Row
        connection.execute('PRAGMA busy_timeout=5000')
        return connection
    def init(self):
        with self.connect() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.executescript(SCHEMA)
            trade_columns={row['name'] for row in connection.execute('PRAGMA table_info(trades)')}
            for name, sql_type in {'timeframe':'TEXT','initial_risk':'REAL','initial_volume':'REAL','scale_out_done':'INTEGER DEFAULT 0','peak_favorable_r':'REAL DEFAULT 0','extension_until':'TEXT','extended_minutes':'INTEGER DEFAULT 0','estimated_spread_cost':'REAL DEFAULT 0','estimated_commission_cost':'REAL DEFAULT 0','features':"TEXT NOT NULL DEFAULT '{}'",'mode':"TEXT NOT NULL DEFAULT 'main'",'review_outcome':'TEXT','review_r':'REAL','review_delta':'REAL','review_time':'TEXT'}.items():
                if name not in trade_columns: connection.execute(f'ALTER TABLE trades ADD COLUMN {name} {sql_type}')
            connection.execute('UPDATE trades SET initial_volume=volume WHERE initial_volume IS NULL')
            partial_columns={row['name'] for row in connection.execute('PRAGMA table_info(partial_closes)')}
            if 'deal_ticket' not in partial_columns: connection.execute('ALTER TABLE partial_closes ADD COLUMN deal_ticket INTEGER')
            connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_partial_deal_ticket ON partial_closes(deal_ticket) WHERE deal_ticket IS NOT NULL')
            status_columns={row['name'] for row in connection.execute('PRAGMA table_info(bot_status)')}
            for name, sql_type in {'mt5_connected':'INTEGER DEFAULT 0','scan_total':'INTEGER','scan_scanned':'INTEGER','scan_complete':'INTEGER','scan_stage1_passed':'INTEGER','scan_top_n':'INTEGER','scan_signals':'INTEGER','scan_seconds':'REAL','scan_rejections':'TEXT','strategy_diagnostics':'TEXT','trade_allowed':'INTEGER DEFAULT 0','scan_groups_open':'TEXT'}.items():
                if name not in status_columns: connection.execute(f'ALTER TABLE bot_status ADD COLUMN {name} {sql_type}')
            columns={row['name'] for row in connection.execute('PRAGMA table_info(signals)')}
            if 'score' not in columns: connection.execute('ALTER TABLE signals ADD COLUMN score REAL DEFAULT 0')
            snapshot_columns={row['name'] for row in connection.execute('PRAGMA table_info(equity_snapshots)')}
            if 'login' not in snapshot_columns: connection.execute('ALTER TABLE equity_snapshots ADD COLUMN login INTEGER')
            if 'features' not in columns: connection.execute("ALTER TABLE signals ADD COLUMN features TEXT NOT NULL DEFAULT '{}'")
            command_columns={row['name'] for row in connection.execute('PRAGMA table_info(commands)')}
            if 'created_at' not in command_columns: connection.execute('ALTER TABLE commands ADD COLUMN created_at TEXT')
            if 'expires_at' not in command_columns: connection.execute('ALTER TABLE commands ADD COLUMN expires_at TEXT')
            rows=connection.execute('SELECT id,time,created_at,expires_at FROM commands WHERE created_at IS NULL OR expires_at IS NULL').fetchall()
            for row in rows:
                created=row['created_at'] or row['time'] or datetime.now(timezone.utc).isoformat()
                try:
                    parsed=datetime.fromisoformat(created.replace('Z','+00:00'))
                    parsed=parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
                    expiry=(parsed+timedelta(seconds=60)).isoformat()
                except ValueError:
                    expiry=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat()
                connection.execute('UPDATE commands SET created_at=COALESCE(created_at,?),expires_at=COALESCE(expires_at,?) WHERE id=?',(created,expiry,row['id']))
            migration='profitable_sl_close_reason_to_trail'
            applied=connection.execute('SELECT 1 FROM schema_migrations WHERE name=?',(migration,)).fetchone()
            if not applied:
                connection.execute("UPDATE trades SET close_reason='trail' WHERE status='closed' AND close_reason='sl' AND profit>0")
                connection.execute('INSERT INTO schema_migrations(name,applied_at) VALUES (?,?)',(migration,datetime.now(timezone.utc).isoformat()))
            mig_acc='account_id_on_trades'
            applied_acc=connection.execute('SELECT 1 FROM schema_migrations WHERE name=?',(mig_acc,)).fetchone()
            if not applied_acc:
                cols=[c[1] for c in connection.execute('PRAGMA table_info(trades)').fetchall()]
                if 'account_id' not in cols:
                    connection.execute("ALTER TABLE trades ADD COLUMN account_id TEXT DEFAULT '346494979'")
                connection.execute("UPDATE trades SET account_id='346494979' WHERE account_id IS NULL OR account_id=''")
                connection.execute('INSERT INTO schema_migrations(name,applied_at) VALUES (?,?)',(mig_acc,datetime.now(timezone.utc).isoformat()))
    def reset_account_balance_state(self,old_account_id='346494979'):
        """Reset per-account state depending on old balance while preserving tagged trades."""
        with self.connect() as connection:
            connection.execute("UPDATE trades SET account_id=? WHERE account_id IS NULL OR account_id='' OR account_id='346494979'",(str(old_account_id),))
            connection.execute('DELETE FROM daily_stats')
            connection.execute('DELETE FROM weekly_limits')
            connection.execute('DELETE FROM equity_snapshots')
    def set_active_account_id(self,account_id):
        """Persist the connected MT5 login so subsequent trade rows are tagged correctly."""
        account_id=str(account_id).strip()
        if not account_id: raise ValueError('account_id must not be empty')
        with self.connect() as connection:
            connection.execute('INSERT INTO account_context(id,account_id,updated_at) VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id,updated_at=excluded.updated_at',(account_id,datetime.now(timezone.utc).isoformat()))
    def get_active_account_id(self):
        with self.connect() as connection:
            row=connection.execute('SELECT account_id FROM account_context WHERE id=1').fetchone()
            return str(row['account_id']) if row else None
    def reset_account_state(self,account_id):
        """Clear live account state after its prior database has been archived."""
        tables=('trades','external_positions','partial_closes','signals','scan_funnel','spread_samples','daily_stats','daily_report','weekly_limits','maintenance_runs','equity_snapshots','bot_events','commands','telegram_updates','adaptive_gates','weekend_control')
        account_id=str(account_id).strip()
        if not account_id: raise ValueError('account_id must not be empty')
        with self.connect() as connection:
            for table in tables: connection.execute(f'DELETE FROM {table}')
            connection.execute('DELETE FROM bot_status')
            connection.execute('INSERT INTO account_context(id,account_id,updated_at) VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id,updated_at=excluded.updated_at',(account_id,datetime.now(timezone.utc).isoformat()))
    @staticmethod
    def _insert(connection,table,values):
        _validate_table(table)
        columns=list(values)
        if not columns: raise ValueError('At least one value is required')
        for k in columns: _validate_identifier(k, 'column')
        connection.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",[values[k] for k in columns])
    def insert_trade(self,**values):
        with self.connect() as connection:
            if 'account_id' not in values:
                row=connection.execute('SELECT account_id FROM account_context WHERE id=1').fetchone()
                if row: values['account_id']=str(row['account_id'])
            self._insert(connection,'trades',values)
    def sync_external_positions(self,account_id,positions,seen_at=None):
        """Persist a read-only mirror of non-bot MT5 positions for the dashboard."""
        account_id=str(account_id).strip()
        if not account_id: raise ValueError('account_id must not be empty')
        seen_at=str(seen_at or datetime.now(timezone.utc).isoformat())
        rows=list(positions or [])
        tickets=[]
        with self.connect() as connection:
            for position in rows:
                ticket=int(position['ticket']); tickets.append(ticket)
                values=(account_id,ticket,str(position['symbol']),int(position.get('magic') or 0),
                        str(position['direction']),float(position['volume']),str(position.get('open_time') or ''),
                        float(position.get('open_price') or 0),float(position.get('sl') or 0),
                        float(position.get('tp') or 0),float(position.get('profit') or 0),
                        float(position.get('swap') or 0),str(position.get('comment') or ''),'open',seen_at)
                connection.execute(
                    'INSERT INTO external_positions(account_id,ticket,symbol,magic,direction,volume,open_time,open_price,sl,tp,profit,swap,comment,status,last_seen) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(account_id,ticket) DO UPDATE SET symbol=excluded.symbol,magic=excluded.magic,direction=excluded.direction,volume=excluded.volume,open_time=excluded.open_time,open_price=excluded.open_price,sl=excluded.sl,tp=excluded.tp,profit=excluded.profit,swap=excluded.swap,comment=excluded.comment,status=excluded.status,last_seen=excluded.last_seen',
                    values)
            if tickets:
                placeholders=','.join('?' for _ in tickets)
                connection.execute(f"UPDATE external_positions SET status='closed',last_seen=? WHERE account_id=? AND status='open' AND ticket NOT IN ({placeholders})",[seen_at,account_id,*tickets])
            else:
                connection.execute("UPDATE external_positions SET status='closed',last_seen=? WHERE account_id=? AND status='open'",(seen_at,account_id))
    def update_trade_close(self,ticket,**values):
        values['status']='closed'; columns=list(values)
        for k in columns: _validate_identifier(k, 'column')
        with self.connect() as connection:
            result=connection.execute(f"UPDATE trades SET {','.join(k+'=?' for k in columns)} WHERE ticket=?",[values[k] for k in columns]+[ticket])
            if not result.rowcount: raise KeyError(f'No trade row for ticket {ticket}')
    def update_open_trade(self,ticket,**values):
        if not values: return
        columns=list(values)
        for k in columns: _validate_identifier(k, 'column')
        with self.connect() as connection:
            result=connection.execute(f"UPDATE trades SET {','.join(k+'=?' for k in columns)} WHERE ticket=? AND status='open'",[values[k] for k in columns]+[ticket])
            return result.rowcount
    def get_exits_to_review(self,reasons,closed_before,closed_after,limit=20):
        """Closed bot-decided exits that have not been checked against what the price did next."""
        marks=','.join('?' for _ in reasons)
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                f"SELECT * FROM trades WHERE status='closed' AND review_outcome IS NULL AND close_reason IN ({marks}) AND close_time<=? AND close_time>=? ORDER BY close_time LIMIT ?",
                (*reasons,str(closed_before),str(closed_after),int(limit)))]
    def set_exit_review(self,ticket,outcome,review_r=None,delta=None):
        with self.connect() as connection:
            connection.execute('UPDATE trades SET review_outcome=?,review_r=?,review_delta=?,review_time=? WHERE ticket=?',
                               (str(outcome),review_r,delta,datetime.now(timezone.utc).isoformat(),int(ticket)))
    def get_open_trades(self):
        with self.connect() as connection: return [dict(row) for row in connection.execute("SELECT * FROM trades WHERE status='open' ORDER BY open_time")]
    def count_mode_trades_since(self,mode,start,open_only=False):
        stamp=start.isoformat() if hasattr(start,'isoformat') else str(start)
        clause="status='open'" if open_only else 'open_time>=?'
        if mode=='exploration':
            modes="mode IN ('exploration','weekend_exploration')"; params=() if open_only else (stamp,)
            clause=f'{modes} AND {clause}'
            sql=f'SELECT COUNT(*) FROM trades WHERE {clause}'
        else:
            params=(str(mode),) if open_only else (str(mode),stamp)
            sql=f'SELECT COUNT(*) FROM trades WHERE mode=? AND {clause}'
        with self.connect() as connection:
            row=connection.execute(sql,params).fetchone()
            return int(row[0])
    def count_symbol_trades_since(self,symbol,since,magic=None):
        stamp=since.isoformat() if hasattr(since,'isoformat') else str(since)
        with self.connect() as connection:
            if magic is None: row=connection.execute("SELECT COUNT(*) FROM trades WHERE symbol=? AND open_time>=?",(symbol,stamp)).fetchone()
            else: row=connection.execute("SELECT COUNT(*) FROM trades WHERE symbol=? AND magic=? AND open_time>=?",(symbol,int(magic),stamp)).fetchone()
            return int(row[0])
    def count_trades_since(self,since,magic=None):
        stamp=since.isoformat() if hasattr(since,'isoformat') else str(since)
        with self.connect() as connection:
            if magic is None: row=connection.execute('SELECT COUNT(*) FROM trades WHERE open_time>=?',(stamp,)).fetchone()
            else: row=connection.execute('SELECT COUNT(*) FROM trades WHERE open_time>=? AND magic=?',(stamp,int(magic))).fetchone()
            return int(row[0])
    def count_underlying_trades_since(self,symbol,since,magic=None):
        from .symbols import underlying_key
        stamp=since.isoformat() if hasattr(since,'isoformat') else str(since)
        with self.connect() as connection:
            rows=connection.execute('SELECT symbol FROM trades WHERE open_time>=?'+(' AND magic=?' if magic is not None else ''),(stamp,int(magic)) if magic is not None else (stamp,)).fetchall()
        key=underlying_key(symbol)
        return sum(underlying_key(row[0])==key for row in rows)
    def underlying_closed_since(self,symbol,since,magic=None):
        from .symbols import underlying_key
        stamp=since.isoformat() if hasattr(since,'isoformat') else str(since)
        with self.connect() as connection:
            rows=connection.execute("SELECT symbol FROM trades WHERE status='closed' AND close_time>=?"+(' AND magic=?' if magic is not None else ''),(stamp,int(magic)) if magic is not None else (stamp,)).fetchall()
        key=underlying_key(symbol)
        return any(underlying_key(row[0])==key for row in rows)
    def count_group_trades_since(self,since,asset_group,magic=None):
        stamp=since.isoformat() if hasattr(since,'isoformat') else str(since)
        with self.connect() as connection:
            row=connection.execute('SELECT COUNT(*) FROM trades WHERE open_time>=? AND asset_group=?'+(' AND magic=?' if magic is not None else ''),(stamp,str(asset_group),int(magic)) if magic is not None else (stamp,str(asset_group))).fetchone()
            return int(row[0])
    def symbol_closed_since(self,symbol,since,magic=None):
        stamp=since.isoformat() if hasattr(since,'isoformat') else str(since)
        with self.connect() as connection:
            if magic is None: row=connection.execute("SELECT 1 FROM trades WHERE symbol=? AND status='closed' AND close_time>=? LIMIT 1",(symbol,stamp)).fetchone()
            else: row=connection.execute("SELECT 1 FROM trades WHERE symbol=? AND magic=? AND status='closed' AND close_time>=? LIMIT 1",(symbol,int(magic),stamp)).fetchone()
            return row is not None
    def get_trade(self,ticket):
        with self.connect() as connection:
            row=connection.execute('SELECT * FROM trades WHERE ticket=?',(ticket,)).fetchone()
            return dict(row) if row else None
    def update_trade_features(self,ticket,features):
        import json
        payload=features if isinstance(features,str) else json.dumps(features,ensure_ascii=False,sort_keys=True,default=str)
        with self.connect() as connection:
            return connection.execute('UPDATE trades SET features=? WHERE ticket=?',(payload,int(ticket))).rowcount
    def latest_trade_id(self):
        with self.connect() as connection: return int(connection.execute('SELECT COALESCE(MAX(id),0) FROM trades').fetchone()[0])
    def get_closed_trades_after_id(self,last_id):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT id,ticket,symbol,direction,strategy,profit,commission,swap,close_reason FROM trades WHERE status='closed' AND id>? ORDER BY id",(int(last_id),))]
    def consecutive_losses(self,magic=None,limit=20):
        """Count the latest closed bot trades that lost money, newest first, until a win."""
        query="SELECT ticket,COALESCE(profit,0)+COALESCE(commission,0)+COALESCE(swap,0) net FROM trades WHERE status='closed'"
        params=[]
        if magic is not None: query+=' AND magic=?'; params.append(int(magic))
        query+=' ORDER BY close_time DESC LIMIT ?'; params.append(max(1,int(limit)))
        with self.connect() as connection:
            rows=[dict(row) for row in connection.execute(query,params)]
            partial={int(row['ticket']):float(row['pnl'] or 0) for row in connection.execute('SELECT ticket,SUM(COALESCE(profit,0)+COALESCE(commission,0)+COALESCE(swap,0)) pnl FROM partial_closes GROUP BY ticket')}
        count=0
        for row in rows:
            if float(row['net'] or 0)+partial.get(int(row['ticket']),0.0)<0: count+=1
            else: break
        return count
    def get_recent_brain_outcomes(self,limit=100,magic=None):
        import json
        query="SELECT ticket,profit,commission,swap,features FROM trades WHERE status='closed' AND mode='main' AND features IS NOT NULL"
        params=[]
        if magic is not None: query+=' AND magic=?'; params.append(int(magic))
        query+=' ORDER BY close_time DESC LIMIT ?'; params.append(max(int(limit),int(limit)*10))
        with self.connect() as connection:
            trades=[dict(row) for row in connection.execute(query,params)]
            partial={int(row['ticket']):float(row['pnl'] or 0) for row in connection.execute('SELECT ticket,SUM(COALESCE(profit,0)+COALESCE(commission,0)+COALESCE(swap,0)) pnl FROM partial_closes GROUP BY ticket')}
        outcomes=[]
        for trade in trades:
            try:
                payload=json.loads(trade.get('features') or '{}')
                if not payload.get('process_review',{}).get('good_process',False): continue
                brain=payload.get('brain',{}); memory=brain.get('memory',{})
            except (TypeError,ValueError): continue
            probability=memory.get('probability'); risk_money=float(brain.get('risk_money') or 0)
            if probability is None or risk_money<=0: continue
            net=float(trade.get('profit') or 0)+float(trade.get('commission') or 0)+float(trade.get('swap') or 0)+partial.get(int(trade['ticket']),0)
            outcomes.append({'probability':float(probability),'win':net>0,'net_r':net/risk_money})
        return outcomes
    def get_trades_for_day(self,day=None):
        if day is None: day=datetime.now(timezone.utc).date()
        if isinstance(day,datetime): day=day.astimezone(timezone.utc).date()
        day=day.isoformat() if hasattr(day,'isoformat') else str(day)
        with self.connect() as connection: return [dict(row) for row in connection.execute('SELECT * FROM trades WHERE substr(open_time,1,10)=? ORDER BY open_time',(day,))]
    def get_closed_trades_for_day(self,day):
        day=day.isoformat() if hasattr(day,'isoformat') else str(day)
        with self.connect() as connection: return [dict(row) for row in connection.execute("SELECT * FROM trades WHERE status='closed' AND substr(close_time,1,10)=? ORDER BY close_time",(day,))]
    def get_closed_trades_between(self,start,end,magic=None):
        first=start.isoformat() if hasattr(start,'isoformat') else str(start)
        last=end.isoformat() if hasattr(end,'isoformat') else str(end)
        sql="SELECT * FROM trades WHERE status='closed' AND close_time>=? AND close_time<?"
        params=[first,last]
        if magic is not None: sql+=' AND magic=?'; params.append(int(magic))
        with self.connect() as connection: return [dict(row) for row in connection.execute(sql+' ORDER BY close_time',params)]
    def get_recent_symbol_trades(self,symbol,limit=100,magic=None):
        import json
        sql="SELECT ticket,symbol,close_time,profit,commission,swap,features FROM trades WHERE status='closed' AND symbol=?"
        params=[str(symbol)]
        if magic is not None: sql+=' AND magic=?'; params.append(int(magic))
        sql+=' ORDER BY close_time DESC,id DESC LIMIT ?'; params.append(int(limit))
        with self.connect() as connection:
            trades=[dict(row) for row in connection.execute(sql,params)]
            partial={int(row['ticket']):float(row['pnl'] or 0) for row in connection.execute('SELECT ticket,SUM(COALESCE(profit,0)+COALESCE(commission,0)+COALESCE(swap,0)) pnl FROM partial_closes GROUP BY ticket')}
        result=[]
        for row in trades:
            try: review=json.loads(row.get('features') or '{}').get('process_review',{})
            except (TypeError,ValueError): review={}
            row['net']=float(row.get('profit') or 0)+float(row.get('commission') or 0)+float(row.get('swap') or 0)+partial.get(int(row['ticket']),0)
            row['outcome_r']=review.get('outcome_r')
            result.append(row)
        return result
    def get_equity_snapshots_since(self,since,login=None):
        """Equity snapshots since a time; with login, only that MT5 account's snapshots."""
        stamp=since.isoformat() if hasattr(since,'isoformat') else str(since)
        with self.connect() as connection:
            if login is None:
                return [dict(row) for row in connection.execute('SELECT time,equity FROM equity_snapshots WHERE time>=? ORDER BY time',(stamp,))]
            return [dict(row) for row in connection.execute('SELECT time,equity FROM equity_snapshots WHERE time>=? AND login=? ORDER BY time',(stamp,int(login)))]
    def get_daily_stats(self,day):
        day=day.isoformat() if hasattr(day,'isoformat') else str(day)
        with self.connect() as connection:
            row=connection.execute('SELECT * FROM daily_stats WHERE date=?',(day,)).fetchone()
            return dict(row) if row else None
    def save_daily_report(self,day,payload,created_at=None):
        import json
        stamp=created_at or datetime.now(timezone.utc).isoformat()
        data=json.dumps(payload,ensure_ascii=False,sort_keys=True)
        with self.connect() as connection:
            connection.execute('INSERT INTO daily_report(date,created_at,payload) VALUES (?,?,?) ON CONFLICT(date) DO NOTHING',(str(day),stamp,data))
    def get_daily_reports(self,days=7):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute('SELECT * FROM daily_report ORDER BY date DESC LIMIT ?',(int(days),))]
    def get_or_create_week_start(self,week_start,balance):
        with self.connect() as connection:
            connection.execute('INSERT OR IGNORE INTO weekly_limits(week_start,start_balance) VALUES (?,?)',(str(week_start),float(balance)))
            return dict(connection.execute('SELECT * FROM weekly_limits WHERE week_start=?',(str(week_start),)).fetchone())
    def mark_week_stopped(self,week_start):
        with self.connect() as connection: connection.execute('UPDATE weekly_limits SET stopped=1 WHERE week_start=?',(str(week_start),))
    def maintain(self,now=None,signals_days=7,events_days=30,equity_days=30,equity_step_minutes=5):
        """Prune old signals/events and thin older equity points."""
        now=now or datetime.now(timezone.utc)
        if now.tzinfo is None: now=now.replace(tzinfo=timezone.utc)
        now=now.astimezone(timezone.utc)
        with self.connect() as connection:
            connection.execute('DELETE FROM signals WHERE time < ?',((now-timedelta(days=signals_days)).isoformat(),))
            connection.execute('DELETE FROM bot_events WHERE time < ?',((now-timedelta(days=events_days)).isoformat(),))
            cutoff=(now-timedelta(days=equity_days)).isoformat()
            connection.execute('''DELETE FROM equity_snapshots WHERE time < ? AND rowid NOT IN (
                SELECT MIN(rowid) FROM equity_snapshots WHERE time < ?
                GROUP BY strftime('%Y-%m-%dT%H',time), CAST(strftime('%M',time) AS INTEGER) / ?
            )''',(cutoff,cutoff,max(1,int(equity_step_minutes))))
    def backup(self,destination):
        """Create a consistent online SQLite backup, including committed WAL data."""
        target=Path(destination); target.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as source, sqlite3.connect(target) as dest: source.backup(dest)
        return target
    def maintenance_done(self,task,run_key):
        with self.connect() as connection:
            return connection.execute('SELECT 1 FROM maintenance_runs WHERE task=? AND run_key=?',(str(task),str(run_key))).fetchone() is not None
    def mark_maintenance_done(self,task,run_key,now=None):
        stamp=(now or datetime.now(timezone.utc)).isoformat()
        with self.connect() as connection:
            connection.execute('INSERT OR IGNORE INTO maintenance_runs(task,run_key,completed_at) VALUES (?,?,?)',(str(task),str(run_key),stamp))
    def vacuum(self):
        """Checkpoint WAL and vacuum only outside any active transaction."""
        with self.connect() as connection:
            connection.execute('PRAGMA wal_checkpoint(PASSIVE)')
            connection.execute('VACUUM')
    def insert_signal(self,**values):
        values.setdefault('time',datetime.now(timezone.utc).isoformat())
        values.setdefault('score',0.0)
        with self.connect() as connection:
            # Repeated rejected decisions are state, not new events. Keep one
            # row until the symbol/strategy/direction/reason tuple changes.
            if not int(values.get('taken',0)) and str(values.get('skip_reason','')).strip():
                prior=connection.execute(
                    "SELECT id FROM signals WHERE symbol=? AND strategy=? AND direction=? AND taken=0 AND skip_reason=? ORDER BY id DESC LIMIT 1",
                    (values.get('symbol'),values.get('strategy'),values.get('direction'),values.get('skip_reason'))).fetchone()
                if prior:
                    columns=[key for key in values if key not in {'symbol','strategy','direction'}]
                    for k in columns: _validate_identifier(k, 'column')
                    connection.execute(f"UPDATE signals SET {','.join(key+'=?' for key in columns)} WHERE id=?",
                                       [values[key] for key in columns]+[int(prior['id'])])
                    return int(prior['id'])
            self._insert(connection,'signals',values)
            return connection.execute('SELECT last_insert_rowid()').fetchone()[0]
    def record_spread_sample(self,*,time,symbol,timeframe,hour_utc,spread_price):
        if not 0<=int(hour_utc)<=23 or float(spread_price)<0: raise ValueError('invalid spread sample')
        with self.connect() as connection:
            connection.execute('INSERT OR IGNORE INTO spread_samples(time,symbol,timeframe,hour_utc,spread_price) VALUES (?,?,?,?,?)',
                               (str(time),str(symbol),str(timeframe),int(hour_utc),float(spread_price)))
    def get_spread_cell(self,symbol,timeframe,hour_utc,since,until=None):
        """Return median spread price and count for a UTC hour cell."""
        import statistics
        start=since.isoformat() if hasattr(since,'isoformat') else str(since)
        end=until.isoformat() if hasattr(until,'isoformat') else (str(until) if until else datetime.now(timezone.utc).isoformat())
        with self.connect() as connection:
            rows=connection.execute('SELECT spread_price FROM spread_samples WHERE symbol=? AND timeframe=? AND hour_utc=? AND time>=? AND time<=? ORDER BY time',
                                    (str(symbol),str(timeframe),int(hour_utc),start,end)).fetchall()
        values=[float(row[0]) for row in rows]
        return {'median_spread_price':statistics.median(values) if values else None,'samples':len(values)}
    def insert_partial_close(self,**values):
        values.setdefault('time',datetime.now(timezone.utc).isoformat())
        columns=list(values)
        for k in columns: _validate_identifier(k, 'column')
        with self.connect() as connection:
            cursor=connection.execute(f"INSERT OR IGNORE INTO partial_closes ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",[values[key] for key in columns])
            return cursor.rowcount
    def get_partial_closes(self,ticket):
        with self.connect() as connection: return [dict(row) for row in connection.execute('SELECT * FROM partial_closes WHERE ticket=? ORDER BY time,id',(int(ticket),))]
    def get_partial_deal_tickets(self,ticket):
        with self.connect() as connection: return {int(row[0]) for row in connection.execute('SELECT deal_ticket FROM partial_closes WHERE ticket=? AND deal_ticket IS NOT NULL',(int(ticket),))}
    def get_active_adaptive_gates(self,now=None):
        now=now or datetime.now(timezone.utc)
        now=now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
        with self.connect() as connection:
            return [dict(row) for row in connection.execute('SELECT * FROM adaptive_gates WHERE disabled_until>? ORDER BY scope,name',(now.isoformat(),))]
    def activate_protection_gate(self,scope,name,disabled_until,reason,now=None):
        """Persist symbol/global protection pauses in the shared adaptive-gate table."""
        if scope not in {'symbol','global'}: raise ValueError('protection gate scope must be symbol or global')
        now=now or datetime.now(timezone.utc)
        now=now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
        expiry=disabled_until.isoformat() if hasattr(disabled_until,'isoformat') else str(disabled_until)
        with self.connect() as connection:
            old=connection.execute('SELECT disabled_until FROM adaptive_gates WHERE scope=? AND name=?',(scope,str(name))).fetchone()
            if old and str(old['disabled_until'])>now.isoformat(): return False
            message=f'Protection gate active {scope}={name} until {expiry}: {reason}'
            connection.execute('INSERT INTO adaptive_gates(scope,name,disabled_until,expectancy,sample_size,reason,updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(scope,name) DO UPDATE SET disabled_until=excluded.disabled_until,expectancy=excluded.expectancy,sample_size=excluded.sample_size,reason=excluded.reason,updated_at=excluded.updated_at',(scope,str(name),expiry,0.0,0,message,now.isoformat()))
            connection.execute('INSERT INTO bot_events(time,level,source,message) VALUES (?,?,?,?)',(now.isoformat(),'WARNING','risk',message))
            return True
    def refresh_adaptive_gates(self,window=20,disable_hours=24,magic=None,now=None):
        """Disable strategies/groups with negative expectancy over recent closed trades."""
        now=now or datetime.now(timezone.utc)
        now=now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
        disabled=[]
        with self.connect() as connection:
            where="status='closed' AND mode='main'"; params=[]
            if magic is not None: where+=' AND magic=?'; params.append(int(magic))
            closed=[dict(row) for row in connection.execute(f'SELECT ticket,strategy,asset_group,close_time,COALESCE(profit,0)+COALESCE(commission,0)+COALESCE(swap,0) AS net FROM trades WHERE {where} ORDER BY close_time DESC,id DESC',params)]
            partial={int(row['ticket']):float(row['pnl'] or 0) for row in connection.execute('SELECT ticket,SUM(COALESCE(profit,0)+COALESCE(commission,0)+COALESCE(swap,0)) pnl FROM partial_closes GROUP BY ticket')}
            groups={}
            for row in closed:
                value=float(row['net'])+partial.get(int(row['ticket']),0)
                for scope,name in (('strategy',row['strategy']),('group',row['asset_group'])):
                    if name: groups.setdefault((scope,str(name)),[]).append((row['close_time'],value))
            expiry=(now+timedelta(hours=float(disable_hours))).isoformat()
            for (scope,name),values in groups.items():
                values=values[:int(window)]
                if len(values)<int(window): continue
                expectancy=sum(value for _,value in values)/len(values)
                if expectancy>=0: continue
                existing=connection.execute('SELECT disabled_until FROM adaptive_gates WHERE scope=? AND name=?',(scope,name)).fetchone()
                if existing:
                    old=datetime.fromisoformat(existing['disabled_until'].replace('Z','+00:00'))
                    if old.tzinfo is None: old=old.replace(tzinfo=timezone.utc)
                    if old>now: continue
                message=f'Adaptive gate disabled {scope}={name} for {disable_hours:g}h; {len(values)} recent trades expectancy={expectancy:.4f}'
                connection.execute('INSERT INTO adaptive_gates(scope,name,disabled_until,expectancy,sample_size,reason,updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(scope,name) DO UPDATE SET disabled_until=excluded.disabled_until,expectancy=excluded.expectancy,sample_size=excluded.sample_size,reason=excluded.reason,updated_at=excluded.updated_at',(scope,name,expiry,expectancy,len(values),message,now.isoformat()))
                connection.execute('INSERT INTO bot_events(time,level,source,message) VALUES (?,?,?,?)',(now.isoformat(),'WARNING','risk',message))
                disabled.append({'scope':scope,'name':name,'disabled_until':expiry,'expectancy':expectancy,'sample_size':len(values),'reason':message})
        return disabled
    def weekend_gate(self,now=None,window=20,disable_days=7):
        """Disable weekend entries for a week after 20 recent weekend trades lose expectancy."""
        now=now or datetime.now(timezone.utc)
        now=now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
        with self.connect() as connection:
            prior=connection.execute('SELECT * FROM weekend_control WHERE id=1').fetchone()
            if prior and prior['disabled_until'] and prior['disabled_until']>now.isoformat(): return dict(prior)
            rows=connection.execute("SELECT profit,commission,swap FROM trades WHERE status='closed' AND mode IN ('weekend','weekend_exploration') ORDER BY close_time DESC LIMIT ?",(int(window),)).fetchall()
            outcomes=[sum(float(row[key] or 0) for key in ('profit','commission','swap')) for row in rows]
            expectancy=sum(outcomes)/len(outcomes) if outcomes else 0.0
            disabled_until=(now+timedelta(days=int(disable_days))).isoformat() if len(outcomes)>=int(window) and expectancy<0 else None
            connection.execute('INSERT INTO weekend_control(id,disabled_until,expectancy,sample_size,updated_at) VALUES (1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET disabled_until=excluded.disabled_until,expectancy=excluded.expectancy,sample_size=excluded.sample_size,updated_at=excluded.updated_at',(disabled_until,expectancy,len(outcomes),now.isoformat()))
            return dict(connection.execute('SELECT * FROM weekend_control WHERE id=1').fetchone())
    def get_partial_pnl_since(self,dt):
        stamp=dt.isoformat() if hasattr(dt,'isoformat') else str(dt)
        with self.connect() as connection:
            row=connection.execute('SELECT COALESCE(SUM(COALESCE(profit,0)+COALESCE(commission,0)+COALESCE(swap,0)),0) FROM partial_closes WHERE time>=?',(stamp,)).fetchone()
            return float(row[0])
    def update_signal(self,signal_id,**values):
        if not values: return
        columns=list(values)
        for k in columns: _validate_identifier(k, 'column')
        with self.connect() as connection:
            connection.execute(f"UPDATE signals SET {','.join(k+'=?' for k in columns)} WHERE id=?",[values[k] for k in columns]+[signal_id])
    def upsert_daily_stats(self,**values):
        columns=list(values)
        if 'date' not in values: raise ValueError('daily_stats.date is required')
        for k in columns: _validate_identifier(k, 'column')
        with self.connect() as connection:
            connection.execute(f"INSERT INTO daily_stats ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) ON CONFLICT(date) DO UPDATE SET {','.join(k+'=excluded.'+k for k in columns if k!='date')}",[values[k] for k in columns])
    def insert_equity_snapshot(self,**values):
        values.setdefault('time',datetime.now(timezone.utc).isoformat())
        with self.connect() as connection: self._insert(connection,'equity_snapshots',values)
    def log_event(self,level,source,message):
        with self.connect() as connection:
            connection.execute('INSERT INTO bot_events(time,level,source,message) VALUES (?,?,?,?)',(datetime.now(timezone.utc).isoformat(),str(level),str(source),str(message)))
    def record_scan_funnel(self,**values):
        import json
        values.setdefault('time',datetime.now(timezone.utc).isoformat())
        for key in ('filter_rejections','signal_rejections','risk_rejections','order_rejections'):
            values[key]=json.dumps(values.get(key) or {},sort_keys=True)
        columns=list(values)
        with self.connect() as connection:
            self._insert(connection,'scan_funnel',values)
            cutoff=(datetime.now(timezone.utc)-timedelta(days=7)).isoformat()
            connection.execute('DELETE FROM scan_funnel WHERE time<?',(cutoff,))
    def upsert_bot_status(self,**values):
        values.setdefault('time',datetime.now(timezone.utc).isoformat()); values['id']=1
        columns=list(values)
        for k in columns: _validate_identifier(k, 'column')
        with self.connect() as connection:
            connection.execute(f"INSERT INTO bot_status ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) ON CONFLICT(id) DO UPDATE SET {','.join(k+'=excluded.'+k for k in columns if k!='id')}",[values[k] for k in columns])
    def get_bot_status(self):
        with self.connect() as connection:
            row=connection.execute('SELECT * FROM bot_status WHERE id=1').fetchone()
            return dict(row) if row else None
    def insert_command(self,command,ttl_seconds=60):
        created=datetime.now(timezone.utc); expires=created+timedelta(seconds=int(ttl_seconds))
        command=str(command)
        manual_close=command.startswith('close_position:') and command.split(':',1)[1].isdigit()
        if command not in {'close_all','pause','resume','stop','close_all_stop','restart','status','today','positions','health','diagnose_order','demo_executor_e2e'} and not manual_close: raise ValueError('Unsupported bot command')
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            active=connection.execute("SELECT id FROM commands WHERE command=? AND status IN ('pending','running') ORDER BY id LIMIT 1",(str(command),)).fetchone()
            if active: raise ValueError(f'A {command} command is already pending or running')
            cursor=connection.execute('INSERT INTO commands(time,created_at,expires_at,command,status,message) VALUES (?,?,?,?,?,?)',(created.isoformat(),created.isoformat(),expires.isoformat(),str(command),'pending',''))
            return cursor.lastrowid
    def record_telegram_update(self,update_id):
        """Atomically deduplicate a Telegram update before queueing its action."""
        with self.connect() as connection:
            cursor=connection.execute('INSERT OR IGNORE INTO telegram_updates(update_id,received_at) VALUES (?,?)',(int(update_id),datetime.now(timezone.utc).isoformat()))
            return cursor.rowcount==1
    def get_recent_commands(self,limit=20):
        with self.connect() as connection: return [dict(row) for row in connection.execute('SELECT id,created_at,expires_at,command,status,message,processed_time FROM commands ORDER BY id DESC LIMIT ?',(int(limit),))]
    def get_pending_commands(self,now=None,bot_started_at=None):
        now=now or datetime.now(timezone.utc)
        now=now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
        started=bot_started_at
        if started is not None: started=started.replace(tzinfo=timezone.utc) if started.tzinfo is None else started.astimezone(timezone.utc)
        with self.connect() as connection:
            rows=connection.execute("SELECT * FROM commands WHERE status IN ('pending','running') ORDER BY id").fetchall(); ready=[]
            for row in rows:
                created=datetime.fromisoformat((row['created_at'] or row['time']).replace('Z','+00:00'))
                expires=datetime.fromisoformat(row['expires_at'].replace('Z','+00:00')) if row['expires_at'] else created+timedelta(seconds=60)
                created=created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created.astimezone(timezone.utc)
                expires=expires.replace(tzinfo=timezone.utc) if expires.tzinfo is None else expires.astimezone(timezone.utc)
                before_start=started is not None and created<started and row['command'] not in {'stop','close_all_stop'}
                if expires<=now or before_start:
                    connection.execute("UPDATE commands SET status='expired',message='expired before processing',processed_time=? WHERE id=?",(now.isoformat(),row['id']))
                elif row['status']=='pending': ready.append(dict(row))
            return ready
    def update_command(self,command_id,status,message=''):
        if status not in {'pending','running','done','failed','expired'}: raise ValueError('Invalid command status')
        with self.connect() as connection:
            connection.execute('UPDATE commands SET status=?,message=?,processed_time=? WHERE id=?',(status,str(message),datetime.now(timezone.utc).isoformat() if status in {'done','failed','expired'} else None,command_id))
    def get_pnl_since(self,dt,magic=None):
        stamp=dt.isoformat() if hasattr(dt,'isoformat') else str(dt)
        with self.connect() as connection:
            if magic is None:
                closed=float(connection.execute("SELECT COALESCE(SUM(COALESCE(profit,0)+COALESCE(commission,0)+COALESCE(swap,0)),0) FROM trades WHERE status='closed' AND close_time>=?",(stamp,)).fetchone()[0])
                partial=float(connection.execute("SELECT COALESCE(SUM(COALESCE(profit,0)+COALESCE(commission,0)+COALESCE(swap,0)),0) FROM partial_closes WHERE time>=?",(stamp,)).fetchone()[0])
            else:
                closed=float(connection.execute("SELECT COALESCE(SUM(COALESCE(profit,0)+COALESCE(commission,0)+COALESCE(swap,0)),0) FROM trades WHERE status='closed' AND close_time>=? AND magic=?",(stamp,int(magic))).fetchone()[0])
                partial=float(connection.execute("SELECT COALESCE(SUM(COALESCE(p.profit,0)+COALESCE(p.commission,0)+COALESCE(p.swap,0)),0) FROM partial_closes p JOIN trades t ON p.ticket=t.ticket WHERE p.time>=? AND t.magic=?",(stamp,int(magic))).fetchone()[0])
            return closed+partial
