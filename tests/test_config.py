from pathlib import Path
from src import config as config_module

def test_runtime_location_guard_blocks_wrong_machine():
    import pytest
    from src.config import assert_runtime_location
    with pytest.raises(RuntimeError,match='requires .server.'):
        assert_runtime_location({'runtime':{'location':'server'}},'local')
    assert_runtime_location({'runtime':{'location':'server'}},'server')

def test_default_config_loads_for_offline_work(monkeypatch):
    monkeypatch.setattr(config_module,'load_dotenv',lambda *args,**kwargs:False)
    root=Path(__file__).resolve().parents[1]
    settings=config_module.load_settings((Path(__file__).resolve().parent / 'fixtures' / 'default_config.yaml'),env_path='deliberately-not-loaded.env',require_credentials=False)
    assert settings.config['account']['mode']=='demo'
    assert settings.risk.max_hold_minutes==30
    assert settings.risk.stagnation_minutes==10
    assert settings.config['strategies']['breakout']['enabled'] is False
    assert settings.risk.first_target_r==0.8
    assert settings.risk.scale_out_pct==50
    assert settings.risk.risk_per_trade_pct==0.25
    assert settings.config['scanner']['min_confidence']==0.55
    assert settings.config['scanner']['require_mtf_agreement'] is True
    assert settings.config['risk']['min_risk_reward']==1.2
    assert settings.risk.no_entry_before_close_minutes==60
    assert settings.risk.max_session_gap_hours==6
    assert settings.config['scanner']['top_movers']==40
    assert settings.config['scanner']['mtf_min_agree']==2
    assert settings.config['scanner']['target_open_positions']==10
    assert settings.config['brain']['acceptance']['min_symbols']==7
    assert settings.risk.max_open_positions==20
    assert settings.risk.max_margin_usage_pct==50
    assert settings.config['scanner']['max_seconds_per_cycle']>0
    assert settings.config['history']['ranges_months']=={'M1':12,'M5':36,'M15':36,'H1':60,'H4':120,'D1':240}
    assert settings.config['schedule']['daily_backup_keep']==7
    assert settings.config['db']['backup_keep_days']==7


def test_locked_risk_ceilings_allow_tightening_but_reject_increases():
    import pytest
    from src.config import validate_risk_ceilings
    risk={'risk_per_trade_pct':.25,'daily_loss_limit_pct':3,'weekly_loss_limit_pct':8,
          'max_open_positions':20,'max_positions_per_group':6,'max_margin_usage_pct':50,
          'max_total_open_risk_pct':5,'max_cost_to_target_ratio':.3,'min_risk_reward':1.2}
    validate_risk_ceilings({'risk':risk})
    tighter={**risk,'risk_per_trade_pct':.1,'max_open_positions':2,'max_margin_usage_pct':20}
    validate_risk_ceilings({'risk':tighter})
    for key,value in (('risk_per_trade_pct',.26),('weekly_loss_limit_pct',8.1),('max_total_open_risk_pct',5.1),('min_risk_reward',1.1)):
        with pytest.raises(ValueError,match='locked'):
            validate_risk_ceilings({'risk':{**risk,key:value}})
def test_config_rejects_zero_risk_percentage(tmp_path,monkeypatch):
    import yaml
    monkeypatch.setattr(config_module,'load_dotenv',lambda *args,**kwargs:False)
    root=Path(__file__).resolve().parents[1]
    data=yaml.safe_load(((Path(__file__).resolve().parent / 'fixtures' / 'default_config.yaml')).read_text(encoding='utf-8'))
    data['risk']['risk_per_trade_pct']=0
    path=tmp_path/'config.yaml'; path.write_text(yaml.safe_dump(data),encoding='utf-8')
    try: config_module.load_settings(path,env_path='absent.env',require_credentials=False)
    except ValueError as exc: assert 'risk.risk_per_trade_pct' in str(exc)
    else: raise AssertionError('zero risk percentage was accepted')


def test_users_own_config_is_valid(monkeypatch):
    """config.yaml may be changed from the dashboard; it only has to stay valid (exact values are not checked)."""
    monkeypatch.setattr(config_module,'load_dotenv',lambda *args,**kwargs:False)
    root=Path(__file__).resolve().parents[1]
    config_module.load_settings(root/'config.yaml',env_path='deliberately-not-loaded.env',require_credentials=False)
