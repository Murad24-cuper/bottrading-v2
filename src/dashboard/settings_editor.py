"""Safe editing of config.yaml from the dashboard.

Every save: (1) applies only whitelisted fields, (2) keeps comments/format
(ruamel round-trip), (3) validates the result with the bot's own loader and its
locked risk ceilings, (4) backs up the previous file, (5) writes atomically.
Nothing outside FIELDS can be changed from the panel.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import io
import os
import re

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import SingleQuotedScalarString as Quoted


@dataclass(frozen=True)
class Field:
    path: tuple[str, ...]
    label: str
    kind: str                      # bool | int | float | time | choice | symbols
    section: str
    help: str = ''
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    choices: tuple = field(default_factory=tuple)


SECTIONS = ('Alətlər', 'Saatlar', 'Risk (demo)', 'Əmr idarəsi', 'Mövqe idarəsi', 'Təkrar əmr qaydaları', 'Xəbər filtri')

FIELDS: tuple[Field, ...] = (
    Field(('universe', 'focus_symbols'), 'Ticarət olunan alətlər', 'symbols', 'Alətlər', 'Bot yalnız bu alətlərdə yeni əmr açır.'),
    Field(('universe', 'trading_hours', 'enabled'), 'Saat qaydası aktivdir', 'bool', 'Saatlar', 'Söndürülsə, bot alət açıq olan hər an əmr açır.'),
    Field(('universe', 'trading_hours', 'timezone'), 'Saat qurşağı', 'choice', 'Saatlar', 'Saatlar bu şəhərin vaxtı ilə yazılır; yay/qış keçidi avtomatikdir.',
          choices=('America/New_York', 'Europe/London', 'Asia/Baku', 'UTC')),
    Field(('universe', 'trading_hours', 'start'), 'Başlanğıc (indekslər və digərləri)', 'time', 'Saatlar'),
    Field(('universe', 'trading_hours', 'end'), 'Son (indekslər və digərləri)', 'time', 'Saatlar'),
    Field(('demo_risk', 'risk_per_trade_pct'), 'Hər əmrdə risk, %', 'float', 'Risk (demo)', 'Balansın neçə faizi bir əmrdə riskə atılır.', .1, 2.0, .1),
    Field(('demo_risk', 'daily_loss_limit_pct'), 'Gündəlik zərər limiti, %', 'float', 'Risk (demo)', 'Bu həddə çatanda bot həmin gün yeni əmr açmır.', 1.0, 10.0, .5),
    Field(('demo_risk', 'weekly_loss_limit_pct'), 'Həftəlik zərər limiti, %', 'float', 'Risk (demo)', '', 1.0, 25.0, .5),
    Field(('demo_risk', 'rolling_drawdown_pct'), '24 saatda düşüşdə ümumi fasilə, %', 'float', 'Risk (demo)', 'Kapital 24 saatda bu qədər düşsə, bütün yeni əmrlər 24 saat dayanır.', 1.0, 10.0, .5),
    Field(('demo_risk', 'min_lot_max_risk_pct'), 'Kiçik balansda minimum lotun maksimum riski, %', 'float', 'Risk (demo)', '0 = söndürülüb.', 0.0, 5.0, .5),
    Field(('demo_exploration', 'max_open_positions'), 'Eyni anda ən çox mövqe', 'int', 'Risk (demo)', '', 1, 3, 1),
    Field(('demo_exploration', 'max_trades_per_day'), 'Gündə ən çox əmr', 'int', 'Risk (demo)', '', 1, 150, 1),
    Field(('risk', 'reward_ratio'), 'Risk / qazanc nisbəti (TP = risk × bu ədəd)', 'choice', 'Əmr idarəsi', '1.5 = 10$ riskə 15$ hədəf.',
          choices=(1.0, 1.25, 1.5, 2.0, 2.5, 3.0)),
    Field(('risk', 'stagnation_minutes'), 'Hərəkətsiz əmri bağlamaq, dəqiqə', 'int', 'Əmr idarəsi', 'Bu müddətdə kifayət qədər irəliləməyən əmr bağlanır.', 3, 120, 1),
    Field(('risk', 'max_hold_minutes'), 'Maksimum saxlama müddəti, dəqiqə', 'int', 'Əmr idarəsi', 'SL/TP də bu müddətə uyğun hesablanır.', 5, 240, 5),
    Field(('demo_exploration', 'stop_mode'), 'SL/TP-nin hesablanması', 'choice', 'Əmr idarəsi', 'horizon = saxlama müddətinə uyğun; strategy = strategiyanın öz (uzaq) səviyyələri.',
          choices=('horizon', 'strategy')),
    Field(('risk', 'early_exit_enabled'), 'Pis gedən əmri erkən bağla', 'bool', 'Mövqe idarəsi',
          'Zərərdə olan əmrdə əleyhinə əlamətlər yığılanda bot SL-i gözləmədən bağlayır.'),
    Field(('risk', 'early_exit_score_threshold'), 'Erkən bağlama üçün lazım olan əlamət sayı (4-dən)', 'int', 'Mövqe idarəsi',
          '2 = tez bağlayır (daha çox yalançı siqnal), 4 = yalnız hamısı birləşəndə.', 2, 4, 1),
    Field(('risk', 'early_exit_max_r'), 'Erkən bağlama nə vaxtdan başlasın (R)', 'float', 'Mövqe idarəsi',
          '-0.20 = riskin 20%-i qədər zərərdən sonra. 0-a yaxın olduqca daha tez işə düşür.', -0.6, -0.05, .05),
    Field(('risk', 'max_hold_extended_minutes'), 'Yaxşı gedən əmr üçün maksimum müddət, dəqiqə', 'int', 'Mövqe idarəsi',
          'Qazancda olan və davam edən əmr bu müddətə qədər saxlanıla bilər. Saxlama müddətinə bərabər = uzatma yoxdur.', 5, 240, 5),
    Field(('risk', 'extend_requires_scale_out'), 'Uzatmaq üçün əvvəl qazancın bir hissəsi götürülsün', 'bool', 'Mövqe idarəsi',
          'Söndürülsə, bot qazancdakı əmrin stopunu girişə (xərclər daxil) çəkib vaxtını uzadır.'),
    Field(('scanner', 'extend_min_score'), 'Uzatma üçün lazım olan davam etmə balı', 'choice', 'Mövqe idarəsi',
          '1.0 = həm qrafiklər eyni istiqamətdə, həm hərəkət güclənir; 0.5 = biri kifayətdir.', choices=(0.5, 1.0)),
    Field(('risk', 'allow_multiple_positions_per_symbol'), 'Eyni alətdə bir neçə mövqe', 'bool', 'Təkrar əmr qaydaları'),
    Field(('risk', 'symbol_cooldown_minutes'), 'Əmrdən sonra gözləmə, dəqiqə', 'int', 'Təkrar əmr qaydaları', '0 = gözləmə yoxdur.', 0, 240, 5),
    Field(('risk', 'max_trades_per_symbol_per_day'), 'Bir alətdə gündə ən çox əmr', 'int', 'Təkrar əmr qaydaları', '0 = limit yoxdur.', 0, 100, 1),
    Field(('protections', 'symbol_pauses_enabled'), '3 zərərdən sonra alətdə 24 saat fasilə', 'bool', 'Təkrar əmr qaydaları'),
    Field(('news', 'enabled'), 'Xəbər filtri aktivdir', 'bool', 'Xəbər filtri', 'Vacib iqtisadi xəbərlər ətrafında yeni əmr açılmır.'),
    Field(('news', 'blackout_minutes_before'), 'Xəbərdən əvvəl, dəqiqə', 'int', 'Xəbər filtri', '', 0, 120, 5),
    Field(('news', 'blackout_minutes_after'), 'Xəbərdən sonra, dəqiqə', 'int', 'Xəbər filtri', '', 0, 120, 5),
)
FIELD_BY_PATH = {f.path: f for f in FIELDS}
TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')


def _yaml() -> YAML:
    y = YAML(); y.preserve_quotes = True; y.width = 4096
    return y


def load_document(path: Path):
    return _yaml().load(Path(path).read_text(encoding='utf-8'))


def dump_document(doc) -> str:
    out = io.StringIO(); _yaml().dump(doc, out)
    return out.getvalue()


def _get(doc, path):
    node = doc
    for key in path:
        if node is None or key not in node: return None
        node = node[key]
    return node


def current_values(doc) -> dict[tuple, Any]:
    """Current value of every editable field (reward ratio is derived from the SL/TP multipliers)."""
    values = {f.path: _get(doc, f.path) for f in FIELDS}
    risk = doc.get('risk', {})
    try: values[('risk', 'reward_ratio')] = round(float(risk['tp_move_mult']) / float(risk['sl_move_mult']), 2)
    except (KeyError, TypeError, ValueError, ZeroDivisionError): values[('risk', 'reward_ratio')] = None
    return values


def symbol_hours(doc) -> dict[str, dict]:
    """Per-symbol trading-hours overrides, e.g. {'GOLD': {'start': '04:00', 'end': '17:00'}}."""
    per = _get(doc, ('universe', 'trading_hours', 'per_symbol')) or {}
    return {str(k): {'start': str(v.get('start', '')), 'end': str(v.get('end', ''))} for k, v in per.items()}


def known_symbols(doc) -> list[str]:
    aliases = _get(doc, ('universe', 'symbol_aliases')) or {}
    focus = [str(s) for s in (_get(doc, ('universe', 'focus_symbols')) or [])]
    return list(dict.fromkeys([*focus, *[str(k) for k in aliases]]))


def _coerce(f: Field, value):
    if f.kind == 'bool':
        if not isinstance(value, bool): raise ValueError(f'{f.label}: bəli/xeyr olmalıdır')
        return value
    if f.kind == 'int':
        number = int(value)
        if float(number) != float(value): raise ValueError(f'{f.label}: tam ədəd olmalıdır')
    elif f.kind == 'float':
        number = round(float(value), 4)
    elif f.kind == 'time':
        text = str(value).strip()
        if not TIME_RE.match(text): raise ValueError(f'{f.label}: saat SS:DD formatında olmalıdır (məs. 09:30)')
        return Quoted(text)   # unquoted 09:30 would be read by YAML 1.1 as a number
    elif f.kind == 'choice':
        if value not in f.choices: raise ValueError(f'{f.label}: icazə verilməyən seçim {value!r}')
        return value
    elif f.kind == 'symbols':
        items = [str(v).strip() for v in value if str(v).strip()]
        if not items: raise ValueError('Ən azı bir alət seçilməlidir')
        return list(dict.fromkeys(items))
    else:
        raise ValueError(f'Naməlum sahə növü: {f.kind}')
    if f.minimum is not None and number < f.minimum: raise ValueError(f'{f.label}: ən azı {f.minimum:g}')
    if f.maximum is not None and number > f.maximum: raise ValueError(f'{f.label}: ən çoxu {f.maximum:g}')
    return number


def _set(doc, path, value):
    node = doc
    for key in path[:-1]:
        if key not in node or node[key] is None: node[key] = {}
        node = node[key]
    if path[-1] in node and isinstance(value, list):
        # Keep the flow style ([a, b]) used in config.yaml.
        existing = node[path[-1]]
        existing.clear(); existing.extend(value)
    else:
        node[path[-1]] = value


def apply_changes(doc, changes: dict[tuple, Any], hours_overrides: dict[str, dict] | None = None):
    """Apply whitelisted changes to the ruamel document in place. Raises ValueError on bad input."""
    for path, value in changes.items():
        f = FIELD_BY_PATH.get(tuple(path))
        if f is None: raise ValueError(f'Bu ayar paneldən dəyişdirilə bilməz: {".".join(path)}')
        clean = _coerce(f, value)
        if f.path == ('risk', 'reward_ratio'):
            sl = float(doc['risk']['sl_move_mult'])
            doc['risk']['tp_move_mult'] = round(sl * float(clean), 4)
            continue
        _set(doc, f.path, clean)
        if f.path == ('risk', 'max_hold_minutes'):
            # Keep dependent settings consistent with the holding time.
            if int(doc['risk'].get('max_hold_extended_minutes', clean)) < clean: doc['risk']['max_hold_extended_minutes'] = clean
            doc['risk']['move_horizon_minutes'] = clean
            if 'demo_exploration' in doc: doc['demo_exploration']['move_horizon_minutes'] = clean
    risk = doc.get('risk', {})
    if 'max_hold_extended_minutes' in risk and int(risk['max_hold_extended_minutes']) < int(risk.get('max_hold_minutes', 0)):
        risk['max_hold_extended_minutes'] = int(risk['max_hold_minutes'])
    if hours_overrides is not None:
        hours = doc['universe'].setdefault('trading_hours', {})
        per = {}
        for symbol, window in hours_overrides.items():
            start, end = str(window.get('start', '')).strip(), str(window.get('end', '')).strip()
            if not (TIME_RE.match(start) and TIME_RE.match(end)): raise ValueError(f'{symbol}: saat SS:DD formatında olmalıdır')
            per[str(symbol)] = {'start': Quoted(start), 'end': Quoted(end)}
        if 'per_symbol' in hours and hasattr(hours['per_symbol'], 'clear'):
            target = hours['per_symbol']; target.clear()
            for symbol, window in per.items():
                from ruamel.yaml.comments import CommentedMap
                entry = CommentedMap(window); entry.fa.set_flow_style(); target[symbol] = entry
        elif per:
            from ruamel.yaml.comments import CommentedMap
            target = CommentedMap()
            for symbol, window in per.items():
                entry = CommentedMap(window); entry.fa.set_flow_style(); target[symbol] = entry
            hours['per_symbol'] = target
    return doc


def validate_text(text: str, root: Path) -> None:
    """Validate with the bot's own loader (schema + locked risk ceilings). Raises ValueError."""
    import tempfile
    from src.config import load_settings
    folder = Path(tempfile.mkdtemp(prefix='cfgcheck-'))
    try:
        candidate = folder / 'config.yaml'; candidate.write_text(text, encoding='utf-8')
        env = Path(root) / '.env'
        try:
            load_settings(config_path=candidate, env_path=env if env.exists() else folder / '.env', require_credentials=False)
        except ValueError: raise
        except Exception as exc: raise ValueError(str(exc)) from exc
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)


