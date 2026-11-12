"""Pure trade-history analysis for the dashboard (no Streamlit; testable)."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable


def _minutes(row) -> float | None:
    try:
        opened = datetime.fromisoformat(str(row['open_time']).replace('Z', '+00:00'))
        closed = datetime.fromisoformat(str(row['close_time']).replace('Z', '+00:00'))
        return (closed - opened).total_seconds() / 60
    except (KeyError, TypeError, ValueError):
        return None


def summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    nets = [float(r.get('net') or 0) for r in rows]
    wins = [n for n in nets if n > 0]; losses = [n for n in nets if n < 0]
    durations = [m for m in (_minutes(r) for r in rows) if m is not None]
    gross_loss = -sum(losses)
    return {'trades': len(rows), 'wins': len(wins), 'win_rate': len(wins) / len(rows) if rows else 0.0,
            'total': sum(nets), 'average': sum(nets) / len(rows) if rows else 0.0,
            'average_win': sum(wins) / len(wins) if wins else 0.0, 'average_loss': sum(losses) / len(losses) if losses else 0.0,
            'profit_factor': (sum(wins) / gross_loss) if gross_loss else (float('inf') if wins else 0.0),
            'best': max(nets) if nets else 0.0, 'worst': min(nets) if nets else 0.0,
            'costs': sum(float(r.get('est_cost') or 0) for r in rows),
            'average_minutes': sum(durations) / len(durations) if durations else None}


def group_by(rows: Iterable[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Summary per value of `key` (e.g. close_reason, symbol), worst total first."""
    buckets: dict[str, list] = defaultdict(list)
    for row in rows: buckets[str(row.get(key) or '?')].append(row)
    out = [{'name': name, **summary(items)} for name, items in buckets.items()]
    return sorted(out, key=lambda item: item['total'])


def daily_totals(rows: Iterable[dict[str, Any]], tz) -> list[dict[str, Any]]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        try: day = datetime.fromisoformat(str(row['close_time']).replace('Z', '+00:00')).astimezone(tz).date().isoformat()
        except (KeyError, TypeError, ValueError): continue
        totals[day] += float(row.get('net') or 0)
    return [{'date': day, 'pnl': value} for day, value in sorted(totals.items())]


def insights(rows: Iterable[dict[str, Any]]) -> list[str]:
    """Short plain-language findings (Azerbaijani) for the history page."""
    rows = list(rows); notes = []
    if len(rows) < 20: notes.append(f'Cəmi {len(rows)} əmr var: nəticələr hələ təsadüfi ola bilər (ən azı 50–100 lazımdır).')
    if not rows: return notes
    reasons = group_by(rows, 'close_reason')
    top = max(reasons, key=lambda item: item['trades'])
    if top['trades'] / len(rows) >= .5:
        notes.append(f'Əmrlərin {top["trades"] / len(rows):.0%}-i "{top["name"]}" səbəbi ilə bağlanıb.')
    s = summary(rows)
    if s['costs'] and s['total'] < 0 < s['total'] + s['costs']:
        notes.append(f'Xərcdən əvvəl nəticə müsbətdir ({s["total"] + s["costs"]:+.2f} $); zərəri spred/komissiya yaradır.')
    if s['average_win'] and s['average_loss'] and abs(s['average_win']) < abs(s['average_loss']) * .5:
        notes.append('Orta qazanc orta zərərin yarısından azdır: qazanclar tez bağlanır.')
    return notes


def exit_review_summary(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per close reason: how often closing was better than keeping the trade (review_delta < 0)."""
    groups: dict[str, list[dict]] = {}
    for row in rows: groups.setdefault(str(row.get('close_reason') or ''), []).append(row)
    out = []
    for reason, items in groups.items():
        priced = [float(r['review_delta']) for r in items if r.get('review_delta') is not None]
        helped = sum(1 for v in priced if v < 0); hurt = sum(1 for v in priced if v > 0)
        out.append({'reason': reason, 'trades': len(items), 'helped': helped, 'hurt': hurt,
                    'saved': round(-sum(priced), 2),
                    'would_sl': sum(1 for r in items if r.get('review_outcome') == 'sl'),
                    'would_tp': sum(1 for r in items if r.get('review_outcome') == 'tp')})
    return sorted(out, key=lambda g: -g['trades'])


def by_hour(rows: Iterable[dict[str, Any]], tz) -> list[dict[str, Any]]:
    """Results grouped by the local hour the trade was OPENED (0-23), only hours that have trades."""
    buckets: dict[int, list[float]] = {}
    for row in rows:
        try: stamp = datetime.fromisoformat(str(row.get('open_time')))
        except (TypeError, ValueError): continue
        if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=timezone.utc)
        buckets.setdefault(stamp.astimezone(tz).hour, []).append(float(row.get('net') or 0))
    out = []
    for hour in sorted(buckets):
        values = buckets[hour]
        out.append({'hour': hour, 'trades': len(values), 'win_rate': sum(1 for v in values if v > 0) / len(values),
                    'average': sum(values) / len(values), 'total': round(sum(values), 2)})
    return out
