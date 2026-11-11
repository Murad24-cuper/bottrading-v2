"""Sequential, read-only live-market scanner with bounded work per cycle."""
from __future__ import annotations
from dataclasses import dataclass
from time import perf_counter
from datetime import datetime,timezone,timedelta
from collections import Counter, defaultdict
import math
import json
from . import market_data, parquet_compat
from . import indicators as ind
from .features import build_features
from .risk import calc_volume
from .strategies import load_strategies
from .symbols import SymbolService
from .trade_rules import confidence_allowed, strategy_mtf_allowed, exploration_mtf_allowed, stop_target_from_atr, rebase_signal_levels, spread_within_median, spread_within_expected_move, weekend_mode_active
from .patterns.chart import detect_chart_patterns
from .patterns.candles import detect_candlestick_patterns
from .patterns.fibonacci import fibonacci_retracement
from .levels import key_levels
from .sessions import session_context
from .market_context import build_market_context,context_for_signal
from .symbols import underlying_key
from .brain.bandit import arm_key, sample_arm_priorities

@dataclass
class Candidate:
    symbol:str
    asset_group:str
    strategy:str
    signal:object
    confidence:float
    volume:float
    risk_money:float
    reward_money:float
    cost:dict
    score:float
    features:dict
    symbol_spec:dict
    skip_reason:str=''
    signal_row_id:int|None=None

def expected_value_score(confidence,reward_money,risk_money,cost):
    return float(confidence)*float(reward_money)-(1-float(confidence))*float(risk_money)-float(cost)

def rank_candidates(candidates, underlying_counts=None, group_counts=None, repeat_penalty=0.0, group_repeat_penalty=0.0):
    """Discard non-positive scores and keep the best signal for each symbol."""
    best={}
    underlying_counts=underlying_counts or {}; group_counts=group_counts or {}
    for item in candidates:
        item.score-=float(item.risk_money)*(float(repeat_penalty)*underlying_counts.get(underlying_key(item.symbol),0)+float(group_repeat_penalty)*group_counts.get(item.asset_group,0))
        if item.score<=0: continue
        prior=best.get(item.symbol)
        if prior is None or item.score>prior.score: best[item.symbol]=item
    return sorted(best.values(),key=lambda item:item.score,reverse=True)

def adaptive_top_movers(base: int, target: int, open_count: int) -> int:
    """Expand analysis breadth toward 100 while owned positions are below target."""
    target=max(1,int(target)); base=max(1,min(100,int(base)))
    vacancies=max(0,target-int(open_count))
    return min(100,base+math.ceil((min(vacancies,target)/target)*(100-base))) if vacancies else base

def select_diverse_movers(ranked, groups, minimum_per_group=5, limit=40, traded_today=None):
    """Select high-ranked movers with round-robin group coverage and low repetition."""
    traded_today=traded_today or set(); buckets=defaultdict(list)
    seen=set()
    for _,symbol in ranked:
        key=underlying_key(symbol)
        if key in seen: continue
        seen.add(key); buckets[groups.get(symbol,'other')].append(symbol)
    selected=[]; offsets=Counter()
    active=sorted(buckets)
    for _ in range(max(0,int(minimum_per_group))):
        for group in active:
            choices=buckets[group]
            choices.sort(key=lambda s:(underlying_key(s) in traded_today, next((i for i,(_,n) in enumerate(ranked) if n==s),len(ranked))))
            if offsets[group]<len(choices) and len(selected)<limit:
                selected.append(choices[offsets[group]]); offsets[group]+=1
    remaining=[symbol for _,symbol in ranked if symbol not in selected]
    remaining.sort(key=lambda s:(underlying_key(s) in traded_today, next((i for i,(_,n) in enumerate(ranked) if n==s),len(ranked))))
    for symbol in remaining:
        key=underlying_key(symbol)
        if any(underlying_key(item)==key for item in selected): continue
        if len(selected)>=limit: break
        selected.append(symbol)
    return selected

def weekend_index_names(symbols, groups):
    """Return the discovered index symbols included in an active universe."""
    return sorted({str(symbol) for symbol in symbols if groups.get(symbol) == 'indices'})

