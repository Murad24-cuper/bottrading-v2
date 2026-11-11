"""Open-position price charts: candles with entry, stop-loss, take-profit and price lines."""
from __future__ import annotations
from typing import Any

import altair as alt
import pandas as pd

UP='#16a34a'; DOWN='#dc2626'; ENTRY='#2563eb'; SL='#dc2626'; TP='#16a34a'; PRICE='#334155'


def chart_frame(payload: dict[str, Any], timeframe: str) -> pd.DataFrame:
    rows=(payload.get('bars') or {}).get(timeframe) or []
    frame=pd.DataFrame(rows)
    if frame.empty: return frame
    frame['time']=pd.to_datetime(frame['t'],utc=True).dt.tz_convert('Asia/Baku').dt.tz_localize(None)
    frame['up']=frame['c']>=frame['o']
    return frame


def level_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Horizontal reference lines; missing levels are skipped."""
    levels=[('Giriş',payload.get('open_price'),ENTRY,'solid'),('SL',payload.get('sl'),SL,'dashed'),
            ('TP',payload.get('tp'),TP,'dashed'),('Cari',payload.get('current'),PRICE,'dotted')]
    return [{'name':name,'price':float(price),'color':color,'dash':dash} for name,price,color,dash in levels if price]


def position_chart(payload: dict[str, Any], timeframe: str='M1', height: int=300) -> alt.LayerChart|None:
    frame=chart_frame(payload,timeframe)
    if frame.empty: return None
    levels=pd.DataFrame(level_rows(payload))
    prices=list(frame['l'])+list(frame['h'])+(list(levels['price']) if not levels.empty else [])
    low,high=min(prices),max(prices); pad=(high-low)*.06 or abs(high)*.0005 or 1
    y=alt.Y('l:Q',scale=alt.Scale(domain=[low-pad,high+pad],zero=False),title=None,axis=alt.Axis(format='~r',grid=True,gridOpacity=.15))
    x=alt.X('time:T',title=None,axis=alt.Axis(format='%H:%M',labelAngle=0,grid=False))
    color=alt.condition('datum.up',alt.value(UP),alt.value(DOWN))
    tooltip=[alt.Tooltip('time:T',title='Vaxt',format='%d.%m %H:%M'),alt.Tooltip('o:Q',title='Açılış'),alt.Tooltip('h:Q',title='Yüksək'),
             alt.Tooltip('l:Q',title='Aşağı'),alt.Tooltip('c:Q',title='Bağlanış')]
    base=alt.Chart(frame).encode(x=x)
    wicks=base.mark_rule(strokeWidth=1).encode(y=y,y2='h:Q',color=color,tooltip=tooltip)
    bodies=base.mark_bar(size=max(2,int(560/max(len(frame),1))-1)).encode(y=alt.Y('o:Q'),y2='c:Q',color=color,tooltip=tooltip)
    layers=[wicks,bodies]
    if not levels.empty:
        for dash,pattern in (('solid',[]),('dashed',[6,4]),('dotted',[2,3])):
            part=levels[levels.dash==dash]
            if part.empty: continue
            lines=alt.Chart(part).mark_rule(strokeWidth=1.6,strokeDash=pattern).encode(y='price:Q',color=alt.Color('color:N',scale=None),tooltip=[alt.Tooltip('name:N',title='Səviyyə'),alt.Tooltip('price:Q',title='Qiymət')])
            labels=alt.Chart(part).mark_text(align='left',dx=4,dy=-6,fontSize=11,fontWeight='bold').encode(
                x=alt.value(4),y='price:Q',text=alt.Text('name:N'),color=alt.Color('color:N',scale=None))
            layers.extend([lines,labels])
    opened=pd.to_datetime(payload.get('open_time'),utc=True,errors='coerce')
    if not pd.isna(opened) and frame['time'].min()<=opened.tz_convert('Asia/Baku').tz_localize(None)<=frame['time'].max():
        marker=pd.DataFrame([{'time':opened.tz_convert('Asia/Baku').tz_localize(None)}])
        layers.append(alt.Chart(marker).mark_rule(color=ENTRY,strokeDash=[2,4],opacity=.7).encode(x='time:T'))
    return alt.layer(*layers).properties(height=height).configure_view(strokeWidth=0).configure(background='transparent')


def r_multiple(payload: dict[str, Any]) -> float|None:
    """Current distance from entry in units of the stop distance (1R = SL distance)."""
    entry=payload.get('open_price'); sl=payload.get('sl'); current=payload.get('current')
    if not entry or not sl or current is None or entry==sl: return None
    risk=abs(float(entry)-float(sl)); move=float(current)-float(entry)
    return (move if payload.get('direction')=='buy' else -move)/risk
