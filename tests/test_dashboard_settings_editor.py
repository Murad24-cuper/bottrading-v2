import shutil
from pathlib import Path

import pytest
import yaml

from src.dashboard import settings_editor as se

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / 'config.yaml'; shutil.copy((Path(__file__).resolve().parent / 'fixtures' / 'default_config.yaml'), path)
    return path


def test_every_field_resolves_in_shipped_config(cfg):
    values = se.current_values(se.load_document(cfg))
    missing = [f.path for f in se.FIELDS if values.get(f.path) is None]
    assert missing == []
    assert {f.section for f in se.FIELDS} <= set(se.SECTIONS)


def test_round_trip_without_changes_is_byte_identical(cfg):
    assert se.dump_document(se.load_document(cfg)) == cfg.read_text(encoding='utf-8')


def test_apply_changes_keeps_comments_and_quotes_times(cfg, tmp_path):
    doc = se.load_document(cfg)
    se.apply_changes(doc, {('risk', 'allow_multiple_positions_per_symbol'): False,
                           ('universe', 'trading_hours', 'start'): '10:00',
                           ('risk', 'reward_ratio'): 2.0},
                     {'GOLD': {'start': '04:00', 'end': '17:00'}})
    text = se.dump_document(doc)
    assert "start: '10:00'" in text and '#' in text
    data = yaml.safe_load(text)
    hours = data['universe']['trading_hours']
    assert hours['start'] == '10:00' and hours['per_symbol']['GOLD'] == {'start': '04:00', 'end': '17:00'}
    risk = data['risk']
    assert risk['tp_move_mult'] == pytest.approx(risk['sl_move_mult'] * 2.0)
    se.validate_text(text, ROOT)


def test_unknown_field_and_bad_time_are_rejected(cfg):
    doc = se.load_document(cfg)
    with pytest.raises(ValueError): se.apply_changes(doc, {('live', 'mode'): 'trade'})
    with pytest.raises(ValueError): se.apply_changes(doc, {('universe', 'trading_hours', 'start'): '25:99'})
    with pytest.raises(ValueError): se.apply_changes(doc, {}, {'GOLD': {'start': 'x', 'end': '17:00'}})


def test_values_outside_field_range_are_rejected(cfg):
    doc = se.load_document(cfg)
    with pytest.raises(ValueError): se.apply_changes(doc, {('demo_risk', 'risk_per_trade_pct'): 5.0})


def test_validation_rejects_demo_risk_above_locked_ceiling(cfg):
    text = cfg.read_text(encoding='utf-8')
    data = yaml.safe_load(text); data['demo_risk']['risk_per_trade_pct'] = 5.0
    with pytest.raises(ValueError, match='ceiling'): se.validate_text(yaml.safe_dump(data), ROOT)


def test_save_backs_up_and_restore_reverts(cfg, tmp_path):
    data_dir = tmp_path / 'data'; original = cfg.read_text(encoding='utf-8')
    doc = se.load_document(cfg); se.apply_changes(doc, {('risk', 'symbol_cooldown_minutes'): 15})
    backup = se.save(cfg, data_dir, se.dump_document(doc), ROOT)
    assert backup.read_text(encoding='utf-8') == original
    assert yaml.safe_load(cfg.read_text(encoding='utf-8'))['risk']['symbol_cooldown_minutes'] == 15
    se.restore(cfg, data_dir, backup, ROOT)
    assert cfg.read_text(encoding='utf-8') == original
    assert len(se.list_backups(data_dir)) == 2


def test_invalid_save_leaves_file_untouched(cfg, tmp_path):
    original = cfg.read_text(encoding='utf-8')
    with pytest.raises(ValueError): se.save(cfg, tmp_path / 'data', 'demo_risk: [', ROOT)
    assert cfg.read_text(encoding='utf-8') == original and se.list_backups(tmp_path / 'data') == []


def test_restore_refuses_files_outside_backup_folder(cfg, tmp_path):
    stray = tmp_path / 'other.yaml'; stray.write_text(cfg.read_text(encoding='utf-8'), encoding='utf-8')
    with pytest.raises(ValueError): se.restore(cfg, tmp_path / 'data', stray, ROOT)


def test_describe_changes_lists_only_changed_fields():
    path = ('risk', 'symbol_cooldown_minutes')
    lines = se.describe_changes({path: 0}, {path: 15})
    assert len(lines) == 1 and '0 → 15' in lines[0]