def backup_dir(data_dir: Path) -> Path:
    return Path(data_dir) / 'config_backups'


def list_backups(data_dir: Path) -> list[Path]:
    folder = backup_dir(data_dir)
    return sorted(folder.glob('config-*.yaml'), reverse=True) if folder.is_dir() else []


def save(config_path: Path, data_dir: Path, text: str, root: Path, keep: int = 30) -> Path:
    """Validate, back up the current file, then replace it atomically. Returns the backup path."""
    validate_text(text, root)
    config_path = Path(config_path)
    folder = backup_dir(data_dir); folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    backup = folder / f'config-{stamp}.yaml'
    backup.write_bytes(config_path.read_bytes())
    tmp = config_path.with_suffix('.yaml.tmp')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, config_path)
    for old in list_backups(data_dir)[max(1, int(keep)):]:
        try: old.unlink()
        except OSError: pass
    return backup


def restore(config_path: Path, data_dir: Path, backup: Path, root: Path) -> Path:
    """Restore a backup (itself validated and backed up first, so restores can be undone)."""
    backup = Path(backup)
    if backup.parent.resolve() != backup_dir(data_dir).resolve(): raise ValueError('Yalnız panelin ehtiyat nüsxələri bərpa oluna bilər')
    return save(config_path, data_dir, backup.read_text(encoding='utf-8'), root)


def describe_changes(before: dict, after: dict) -> list[str]:
    lines = []
    for f in FIELDS:
        old, new = before.get(f.path), after.get(f.path)
        if old != new and new is not None: lines.append(f'{f.label}: {old} → {new}')
    return lines
