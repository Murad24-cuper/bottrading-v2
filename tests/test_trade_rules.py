import numpy as np
import pandas as pd

from src.trade_rules import adverse_exit_score, confidence_allowed, mtf_direction_agrees, strategy_mtf_allowed, exploration_mtf_allowed, weekend_mode_active, scale_out_volume, should_exit_early, stop_only_tightens, stagnation_due, stop_protects_profit, continuation_metrics, stop_target_from_atr, spread_within_median, spread_within_expected_move, may_extend


def _frame(direction='buy', bars=80):
    close = np.arange(bars, dtype=float) if direction == 'buy' else np.arange(bars, dtype=float)[::-1]
    return pd.DataFrame({'open': close, 'high': close + .2, 'low': close - .2, 'close': close,
                         'tick_volume': np.full(bars, 10.)})


def test_mtf_agreement_requires_all_three_directions():
    candles = {tf: _frame() for tf in ('M1', 'M5', 'M15')}
    assert mtf_direction_agrees(candles, 'buy')
    candles['M5'] = _frame('sell')
    assert not mtf_direction_agrees(candles, 'buy')

def test_spread_filter_compares_same_price_units_over_horizon():
    assert spread_within_expected_move(.001, .0002, 25, 1.0)
    assert not spread_within_expected_move(.0011, .0002, 25, 1.0)
    assert not spread_within_expected_move(.1, 0, 25, 1.0)

def test_mtf_two_of_three_agreement_accepts_one_disagreement():
    candles={tf:_frame() for tf in ('M1','M5','M15')}; candles['M5']=_frame('sell')
    assert mtf_direction_agrees(candles,'buy',min_agree=2)
    assert not mtf_direction_agrees(candles,'buy',min_agree=3)
    assert not mtf_direction_agrees({'M1':candles['M1']},'buy',min_agree=2)

def test_mean_reversion_bypasses_only_generic_direction_agreement():
    candles={tf:_frame('sell') for tf in ('M1','M5','M15')}
    assert strategy_mtf_allowed('mean_reversion',candles,'buy',min_agree=2)
    assert not strategy_mtf_allowed('trend_pullback',candles,'buy',min_agree=2)

def test_h4_exploration_ignores_lower_timeframe_disagreement_but_normal_does_not():
    candles={tf:_frame('sell') for tf in ('M1','M5','M15')}
    assert exploration_mtf_allowed('ts_momentum','H4',candles,'buy',min_agree=2)
    assert not strategy_mtf_allowed('ts_momentum',candles,'buy',min_agree=2)
    assert not exploration_mtf_allowed('ts_momentum','M15',candles,'buy',min_agree=2)

def test_weekend_policy_activates_only_for_enabled_weekend_utc():
    from datetime import datetime,timezone
    cfg={'weekend':{'enabled':True}}
    assert weekend_mode_active(datetime(2026,11,8,12,tzinfo=timezone.utc),cfg)
    assert not weekend_mode_active(datetime(2026,11,8,22,tzinfo=timezone.utc),cfg,forex_open=True)
    assert not weekend_mode_active(datetime(2026,11,9,12,tzinfo=timezone.utc),cfg)
    assert not weekend_mode_active(datetime(2026,11,8,12,tzinfo=timezone.utc),{'weekend':{'enabled':False}})


def test_stop_target_from_atr_uses_configured_horizon():
    from pathlib import Path
    import yaml
    horizon=yaml.safe_load((Path(__file__).resolve().parent / 'fixtures' / 'default_config.yaml').read_text(encoding='utf-8'))['risk']['move_horizon_minutes']
    sl, tp = stop_target_from_atr(100, 'buy', 2, .5, .8, 1.2, horizon)
    assert sl == 100 - 2 * np.sqrt(horizon) * .8
    assert tp == 100 + 2 * np.sqrt(horizon) * 1.2 + .5


def test_spread_must_be_within_median_multiple():
    assert spread_within_median(15, 10, 1.5)
    assert not spread_within_median(15.01, 10, 1.5)
    assert not spread_within_median(1, 0, 1.5)


def test_confidence_gate():
    assert confidence_allowed(.6, .6)
    assert not confidence_allowed(.59, .6)


def test_extension_requires_protected_profitable_trade_and_score():
    args = dict(in_profit=True, scaled_out=True, stop_protects_profit=True, mtf_agrees=True,
                momentum_rising=True, continuation_score=.8, minimum_score=.7,
                age_minutes=10, max_minutes=60)
    assert may_extend(**args)
    assert not may_extend(**{**args, 'in_profit': False})
    assert not may_extend(**{**args, 'stop_protects_profit': False})
    assert not may_extend(**{**args, 'age_minutes': 60})


def test_early_exit_score_and_r_band():
    cfg={'early_exit_ema_fast':5,'early_exit_ema_slow':13,'early_exit_swing_lookback':5,
         'early_exit_rsi_period':14,'early_exit_roc_period':5,'early_exit_volume_lookback':20,
         'early_exit_volume_spike_ratio':1.5,'early_exit_weights':{'ema_cross':1,'swing_break':1,'momentum':1,'volume_spike':1},
         'early_exit_enabled':True,'early_exit_min_r':-.7,'early_exit_max_r':-.2,'early_exit_score_threshold':3}
    close=np.r_[np.arange(60,0,-1,dtype=float)]
    frame=pd.DataFrame({'open':close+0.2,'high':close+0.3,'low':close-0.3,'close':close,
                        'tick_volume':np.r_[np.full(59,10.),100.]})
    score,reasons=adverse_exit_score(frame,'buy',cfg)
    assert score>=3 and len(reasons)>=3
    assert should_exit_early(-.4,score,cfg)
    assert not should_exit_early(-.1,score,cfg)
    assert not should_exit_early(-.8,score,cfg)


def test_scale_out_respects_step_minimum_and_closes_all_if_unsplittable():
    assert scale_out_volume(.10,.01,.01,50)==(.05,False)
    assert scale_out_volume(.01,.01,.01,50)==(.01,True)


def test_trailing_stop_only_tightens_and_stagnation_rule():
    assert stop_only_tightens('buy',100,100.1)
    assert not stop_only_tightens('buy',100,99.9)
    assert stop_only_tightens('sell',100,99.9)
    assert not stop_only_tightens('sell',100,100.1)
    assert stagnation_due(4,.1,4,.2)
    assert not stagnation_due(3,.1,4,.2)


def test_extension_requires_cost_protecting_stop_and_rising_momentum():
    assert stop_protects_profit('buy',101,100,.5)
    assert not stop_protects_profit('buy',100.4,100,.5)
    assert stop_protects_profit('sell',99,100,.5)
    index=np.arange(100,dtype=float); rising_close=np.exp(index*.01+index**2*.00002)
    rising_frame=pd.DataFrame({'open':rising_close,'high':rising_close*1.001,'low':rising_close*.999,
                               'close':rising_close,'tick_volume':np.full(100,10.)})
    bars={tf:rising_frame for tf in ('M1','M5','M15')}
    aligned,rising,score=continuation_metrics(bars,'buy')
    assert aligned and rising and score==1
    assert not continuation_metrics({'M1':bars['M1']},'buy')[0]
