"""MT5 order execution limited to positions carrying the configured magic."""
from __future__ import annotations
from datetime import datetime,timezone,timedelta
import math,json,threading
from decimal import Decimal,ROUND_FLOOR
from .models import TradeResult
from .logger import get_logger
from .time_utils import mt5_epoch_to_utc
from . import market_data, indicators as ind
from .risk import calc_volume, calc_volume_stressed
from .trade_rules import adverse_exit_score, continuation_metrics, may_extend, scale_out_volume, should_exit_early, stagnation_due, stop_only_tightens, stop_protects_profit

log=get_logger(__name__)

class Executor:
    def __init__(self,mt5,settings,db,symbol_service=None):
        self.mt5,self.settings,self.db,self.symbol_service=mt5,settings,db,symbol_service
        self._in_flight: set[str] = set()
        self._in_flight_lock=threading.Lock()
    def kill_switch_active(self): return (self.settings.root/'STOP').exists() or (self.settings.root/'STOP.txt').exists()
    def _event(self,message,level='INFO'):
        log.log(getattr(__import__('logging'),level),message)
        self.db.log_event(level,'executor',message)
    def _failure(self,message,volume=0,ticket=None,retcode=None):
        self._event(message,'WARNING'); return TradeResult(False,ticket,None,volume,retcode,message)
    def _asset_group(self,symbol):
        if self.symbol_service: return self.symbol_service.get_asset_group(symbol)
        n=symbol.upper()
        for group,patterns in {'metals':('XAU','XAG','GOLD','SILVER'),'indices':('US30','US100','NAS','SPX','DAX','UK100'),'energy':('OIL','WTI','BRENT','GAS'),'crypto':('BTC','ETH')}.items():
            if any(p in n for p in patterns): return group
        return 'forex' if len(n)>=6 and n[:6].isalpha() else 'other'
    def _filling_mode(self,info):
        m=self.mt5; flags=int(getattr(info,'filling_mode',0) or 0)
        if flags & int(getattr(m,'SYMBOL_FILLING_FOK',1)): return m.ORDER_FILLING_FOK
        if flags & int(getattr(m,'SYMBOL_FILLING_IOC',2)): return m.ORDER_FILLING_IOC
        if getattr(info,'trade_exemode',None)==getattr(m,'SYMBOL_TRADE_EXECUTION_MARKET',2): return None
        return m.ORDER_FILLING_RETURN
    @staticmethod
    def _price(info,value):
        digits=max(0,int(getattr(info,'digits',5) or 0))
        return float(round(float(value),digits))
    @staticmethod
    def _volume(info,value):
        minimum=Decimal(str(getattr(info,'volume_min',0) or 0)); step=Decimal(str(getattr(info,'volume_step',0) or 0))
        requested=Decimal(str(float(value)))
        if minimum<=0 or step<=0 or requested<minimum: return float(requested)
        steps=((requested-minimum)/step).to_integral_value(rounding=ROUND_FLOOR)
        return float(minimum+steps*step)
    @staticmethod
    def _comment(value):
        return str(value).encode('ascii','replace').decode('ascii')[:31]
    def build_open_request(self,signal,volume,info,tick,filling,features=None):
        """Build the flat keyword payload accepted by the installed MT5 wrapper."""
        buy=signal.direction=='buy'; volume=self._volume(info,volume)
        price=self._price(info,tick.ask if buy else tick.bid)
        stop=self._price(info,signal.sl_price); target=self._price(info,signal.tp_price)
        exp_info=features.get('exploration',{}) if isinstance(features,dict) else {}
        fam=exp_info.get('family',signal.strategy_name); var=exp_info.get('variant','v1'); tf=exp_info.get('timeframe',getattr(signal,'timeframe','M15'))
        trade_comment=f"exp:{fam[:10]}:{var[:4]}:{tf[:3]}" if (features and features.get('trade_mode') in ('exploration','weekend_exploration')) else signal.strategy_name
        request={'action':int(self.mt5.TRADE_ACTION_DEAL),'symbol':str(signal.symbol),'volume':float(volume),'type':int(self.mt5.ORDER_TYPE_BUY if buy else self.mt5.ORDER_TYPE_SELL),'price':float(price),'sl':float(stop),'tp':float(target),'deviation':int(self.settings.config['executor']['deviation_points']),'magic':int(self.settings.magic_number),'comment':self._comment(trade_comment),'type_time':int(self.mt5.ORDER_TIME_GTC),'type_filling':int(filling)}
        return {'request':request,'volume':volume,'price':price,'sl':stop,'tp':target}
    def open_trade(self,signal,volume,expected_cost=None,features=None):
        with self._in_flight_lock:
            if signal.symbol in self._in_flight: busy=True
            else: busy=False; self._in_flight.add(signal.symbol)
        if busy: return self._failure(f'Refused open: order for {signal.symbol} is already in-flight', volume)
        try:
            return self._open_trade_internal(signal, volume, expected_cost=expected_cost, features=features)
        finally:
            with self._in_flight_lock: self._in_flight.discard(signal.symbol)
    @staticmethod
    def _level_distance(info,name):
        try: return max(0.0,float(getattr(info,name,0) or 0)*float(getattr(info,'point',0) or 0))
        except (TypeError,ValueError): return 0.0
    def _freeze_reason(self,p,info,tick):
        """Return why the broker would reject a change to this position (TRADE_RETCODE_FROZEN), else None.

        MT5 rule: a position cannot be modified or closed while the closing price is
        within SYMBOL_TRADE_FREEZE_LEVEL points of its current SL or TP."""
        freeze=self._level_distance(info,'trade_freeze_level')
        if freeze<=0 or tick is None: return None
        is_buy=p.type==self.mt5.POSITION_TYPE_BUY; price=float(tick.bid if is_buy else tick.ask)
        sl=float(getattr(p,'sl',0) or 0); tp=float(getattr(p,'tp',0) or 0)
        if sl>0 and abs(price-sl)<=freeze: return f'price {price} is within freeze level {freeze} of SL {sl}'
        if tp>0 and abs(tp-price)<=freeze: return f'price {price} is within freeze level {freeze} of TP {tp}'
        return None

    def _open_trade_internal(self,signal,volume,expected_cost=None,features=None):
        m=self.mt5
        exploration_mode=bool(features and features.get('trade_mode') in ('exploration','weekend_exploration'))
        if exploration_mode:
            demo_cfg=self.settings.config.get('demo_exploration',{})
            risk_pct=float(demo_cfg.get('risk_per_trade_pct',0.1))
            if features.get('trade_mode')=='weekend_exploration':
                risk_pct=min(risk_pct,float(self.settings.config.get('weekend',{}).get('risk_per_trade_pct',risk_pct)))
        else:
            risk_pct=float(self.settings.risk.risk_per_trade_pct)
        if self.kill_switch_active(): return self._failure('Refused open: STOP kill switch is active',volume)
        if signal.sl_price is None: return self._failure('Refused open: stop-loss is required',volume)
        if signal.tp_price is None: return self._failure('Refused open: take-profit is required',volume)
        info=m.symbol_info(signal.symbol); tick=m.symbol_info_tick(signal.symbol)
        if info is None or tick is None: return self._failure(f'Symbol or tick unavailable: {m.last_error()}',volume)
        service=self.symbol_service
        if service is None:
            from .symbols import SymbolService
            service=SymbolService(m,self.settings); self.symbol_service=service
        tradable,trade_reason=service.is_tradable(signal.symbol)
        if not tradable: return self._failure(f'Refused open: {trade_reason}',volume)
        if info.trade_mode!=getattr(m,'SYMBOL_TRADE_MODE_FULL',4): return self._failure('Refused open: symbol is not enabled for full trading',volume)
        account=m.account_info()
        if account is None: return self._failure(f'Account info unavailable: {m.last_error()}',volume)
        if 'demo_risk' in self.settings.config:
            # Same account-type risk the scanner/bot used; account type comes from MT5, not config.
            from .risk_profile import applied_risk_pct,is_demo_account
            mode=str((features or {}).get('trade_mode') or 'main') if isinstance(features,dict) else 'main'
            risk_pct=applied_risk_pct(self.settings.config,is_demo_account(account,m),mode,features if isinstance(features,dict) else None)
        demo_mode=getattr(m,'ACCOUNT_TRADE_MODE_DEMO',0)
        if exploration_mode and account.trade_mode!=demo_mode:
            return self._failure('Refused open: exploration trades are DEMO only',volume)
        if account.trade_mode!=demo_mode and (not self.settings.allow_real or self.settings.config['account']['mode']!='real'):
            return self._failure('Refused open: real account requires account.mode=real and ALLOW_REAL=true',volume)
        if account.margin_mode==getattr(m,'ACCOUNT_MARGIN_MODE_RETAIL_NETTING',0):
            existing=m.positions_get(symbol=signal.symbol) or []
            if existing: return self._failure('Refused open: netting account already has a position on this symbol',volume)
        volume=self._volume(info,volume)
        buy=signal.direction=='buy'
        if signal.direction not in ('buy','sell'): return self._failure('Refused open: direction must be buy or sell',volume)
        if buy and not signal.sl_price<tick.ask<signal.tp_price: return self._failure('Refused open: invalid stop/target side for buy',volume)
        if not buy and not signal.tp_price<tick.bid<signal.sl_price: return self._failure('Refused open: invalid stop/target side for sell',volume)
        stops_distance=float(getattr(info,'trade_stops_level',0) or 0)*float(info.point)
        freeze_distance=float(getattr(info,'trade_freeze_level',0) or 0)*float(info.point)
        distance=max(stops_distance,freeze_distance)
        if distance and (abs((tick.ask if buy else tick.bid)-signal.sl_price)<distance or abs((tick.ask if buy else tick.bid)-signal.tp_price)<distance):
            return self._failure('Refused open: SL/TP inside broker trade_stops_level or freeze_level',volume)
        filling=self._filling_mode(info)
        if filling is None: return self._failure('Refused open: no supported filling mode for market execution',volume)
        prepared=self.build_open_request(signal,volume,info,tick,filling,features)
        volume=float(prepared['volume']); price=prepared['price']; stop=prepared['sl']; target=prepared['tp']
        if buy and not stop<price<target: return self._failure('Refused open: rounded stop/target do not bracket buy price',volume)
        if not buy and not target<price<stop: return self._failure('Refused open: rounded stop/target do not bracket sell price',volume)
        minimum=float(getattr(info,'volume_min',0) or 0); step=float(getattr(info,'volume_step',0) or 0); maximum=float(getattr(info,'volume_max',0) or 0)
        if minimum<=0 or step<=0 or maximum<minimum or volume<minimum or volume>maximum or abs((float(volume)-minimum)/step-round((float(volume)-minimum)/step))>1e-7:
            return self._failure('Refused open: volume is outside the broker minimum/step/maximum grid',volume)
        safe_volume,why=calc_volume(float(account.equity),risk_pct,float(price),stop,info._asdict())
        if safe_volume<=0 or float(volume)>safe_volume+1e-9:
            return self._failure(f'Refused open: requested volume exceeds stop-risk size ({why})',volume)
        req=prepared['request']
        exp_info=features.get('exploration',{}) if isinstance(features,dict) else {}
        fam=exp_info.get('family',signal.strategy_name); var=exp_info.get('variant','v1'); tf=exp_info.get('timeframe',getattr(signal,'timeframe','M15'))
        result=m.order_send(**req)
        if result and result.retcode in (getattr(m,'TRADE_RETCODE_REQUOTE',10004),getattr(m,'TRADE_RETCODE_PRICE_CHANGED',10020)):
            tick=m.symbol_info_tick(signal.symbol)
            if tick is None: return self._failure(f'Retry tick unavailable: {m.last_error()}',volume,result.retcode)
            new_price=self._price(info,tick.ask if buy else tick.bid)
            if buy and not signal.sl_price<new_price<signal.tp_price:
                return self._failure(f'Requote cancelled: price {new_price} violates stop/target side for buy',volume,retcode=result.retcode)
            if not buy and not signal.tp_price<new_price<signal.sl_price:
                return self._failure(f'Requote cancelled: price {new_price} violates stop/target side for sell',volume,retcode=result.retcode)
            if distance and (abs(new_price-signal.sl_price)<distance or abs(new_price-signal.tp_price)<distance):
                return self._failure('Requote cancelled: SL/TP inside broker trade_stops_level',volume,retcode=result.retcode)
            costs=expected_cost if isinstance(expected_cost,dict) else {}
            cost_per_lot=float(costs.get('cost',0) or 0)/float(volume) if float(volume)>0 else 0.0
            if cost_per_lot>0:
                new_safe_volume,why=calc_volume_stressed(float(account.equity),risk_pct,float(new_price),stop,info._asdict(),cost_per_lot)
            else:
                new_safe_volume,why=calc_volume(float(account.equity),risk_pct,float(new_price),stop,info._asdict())
            if new_safe_volume<=0 or new_safe_volume<minimum:
                return self._failure(f'Requote cancelled: safe volume below minimum lot ({why})',volume,retcode=result.retcode)
            new_volume=self._volume(info,min(float(volume),float(new_safe_volume)))
            if new_volume<minimum:
                return self._failure(f'Requote cancelled: volume {new_volume} below minimum lot {minimum}',volume,retcode=result.retcode)
            volume=new_volume
            req['price']=float(new_price)
            req['volume']=float(volume)
            result=m.order_send(**req)
        if result is None: return self._failure(f'order_send failed: {m.last_error()}',volume)
        if result.retcode!=m.TRADE_RETCODE_DONE:
            retcode_messages = {
                getattr(m, 'TRADE_RETCODE_MARKET_CLOSED', 10018): 'Market is closed for this symbol',
                getattr(m, 'TRADE_RETCODE_NO_MONEY', 10019): 'Not enough money (insufficient margin)',
                getattr(m, 'TRADE_RETCODE_OFF_QUOTES', 10021): 'No quotes available (off quotes)',
                getattr(m, 'TRADE_RETCODE_TOO_MANY_REQUESTS', 10024): 'Too many requests; broker rate limited',
                getattr(m, 'TRADE_RETCODE_FROZEN', 10029): 'Order or position is frozen by broker',
                getattr(m, 'TRADE_RETCODE_INVALID_STOPS', 10016): 'Invalid SL/TP stops',
            }
            spec_msg = retcode_messages.get(result.retcode, '')
            detail = f"{spec_msg} - {result.comment}" if spec_msg else result.comment
            return self._failure(f'order_send rejected ({result.retcode}): {detail}',volume,retcode=result.retcode)
        ticket=int(getattr(result,'position',0) or getattr(result,'order',0) or getattr(result,'deal',0))
        # Ensure the opened broker position has a valid server-side stop-loss
        opened_p=next((x for x in (m.positions_get() or []) if (int(x.ticket)==ticket or int(getattr(x,'identifier',-1) or -1)==ticket) and int(getattr(x,'magic',0))==self.settings.magic_number),None)
        if opened_p and not float(getattr(opened_p,'sl',0) or 0):
            self.remediate_missing_sl(opened_p,planned_sl=signal.sl_price)
        filled_volume=float(getattr(opened_p,'volume',getattr(result,'volume',volume)) or volume)
        now=datetime.now(timezone.utc).isoformat()
        try:
            costs=expected_cost if isinstance(expected_cost,dict) else {}
            import json
            trade_features=dict(features or {}); trade_mode=str(trade_features.pop('trade_mode','main'))
            if trade_mode in ('exploration','weekend_exploration'):
                trade_features.setdefault('exploration',{})
                trade_features['exploration']['family']=fam
                trade_features['exploration']['variant']=var
                trade_features['exploration']['timeframe']=tf
            self.db.insert_trade(ticket=ticket,symbol=signal.symbol,asset_group=self._asset_group(signal.symbol),direction=signal.direction,volume=filled_volume,initial_volume=filled_volume,open_time=now,open_price=result.price,sl=signal.sl_price,tp=signal.tp_price,profit=0,commission=0,swap=0,strategy=signal.strategy_name,reason=signal.reason,magic=self.settings.magic_number,status='open',timeframe=signal.timeframe,initial_risk=abs(float(result.price)-float(signal.sl_price)),scale_out_done=0,peak_favorable_r=0,estimated_spread_cost=float(costs.get('spread_cost',0) or 0),estimated_commission_cost=float(costs.get('commission',0) or 0),features=json.dumps(trade_features,sort_keys=True,default=str),mode=trade_mode)
        except Exception as exc:
            # The broker position is open; surface the persistence failure loudly for reconciliation.
            self._event(f'CRITICAL: opened ticket {ticket} but could not write trade row: {exc}','ERROR')
        self._event(f'Opened {signal.direction} {signal.symbol} ticket={ticket} volume={filled_volume}')
        return TradeResult(True,ticket,result.price,filled_volume,result.retcode,str(result.comment))
    def close_position(self,ticket,reason):
        m=self.mt5
        if reason not in {'tp','partial_tp','trail','early_exit','momentum_lost','stagnant','sl','time','session_close','manual','kill','weekly_stop'}: return self._failure(f'Refused close: unsupported reason {reason!r}',ticket=ticket)
        p=next((x for x in (m.positions_get() or []) if (int(x.ticket)==int(ticket) or int(getattr(x,'identifier',-1) or -1)==int(ticket)) and int(x.magic)==self.settings.magic_number),None)
        if p is None: return self._failure(f'Refused close: owned position {ticket} not found',ticket=ticket)
        tick=m.symbol_info_tick(p.symbol)
        if tick is None: return self._failure(f'Close tick failed: {m.last_error()}',p.volume,ticket)
        is_buy=p.type==m.POSITION_TYPE_BUY; order_type=m.ORDER_TYPE_SELL if is_buy else m.ORDER_TYPE_BUY
        info=m.symbol_info(p.symbol); price=self._price(info,tick.bid if is_buy else tick.ask) if info else float(tick.bid if is_buy else tick.ask)
        volume=self._volume(info,p.volume) if info else float(p.volume); filling=self._filling_mode(info) if info else None
        if filling is None: return self._failure('Refused close: unsupported filling mode',p.volume,ticket)
        frozen=self._freeze_reason(p,info,tick) if info else None
        if frozen: return self._failure(f'Close ticket {ticket} deferred: {frozen}; retrying next cycle',p.volume,ticket)
        req={'action':int(m.TRADE_ACTION_DEAL),'symbol':str(p.symbol),'position':int(p.ticket),'volume':float(volume),'type':int(order_type),'price':float(price),'deviation':int(self.settings.config['executor']['deviation_points']),'magic':int(self.settings.magic_number),'comment':self._comment(reason),'type_time':int(m.ORDER_TIME_GTC),'type_filling':int(filling)}
        result=m.order_send(**req)
        if result is None or result.retcode!=m.TRADE_RETCODE_DONE:
            return self._failure(f'Close ticket {ticket} failed: {getattr(result,"comment",m.last_error())}',p.volume,ticket,getattr(result,'retcode',None))
        aliases={int(ticket),int(p.ticket),int(getattr(p,'identifier',p.ticket) or p.ticket)}
        self.sync_positions(close_reasons={key:reason for key in aliases},close_data={key:{'close_price':float(result.price),'profit':float(p.profit),'swap':float(p.swap)} for key in aliases})
        self._event(f'Close accepted ticket={ticket} reason={reason}')
        return TradeResult(True,int(ticket),result.price,p.volume,result.retcode,str(result.comment))
    def close_all(self,reason):
        results=[self.close_position(p.ticket,reason) for p in (self.mt5.positions_get() or []) if int(p.magic)==self.settings.magic_number]
        self._event(f'Close-all requested reason={reason}; positions={len(results)}')
        return results
    def _close_partial(self,p,volume,reason='partial_tp'):
        m=self.mt5; info=m.symbol_info(p.symbol)
        if int(getattr(p,'magic',0))!=self.settings.magic_number: return self._failure('Refused partial close: position is not owned',volume,int(p.ticket))
        if info is None: return self._failure(f'Partial close symbol unavailable: {m.last_error()}',volume,int(p.ticket))
        minimum=float(getattr(info,'volume_min',0) or 0); step=float(getattr(info,'volume_step',0) or 0)
        if minimum<=0 or step<=0 or volume<minimum-1e-9 or volume>float(p.volume)+1e-9:
            return self._failure('Refused partial close: volume is outside the open volume or broker minimum/step grid',volume,int(p.ticket))
        volume=self._volume(info,volume)
        if volume<minimum-1e-9: return self._failure('Refused partial close: rounded volume is below broker minimum',volume,int(p.ticket))
        tick=m.symbol_info_tick(p.symbol)
        if tick is None: return self._failure(f'Partial close quote unavailable: {m.last_error()}',volume,int(p.ticket))
        buy=p.type==m.POSITION_TYPE_BUY; order_type=m.ORDER_TYPE_SELL if buy else m.ORDER_TYPE_BUY; price=self._price(info,tick.bid if buy else tick.ask)
        filling=self._filling_mode(info)
        if filling is None: return self._failure('Refused partial close: unsupported filling mode',volume,int(p.ticket))
        frozen=self._freeze_reason(p,info,tick)
        if frozen: return self._failure(f'Partial close ticket {p.ticket} deferred: {frozen}; retrying next cycle',volume,int(p.ticket))
        req={'action':int(m.TRADE_ACTION_DEAL),'symbol':str(p.symbol),'position':int(p.ticket),'volume':float(volume),'type':int(order_type),'price':float(price),'deviation':int(self.settings.config['executor']['deviation_points']),'magic':int(self.settings.magic_number),'comment':self._comment(reason),'type_time':int(m.ORDER_TIME_GTC),'type_filling':int(filling)}
        result=m.order_send(**req)
        if result is None or result.retcode!=m.TRADE_RETCODE_DONE: return self._failure(f'Partial close ticket {p.ticket} failed: {getattr(result,"comment",m.last_error())}',volume,int(p.ticket),getattr(result,'retcode',None))
        self.sync_positions()
        self.db.update_open_trade(int(p.ticket),scale_out_done=1,volume=max(0.0,float(p.volume)-float(volume)))
        self._event(f'Partial close ticket={p.ticket} volume={volume} reason={reason}')
        return TradeResult(True,int(p.ticket),float(result.price),float(volume),result.retcode,str(result.comment))
    def modify_sl_tp(self,ticket,sl,tp):
        m=self.mt5; p=next((x for x in (m.positions_get() or []) if (int(x.ticket)==int(ticket) or int(getattr(x,'identifier',-1) or -1)==int(ticket)) and int(x.magic)==self.settings.magic_number),None)
        if p is None:
            self._event(f'Refused SL/TP modification: ticket={ticket} is not owned','WARNING'); return False
        if float(sl or 0)<=0 or float(tp or 0)<=0:
            self._event(f'Refused SL/TP modification: ticket={ticket} must retain positive SL and TP','WARNING'); return False
        info=self.mt5.symbol_info(p.symbol)
        if info is None: return False
        sl=self._price(info,sl); tp=self._price(info,tp)
        is_buy=p.type==self.mt5.POSITION_TYPE_BUY; current_sl=float(p.sl or 0)
        if current_sl>0 and not stop_only_tightens('buy' if is_buy else 'sell',current_sl,float(sl)):
            self._event(f'Refused SL/TP modification: ticket={ticket} would loosen its stop','WARNING'); return False
        tick=self.mt5.symbol_info_tick(p.symbol)
        if tick is None or (is_buy and float(sl)>=float(tick.bid)) or (not is_buy and float(sl)<=float(tick.ask)):
            self._event(f'Refused SL/TP modification: ticket={ticket} stop is not on the protective side of the market','WARNING'); return False
        frozen=self._freeze_reason(p,info,tick)
        if frozen:
            self._event(f'SL/TP modification ticket={ticket} deferred: {frozen}; retrying next cycle','WARNING'); return False
        stops=self._level_distance(info,'trade_stops_level'); market=float(tick.bid if is_buy else tick.ask)
        if stops>0 and (abs(market-float(sl))<stops or abs(float(tp)-market)<stops):
            self._event(f'Refused SL/TP modification: ticket={ticket} new SL/TP is inside broker trade_stops_level {stops}','WARNING'); return False
        req={'action':int(m.TRADE_ACTION_SLTP),'position':int(ticket),'symbol':str(p.symbol),'sl':float(sl),'tp':float(tp),'magic':int(self.settings.magic_number)}
        result=m.order_send(**req); ok=bool(result and result.retcode==m.TRADE_RETCODE_DONE)
        if ok:
            try: self.db.update_open_trade(int(p.ticket),sl=float(sl),tp=float(tp))
            except Exception as exc: self._event(f'Could not persist updated SL/TP for ticket={p.ticket}: {exc}','WARNING')
        self._event(f'Modify SL/TP ticket={ticket} success={ok}'+('' if ok else f' error={getattr(result,"comment",m.last_error())}'), 'INFO' if ok else 'WARNING')
        return ok
    def remediate_missing_sl(self,position,planned_sl=None,max_retries=3):
        """Ensure a live position has a valid server-side stop loss; close immediately if impossible."""
        ticket=int(position.ticket)
        current_sl=float(getattr(position,'sl',0) or 0)
        if current_sl>0: return True
        symbol=str(position.symbol)
        is_buy=getattr(position,'type',0)==getattr(self.mt5,'POSITION_TYPE_BUY',0)
        row=(self.db.get_trade(ticket) if hasattr(self.db,'get_trade') else None) or {}
        sl_candidate=planned_sl or float(row.get('sl') or 0)
        tp_candidate=float(getattr(position,'tp',0) or 0) or float(row.get('tp') or 0)
        info=self.mt5.symbol_info(symbol)
        tick=self.mt5.symbol_info_tick(symbol)
        point=float(getattr(info,'point',1e-5) or 1e-5) if info else 1e-5
        stops_level=float(getattr(info,'trade_stops_level',0) or 0)*point if info else 0.0
        bid=float(tick.bid) if tick else float(getattr(position,'price_open',0) or 0)
        ask=float(tick.ask) if tick else float(getattr(position,'price_open',0) or 0)
        market_price=bid if is_buy else ask
        if not sl_candidate or (is_buy and sl_candidate>=market_price) or (not is_buy and sl_candidate<=market_price):
            atr_distance=0.0
            try:
                frame=market_data.get_closed_candles(self.mt5,symbol,'M1',30)
                if frame is not None and not frame.empty: atr_distance=float(ind.atr(frame).iloc[-1])*2.0
            except Exception: pass
            if not atr_distance or not math.isfinite(atr_distance) or atr_distance<=0:
                atr_distance=max(50*point,abs(market_price*0.005)) if market_price>0 else 50*point
            atr_distance=max(atr_distance,stops_level+5*point)
            sl_candidate=round(market_price-atr_distance if is_buy else market_price+atr_distance,5)
        if not tp_candidate or (is_buy and tp_candidate<=market_price) or (not is_buy and tp_candidate>=market_price):
            tp_candidate=round(market_price+2.0*abs(market_price-sl_candidate) if is_buy else market_price-2.0*abs(market_price-sl_candidate),5)
        for attempt in range(1,max_retries+1):
            sl_candidate=self._price(info,sl_candidate); tp_candidate=self._price(info,tp_candidate)
            req={'action':int(self.mt5.TRADE_ACTION_SLTP),'position':int(ticket),'symbol':str(symbol),'sl':float(sl_candidate),'tp':float(tp_candidate),'magic':int(self.settings.magic_number)}
            res=self.mt5.order_send(**req)
            if res and res.retcode==getattr(self.mt5,'TRADE_RETCODE_DONE',10009):
                self._event(f'Recovered missing SL for ticket={ticket} symbol={symbol} sl={sl_candidate} (attempt {attempt})','WARNING')
                try: self.db.update_open_trade(ticket,sl=sl_candidate,tp=tp_candidate)
                except Exception: pass
                if hasattr(position,'sl'): position.sl=sl_candidate
                return True
            import time
            time.sleep(0.05*attempt)
        self._event(f'CRITICAL: Emergency remediation failed to set SL on ticket={ticket} ({symbol}) after {max_retries} attempts; closing position at market','CRITICAL')
        self.close_position(ticket,'kill')
        return False
    def enforce_time_stop(self,settings=None):
        """Manage scale-outs, protective stops, momentum exits and the safety ceiling."""
        settings=settings or self.settings; config=settings.risk; raw=settings.config['risk']; now=datetime.now(timezone.utc); actions=[]
        for p in (self.mt5.positions_get() or []):
            if int(p.magic)!=self.settings.magic_number: continue
            row=next((item for item in self.db.get_open_trades() if int(item['ticket']) in {int(p.ticket),int(getattr(p,'identifier',p.ticket) or p.ticket)}),None)
            if row is None: continue
            opened=mt5_epoch_to_utc(p.time,self.mt5); age=(now-opened).total_seconds()/60
            if age>=float(raw['max_hold_extended_minutes']): actions.append(self.close_position(p.ticket,'time')); continue
            if age>=config.max_hold_minutes:
                deadline=row.get('extension_until')
                try: deadline_dt=datetime.fromisoformat(deadline) if deadline else None
                except (TypeError,ValueError): deadline_dt=None
                if deadline_dt and deadline_dt.tzinfo is None: deadline_dt=deadline_dt.replace(tzinfo=timezone.utc)
                if deadline_dt and now<deadline_dt:
                    pass
                else:
                    is_buy=p.type==self.mt5.POSITION_TYPE_BUY; direction='buy' if is_buy else 'sell'
                    entry=float(p.price_open); tick=self.mt5.symbol_info_tick(p.symbol)
                    current=float(tick.bid if is_buy else tick.ask) if tick else entry
                    in_profit=current>entry if is_buy else current<entry
                    stop=float(p.sl or 0); info=self.mt5.symbol_info(p.symbol)
                    cost_distance=0.0
                    try:
                        estimated=market_data.get_cost_estimate(self.mt5,p.symbol,float(p.volume))['cost']
                        tv=float(info.trade_tick_value or 0) if info else 0; ts=float(info.trade_tick_size or 0) if info else 0
                        if tv>0 and ts>0: cost_distance=float(estimated)*float(config.breakeven_cost_multiplier)/max(float(p.volume),1e-12)*ts/tv
                    except Exception: pass
                    protected=stop_protects_profit(direction,stop,entry,cost_distance)
                    mtf=rising=False; score=0.0
                    try:
                        candles={tf:market_data.get_closed_candles(self.mt5,p.symbol,tf,80) for tf in ('M1','M5','M15')}
                        mtf,rising,score=continuation_metrics(candles,direction)
                    except Exception as exc: self._event(f'Could not score extension ticket={p.ticket}: {exc}','WARNING')
                    minimum=float(settings.config['scanner'].get('extend_min_score',raw.get('extend_min_score',.7)))
                    can_extend=may_extend(in_profit,int(row.get('scale_out_done') or 0)==1,protected,mtf,rising,score,minimum,age,float(raw['max_hold_extended_minutes']))
                    if not can_extend:
                        self._event(f'Time stop ticket={p.ticket} refused extension age={age:.1f} score={score:.2f} protected={protected} reason=continuation or protection criteria failed')
                        actions.append(self.close_position(p.ticket,'time')); continue
                    step=min(float(raw['extend_step_minutes']),float(raw['max_hold_extended_minutes'])-age)
                    next_deadline=now+timedelta(minutes=step)
                    extended=min(int(raw['max_hold_extended_minutes'])-config.max_hold_minutes,int(row.get('extended_minutes') or 0)+int(math.ceil(step)))
                    self.db.update_open_trade(int(row['ticket']),extension_until=next_deadline.isoformat(),extended_minutes=extended)
                    row=self.db.get_trade(int(row['ticket'])) or row
                    self._event(f'Protected-profit extension ticket={p.ticket} age={age:.1f} score={score:.2f} until={next_deadline.isoformat()}')
            info=self.mt5.symbol_info(p.symbol); tick=self.mt5.symbol_info_tick(p.symbol)
            if info is None or tick is None: continue
            is_buy=p.type==self.mt5.POSITION_TYPE_BUY; current=float(tick.bid if is_buy else tick.ask); entry=float(p.price_open)
            initial_risk=float(row.get('initial_risk') or abs(entry-float(row.get('sl') or p.sl)))
            if initial_risk<=0: continue
            favorable=(current-entry) if is_buy else (entry-current); current_r=favorable/initial_risk
            peak=max(float(row.get('peak_favorable_r') or 0),current_r)
            self.db.update_open_trade(row['ticket'],peak_favorable_r=peak)
            if not int(row.get('scale_out_done') or 0) and current_r>=config.first_target_r:
                partial,close_all=scale_out_volume(float(p.volume),float(info.volume_step),float(info.volume_min),float(config.scale_out_pct))
                if close_all:
                    actions.append(self.close_position(p.ticket,'partial_tp')); continue
                actions.append(self._close_partial(p,partial))
                # Refresh the position after the partial fill before managing its stop.
                refreshed=next((x for x in (self.mt5.positions_get() or []) if int(x.ticket)==int(p.ticket)),None)
                if refreshed: p=refreshed
                row=self.db.get_trade(int(row['ticket'])) or row
            if int(row.get('scale_out_done') or 0):
                protective_sl=float(p.sl or 0)
                try:
                    cost=market_data.get_cost_estimate(self.mt5,p.symbol,float(p.volume))['cost']
                    tick_value=float(info.trade_tick_value or 0); tick_size=float(info.trade_tick_size or 0)
                    cost_distance=(cost*float(config.breakeven_cost_multiplier)/max(float(p.volume),1e-12))*tick_size/tick_value if tick_size>0 and tick_value>0 else 0.0
                    breakeven=entry+cost_distance if is_buy else entry-cost_distance
                    tighter=breakeven>protective_sl if is_buy else (protective_sl==0 or breakeven<protective_sl)
                    stop_ok=breakeven<current if is_buy else breakeven>current
                    if tighter and stop_ok and self.modify_sl_tp(p.ticket,breakeven,float(p.tp or 0)): protective_sl=breakeven
                except Exception as exc:
                    self._event(f'Could not estimate breakeven costs ticket={p.ticket}: {exc}','WARNING')
                try: frame=market_data.get_closed_candles(self.mt5,p.symbol,'M1',max(30,int(config.momentum_lookback_bars)+2))
                except Exception as exc:
                    self._event(f'Could not read management candles ticket={p.ticket}: {exc}','WARNING'); frame=None
                if frame is not None and len(frame)>=int(config.momentum_lookback_bars)+1:
                    momentum=float(frame.close.iloc[-1])-float(frame.close.iloc[-1-int(config.momentum_lookback_bars)])
                    if (is_buy and momentum<0) or (not is_buy and momentum>0):
                        actions.append(self.close_position(p.ticket,'momentum_lost')); continue
                atr_frame=frame
                if atr_frame is not None and not atr_frame.empty:
                    atr=float(ind.atr(atr_frame).iloc[-1]); trail_distance=atr*float(config.trail_atr_mult)
                    trail=current-trail_distance if is_buy else current+trail_distance
                    tighter=trail>protective_sl if is_buy else (protective_sl==0 or trail<protective_sl)
                    valid=trail<current if is_buy else trail>current
                    if math.isfinite(trail) and tighter and valid: self.modify_sl_tp(p.ticket,trail,float(p.tp or 0))
            if stagnation_due(age,peak,config.stagnation_minutes,config.stagnation_min_r): actions.append(self.close_position(p.ticket,'stagnant'))
        return actions
    def enforce_adverse_move_exit(self, settings=None):
        """Close owned positions in the configured adverse R band when M1 evidence turns."""
        cfg=(settings or self.settings).config['risk']; actions=[]
        if not cfg.get('early_exit_enabled',True): return actions
        rows={int(row['ticket']):row for row in self.db.get_open_trades()}
        for position in (self.mt5.positions_get() or []):
            if int(position.magic)!=self.settings.magic_number: continue
            row=rows.get(int(position.ticket))
            if row is None: continue
            entry=float(position.price_open); is_buy=position.type==self.mt5.POSITION_TYPE_BUY
            tick=self.mt5.symbol_info_tick(position.symbol)
            if tick is None: continue
            current=float(tick.bid if is_buy else tick.ask)
            initial_risk=float(row.get('initial_risk') or abs(entry-float(row.get('sl') or position.sl)))
            if initial_risk<=0: continue
            current_r=((current-entry) if is_buy else (entry-current))/initial_risk
            try: frame=market_data.get_closed_candles(self.mt5,position.symbol,'M1',max(60,int(cfg['early_exit_volume_lookback'])+5))
            except Exception as exc:
                self._event(f'Adverse exit data unavailable ticket={position.ticket}: {exc}','WARNING'); continue
            score,reasons=adverse_exit_score(frame,'buy' if is_buy else 'sell',cfg)
            if should_exit_early(current_r,score,cfg):
                reason='; '.join(reasons) or 'adverse indicators aligned'
                self._event(f'Early exit ticket={position.ticket} R={current_r:.3f} score={score} reasons={reason}','WARNING')
                actions.append(self.close_position(position.ticket,'early_exit'))
        return actions
    def enforce_session_close(self,settings=None,now_utc=None):
        """Close only symbols approaching a broker-observed long session gap."""
        risk=(settings or self.settings).risk; now=now_utc or datetime.now(timezone.utc)
        service=self.symbol_service
        if service is None:
            from .symbols import SymbolService
            service=SymbolService(self.mt5,self.settings)
            self.symbol_service=service
        actions=[]
        for position in (self.mt5.positions_get() or []):
            if int(position.magic)!=self.settings.magic_number: continue
            upcoming=service.next_session_close(position.symbol,now,risk.max_session_gap_hours)
            if not upcoming: continue
            minutes=(upcoming[0]-now).total_seconds()/60
            if 0<=minutes<=risk.no_entry_before_close_minutes:
                actions.append(self.close_position(position.ticket,'session_close'))
        return actions
    def enforce_news_positions(self,events,now_utc=None):
        """Protect owned positions before mapped high-impact events; never alter manual trades."""
        from .news import event_matches_symbol
        now=now_utc or datetime.now(timezone.utc)
        now=now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
        actions=[]
        for position in (self.mt5.positions_get() or []):
            if int(position.magic)!=self.settings.magic_number: continue
            before=float(self.settings.config.get('news',{}).get('blackout_minutes_before',15)) if hasattr(self.settings,'config') else 15.0
            upcoming=[event for event in events if str(event.importance).upper()=='HIGH' and event_matches_symbol(event,position.symbol) and timedelta(0)<=event.time-now<=timedelta(minutes=before)]
            if not upcoming: continue
            event=min(upcoming,key=lambda item:item.time); minutes=(event.time-now).total_seconds()/60
            is_buy=position.type==self.mt5.POSITION_TYPE_BUY
            tick=self.mt5.symbol_info_tick(position.symbol); info=self.mt5.symbol_info(position.symbol)
            if tick is None or info is None: continue
            current=float(tick.bid if is_buy else tick.ask); entry=float(position.price_open)
            in_profit=current>entry if is_buy else current<entry
            if in_profit:
                try:
                    cost=market_data.get_cost_estimate(self.mt5,position.symbol,float(position.volume))['cost']
                    tick_value=float(info.trade_tick_value or info.trade_tick_value_profit or 0); tick_size=float(info.trade_tick_size or 0)
                    distance=(float(cost)*tick_size/max(tick_value,1e-12)) if tick_size>0 and tick_value>0 else 0.0
                    stop=entry+distance if is_buy else entry-distance
                    protective=stop>float(position.sl or 0) if is_buy else (not position.sl or stop<float(position.sl))
                    valid=stop<current if is_buy else stop>current
                    if protective and valid: self.modify_sl_tp(position.ticket,stop,float(position.tp or 0))
                except Exception as exc: self._event(f'News breakeven protection failed ticket={position.ticket}: {exc}','WARNING')
            elif minutes<=5:
                actions.append(self.close_position(position.ticket,'news'))
        return actions
    def _history(self,ticket):
        try: return list(self.mt5.history_deals_get(position=int(ticket)) or [])
        except Exception:
            try:
                deals=self.mt5.history_deals_get(datetime.now(timezone.utc)-timedelta(days=30),datetime.now(timezone.utc),group='*') or []
                return [deal for deal in deals if int(getattr(deal,'position_id',-1))==int(ticket)]
            except Exception: return []
    def _record_partial_deal(self,row,deal):
        deal_ticket=int(getattr(deal,'ticket',0) or 0)
        reason=str(getattr(deal,'comment','') or '').strip().lower()
        if reason not in {'tp','partial_tp','trail','early_exit','momentum_lost','stagnant','sl','time','session_close','manual','kill'}:
            reason=self._close_reason([deal],forced='partial_tp')
        self.db.insert_partial_close(deal_ticket=deal_ticket or None,ticket=int(row['ticket']),symbol=row['symbol'],time=mt5_epoch_to_utc(deal.time,self.mt5).isoformat(),volume=float(getattr(deal,'volume',0) or 0),price=float(getattr(deal,'price',0) or 0),profit=float(getattr(deal,'profit',0) or 0),commission=float(getattr(deal,'commission',0) or 0),swap=float(getattr(deal,'swap',0) or 0),reason=reason)
    def _close_reason(self,deals,forced=None,row=None):
        if forced in {'partial_tp','trail','early_exit','momentum_lost','stagnant','time','session_close','news','manual','kill','weekly_stop'}: return forced
        m=self.mt5
        for deal in reversed(deals):
            reason=getattr(deal,'reason',None)
            if reason==getattr(m,'DEAL_REASON_TP',5): return 'tp'
            if reason==getattr(m,'DEAL_REASON_SL',4):
                if float(getattr(deal,'profit',0) or 0)>0 or self._sl_was_moved(row): return 'trail'
                return 'sl'
        return forced or 'manual'
    def _sync_external_positions(self,broker_positions):
        """Mirror non-bot broker positions for display without managing them."""
        if not hasattr(self.db,'sync_external_positions') or not hasattr(self.db,'get_active_account_id'):
            return
        try:
            account_id=self.db.get_active_account_id()
            if not account_id:
                return
            buy_type=getattr(self.mt5,'POSITION_TYPE_BUY',0)
            external=[]
            for position in broker_positions:
                if int(getattr(position,'magic',0) or 0)==self.settings.magic_number:
                    continue
                opened=mt5_epoch_to_utc(getattr(position,'time',0),self.mt5).isoformat() if getattr(position,'time',0) else ''
                external.append({'ticket':int(position.ticket),'symbol':str(position.symbol),'magic':int(getattr(position,'magic',0) or 0),
                    'direction':'buy' if getattr(position,'type',None)==buy_type else 'sell','volume':float(position.volume),
                    'open_time':opened,'open_price':float(getattr(position,'price_open',0) or 0),'sl':float(getattr(position,'sl',0) or 0),
                    'tp':float(getattr(position,'tp',0) or 0),'profit':float(getattr(position,'profit',0) or 0),
                    'swap':float(getattr(position,'swap',0) or 0),'comment':str(getattr(position,'comment','') or '')})
            self.db.sync_external_positions(account_id,external)
        except Exception as exc:
            self._event(f'External position mirror failed: {type(exc).__name__}: {exc}','WARNING')
    @staticmethod
    def _sl_was_moved(row):
        if not row: return False
        try:
            entry=float(row.get('open_price') or 0); risk=float(row.get('initial_risk') or 0); current=float(row.get('sl') or 0)
            if entry<=0 or risk<=0 or current<=0: return False
            original=entry-risk if row.get('direction')=='buy' else entry+risk
            return abs(current-original)>max(1e-9,abs(original)*1e-9)
        except (TypeError,ValueError,AttributeError): return False
    def sync_positions(self,close_reasons=None,close_data=None):
        """Reconcile DB open rows against broker positions and closing deal history."""
        broker_positions=list(self.mt5.positions_get() or [])
        self._sync_external_positions(broker_positions)
        live=set()
        for position in broker_positions:
            if int(position.magic)==self.settings.magic_number:
                live.add(int(position.ticket))
                if getattr(position,'identifier',None): live.add(int(position.identifier))
        reasons=close_reasons or {}; fallbacks=close_data or {}; reconciled=[]
        # Import broker-side positions missed by a prior database write. Manual trades are excluded.
        open_rows=self.db.get_open_trades(); db_open={int(row['ticket']) for row in open_rows}
        for position in broker_positions:
            if int(position.magic)!=self.settings.magic_number: continue
            ticket=int(position.ticket)
            identifier=int(getattr(position,'identifier',ticket))
            if ticket in db_open or identifier in db_open or self.db.get_trade(ticket) or self.db.get_trade(identifier): continue
            opened=mt5_epoch_to_utc(position.time,self.mt5).isoformat()
            direction='buy' if position.type==self.mt5.POSITION_TYPE_BUY else 'sell'
            self.db.insert_trade(ticket=ticket,symbol=position.symbol,asset_group=self._asset_group(position.symbol),direction=direction,volume=position.volume,initial_volume=position.volume,open_time=opened,open_price=position.price_open,sl=position.sl,tp=position.tp,profit=position.profit,commission=0,swap=position.swap,magic=self.settings.magic_number,status='open',strategy='',reason='recovered from broker')
            self._event(f'Recovered missing DB row for owned ticket={ticket}','WARNING')
            db_open.add(ticket)
        current_positions=[p for p in broker_positions if int(p.magic)==self.settings.magic_number]
        for position in current_positions:
            identifiers={int(position.ticket),int(getattr(position,'identifier',position.ticket) or position.ticket)}
            row=next((item for item in self.db.get_open_trades() if int(item['ticket']) in identifiers),None)
            if row:
                deals=self._history(int(getattr(position,'identifier',position.ticket) or position.ticket))
                closing=[deal for deal in deals if getattr(deal,'entry',None) in (getattr(self.mt5,'DEAL_ENTRY_OUT',1),getattr(self.mt5,'DEAL_ENTRY_OUT_BY',3),getattr(self.mt5,'DEAL_ENTRY_INOUT',2))]
                for deal in closing: self._record_partial_deal(row,deal)
                partial_tickets=self.db.get_partial_deal_tickets(int(row['ticket']))
                open_commission=sum(float(getattr(deal,'commission',0) or 0) for deal in deals if int(getattr(deal,'ticket',0) or 0) not in partial_tickets)
                updates={'profit':float(position.profit),'commission':open_commission,'sl':position.sl,'tp':position.tp,'swap':float(position.swap),'volume':float(position.volume)}
                try:
                    margin=self.mt5.order_calc_margin(position.type,position.symbol,position.volume,position.price_current)
                    if margin is not None: updates['margin_used']=float(margin)
                except Exception: pass
                self.db.update_open_trade(row['ticket'],**updates)
                if closing: self.db.update_open_trade(row['ticket'],scale_out_done=1)
        for row in self.db.get_open_trades():
            ticket=int(row['ticket'])
            forced=ticket in reasons
            if int(row.get('magic') or 0)!=self.settings.magic_number or (ticket in live and not forced): continue
            deals=self._history(ticket)
            if not deals and ticket not in reasons:
                fallback=fallbacks.get(ticket,{})
                close_price=float(fallback.get('close_price',row.get('open_price') or 0))
                close_time=datetime.now(timezone.utc).isoformat()
                profit=float(fallback.get('profit',0)); commission=0.0; swap=float(fallback.get('swap',0))
                close_reason='reconciled_stale'
                self.db.update_trade_close(ticket,close_time=close_time,close_price=close_price,profit=profit,commission=commission,swap=swap,close_reason=close_reason)
                self._event(f'Closed stale DB record ticket={ticket} (position no longer on MT5)','WARNING')
                reconciled.append(ticket)
                continue
            closing=[d for d in deals if getattr(d,'entry',None) in (getattr(self.mt5,'DEAL_ENTRY_OUT',1),getattr(self.mt5,'DEAL_ENTRY_OUT_BY',3),getattr(self.mt5,'DEAL_ENTRY_INOUT',2))]
            initial_volume=float(row.get('initial_volume') or row.get('volume') or 0)
            partial_tickets=self.db.get_partial_deal_tickets(ticket)
            partial_volume=sum(float(item.get('volume') or 0) for item in self.db.get_partial_closes(ticket))
            accumulated=partial_volume
            for deal in closing:
                deal_ticket=int(getattr(deal,'ticket',0) or 0); deal_volume=float(getattr(deal,'volume',0) or 0)
                if deal_ticket in partial_tickets: continue
                if initial_volume and accumulated+deal_volume<initial_volume-1e-8:
                    self._record_partial_deal(row,deal)
                    if deal_ticket: partial_tickets.add(deal_ticket)
                    accumulated+=deal_volume
            final_closing=[d for d in closing if int(getattr(d,'ticket',0) or 0) not in partial_tickets]
            if ticket not in live and not final_closing and closing:
                final_closing=[closing[-1]]
            source=final_closing or closing or deals
            if source:
                last=source[-1]; close_price=float(last.price); close_time=mt5_epoch_to_utc(last.time,self.mt5).isoformat()
                profit=sum(float(getattr(d,'profit',0) or 0) for d in final_closing)
                commission=sum(float(getattr(d,'commission',0) or 0) for d in deals if int(getattr(d,'ticket',0) or 0) not in partial_tickets)
                swap=sum(float(getattr(d,'swap',0) or 0) for d in deals if int(getattr(d,'ticket',0) or 0) not in partial_tickets)
            else:
                fallback=fallbacks.get(ticket,{})
                close_price=float(fallback.get('close_price',row.get('open_price') or 0)); close_time=datetime.now(timezone.utc).isoformat()
                profit=float(fallback.get('profit',0)); commission=0.0; swap=float(fallback.get('swap',0))
            close_reason=self._close_reason(final_closing,reasons.get(ticket),row)
            self.db.update_trade_close(ticket,close_time=close_time,close_price=close_price,profit=profit,commission=commission,swap=swap,close_reason=close_reason)
            try:
                closed=self.db.get_trade(ticket) or row
                features=json.loads(closed.get('features') or '{}')
                review=features.get('process_review')
                if isinstance(review,dict):
                    partial_net=sum(float(item.get('profit') or 0)+float(item.get('commission') or 0)+float(item.get('swap') or 0) for item in self.db.get_partial_closes(ticket))
                    risk_money=float(features.get('sizing',{}).get('stressed_risk_money') or features.get('brain',{}).get('risk_money') or 0)
                    net=profit+commission+swap+partial_net
                    review['outcome_r']=net/risk_money if risk_money>0 else None
                    review['outcome_grade']='win' if net>0 else 'loss' if net<0 else 'flat'
                    self.db.update_trade_features(ticket,features)
            except (ValueError,TypeError,AttributeError) as exc:
                self._event(f'Process/outcome review failed ticket={ticket}: {type(exc).__name__}','WARNING')
            self._event(f'Synced closed ticket={ticket} reason={close_reason} profit={profit}')
            reconciled.append(ticket)
        return reconciled