class MarketScanner:
    def __init__(self,mt5,settings,db,symbol_service=None):
        self.mt5,self.settings,self.db=mt5,settings,db
        self.symbols=symbol_service or SymbolService(mt5,settings)
        self._cache={}; self._cursor=0; self._strategies=load_strategies(settings); self.last_universe_size=0; self._weekend_index_signature=None
        self._volatility_cache={}; self._spread_cache={}; self.last_scan_stats={}; self.last_strategy_diagnostics={}; self._news_events=[]; self._news_updated=None; self._news_source='not loaded'; self._news_age_hours=None; self._news_loaded_at=0.0; self._news_notice_at=0.0; self._news_logged_updated=None; self._news_events_logged_marker=None
        self._brain_rows=self._load_brain_rows()
        self._brain_pca=None
        pca_path=(getattr(self.settings,'data_dir',self.settings.root/'data')/'brain'/'pca.json')
        if pca_path.is_file():
            try:
                payload=json.loads(pca_path.read_text(encoding='utf-8'))
                self._brain_pca={'mean':__import__('numpy').asarray(payload['mean'],dtype='float32'),'components':__import__('numpy').asarray(payload['components'],dtype='float32')}
            except Exception as exc: self.db.log_event('WARNING','brain',f'PCA transform unavailable; reference memory disabled: {type(exc).__name__}')
        self._brain_ood_guard=None
        guard_path=(getattr(self.settings,'data_dir',self.settings.root/'data')/'brain'/'ood_guard.json')
        if guard_path.is_file():
            try:
                guard=json.loads(guard_path.read_text(encoding='utf-8'))
                float(guard['threshold']); self._brain_ood_guard=guard
            except Exception as exc: self.db.log_event('WARNING','brain',f'OOD guard unavailable; brain cannot size live risk: {type(exc).__name__}')
        self._brain_live_cache=(0.0,None)
    def _brain_live(self):
        """Live/shadow status of the brain, re-read at most once a minute."""
        import time
        checked,status=self._brain_live_cache
        if status is None or time.monotonic()-checked>=60:
            from .brain.live import live_status
            status=live_status(self.settings.config.get('brain',{}),getattr(self.settings,'data_dir',self.settings.root/'data'))
            self._brain_live_cache=(time.monotonic(),status)
        return status
    def _load_news_calendar(self,now):
        """Refresh the local terminal calendar cache at most once a minute."""
        import time
        if not self.settings.config.get('news',{}).get('enabled',False): return
        if time.monotonic()-self._news_loaded_at<60: return
        from .news import load_calendar,terminal_calendar_path
        self._news_loaded_at=time.monotonic()
        try:
            path=terminal_calendar_path(self.mt5)
            raw=self.settings.config.get('paths',{}).get('data_dir','data')
            from pathlib import Path
            data_dir=Path(raw); data_dir=data_dir if data_dir.is_absolute() else Path(self.settings.root)/data_dir
            self._news_events,self._news_updated,self._news_source,self._news_age_hours=load_calendar(
                path,data_dir/'calendar'/'ff_calendar_thisweek.json',now,
                float(self.settings.config['news'].get('refresh_hours',6)),
                float(self.settings.config['news'].get('stale_after_hours',24)))
        except Exception as exc:
            self._news_events,self._news_updated,self._news_source,self._news_age_hours=[],None,'fallback window',None
            self.db.log_event('WARNING','news',f'Calendar file could not be read: {type(exc).__name__}')
        stale=self._news_source=='fallback window'
        source_age='unknown' if self._news_age_hours is None else f'{self._news_age_hours:.1f}h'
        source_marker=(self._news_source,self._news_updated)
        if not stale and source_marker!=self._news_logged_updated:
            self.db.log_event('INFO','news',f'Calendar source active · {self._news_source} · age {source_age}')
            self._news_logged_updated=source_marker
        if not stale and source_marker!=self._news_events_logged_marker:
            from .news import CURRENCY_SYMBOLS
            upcoming=[event for event in self._news_events if now<=event.time<=now+timedelta(hours=24)]
            for event in upcoming[:20]:
                symbols=', '.join(CURRENCY_SYMBOLS.get(event.currency,()))
                self.db.log_event('INFO','news',f"Yaxın xəbər · {event.time.isoformat()} · {event.currency} · {event.importance} · {event.name} · əlaqəli alətlər: {symbols}")
            self._news_events_logged_marker=source_marker
        if stale and time.monotonic()-self._news_notice_at>=1800:
            self._news_notice_at=time.monotonic()
            self.db.log_event('WARNING','news','Both calendar sources unavailable or older than 24 hours; conservative 12:30-14:30 UTC entry filter is active for USD/EUR/GBP/JPY instruments')
    def _load_brain_rows(self):
        """Load optional historical memory; absence keeps the brain shadow-only."""
        data_dir=getattr(self.settings,'data_dir',None)
        if data_dir is None:
            from pathlib import Path
            configured=Path(self.settings.config.get('paths',{}).get('data_dir','data'))
            data_dir=configured if configured.is_absolute() else Path(self.settings.root)/configured
        path=data_dir/'brain'/'reference.parquet'
        if not path.exists(): return {}
        try:
            frame=parquet_compat.read_parquet(path)
            required={'time','symbol','asset_group','vector','win','net_r'}
            if not required.issubset(frame.columns): raise ValueError(f'missing {sorted(required-set(frame.columns))}')
            from .brain.memory import prepare_memory
            return {str(group):prepare_memory(rows.to_dict('records')) for group,rows in frame.groupby('asset_group')}
        except Exception as exc:
            self.db.log_event('WARNING','brain',f'Brain reference memory unavailable; shadow score has no neighbours: {exc}')
            return {}
    def _bars(self,symbol,timeframe,count):
        key=(symbol,timeframe); cached=self._cache.get(key)
        try:
            latest=market_data.get_closed_candles(self.mt5,symbol,timeframe,1)
        except RuntimeError as exc:
            self.db.log_event('WARNING','scanner',f'Skipping {symbol} {timeframe}: closed-bar read failed: {exc}')
            return None
        if latest.empty: return cached
        if cached is None or cached.empty or latest.index[-1]>cached.index[-1] or len(cached)<count:
            try:
                cached=market_data.get_closed_candles(self.mt5,symbol,timeframe,count)
            except RuntimeError as exc:
                self.db.log_event('WARNING','scanner',f'Skipping {symbol} {timeframe}: history refresh failed: {exc}')
                return None
            info=self.mt5.symbol_info(symbol); point=float(self.settings.config['scanner'].get('timeframe_point_sizes',{}).get(symbol) or (info.point if info and info.point else 0.0))
            cached=cached.copy(); cached['spread_points']=cached['spread']; cached['point_size']=point; cached['spread_price']=cached['spread']*point
            self._cache[key]=cached
        self._record_spread_sample(symbol,timeframe)
        return cached
    def _record_spread_sample(self,symbol,timeframe):
        """Persist one observed spread per symbol/timeframe at the configured cadence."""
        import time
        cfg=self.settings.config.get('cost_model',{})
        if not cfg.get('enabled',False) or not hasattr(self.db,'record_spread_sample'): return
        now_mono=time.monotonic(); key=(symbol,timeframe)
        if now_mono-self._spread_cache.get(key,0.0)<float(cfg.get('sample_interval_seconds',60)): return
        self._spread_cache[key]=now_mono
        try:
            tick=self.mt5.symbol_info_tick(symbol); info=self.mt5.symbol_info(symbol)
            if tick is None or info is None or not float(getattr(info,'point',0) or 0): return
            spread=max(0.0,float(tick.ask)-float(tick.bid))
            now=datetime.now(timezone.utc)
            self.db.record_spread_sample(time=now.isoformat(),symbol=symbol,timeframe=timeframe,
                                         hour_utc=now.hour,spread_price=spread)
        except Exception as exc:
            self.db.log_event('WARNING','cost_model',f'Spread sample failed for {symbol} {timeframe}: {type(exc).__name__}')
    def scan_once(self):
        started=perf_counter(); cfg=self.settings.config['scanner']
        account_snapshot=self.mt5.account_info() if hasattr(self.mt5,'account_info') else None
        demo_exploration_mode=bool(self.settings.config.get('live',{}).get('normal_trading_disabled',False)
            and self.settings.config.get('demo_exploration',{}).get('enabled',False)
            and account_snapshot is not None
            and getattr(account_snapshot,'trade_mode',None)==getattr(self.mt5,'ACCOUNT_TRADE_MODE_DEMO',0))
        self._load_news_calendar(datetime.now(timezone.utc))
        active_gates=self.db.get_active_adaptive_gates() if hasattr(self.db,'get_active_adaptive_gates') else []
        disabled_strategies={row['name'] for row in active_gates if row['scope']=='strategy'}
        disabled_groups={row['name'] for row in active_gates if row['scope']=='group'}
        disabled_symbols={row['name'] for row in active_gates if row['scope']=='symbol'}
        global_pause=any(row['scope']=='global' and row['name']=='all' for row in active_gates)
        candidates=self.symbols.get_candidates() if hasattr(self.symbols,'get_candidates') else self.symbols.get_universe()
        owned=[p for p in (self.mt5.positions_get() or []) if int(p.magic)==self.settings.magic_number] if hasattr(self.mt5,'positions_get') else []
        open_symbols={str(p.symbol) for p in owned}
        universe=list(dict.fromkeys([*candidates,*sorted(open_symbols)])); self.last_universe_size=len(universe)
        now_utc=datetime.now(timezone.utc)
        if now_utc.weekday()>=5 and self.settings.config.get('weekend',{}).get('enabled',False):
            groups={symbol:self.symbols.get_asset_group(symbol) for symbol in universe}
            weekend_indices=weekend_index_names(universe,groups)
            if tuple(weekend_indices)!=self._weekend_index_signature:
                message=', '.join(weekend_indices) if weekend_indices else 'yoxdur'
                self.db.log_event('INFO','scanner',f'Həftəsonu açıq indekslər aşkarlandı: {message}')
                self._weekend_index_signature=tuple(weekend_indices)
        stage1=[]; rejection=Counter(); snapshots={}; movement=[]; move_rejections=Counter(); quote_open_groups=Counter()
        budget=float(cfg['max_seconds_per_cycle']); stage1_budget=budget*0.55; start=self._cursor%max(1,len(universe)); visited=0
        minutes=int(cfg['movement_timeframe_minutes']); m1_count=max(10,minutes)
        while visited<len(universe):
            if visited and perf_counter()-started>=stage1_budget: break
            symbol=universe[(start+visited)%len(universe)]; visited+=1
            force=symbol in open_symbols
            if not force:
                try: ok,reason=self.symbols.is_tradable(symbol,check_session=False)
                except TypeError: ok,reason=self.symbols.is_tradable(symbol)
                if not ok: rejection[reason]+=1; continue
                quote_open_groups[self.symbols.get_asset_group(symbol)]+=1
            info=self.mt5.symbol_info(symbol); tick=self.mt5.symbol_info_tick(symbol) if hasattr(self.mt5,'symbol_info_tick') else None
            if not force and (info is None or tick is None): rejection['tick unavailable']+=1; continue
            point=float(getattr(info,'point',0) or 0) if info else 0.0
            spread_price=max(0.0,float(tick.ask)-float(tick.bid)) if tick else 0.0
            if not force and spread_price>0 and point>0:
                spread_cfg=cfg
                lookback=int(spread_cfg.get('spread_median_lookback_hours',24))*60
                median_cache=self._spread_cache.get(symbol)
                if not median_cache or (datetime.now(timezone.utc)-median_cache['time']).total_seconds()>int(spread_cfg.get('spread_median_cache_seconds',300)):
                    spread_frame=self._bars(symbol,'M1',lookback+5)
                    median=float(spread_frame.spread.tail(lookback).median()) if spread_frame is not None and not spread_frame.empty else 0.0
                    median_cache={'median_points':median,'time':datetime.now(timezone.utc)}
                    self._spread_cache[symbol]=median_cache
                current_points=spread_price/point
                median_points=float(median_cache['median_points'])
                if median_points>0 and not spread_within_median(current_points,median_points,float(spread_cfg.get('max_spread_vs_median',1.8))):
                    rejection['spread above 24h median limit']+=1; continue
            cached=self._volatility_cache.get(symbol); max_ratio=float(cfg['max_spread_volatility_ratio'])
            horizon=max(1.0,float(cfg.get('spread_volatility_horizon_minutes',self.settings.config['risk']['move_horizon_minutes'])))
            if not force and cached and (datetime.now(timezone.utc)-cached['time']).total_seconds()<=int(cfg['movement_timeframe_minutes'])*60 and not spread_within_expected_move(spread_price,cached['atr'],horizon,max_ratio):
                rejection['spread too high relative to recent volatility']+=1; continue
            snapshots[symbol]={'point':point,'spread_price':spread_price}
            stage1.append(symbol)
            frame=self._bars(symbol,'M1',m1_count)
            if frame is None or len(frame)<5:
                frame=self._bars(symbol,'M5',max(3,(minutes+4)//5))
            if frame is None or len(frame)<3:
                if symbol in open_symbols: movement.append((float('-inf'),symbol)); continue
                move_rejections['recent movement unavailable']+=1; continue
            price=float(frame.close.iloc[-1]); recent_range=float(frame.high.tail(m1_count).max()-frame.low.tail(m1_count).min())
            atr=float(ind.atr(frame).iloc[-1]); spread_price=snapshots.get(symbol,{}).get('spread_price',0.0)
            if spread_price<=0 and 'spread' in frame:
                point=snapshots.get(symbol,{}).get('point',0.0); spread_price=float(frame.spread.iloc[-1])*point
            if not price or not math.isfinite(price) or not math.isfinite(atr) or atr<=0:
                if symbol in open_symbols: movement.append((float('-inf'),symbol)); continue
                move_rejections['movement indicators unavailable']+=1; continue
            horizon=max(1.0,float(cfg.get('spread_volatility_horizon_minutes',self.settings.config['risk']['move_horizon_minutes'])))
            expected_move=atr*math.sqrt(horizon)
            self._volatility_cache[symbol]={'atr':atr,'time':datetime.now(timezone.utc)}
            if symbol not in open_symbols and not spread_within_expected_move(spread_price,atr,horizon,max_ratio):
                move_rejections['spread too high relative to recent volatility']+=1; continue
            relative_range=recent_range/max(spread_price,snapshots.get(symbol,{}).get('point',0.0)*.01,1e-12)
            score=expected_move/max(spread_price,snapshots.get(symbol,{}).get('point',0.0),1e-12)
            movement.append((score,symbol))
        self._cursor=(start+visited)%max(1,len(universe))
        top_n=adaptive_top_movers(int(cfg['top_movers']),int(cfg.get('target_open_positions',15)),len(owned))
        ranked=sorted(movement,key=lambda item:item[0],reverse=True)
        today_trades=self.db.get_trades_for_day(datetime.now(timezone.utc).date())
        underlying_counts=Counter(underlying_key(row['symbol']) for row in today_trades)
        group_counts=Counter(row.get('asset_group') or self.symbols.get_asset_group(row['symbol']) for row in today_trades)
        ranked=sorted(ranked,key=lambda item:(underlying_key(item[1]) in {key for key,count in underlying_counts.items() if count},-item[0]))
        group_map={symbol:self.symbols.get_asset_group(symbol) for _,symbol in ranked}
        traded={key for key,count in underlying_counts.items() if count}
        top_symbols=select_diverse_movers(ranked,group_map,int(cfg.get('min_movers_per_group',5)),top_n,traded)
        top_symbols.extend(symbol for symbol in sorted(open_symbols) if symbol not in top_symbols)
        cached_context={symbol:{'M15':self._cache[(symbol,'M15')]} for symbol in top_symbols if (symbol,'M15') in self._cache}
        context_groups={symbol:self.symbols.get_asset_group(symbol) for symbol in cached_context}
        cycle_context=build_market_context(cached_context,context_groups)
        results=[]; strategy_diagnostics=defaultdict(Counter)
        analysed=[]
        for symbol in top_symbols:
            if analysed and perf_counter()-started>=budget: break
            if global_pause:
                rejection['global protection pause']+=1; continue
            if symbol in disabled_symbols:
                rejection[f'protection pause: symbol {symbol}']+=1; continue
            if symbol not in open_symbols:
                try: session_ok,session_reason=self.symbols.is_tradable(symbol)
                except TypeError: session_ok,session_reason=True,'tradable'
                if not session_ok:
                    rejection[session_reason]+=1
                    continue
            group=self.symbols.get_asset_group(symbol); strategies=[s for s in self._strategies if group in s.allowed_groups]
            if group in disabled_groups and not demo_exploration_mode:
                rejection[f'adaptive gate: group {group}']+=1; continue
            if not demo_exploration_mode:
                strategies=[s for s in strategies if s.name not in disabled_strategies]
            needed=sorted({tf for strategy in strategies for tf in strategy.required_timeframes} | ({'M1','M5','M15'} if cfg.get('require_mtf_agreement',True) else set()))
            if not needed: continue
            volatility_enabled=any(strategy.name=='volatility_scalp' for strategy in strategies)
            m1_count=max(int(cfg['candle_count']),int(self.settings.config.get('strategies',{}).get('volatility_scalp',{}).get('params',{}).get('percentile_lookback_bars',7200))) if volatility_enabled else int(cfg['candle_count'])
            candles={tf:self._bars(symbol,tf,m1_count if tf=='M1' else int(cfg['candle_count'])) for tf in needed}
            candles={tf:df for tf,df in candles.items() if df is not None and len(df)>=60}
            if not candles: continue
            analysed.append(symbol)
            features=build_features(candles)
            analysis_candles=dict(candles)
            if 'H1' not in analysis_candles:
                analysis_candles['H1']=self._bars(symbol,'H1',int(cfg.get('analysis_candle_count',250)))
            chart={tf:detect_chart_patterns(analysis_candles[tf],tf,float(cfg.get('pattern_tolerance',.015))) for tf in ('M5','M15') if tf in analysis_candles and analysis_candles[tf] is not None}
            candles_found=[pattern for tf in ('M1','M5','M15') if tf in analysis_candles and analysis_candles[tf] is not None for pattern in detect_candlestick_patterns(analysis_candles[tf],tf)]
            fib={tf:fibonacci_retracement(analysis_candles[tf],tf) for tf in ('M15','H1') if tf in analysis_candles and analysis_candles[tf] is not None}
            levels=key_levels(analysis_candles.get('M15')) if analysis_candles.get('M15') is not None else {}
            session=session_context(analysis_candles.get('M15'))
            features['observations']={'chart_patterns':chart,'candlestick_patterns':candles_found,'fibonacci':fib,'key_levels':levels,'session':session,'market_context':cycle_context}
            info=self.mt5.symbol_info(symbol)
            if info is None: continue
            spec=info._asdict(); account=account_snapshot or self.mt5.account_info()
            if account is None: continue
            for strategy in strategies:
                if any(tf not in candles for tf in strategy.required_timeframes):
                    strategy_diagnostics[strategy.name]['required_timeframe_missing']+=1; continue
                strategy.last_skip_reason='conditions_not_met'
                signal=strategy.generate_signal(symbol,candles)
                if signal is None:
                    strategy_diagnostics[strategy.name][getattr(strategy,'last_skip_reason','conditions_not_met')]+=1; continue
                news_cfg=self.settings.config.get('news',{})
                if news_cfg.get('enabled',False):
                    from .news import entry_block_reason
                    news_reason=entry_block_reason(symbol,datetime.now(timezone.utc),self._news_events,self._news_updated,float(news_cfg.get('stale_after_hours',2)),
                                                   float(news_cfg.get('blackout_minutes_before',15)),float(news_cfg.get('blackout_minutes_after',15)))
                    if news_reason:
                        reason=f'news filter: {news_reason}'
                        self._write_signal(signal,0,reason)
                        strategy_diagnostics[strategy.name]['news_filter']+=1
                        continue
                feature_snapshot=dict(features); observations=dict(features.get('observations',{}))
                observations['signal_context']=context_for_signal(cycle_context,symbol,signal.direction,group)
                from .news import minutes_to_next_high_impact
                minutes_to_event=minutes_to_next_high_impact(symbol,datetime.now(timezone.utc),self._news_events)
                observations['news_context']={'minutes_to_next_event':minutes_to_event}
                feature_snapshot['observations']=observations
                feature_snapshot['news_context']=observations['news_context']
                from .brain.checklist import evaluate as evaluate_brain_checklist
                from .brain.decide import decide as decide_brain
                brain_cfg=self.settings.config.get('brain',{})
                brain_checklist=evaluate_brain_checklist(features,signal.direction,brain_cfg.get('checklist',{}).get('weights',{}),observations,signal.entry_hint,signal.tp_price)
                from .brain.memory import query_memory,situation_vector
                brain_rows=self._brain_rows.get(group,[])
                query_context={**observations['signal_context'],'minutes_to_next_event':minutes_to_event}
                query_vector=situation_vector(candles,query_context,signal.direction)
                if self._brain_pca is not None:
                    from .brain.memory import apply_pca
                    query_vector=apply_pca(query_vector,self._brain_pca)[0]
                brain_memory=query_memory(query_vector,brain_rows,datetime.now(timezone.utc),symbol,int(brain_cfg.get('k_neighbors',300)),int(brain_cfg.get('query_exclude_same_symbol_days',1))) if brain_rows else {'count':0,'probability':None,'avg_win_r':0.0,'avg_loss_r':0.0,'cases':[],'mean_distance':None}
                from .brain.ood import distance_is_out_of_distribution
                brain_memory['out_of_distribution']=distance_is_out_of_distribution(brain_memory.get('nearest_distance'),self._brain_ood_guard)
                brain_live=self._brain_live()
                brain_mode='live' if brain_live['active'] else ('shadow' if brain_cfg.get('shadow',True) or not brain_cfg.get('enabled',False) else 'disabled')
                feature_snapshot['brain']={'checklist':brain_checklist,'memory':brain_memory,'decision':decide_brain(brain_checklist,brain_memory,brain_cfg),'suggested_risk_pct':0.0,'mode':brain_mode,'live_reasons':brain_live['reasons']}
                exploration_cfg=self.settings.config.get('demo_exploration',{})
                demo_mode=getattr(account,'trade_mode',None)==getattr(self.mt5,'ACCOUNT_TRADE_MODE_DEMO',0)
                exploration_enabled=bool(exploration_cfg.get('enabled',False) and demo_mode)
                if exploration_enabled and self.settings.config.get('live',{}).get('normal_trading_disabled',False):
                    feature_snapshot.setdefault('exploration',{})['eligible']=True
                    feature_snapshot['exploration'].setdefault('soft_failures',[]).append('no strategy passed the edge gate; DEMO exploration only')
                    feature_snapshot['exploration']['family']=getattr(strategy,'family',strategy.name)
                    feature_snapshot['exploration']['variant']=getattr(strategy,'variant','v1')
                    feature_snapshot['exploration']['timeframe']=getattr(signal,'timeframe','M15')
                if not confidence_allowed(signal.confidence,float(cfg.get('min_confidence',0.0))):
                    if not exploration_enabled or not confidence_allowed(signal.confidence,float(exploration_cfg.get('min_confidence',.45))):
                        reason='confidence below scanner.min_confidence'; self._write_signal(signal,0,reason,features=feature_snapshot); strategy_diagnostics[strategy.name]['confidence_below_minimum']+=1; continue
                    explore=feature_snapshot.setdefault('exploration',{'eligible':True,'soft_failures':[]})
                    explore['eligible']=True
                    explore.setdefault('soft_failures',[]).append('confidence below scanner.min_confidence')
                mtf_args={'enabled':cfg.get('require_mtf_agreement',True),'fast':int(cfg.get('mtf_ema_fast',5)),'slow':int(cfg.get('mtf_ema_slow',13))}
                main_mtf=strategy_mtf_allowed(strategy.name,candles,signal.direction,min_agree=int(cfg.get('mtf_min_agree',3)),**mtf_args)
                if not main_mtf:
                    explore_mtf=exploration_enabled and exploration_mtf_allowed(strategy.name,signal.timeframe,candles,signal.direction,fast=mtf_args['fast'],slow=mtf_args['slow'],min_agree=int(exploration_cfg.get('mtf_min_agree',2)))
                    if explore_mtf:
                        feature_snapshot.setdefault('exploration',{'eligible':True,'soft_failures':[]})['soft_failures'].append('main MTF agreement threshold not met')
                    else:
                        reason='M1/M5/M15 direction disagreement'; self._write_signal(signal,0,reason,features=feature_snapshot); strategy_diagnostics[strategy.name]['mtf_disagreement']+=1; continue
                family_entry=float(signal.entry_hint) if signal.entry_hint is not None else None
                tick=self.mt5.symbol_info_tick(symbol)
                if tick is None:
                    self._write_signal(signal,0,'tick unavailable at entry sizing',features=feature_snapshot); continue
                # Size from the executable side used by order_send, not a prior candle close.
                signal.entry_hint=float(tick.ask if signal.direction=='buy' else tick.bid)
                spread=max(0.0,float(tick.ask)-float(tick.bid))
                risk_cfg=self.settings.config['risk']
                now=datetime.now(timezone.utc)
                try:
                    forex_open=self.symbols.forex_session_open(now)
                except Exception: forex_open=False
                weekend=weekend_mode_active(now,self.settings.config,forex_open)
                if exploration_enabled and signal.sl_price is not None and signal.tp_price is not None and family_entry is not None:
                    signal.sl_price,signal.tp_price=rebase_signal_levels(family_entry,signal.sl_price,signal.tp_price,signal.direction,signal.entry_hint)
                else:
                    if 'M1' not in candles:
                        horizon=int(self.settings.config['risk']['move_horizon_minutes'])
                        self._write_signal(signal,0,f'M1 history missing for {horizon}-minute target',features=feature_snapshot); continue
                    m1_atr=float(ind.atr(candles['M1']).iloc[-1])
                    if not math.isfinite(m1_atr) or m1_atr<=0:
                        self._write_signal(signal,0,'M1 ATR unavailable',features=feature_snapshot); continue
                    move_horizon=float(self.settings.config.get('weekend',{}).get('move_horizon_minutes',60)) if weekend else float(risk_cfg['move_horizon_minutes'])
                    signal.sl_price,signal.tp_price=stop_target_from_atr(float(signal.entry_hint),signal.direction,m1_atr,spread,float(risk_cfg['sl_move_mult']),float(risk_cfg['tp_move_mult']),move_horizon)
                if signal.sl_price is None or signal.tp_price is None or signal.entry_hint is None:
                    self._write_signal(signal,0,'signal missing entry, SL, or TP',features=feature_snapshot); continue
                if signal.direction=='buy' and not signal.sl_price<signal.entry_hint<signal.tp_price:
                    self._write_signal(signal,0,'invalid stop/target side',features=feature_snapshot); continue
                if signal.direction=='sell' and not signal.tp_price<signal.entry_hint<signal.sl_price:
                    self._write_signal(signal,0,'invalid stop/target side',features=feature_snapshot); continue
                if 'demo_risk' in self.settings.config:
                    from .risk_profile import base_risk_pct
                    scan_mode=('weekend_exploration' if weekend else 'exploration') if exploration_enabled else ('weekend' if weekend else 'main')
                    risk_pct=base_risk_pct(self.settings.config,bool(demo_mode),scan_mode)
                else:
                    risk_pct=float(exploration_cfg.get('risk_per_trade_pct',0.1)) if exploration_enabled else (min(self.settings.risk.risk_per_trade_pct,float(self.settings.config.get('weekend',{}).get('risk_per_trade_pct',self.settings.risk.risk_per_trade_pct))) if weekend else self.settings.risk.risk_per_trade_pct)
                volume,volume_reason=calc_volume(account.equity,risk_pct,signal.entry_hint,signal.sl_price,spec)
                if volume<=0 and volume_reason=='min lot exceeds risk' and 'demo_risk' in self.settings.config:
                    # Small balance: trade the broker minimum lot if it stays under the small-account cap.
                    from .risk import min_lot_risk_money
                    from .risk_profile import min_lot_cap_pct
                    cap=min_lot_cap_pct(self.settings.config,bool(demo_mode)); needed=min_lot_risk_money(signal.entry_hint,signal.sl_price,spec)
                    needed_pct=needed/float(account.equity)*100 if needed and float(account.equity)>0 else None
                    if cap>0 and needed_pct is not None and needed_pct<=cap:
                        volume=float(spec.get('volume_min')); volume_reason='ok'
                        feature_snapshot['min_lot']={'active':True,'needed_pct':needed_pct,'cap_pct':cap}
                    elif needed_pct is not None:
                        volume_reason=f'min lot risks {needed_pct:.2f}% of equity (limit {cap:g}%)'
                if volume<=0:
                    self._write_signal(signal,0,volume_reason,features=feature_snapshot); continue
                cost=market_data.get_cost_estimate(self.mt5,symbol,volume)
                risk_money=market_data.price_distance_to_money(self.mt5,symbol,abs(signal.entry_hint-signal.sl_price),volume)
                reward_money=market_data.price_distance_to_money(self.mt5,symbol,abs(signal.tp_price-signal.entry_hint),volume)
                cost_gate_reason=''
                cost_cfg=self.settings.config.get('cost_model',{})
                if cost_cfg.get('enabled',False):
                    since=now-timedelta(hours=float(cost_cfg.get('lookback_hours',24)))
                    cell=self.db.get_spread_cell(symbol,signal.timeframe,now.hour,since,now) if hasattr(self.db,'get_spread_cell') else {'samples':0,'median_spread_price':None}
                    from .costs import evaluate_live_cost_gate
                    tick_size=float(spec.get('trade_tick_size') or spec.get('tick_size') or 0)
                    tick_value=float(spec.get('trade_tick_value_loss') or spec.get('trade_tick_value') or spec.get('tick_value') or 0)
                    point_size=float(spec.get('point') or spec.get('point_size') or 0)
                    mapped,cost_gate_reason=evaluate_live_cost_gate(
                        samples=int(cell['samples']),min_samples=int(cost_cfg.get('min_samples_per_cell',5)),
                        median_spread_price=cell['median_spread_price'],
                        symbol_info_spread_points=float(spec.get('spread') or 0),point_size=point_size,
                        fallback_spread_multiplier=1.25,
                        stop_distance=abs(signal.entry_hint-signal.sl_price),tick_size=tick_size,
                        tick_value=tick_value,volume=volume,
                        commission_per_lot_round_trip=(float(cost.get('commission',0))/volume
                            if cost.get('commission_known',False) else None),
                        slippage_points=float(cost_cfg.get('slippage_points',0)),
                        max_stop_ratio=float(cost_cfg.get('max_stop_ratio',.1)),
                        one_bar_delay=bool(cost_cfg.get('one_bar_delay',False)))
                    feature_snapshot['cost_map']={'hour_utc':now.hour,'samples':cell['samples'],
                        'median_spread_price':cell['median_spread_price'],
                        'spread_source':mapped.get('spread_source'),
                        'effective_spread_price':mapped.get('spread_price'),
                        'commission_known':bool(cost.get('commission_known',False)),
                        'commission_estimated':bool(cost.get('commission_estimated',False)),
                        'commission':mapped.get('commission'),
                        'slippage_cost':mapped.get('spread_slippage'),
                        'cost_stop_ratio':mapped.get('cost_stop_ratio')}
                feature_snapshot['brain']['risk_money']=risk_money
                from .brain.sizing import fractional_kelly_risk_pct
                feature_snapshot['brain']['decision']=decide_brain(brain_checklist,brain_memory,brain_cfg,float(cost.get('cost',0))/max(risk_money,1e-12))
                feature_snapshot['brain']['suggested_risk_pct']=fractional_kelly_risk_pct(float(brain_memory['probability']),float(brain_memory['avg_win_r']),float(brain_cfg.get('kelly_fraction',.25))) if brain_memory['probability'] is not None else 0.0
                brain_block_reason=''
                if brain_live['active']:
                    brain_decision=feature_snapshot['brain']['decision']
                    if not brain_decision.get('accepted'):
                        # Live brain may only block: it vetoes signals it rejects.
                        brain_block_reason='brain filter: '+'; '.join(brain_decision.get('reasons') or ['rejected'])
                    elif 'demo_risk' in self.settings.config:
                        from .brain.live import dynamic_risk
                        from .risk_profile import account_risk_profile
                        profile=account_risk_profile(self.settings.config,bool(demo_mode))
                        losses=self.db.consecutive_losses(self.settings.magic_number) if hasattr(self.db,'consecutive_losses') else 0
                        sizing=dynamic_risk(brain_memory,brain_decision,float(brain_cfg.get('kelly_fraction',.25)),float(profile['brain_min_risk_pct']),float(profile['brain_max_risk_pct']),losses,int(brain_cfg.get('loss_streak_base_risk',3)))
                        feature_snapshot['brain']['sizing']=sizing
                        if sizing['active'] and abs(float(sizing['risk_pct'])-risk_pct)>1e-9:
                            brain_volume,brain_volume_reason=calc_volume(account.equity,float(sizing['risk_pct']),signal.entry_hint,signal.sl_price,spec)
                            if brain_volume>0:
                                risk_pct=float(sizing['risk_pct']); volume=brain_volume
                                cost=market_data.get_cost_estimate(self.mt5,symbol,volume)
                                risk_money=market_data.price_distance_to_money(self.mt5,symbol,abs(signal.entry_hint-signal.sl_price),volume)
                                reward_money=market_data.price_distance_to_money(self.mt5,symbol,abs(signal.tp_price-signal.entry_hint),volume)
                                feature_snapshot['brain']['risk_money']=risk_money
                            else:
                                sizing.update({'active':False,'reason':f'brain risk volume unavailable: {brain_volume_reason}'})
                        feature_snapshot['brain']['applied_risk_pct']=risk_pct
                        self.db.log_event('INFO','brain',f"{symbol} {signal.direction}: risk {risk_pct:.3f}% ({sizing['reason']})")
                score=expected_value_score(signal.confidence,reward_money,risk_money,cost['cost'])
                reason=cost_gate_reason or brain_block_reason or ('expected value is non-positive' if score<=0 else ('below scanner.min_score' if score<float(cfg['min_score']) else ''))
                candidate=Candidate(symbol,group,strategy.name,signal,signal.confidence,volume,risk_money,reward_money,cost,score,feature_snapshot,spec,reason)
                candidate.signal_row_id=self._write_signal(signal,cost['cost'],reason,score,feature_snapshot); results.append(candidate)
        self.last_strategy_diagnostics={name:dict(counts) for name,counts in strategy_diagnostics.items()}
        eligible=[c for c in results if not c.skip_reason]
        if self.settings.config.get('self_learning',{}).get('bandit_enabled',True) and eligible:
            session_name=session_context(None).get('session','off-hours')
            arm_for=lambda item:arm_key(item.strategy,item.asset_group,session_name)
            arms=list(dict.fromkeys(arm_for(item) for item in eligible))
            try:
                with self.db.connect() as connection:
                    closed=connection.execute("SELECT strategy,asset_group,open_time,features,COALESCE(profit,0)+COALESCE(commission,0)+COALESCE(swap,0) net FROM trades WHERE status='closed' AND mode='main' ORDER BY close_time DESC LIMIT ?",(max(1,int(self.settings.config.get('self_learning',{}).get('bandit_history_trades',1000))*10),)).fetchall()
                history=[]
                for row in closed:
                    try:
                        if not json.loads(row['features'] or '{}').get('process_review',{}).get('good_process',False): continue
                        opened=datetime.fromisoformat(str(row['open_time']).replace('Z','+00:00'))
                        hour=opened.astimezone(timezone.utc).hour
                    except (ValueError,TypeError): continue
                    hist_session='London-NY overlap' if 12<=hour<16 else 'London' if 7<=hour<12 else 'New York' if 16<=hour<21 else 'Asia' if hour<7 else 'off-hours'
                    history.append({'arm':arm_key(row['strategy'],row['asset_group'],hist_session),'win':float(row['net'])>0})
                priorities=sample_arm_priorities(history,arms)
                for item in eligible:
                    share=priorities.get(arm_for(item),1/max(1,len(arms)))
                    item.features.setdefault('self_learning',{})['bandit_share']=share
                    item.features['self_learning']['bandit_arm']=arm_for(item)
                    item.score*=1.0+share
            except Exception as exc:
                self.db.log_event('WARNING','scanner',f'Bandit priority unavailable; baseline ranking used: {type(exc).__name__}: {exc}')
        best=rank_candidates(eligible,underlying_counts,group_counts,float(cfg.get('repeat_penalty',0.15)),float(cfg.get('group_repeat_penalty',0.05)))
        winners={c.symbol:c for c in best}
        for item in results:
            if item.symbol in winners and winners[item.symbol] is not item: item.skip_reason='superseded by higher-scoring signal for symbol'
            if item.skip_reason and item.signal_row_id is not None: self._set_skip_reason(item.signal_row_id,item.skip_reason)
        limit=int(cfg['max_candidates'])
        selected=best[:limit]
        for item in best[limit:]:
            item.skip_reason='scanner candidate limit reached'
            if item.signal_row_id is not None: self._set_skip_reason(item.signal_row_id,item.skip_reason)
        elapsed=perf_counter()-started
        groups_open=quote_open_groups
        self.last_scan_stats={'total_symbols':len(universe),'symbols_scanned':visited,'scan_complete':visited>=len(universe),'stage1_passed':len(stage1),'top_movers':len(analysed),'top_movers_selected':len(top_symbols),'signals':len(results),'scan_seconds':round(elapsed,3),'stage1_rejections':dict(rejection+move_rejections),'strategy_diagnostics':self.last_strategy_diagnostics,'groups_open':dict(groups_open)}
        if visited<len(universe): self.db.log_event('INFO','scanner',f'Scan budget reached after {visited}/{len(universe)} symbols; rotation resumes at {self._cursor}')
        self.db.log_event('INFO','scanner',f"Scan total={len(universe)} scanned={visited} complete={visited>=len(universe)} stage1_passed={len(stage1)} top_movers_selected={len(top_symbols)} analysed={len(analysed)} signals={len(results)} seconds={elapsed:.3f} stage1_rejections={dict(rejection+move_rejections)} strategy_diagnostics={self.last_strategy_diagnostics}")
        return selected
    def scan(self):
        """Part 3 live-loop interface; performs one bounded, read-only scan."""
        return self.scan_once()
    def _write_signal(self,signal,cost,skip_reason,score=0.0,features=None):
        return self.db.insert_signal(time=datetime.now(timezone.utc).isoformat(),symbol=signal.symbol,strategy=signal.strategy_name,direction=signal.direction,timeframe=signal.timeframe,confidence=signal.confidence,entry=signal.entry_hint,sl=signal.sl_price,tp=signal.tp_price,expected_cost=cost,score=score,taken=0,skip_reason=skip_reason or '',features=json.dumps(features or {},sort_keys=True,default=str))
    def _set_skip_reason(self,row_id,reason):
        with self.db.connect() as connection:
            connection.execute('UPDATE signals SET skip_reason=? WHERE id=?',(reason,row_id))
