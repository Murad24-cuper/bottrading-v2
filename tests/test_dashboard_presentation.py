from src.dashboard.presentation import (
    close_reason_label, direction_label, format_duration, format_money,
    feature_label, local_time, strategy_label, translate_message,
)


def test_azerbaijani_labels_and_money_formatting():
    assert direction_label('buy')=='Alış'
    assert direction_label('sell')=='Satış'
    assert strategy_label('trend_pullback')=='Trend'
    assert strategy_label('mean_reversion')=='Geri dönüş'
    assert strategy_label('breakout')=='Qırılma'
    assert strategy_label('volatility_scalp')=='Sürətli'
    assert close_reason_label('partial_tp')=='Qazancın bir hissəsi götürüldü'
    assert close_reason_label('trail')=='Qazanc qorundu (izləyən stop)'
    assert close_reason_label('early_exit')=='Erkən çıxış - risk artdı'
    assert close_reason_label('extended')=='Uzadılıb (qazanc qorunur)'
    assert close_reason_label('time')=='Vaxt bitdi'
    assert format_money(1010.09)=='1,010.09 $'
    assert format_money(1.2,signed=True)=='+1.20 $'
    assert format_money(-1.2,signed=True)=='-1.20 $'


def test_duration_and_local_time_are_azerbaijan_time():
    assert format_duration(12.9)=='12 dəq'
    assert format_duration(65)=='1 saat 5 dəq'
    assert format_duration(60)=='1 saat'
    assert local_time('2026-11-06T07:00:00+00:00')=='11:00'


def test_bot_messages_are_translated():
    assert translate_message('daily loss limit reached')=='Günlük zərər limiti aşılıb'
    assert 'Skanlama' in translate_message('Scan total=100 scanned=100 complete=True')
    assert translate_message('rsi_not_overbought')=='RSI həddən artıq alış zonasını göstərmir'
    assert 'Dəyişiklik yoxdur' in translate_message('Modify SL/TP ticket=1 success=False error=No changes')
    assert feature_label('observations.with_usd_context=true')=='müşahidə.USD konteksti ilə=bəli'


def test_every_configured_strategy_has_a_label_and_unknown_is_readable():
    import yaml
    from pathlib import Path
    from src.dashboard.presentation import STRATEGY_LABELS
    config = yaml.safe_load((Path(__file__).resolve().parent / 'fixtures' / 'default_config.yaml').read_text(encoding='utf-8'))
    assert set(config['live']['strategies']) <= set(STRATEGY_LABELS)
    assert strategy_label('session_breakout') == 'Sessiya qırılması'
    assert strategy_label('') == 'Bərpa olunub (MT5-dən)'
    assert strategy_label('new_idea') == 'New idea'
