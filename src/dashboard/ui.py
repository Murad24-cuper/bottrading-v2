"""Visual building blocks for the dashboard: theme CSS and small HTML cards.

Responsive by design: cards wrap on narrow screens, tables scroll sideways,
buttons are large enough to tap. Text passed in is escaped."""
from __future__ import annotations
from html import escape

TONES={'good':'#16a34a','warn':'#d97706','bad':'#dc2626','neutral':'#64748b','info':'#2563eb'}

CSS="""
<style>
:root{--card-bg:#ffffff;--card-border:#e2e8f0;--muted:#64748b}
.block-container{padding-top:1.2rem;padding-bottom:3rem;max-width:1400px}
h3{margin-top:1.4rem !important}
.hero{border-radius:14px;padding:14px 18px;margin:4px 0 14px;color:#fff;display:flex;flex-wrap:wrap;gap:6px 18px;align-items:center;justify-content:space-between}
.hero-title{font-size:1.25rem;font-weight:750;line-height:1.3}
.hero-sub{font-size:.85rem;opacity:.92}
.pills{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 12px}
.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;font-size:.8rem;font-weight:600;border:1px solid var(--card-border);background:var(--card-bg)}
.pill i{width:8px;height:8px;border-radius:50%;display:inline-block}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:0 0 12px}
.kpi{background:var(--card-bg);border:1px solid var(--card-border);border-radius:12px;padding:12px 14px;min-height:88px;box-shadow:0 1px 2px rgba(15,23,42,.05)}
.kpi-label{font-size:.8rem;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.03em}
.kpi-value{font-size:clamp(1.25rem,2.6vw,1.75rem);font-weight:750;margin-top:4px;font-variant-numeric:tabular-nums}
.kpi-detail{font-size:.78rem;color:var(--muted);margin-top:2px}
.limit{background:var(--card-bg);border:1px solid var(--card-border);border-radius:12px;padding:10px 14px;margin:0 0 10px;box-shadow:0 1px 2px rgba(15,23,42,.05)}
.limit-head{display:flex;justify-content:space-between;gap:8px;font-size:.85rem;font-weight:650;flex-wrap:wrap}
.limit-track{height:10px;border-radius:6px;background:#e2e8f0;margin-top:8px;overflow:hidden}
.limit-fill{height:100%;border-radius:6px}
.limit-foot{font-size:.75rem;color:var(--muted);margin-top:5px}
.note{font-size:.8rem;color:var(--muted)}
div[data-testid="stButton"] button{min-height:3rem;font-weight:650;border-radius:10px}
div[data-testid="stDataFrame"]{border-radius:10px;overflow:hidden;border:1px solid var(--card-border)}
div[data-testid="stVerticalBlockBorderWrapper"]{background:#fff}
div[data-testid="stTable"]{overflow-x:auto}
@media (max-width:640px){
  .block-container{padding-left:.6rem;padding-right:.6rem;padding-top:.6rem}
  .hero{padding:12px 14px}.hero-title{font-size:1.05rem}
  .kpis{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
  .kpi{padding:10px 11px;min-height:76px}.kpi-label{font-size:.7rem}
  div[data-testid="stButton"] button{min-height:3.3rem;font-size:1rem}
  button[data-baseweb="tab"]{padding-left:.6rem !important;padding-right:.6rem !important}
  h3{font-size:1.1rem !important}
}
</style>
"""


def hero(title: str, tone: str, subtitle: str='') -> str:
    color=TONES.get(tone,TONES['neutral'])
    sub=f'<div class="hero-sub">{escape(subtitle)}</div>' if subtitle else ''
    return f'<div class="hero" style="background:linear-gradient(135deg,{color},{color}cc)"><div class="hero-title">{escape(title)}</div>{sub}</div>'


def pills(items: list[tuple[str,str]]) -> str:
    """items: (text, tone)"""
    body=''.join(f'<span class="pill"><i style="background:{TONES.get(tone,TONES["neutral"])}"></i>{escape(text)}</span>' for text,tone in items)
    return f'<div class="pills">{body}</div>'


def kpis(items: list[tuple[str,str,str,str]]) -> str:
    """items: (label, value, tone, detail); tone '' keeps the default text colour."""
    cards=[]
    for label,value,tone,detail in items:
        style=f' style="color:{TONES[tone]}"' if tone in TONES else ''
        cards.append(f'<div class="kpi"><div class="kpi-label">{escape(label)}</div><div class="kpi-value"{style}>{escape(value)}</div><div class="kpi-detail">{escape(detail)}</div></div>')
    return f'<div class="kpis">{"".join(cards)}</div>'


def limit_bar(label: str, used_share: float, right: str, foot: str='') -> str:
    share=max(0.0,min(1.0,float(used_share)))
    tone='bad' if share>=.8 else 'warn' if share>=.5 else 'good'
    foot_html=f'<div class="limit-foot">{escape(foot)}</div>' if foot else ''
    return (f'<div class="limit"><div class="limit-head"><span>{escape(label)}</span><span>{escape(right)}</span></div>'
            f'<div class="limit-track"><div class="limit-fill" style="width:{share*100:.1f}%;background:{TONES[tone]}"></div></div>{foot_html}</div>')
