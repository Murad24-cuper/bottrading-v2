"""Trading-bot dashboard: overview, positions, history, settings, brain and system pages.

Reads SQLite and local files only; actions go to the bot as queued commands."""
from __future__ import annotations
from datetime import datetime,timezone,time as dt_time,timedelta
from pathlib import Path
import hmac,os,subprocess,time,json
import sys
import yaml
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from src.dashboard import queries
from src.dashboard.readiness import readiness_checks, simulate_small_account
from src.paths import project_paths
from src.parquet_compat import assert_fastparquet_available
from src.dashboard import ui
from src.dashboard import history as trade_history
from src.dashboard import settings_editor
from src.dashboard.overview import brain_overview, risk_overview, runtime_label, stop_state
from src.dashboard.presentation import (AZ_TZ, close_reason_label,
                                        direction_label, format_duration,
                                        format_money, local_time,
                                        strategy_label, translate_message)
assert_fastparquet_available()
load_dotenv(ROOT/'.env',override=False)
DATA_DIR,LOG_DIR=project_paths(ROOT)
_APP_CONFIG=yaml.safe_load((ROOT/'config.yaml').read_text(encoding='utf-8'))
_configured_db=Path(_APP_CONFIG.get('db_path','data/bot.db'))
DB_PATH=Path(os.getenv('BOT_DB_PATH','')) if os.getenv('BOT_DB_PATH') else (_configured_db if _configured_db.is_absolute() else ROOT/_configured_db)

def _lock_pid():
    try: return int((DATA_DIR/'bot.lock').read_text(encoding='utf-8').split()[0])
    except (OSError,ValueError,IndexError): return None

def _supervisor_pid():
    try: return int((DATA_DIR/'supervisor.pid').read_text(encoding='utf-8').strip())
    except (OSError,ValueError): return None

def _supervisor_running():
    pid=_supervisor_pid()
    if _APP_CONFIG.get('runtime',{}).get('location','local')=='server':
        return bool(pid)
    if pid and _pid_running(pid): return True
    if pid:
        try: (DATA_DIR/'supervisor.pid').unlink(missing_ok=True)
        except OSError: pass
    return False

def _pid_running(pid):
    if not pid: return False
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype=wintypes.BOOL
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle: return ctypes.get_last_error()==5  # Access denied still means the PID exists.
        try:
            exit_code=wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(exit_code)) and exit_code.value==259)  # STILL_ACTIVE
        finally: kernel.CloseHandle(handle)
    try: os.kill(int(pid),0); return True
    except (OSError,ValueError,ProcessLookupError): return False

def _heartbeat_fresh(status,now=None):
    try: return bool(status and status.get('status') in {'running','managing_only','error'} and status.get('time') and (now or datetime.now(timezone.utc))-_as_utc(status['time'])<=timedelta(seconds=60))
    except (ValueError,TypeError): return False

def _start_bot():
    """Start one detached bot process, with both UI and bot locks guarding races."""
    server_mode=_APP_CONFIG.get('runtime',{}).get('location','local')=='server'
    status=queries.get_bot_status(DB_PATH); pid=None if server_mode else _lock_pid(); running=False if server_mode else _pid_running(pid); supervisor_running=False if server_mode else _supervisor_running()
    lock_path=DATA_DIR/'bot.lock'
    if running or supervisor_running or _heartbeat_fresh(status): return False,'Bot artıq işləyir kimi görünür.'
    if lock_path.exists() and not running:
        lock_path.unlink(missing_ok=True)
    guard=DATA_DIR/'dashboard-start.lock'; guard.parent.mkdir(parents=True,exist_ok=True)
    try: handle=guard.open('x',encoding='utf-8')
    except FileExistsError: return False,'A Start request is already in progress.'
    try:
        handle.write(f'{os.getpid()} {time.time_ns()}\n'); handle.close()
        stop=ROOT/'STOP'
        if stop.exists(): stop.unlink()
        (DATA_DIR/'supervisor.stop').unlink(missing_ok=True)
        if server_mode:
            return True,'Server supervisor start requested.'
        python=ROOT/'.venv'/'Scripts'/'pythonw.exe'; script=ROOT/'scripts'/'run_forever.py'
        log_path=LOG_DIR/'bot_stdout.log'; log_path.parent.mkdir(parents=True,exist_ok=True)
        flags=getattr(subprocess,'DETACHED_PROCESS',0x00000008)|getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0x00000200)
        breakaway=getattr(subprocess,'CREATE_BREAKAWAY_FROM_JOB',0x01000000)
        with log_path.open('a',encoding='utf-8',buffering=1) as output:
            options={'cwd':ROOT,'stdin':subprocess.DEVNULL,'stdout':output,'stderr':subprocess.STDOUT,'close_fds':True}
            try: subprocess.Popen([str(python),str(script)],creationflags=flags|breakaway,**options)
            except OSError: subprocess.Popen([str(python),str(script)],creationflags=flags,**options)
        return True,'Bot start requested.'
    except Exception as exc:
        (ROOT/'STOP').write_text('PAUSE\n',encoding='utf-8')
        return False,f'Could not start bot: {exc}'
    finally:
        try: handle.close()
        except Exception: pass
        guard.unlink(missing_ok=True)

def _command_ttl():
    import yaml
    config=yaml.safe_load((ROOT/'config.yaml').read_text(encoding='utf-8'))
    return int(config['live']['command_ttl_seconds'])

def _authenticated():
    password=os.getenv('DASHBOARD_PASSWORD','')
    if not password: return True
    if st.session_state.get('dashboard_authenticated'): return True
    with st.form('dashboard_login'):
        entered=st.text_input('Panel parolu',type='password'); submitted=st.form_submit_button('Daxil ol')
    if submitted:
        if hmac.compare_digest(entered,password): st.session_state.dashboard_authenticated=True; st.rerun()
        else: st.error('Parol yanlışdır')
    return False

def _as_utc(value):
    parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)

def _local(value,tz=None,fmt='%Y-%m-%d %H:%M:%S'):
    return local_time(value,fmt)


def _money_color(value):
    number=float(value or 0)
    return 'color: #16a34a; font-weight: 600' if number>0 else 'color: #dc2626; font-weight: 600' if number<0 else 'color: #64748b'

PAGES=['🏠 İcmal','📈 Mövqelər','📋 Tarixçə','⚙️ Ayarlar','🧠 Beyin','🛠 Sistem']
PAGE_KEYS=['overview','positions','history','settings','brain','system']
GROUP_LABELS={'forex':'Valyuta','metals':'Metallar','indices':'İndekslər','energy':'Enerji','crypto':'Kripto','stocks':'Səhmlər','other':'Digər'}


def _table(frame,phone=False,phone_columns=None,money=None):
    """Scrollable table; on phones only the most important columns are shown."""
    if phone and phone_columns: frame=frame[[c for c in phone_columns if c in frame.columns]]
    if money and money in frame.columns:
        frame=frame.style.format({money:lambda value:format_money(value,signed=True)}).map(_money_color,subset=[money])
    st.dataframe(frame,width='stretch',hide_index=True)


def _send(command,ok_text):
    """Queue a bot command and remember the outcome for the next rerun."""
    try:
        queries.enqueue_command(command,DB_PATH,ttl_seconds=_command_ttl())
        st.session_state.action_message=ok_text; st.session_state.action_level='success'
    except Exception as exc:
        st.session_state.action_message=f'Əmr göndərilmədi: {translate_message(exc)}'; st.session_state.action_level='error'


def _flash():
    if st.session_state.get('action_message'):
        message=st.session_state.pop('action_message'); level=st.session_state.pop('action_level','info')
        getattr(st,level if level in {'success','warning','error','info'} else 'info')(message)


def _page():
    """Active page, remembered in the URL (?page=...) so a bookmark opens it directly."""
    current=st.query_params.get('page','overview')
    index=PAGE_KEYS.index(current) if current in PAGE_KEYS else 0
    choice=st.segmented_control('Bölmə',PAGES,default=PAGES[index],key='nav',label_visibility='collapsed')
    key=PAGE_KEYS[PAGES.index(choice)] if choice in PAGES else PAGE_KEYS[index]
    if key!=current: st.query_params['page']=key
    return key


def _phone_toggle():
    current=st.query_params.get('view')=='phone'
    phone=st.toggle('📱 Telefon',value=current,key='phone_view',help='Kiçik ekran üçün sadə görünüş; ünvanı əlfəcinə əlavə etsəniz, həmişə belə açılacaq.')
    if phone!=current:
        if phone: st.query_params['view']='phone'
        else: st.query_params.pop('view',None)
    return phone


class Ctx:
    """Everything the pages share, read once per rerun."""
    def __init__(self,phone):
        self.phone=phone; self.config=_APP_CONFIG
        now_local=datetime.now(AZ_TZ); today=now_local.date()
        self.today_start=datetime.combine(today,dt_time.min,tzinfo=AZ_TZ).astimezone(timezone.utc).isoformat()
        self.today_end=(datetime.combine(today,dt_time.min,tzinfo=AZ_TZ)+timedelta(days=1)).astimezone(timezone.utc).isoformat()
        self.status=queries.get_bot_status(DB_PATH); self.snapshot=queries.get_latest_snapshot(DB_PATH)
        self.stale=not _heartbeat_fresh(self.status); pid=_lock_pid(); self.supervisor_running=_supervisor_running()
        server=self.config.get('runtime',{}).get('location','local')=='server'
        self.process_running=(self.supervisor_running or _heartbeat_fresh(self.status)) if server else (_pid_running(pid) or self.supervisor_running)
        self.account=(self.status or {}).get('account_type','UNKNOWN').upper()
        self.paused=(ROOT/'STOP').exists(); self.stop_mode=stop_state(ROOT)
        self.mt5_connected=bool((self.status or {}).get('mt5_connected',0)) and not self.stale
        self.magic=self.config.get('magic_number')
        now_utc=datetime.now(timezone.utc); self.opened=queries.get_open_positions(DB_PATH)
        for row in self.opened:
            row['duration']=format_duration((now_utc-_as_utc(row['open_time'])).total_seconds()/60)
            row['direction_label']=direction_label(row['direction']); row['strategy_label']=strategy_label(row['strategy'])
            row['net_result']=float(row.get('profit') or 0)+float(row.get('commission') or 0)+float(row.get('swap') or 0)
        self.floating=sum(row['net_result'] for row in self.opened)
        self.brain_state=brain_overview(self.config,DATA_DIR)


def _header(ctx):
    if ctx.stop_mode=='halt' and not ctx.process_running: text,kind='⛔ Tam dayandırılıb (HALT) · avtomatik başlamayacaq','bad'
    elif not ctx.process_running: text,kind='🔴 Bot dayanıb','bad'
    elif ctx.stale: text,kind='🔴 Bot cavab vermir · son siqnal 60 saniyədən köhnədir','bad'
    elif not ctx.mt5_connected: text,kind='🔴 MT5 ilə əlaqə yoxdur','bad'
    elif ctx.status and not bool(ctx.status.get('trade_allowed',1)): text,kind='🔴 MT5-də "Algo Trading" bağlıdır','bad'
    elif ctx.paused: text,kind='🟡 Fasilədə · yeni əmr açılmır, açıq mövqelər idarə olunur','warn'
    else: text,kind='🟢 Bot işləyir','good'
    try:
        raw=json.loads((ctx.status or {}).get('scan_groups_open') or '{}')
        groups=', '.join(GROUP_LABELS.get(g,g) for g,c in raw.items() if int(c)>0) or 'hazırda açıq bazar yoxdur'
    except (ValueError,TypeError): groups='bazar qrupları məlum deyil'
    st.markdown(ui.hero(text,kind,f'Açıq bazarlar: {groups}'),unsafe_allow_html=True)
    runtime=runtime_label(ctx.config)
    telegram=os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID')
    st.markdown(ui.pills([(f'Hesab: {ctx.account if ctx.account in {"DEMO","REAL"} else "naməlum"}','info' if ctx.account=='DEMO' else 'warn'),
                          (runtime['text'],runtime['state']),
                          ('MT5 qoşulub' if ctx.mt5_connected else 'MT5 qoşulmayıb','good' if ctx.mt5_connected else 'bad'),
                          (ctx.brain_state['title'],ctx.brain_state['state']),
                          (('Telegram aktivdir','good') if telegram else ('Telegram söndürülüb','warn'))]),unsafe_allow_html=True)
    if runtime['place']=='Linux server' and not os.getenv('DASHBOARD_PASSWORD'):
        st.warning('Panel serverdə parolsuz işləyir. .env faylında DASHBOARD_PASSWORD təyin edin.')


def _controls(ctx):
    _flash()
    cols=st.columns(1 if ctx.phone else 3)
    with cols[0]:
        if st.button('⏹ Botu dayandır' if ctx.process_running else '▶ Botu başlat',key='bot_toggle',width='stretch',type='primary'):
            if ctx.process_running: _send('stop','Dayandırma əmri göndərildi.')
            else:
                ok,message=_start_bot()
                st.session_state.action_message='Botun başladılması istənildi.' if ok else f'Bot başlamadı: {translate_message(message)}'
                st.session_state.action_level='success' if ok else 'error'
            st.rerun()
    with cols[1 if not ctx.phone else 0]:
        if st.button('▶ Davam et' if ctx.paused else '⏸ Fasilə',key='pause_toggle',disabled=not ctx.process_running,width='stretch'):
            _send('resume' if ctx.paused else 'pause','Davam etmə əmri göndərildi.' if ctx.paused else 'Fasilə əmri göndərildi.'); st.rerun()
    with cols[2 if not ctx.phone else 0]:
        with st.popover('✖ Hamısını bağla və dayan',width='stretch'):
            st.write('Bütün açıq mövqelər bazar qiyməti ilə bağlanacaq və bot dayanacaq.')
            if st.button('Bəli, hamısını bağla',key='close_all_stop',disabled=not ctx.process_running,type='primary'):
                _send('close_all_stop','Bütün mövqeləri bağlama və dayandırma əmri göndərildi.'); st.rerun()


def _view_overview(ctx):
    now_utc=datetime.now(timezone.utc)
    utc_day=datetime.combine(now_utc.date(),dt_time.min,tzinfo=timezone.utc); utc_week=utc_day-timedelta(days=utc_day.weekday())
    pnl_day=queries.get_pnl_since(utc_day.isoformat(),DB_PATH,ctx.magic)['pnl']; pnl_week=queries.get_pnl_since(utc_week.isoformat(),DB_PATH,ctx.magic)['pnl']
    balance=ctx.snapshot.get('balance') if ctx.snapshot else None; equity=ctx.snapshot.get('equity') if ctx.snapshot else None
    risk=risk_overview(ctx.config,ctx.account,balance,pnl_day,pnl_week,ctx.floating)
    today=queries.get_pnl_since(ctx.today_start,DB_PATH,ctx.magic); result=float(today['pnl'])+ctx.floating
    tone=lambda v:'good' if v>0 else 'bad' if v<0 else ''
    st.markdown(ui.kpis([('Balans',format_money(balance),'',f'Kapital {format_money(equity)}'),
                         ('Bu gün',format_money(result,signed=True),tone(result),f'{today["trades"]} bağlanıb · {today["win_rate"]:.0%} qazanc'),
                         ('Açıq mövqelər',str(len(ctx.opened)),'',f'Cari nəticə {format_money(ctx.floating,signed=True)}'),
                         ('Hər əmrdə risk',f'{risk["risk_pct"]:g}%','',f'≈ {format_money(risk["risk_money"])} · {risk["account"]}')]),unsafe_allow_html=True)
    cols=st.columns(1 if ctx.phone else 2)
    for col,(label,key) in zip(cols*2,[('Gündəlik zərər limiti','daily'),('Həftəlik zərər limiti','weekly')]):
        item=risk[key]
        with col: st.markdown(ui.limit_bar(label,item['used_share'],f'{item["used_share"]:.0%} istifadə olunub',f'Zərər {format_money(item["loss_money"])} / {format_money(item["limit_money"])} ({item["limit_pct"]:g}%) · qalıb {format_money(item["left_money"])}'),unsafe_allow_html=True)
    _controls(ctx)
    hours=_hours_summary(ctx.config)
    if hours: st.caption('Yeni əmr saatları (Bakı vaxtı, bu gün): '+hours)
    left,right=st.columns([3,2]) if not ctx.phone else (st.container(),st.container())
    with left:
        st.markdown('#### Bu gün bağlananlar')
        rows=queries.get_trade_history(ctx.today_start,ctx.today_end,DB_PATH,ctx.magic)
        if rows:
            frame=pd.DataFrame([{'Saat':local_time(r['close_time'],'%H:%M'),'Alət':r['symbol'],'İstiqamət':direction_label(r['direction']),'Nəticə':float(r['net']),'Səbəb':close_reason_label(r['close_reason'])} for r in rows])
            _table(frame,ctx.phone,['Saat','Alət','Nəticə'],money='Nəticə')
        else: st.caption('Bu gün bağlanan əmr yoxdur.')
    with right:
        st.markdown('#### Bu günün kapitalı')
        curve=queries.get_equity_curve(ctx.today_start,ctx.today_end,DB_PATH)
        if curve:
            frame=pd.DataFrame(curve); frame['Saat']=pd.to_datetime(frame.time,utc=True).dt.tz_convert(AZ_TZ)
            st.area_chart(frame.set_index('Saat')[['equity']].rename(columns={'equity':'Kapital'}),height=180)
        else: st.caption('Bu gün üçün kapital qeydi yoxdur.')
    with st.expander('📰 Yaxın xəbərlər'):
        news=[r for r in queries.get_recent_events(DB_PATH,100) if r.get('source')=='news' and 'Yaxın xəbər ·' in r.get('message','')]
        if news: st.dataframe(pd.DataFrame([{'Xəbər':r['message'].replace('Yaxın xəbər · ','')} for r in news[:20]]),width='stretch',hide_index=True)
        else: st.caption('Növbəti 24 saat üçün xəbər yoxdur.')


def _hours_summary(config):
    """Today's entry windows converted to Baku time, e.g. 'GOLD 13:00–02:00 · digərləri 18:30–01:00'."""
    from zoneinfo import ZoneInfo
    hours=(config.get('universe',{}) or {}).get('trading_hours') or {}
    if not hours.get('enabled'): return 'məhdudiyyət yoxdur'
    tz=ZoneInfo(str(hours.get('timezone','UTC'))); today=datetime.now(tz).date()
    def window(start,end):
        try:
            s=datetime.combine(today,dt_time.fromisoformat(str(start)),tzinfo=tz).astimezone(AZ_TZ)
            e=datetime.combine(today,dt_time.fromisoformat(str(end)),tzinfo=tz).astimezone(AZ_TZ)
            return f'{s:%H:%M}–{e:%H:%M}'
        except ValueError: return f'{start}–{end}'
    parts=[f'{name} {window(w.get("start"),w.get("end"))}' for name,w in (hours.get('per_symbol') or {}).items()]
    parts.append(('digərləri ' if parts else '')+window(hours.get('start'),hours.get('end')))
    return ' · '.join(parts)


def _position_chart_card(row,timeframe,phone):
    from src.dashboard.charts import position_chart, r_multiple
    from src.position_charts import read_position_chart
    payload=read_position_chart(DATA_DIR,row['ticket'])
    r_value=r_multiple(payload) if payload else None
    result=float(row.get('net_result') or 0)
    color='#16a34a' if result>0 else '#dc2626' if result<0 else '#64748b'
    r_text=f' · {r_value:+.2f}R' if r_value is not None else ''
    head,button=st.columns([4,1])
    head.markdown(f"<div style='display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;font-weight:650'><span>{row['symbol']} · {row['direction_label']} · {row['volume']} lot · {row['duration']}</span><span style='color:{color}'>{format_money(result,signed=True)}{r_text}</span></div>",unsafe_allow_html=True)
    with button.popover('Bağla',width='stretch'):
        st.write(f"{row['symbol']} mövqeyi (bilet {row['ticket']}) bazar qiyməti ilə bağlanacaq.")
        if st.button('Bəli, bağla',key=f"close_{row['ticket']}",type='primary'):
            _send(f"close_position:{int(row['ticket'])}",f"{row['symbol']} mövqeyinin bağlanması istənildi."); st.rerun()
    chart=position_chart(payload,timeframe,height=240 if phone else 300) if payload else None
    if chart is None: st.caption('Qrafik hələ hazır deyil: bot bir neçə saniyəyə yazacaq.'); return
    st.altair_chart(chart,width='stretch')
    try: age=(datetime.now(timezone.utc)-_as_utc(payload['updated_at'])).total_seconds()
    except (KeyError,ValueError,TypeError): age=None
    st.caption('Mavi: giriş · qırmızı: SL · yaşıl: TP · nöqtəli: cari qiymət'+(f' · {age:.0f} san əvvəl yenilənib' if age is not None else '')+(' · ⚠ köhnədir' if age is not None and age>30 else ''))
    _analysis_card(payload.get('analysis'))


DANGER_TEXT={'EMA kəsişməsi əks istiqamətdə':'ortalamalar əleyhinə kəsişib','son M1 diapazonu qırıldı':'son dib/zirvə qırılıb',
             'RSI/ROC impulsu zəifləyib':'impuls əleyhinədir','əks istiqamətdə həcm sıçrayışı':'əleyhinə həcm sıçrayışı'}


def _analysis_card(analysis):
    """What the bot's management rules say about this position right now."""
    if not analysis: st.caption('🧠 Təhlil hələ yoxdur: bot yeni kodla işə düşəndən sonra görünəcək.'); return
    colors={'good':'#16a34a','warn':'#d97706','bad':'#dc2626','neutral':'#475569'}
    color=colors.get(analysis.get('tone'),'#475569')
    danger=analysis.get('danger',{}); cont=analysis.get('continuation',{})
    score,maximum,threshold=int(danger.get('score',0)),max(1,int(danger.get('max',4))),int(danger.get('threshold',3))
    bar_color='#dc2626' if score>=threshold else '#d97706' if score>=threshold-1 else '#16a34a'
    reasons=', '.join(DANGER_TEXT.get(r,r) for r in danger.get('reasons',[])) or 'əleyhinə əlamət yoxdur'
    current_r=analysis.get('current_r'); r_text=f'{current_r:+.2f}R' if current_r is not None else '—'
    cont_parts=[('qrafiklər eyni istiqamətdə' if cont.get('mtf') else 'qrafiklər razı deyil'),('hərəkət güclənir' if cont.get('rising') else 'hərəkət güclənmir')]
    left=analysis.get('minutes_left'); hard=analysis.get('hard_limit_minutes')
    off='' if danger.get('enabled',True) else ' · erkən bağlama söndürülüb'
    st.markdown(f"""<div style='border-left:4px solid {color};background:#f8fafc;padding:8px 12px;border-radius:6px;font-size:.88rem;line-height:1.5'>
<div style='font-weight:650;color:{color}'>🧠 {analysis.get('plan','')}</div>
<div style='display:flex;align-items:center;gap:8px;margin-top:4px'><span>Təhlükə {score}/{maximum}</span>
<div style='flex:1;height:6px;background:#e2e8f0;border-radius:3px'><div style='width:{min(100,score/maximum*100):.0f}%;height:6px;background:{bar_color};border-radius:3px'></div></div>
<span style='color:#64748b'>{threshold}-də bağlayır{off}</span></div>
<div style='color:#475569'>{reasons}</div>
<div style='color:#475569'>İndi {r_text} · ən yaxşı {float(analysis.get('peak_r') or 0):+.2f}R · davam: {', '.join(cont_parts)}</div>
<div style='color:#64748b'>Vaxt: {float(left or 0):.0f} dəq qalıb{f' · uzadılsa ən çox {float(hard):.0f} dəq' if hard else ''}{' · uzadılıb' if analysis.get('extended') else ''}</div>
</div>""",unsafe_allow_html=True)


def _view_positions(ctx):
    _flash()
    if not ctx.opened:
        st.info('Hazırda açıq bot mövqeyi yoxdur.')
    else:
        labels={'M1 · son 2 saat':'M1','M5 · son 10 saat':'M5'}
        choice=st.segmented_control('Zaman çərçivəsi',list(labels),default='M1 · son 2 saat',key='chart_tf')
        timeframe=labels.get(choice,'M1')
        if ctx.phone:
            picked=st.selectbox('Mövqe',ctx.opened,key='chart_position',format_func=lambda r:f"{r['symbol']} · {r['direction_label']} · {format_money(r['net_result'],signed=True)}")
            with st.container(border=True): _position_chart_card(picked,timeframe,True)
        else:
            for start in range(0,len(ctx.opened),2):
                cols=st.columns(2)
                for col,row in zip(cols,ctx.opened[start:start+2]):
                    with col,st.container(border=True): _position_chart_card(row,timeframe,False)
        st.markdown('#### Cədvəl')
        frame=pd.DataFrame([{'Bilet':r['ticket'],'Alət':r['symbol'],'İstiqamət':r['direction_label'],'Həcm':r['volume'],'Giriş':r['open_price'],'SL':r['sl'],'TP':r['tp'],'Nəticə':r['net_result'],'Müddət':r['duration'],'Strategiya':r['strategy_label']} for r in ctx.opened])
        _table(frame,ctx.phone,['Alət','İstiqamət','Nəticə','Müddət'],money='Nəticə')
    external=queries.get_external_open_positions(DB_PATH)
    if external:
        with st.expander(f'Bot xaricindəki mövqelər ({len(external)}) · yalnız izləmə'):
            frame=pd.DataFrame([{'Alət':r['symbol'],'İstiqamət':direction_label(r['direction']),'Həcm':r['volume'],'Nəticə':float(r.get('profit') or 0)+float(r.get('swap') or 0),'Magic':r['magic']} for r in external])
            _table(frame,ctx.phone,['Alət','Nəticə'],money='Nəticə')


def _view_history(ctx):
    presets={'Bu gün':0,'7 gün':7,'30 gün':30,'90 gün':90,'Hamısı':None}
    cols=st.columns([2,2]) if not ctx.phone else (st.container(),st.container())
    with cols[0]: period=st.segmented_control('Dövr',list(presets),default='7 gün',key='hist_period')
    days=presets.get(period or '7 gün')
    end=datetime.now(timezone.utc)
    start=ctx.today_start if days==0 else ('1970-01-01T00:00:00+00:00' if days is None else (end-timedelta(days=days)).isoformat())
    rows=queries.get_trade_history(start,end.isoformat(),DB_PATH,ctx.magic)
    symbols=sorted({r['symbol'] for r in rows})
    with cols[1]: chosen=st.multiselect('Alət',symbols,key='hist_symbols',placeholder='Hamısı')
    if chosen: rows=[r for r in rows if r['symbol'] in chosen]
    s=trade_history.summary(rows)
    pf='∞' if s['profit_factor']==float('inf') else f"{s['profit_factor']:.2f}"
    tone=lambda v:'good' if v>0 else 'bad' if v<0 else ''
    st.markdown(ui.kpis([('Nəticə',format_money(s['total'],signed=True),tone(s['total']),f"{s['trades']} əmr · orta {format_money(s['average'],signed=True)}"),
                         ('Qazanma faizi',f"{s['win_rate']:.0%}",'',f"{s['wins']} qazanan / {s['trades']-s['wins']} itirən"),
                         ('Orta qazanc / zərər',f"{format_money(s['average_win'])} / {format_money(abs(s['average_loss']))}",'',f'Qazanc əmsalı (PF) {pf}'),
                         ('Xərclər',format_money(s['costs']),'',f"Orta müddət {s['average_minutes']:.0f} dəq" if s['average_minutes'] else 'Orta müddət —')]),unsafe_allow_html=True)
    for note in trade_history.insights(rows): st.caption('💡 '+note)
    if not rows: st.info('Seçilmiş dövrdə bağlanmış əmr yoxdur.'); return
    daily=trade_history.daily_totals(rows,AZ_TZ)
    if len(daily)>1:
        frame=pd.DataFrame(daily); frame['Tarix']=pd.to_datetime(frame.date)
        st.bar_chart(frame.set_index('Tarix')[['pnl']].rename(columns={'pnl':'Gündəlik nəticə ($)'}),height=200)
    hours=trade_history.by_hour(rows,AZ_TZ)
    if hours:
        st.markdown('#### Saata görə nəticə (Bakı vaxtı, açılış saatı)')
        st.caption('Hansı saatlarda pul qazanılır, hansında itirilir. Zərər verən saatları Ayarlar → Saatlar-dan bağlamaq olar.')
        frame=pd.DataFrame([{'Saat':f"{h['hour']:02d}:00",'Nəticə ($)':h['total']} for h in hours])
        st.bar_chart(frame.set_index('Saat'),height=200)
        frame=pd.DataFrame([{'Saat':f"{h['hour']:02d}:00–{(h['hour']+1)%24:02d}:00",'Əmr':h['trades'],'Qazanma %':f"{h['win_rate']:.0%}",'Orta':h['average'],'Cəmi':h['total']} for h in hours])
        st.dataframe(frame.style.format({'Orta':lambda v:format_money(v,signed=True),'Cəmi':lambda v:format_money(v,signed=True)}).map(_money_color,subset=['Cəmi']),width='stretch',hide_index=True)
    left,right=st.columns(2) if not ctx.phone else (st.container(),st.container())
    def breakdown(title,key,label):
        st.markdown(f'#### {title}')
        frame=pd.DataFrame([{label:(close_reason_label(g['name']) if key=='close_reason' else g['name']),'Əmr':g['trades'],'Qazanma %':f"{g['win_rate']:.0%}",'Orta':g['average'],'Cəmi':g['total']} for g in trade_history.group_by(rows,key)])
        st.dataframe(frame.style.format({'Orta':lambda v:format_money(v,signed=True),'Cəmi':lambda v:format_money(v,signed=True)}).map(_money_color,subset=['Cəmi']),width='stretch',hide_index=True)
    with left: breakdown('Bağlanma səbəbinə görə','close_reason','Səbəb')
    with right: breakdown('Alətə görə','symbol','Alət')
    reviews=queries.get_exit_reviews(start,end.isoformat(),DB_PATH,ctx.magic)
    if chosen: reviews=[r for r in reviews if r['symbol'] in chosen]
    st.markdown('#### Botun öz bağlamaları düzgün idimi?')
    st.caption('Bot əmri özü bağlayanda (erkən, hərəkətsizlik, vaxt), sonrakı 30 dəqiqədə qiymətin ilkin SL və TP-yə nisbətən nə etdiyi yoxlanılır. "Qənaət" müsbətdirsə, bağlamaq saxlamaqdan yaxşı olub.')
    summary=trade_history.exit_review_summary(reviews)
    if summary:
        frame=pd.DataFrame([{'Səbəb':close_reason_label(g['reason']),'Yoxlanıb':g['trades'],'Kömək etdi':g['helped'],'Zərər verdi':g['hurt'],'Saxlasaydı SL':g['would_sl'],'Saxlasaydı TP':g['would_tp'],'Qənaət':g['saved']} for g in summary])
        st.dataframe(frame.style.format({'Qənaət':lambda v:format_money(v,signed=True)}).map(_money_color,subset=['Qənaət']),width='stretch',hide_index=True)
    else: st.caption('Hələ yoxlanmış bağlama yoxdur: bot hər bağlamadan 30 dəqiqə sonra yoxlayır.')
    st.markdown('#### Bütün əmrlər')
    frame=pd.DataFrame([{'Bağlanıb':local_time(r['close_time'],'%d.%m %H:%M'),'Alət':r['symbol'],'İstiqamət':direction_label(r['direction']),'Həcm':r['volume'],'Giriş':r['open_price'],'Çıxış':r['close_price'],'Nəticə':float(r['net']),'Səbəb':close_reason_label(r['close_reason']),'Strategiya':strategy_label(r['strategy'])} for r in rows])
    _table(frame,ctx.phone,['Bağlanıb','Alət','Nəticə'],money='Nəticə')
    st.download_button('⬇ CSV kimi yüklə',frame.to_csv(index=False).encode('utf-8-sig'),file_name='emrler.csv',mime='text/csv')


def _view_settings(ctx):
    _flash()
    if os.getenv('DASHBOARD_PASSWORD') and not st.session_state.get('dashboard_authenticated'):
        st.warning('Ayarları dəyişmək üçün panelə parol ilə daxil olun.'); return
    config_path=ROOT/'config.yaml'
    doc=settings_editor.load_document(config_path); values=settings_editor.current_values(doc)
    st.caption('Dəyişikliklər "Yadda saxla" düyməsi ilə tətbiq olunur: ayarlar yoxlanılır, köhnə fayl ehtiyat nüsxəyə yazılır və bot yenidən başladılır. Açıq mövqelərə toxunulmur.')
    real=ctx.config.get('risk',{})
    with st.form('settings_form',border=False):
        changes={}; widget=0
        def field_input(f,container):
            nonlocal widget; widget+=1; key=f'set_{widget}'; value=values.get(f.path)
            if f.kind=='bool': return container.toggle(f.label,value=bool(value),help=f.help or None,key=key)
            if f.kind=='int': return container.number_input(f.label,min_value=int(f.minimum),max_value=int(f.maximum),value=int(value if value is not None else f.minimum),step=int(f.step or 1),help=f.help or None,key=key)
            if f.kind=='float': return container.number_input(f.label,min_value=float(f.minimum),max_value=float(f.maximum),value=float(value if value is not None else f.minimum),step=float(f.step or .1),format='%.2f',help=f.help or None,key=key)
            if f.kind=='time': return container.text_input(f.label,value=str(value or ''),help='SS:DD, məsələn 09:30',key=key)
            if f.kind=='choice':
                options=list(f.choices); index=options.index(value) if value in options else 0
                return container.selectbox(f.label,options,index=index,help=f.help or None,key=key,format_func=(lambda o:f'1 : {o:g}') if f.path==('risk','reward_ratio') else (lambda o:f'{o:g}' if isinstance(o,float) else str(o)))
            if f.kind=='symbols': return container.multiselect(f.label,settings_editor.known_symbols(doc),default=list(value or []),help=f.help or None,key=key)
        tabs=st.tabs(list(settings_editor.SECTIONS))
        for section,tab in zip(settings_editor.SECTIONS,tabs):
            with tab:
                fields=[f for f in settings_editor.FIELDS if f.section==section]
                grid=st.columns(1 if ctx.phone else 2)
                for i,f in enumerate(fields): changes[f.path]=field_input(f,grid[i%len(grid)])
                if section=='Saatlar':
                    st.markdown('**Ayrıca saatı olan alətlər** (boş buraxılan sətir silinir)')
                    current=settings_editor.symbol_hours(doc)
                    base=pd.DataFrame([{'Alət':k,'Başlanğıc':v['start'],'Son':v['end']} for k,v in current.items()] or [{'Alət':'','Başlanğıc':'','Son':''}])
                    edited=st.data_editor(base,num_rows='dynamic',width='stretch',hide_index=True,key='hours_editor')
                    st.caption('Saatlar yuxarıda seçilmiş saat qurşağı ilə yazılır. İndiki pəncərələr Bakı vaxtı ilə: '+_hours_summary(ctx.config))
                if section=='Risk (demo)':
                    st.info(f"Real hesab limitləri kilidlidir və buradan dəyişmir: hər əmrdə {real.get('risk_per_trade_pct',.25)}%, gündəlik {real.get('daily_loss_limit_pct',3)}%, həftəlik {real.get('weekly_loss_limit_pct',8)}%.")
        submitted=st.form_submit_button('💾 Yadda saxla və botu yenidən başlat',type='primary',width='stretch')
    if submitted:
        hours_rows={str(r['Alət']).strip():{'start':str(r['Başlanğıc']).strip(),'end':str(r['Son']).strip()} for r in edited.to_dict('records') if str(r.get('Alət') or '').strip()}
        try:
            before=dict(values)
            settings_editor.apply_changes(doc,changes,hours_rows)
            settings_editor.save(config_path,DATA_DIR,settings_editor.dump_document(doc),ROOT)
            diff=settings_editor.describe_changes(before,settings_editor.current_values(doc))
            restarted=''
            if ctx.process_running:
                try: queries.enqueue_command('restart',DB_PATH,ttl_seconds=_command_ttl()); restarted=' Bot yenidən başladılır (≈15–30 san).'
                except Exception as exc: restarted=f' Botu yenidən başlatmaq alınmadı: {translate_message(exc)}'
            else: restarted=' Bot dayanıqdır: ayarlar bot başlayanda tətbiq olunacaq.'
            st.session_state.action_message='Ayarlar yadda saxlanıldı.'+restarted+('\n\n'+'\n'.join('• '+line for line in diff) if diff else ''); st.session_state.action_level='success'
            st.rerun()
        except ValueError as exc:
            st.error(f'Yadda saxlanılmadı: {translate_message(exc)}')
    with st.expander('🕘 Əvvəlki ayarlara qayıt'):
        backups=settings_editor.list_backups(DATA_DIR)
        if not backups: st.caption('Hələ ehtiyat nüsxə yoxdur: ilk yadda saxlamadan sonra yaranacaq.')
        else:
            def label(path):
                stamp=path.stem.replace('config-','')
                try: return datetime.strptime(stamp[:15],'%Y%m%d-%H%M%S').replace(tzinfo=timezone.utc).astimezone(AZ_TZ).strftime('%d.%m.%Y %H:%M:%S')
                except ValueError: return path.name
            picked=st.selectbox('Ehtiyat nüsxə (yadda saxlamadan əvvəlki vəziyyət)',backups,format_func=label,key='restore_pick')
            if st.button('Bu ayarları bərpa et və botu yenidən başlat',key='restore_btn'):
                try:
                    settings_editor.restore(ROOT/'config.yaml',DATA_DIR,picked,ROOT)
                    if ctx.process_running: _send('restart','Ayarlar bərpa olundu, bot yenidən başladılır.')
                    else: st.session_state.action_message='Ayarlar bərpa olundu.'; st.session_state.action_level='success'
                    st.rerun()
                except ValueError as exc: st.error(f'Bərpa alınmadı: {translate_message(exc)}')
    with st.expander('✉️ Telegram'):
        if os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID'):
            if st.button('Test mesajı göndər',key='tg_test'):
                try:
                    from src.notify import TelegramNotifier
                    notifier=TelegramNotifier(); notifier.send('dashboard_test','✅ Paneldən test mesajı'); notifier.flush()
                    st.success('Göndərildi, Telegram-ı yoxlayın.')
                except Exception as exc: st.error(f'Göndərilmədi: {exc}')
        else: st.caption('Telegram qurulmayıb: .env faylına TELEGRAM_BOT_TOKEN və TELEGRAM_CHAT_ID əlavə edin.')


def _view_brain(ctx):
    brain_cfg=ctx.config.get('brain',{})
    try: gate=json.loads((DATA_DIR/'brain'/'gate.json').read_text(encoding='utf-8'))
    except (OSError,ValueError): gate={'status':'FAIL','reasons':['Yekun test qiymətləndirməsi hələ aparılmayıb.']}
    try: live_gate=json.loads((DATA_DIR/'brain'/'live_gate.json').read_text(encoding='utf-8'))
    except (OSError,ValueError): live_gate={}
    state=ctx.brain_state
    if state['state']=='good': st.success(f"{state['title']} · {state['text']}")
    else:
        st.warning(f"{state['title']} · {state['text']}")
        for reason in state['reasons']: st.caption('• '+reason)
        for reason in gate.get('reasons',[]): st.caption('• '+translate_message(reason))
    brain_risk=risk_overview(ctx.config,ctx.account,None,0,0,0)
    if brain_risk['brain_max_pct']>0:
        st.caption(f"Beyin aktiv olanda risk aralığı ({brain_risk['account']}): {brain_risk['brain_min_pct']:g}% – {brain_risk['brain_max_pct']:g}% · ardıcıl {int(brain_cfg.get('loss_streak_base_risk',3))} zərərdən sonra baza riskə qayıdır")
    if live_gate.get('mode')=='shadow': st.error('Canlı sapma qoruyucusu beyni müşahidə rejiminə qaytarıb.')
    if gate.get('evaluated_at'): st.caption('Son qiymətləndirmə: '+local_time(gate['evaluated_at']))
    calibration=gate.get('metrics',{}).get('calibration',[])
    if calibration:
        st.markdown('#### Ehtimal kalibrlənməsi')
        cal=pd.DataFrame(calibration).rename(columns={'predicted':'Proqnoz','actual':'Fakt','bucket':'Aralıq'}).set_index('Aralıq')
        st.line_chart(cal[['Proqnoz','Fakt']],height=220)
    ablation=gate.get('metrics',{}).get('ablation',{})
    if ablation:
        st.markdown('#### Qatların müqayisəsi')
        st.dataframe(pd.DataFrame([{'Model':n,'Əmr':r['trades'],'Qazanma %':f"{r['win_rate']:.1%}",'Gözlənilən (R)':r['expectancy_r'],'PF':r['profit_factor']} for n,r in ablation.items()]),width='stretch',hide_index=True)


def _view_system(ctx):
    stats=ctx.status or {}
    heartbeat=(datetime.now(timezone.utc)-_as_utc(stats['time'])).total_seconds() if stats.get('time') else None
    restarts=queries.get_supervisor_restart_count(ctx.today_start,ctx.today_end,DB_PATH)
    st.markdown(ui.kpis([('MT5','Qoşulub' if ctx.mt5_connected else 'Yoxdur','good' if ctx.mt5_connected else 'bad',f"Algo Trading {'açıq' if stats.get('trade_allowed') else 'bağlı'}"),
                         ('Son siqnal',f'{heartbeat:.0f} san' if heartbeat is not None else '—','good' if heartbeat is not None and heartbeat<60 else 'bad','botun ürək döyüntüsü'),
                         ('Nəzarətçi','İşləyir' if ctx.supervisor_running else 'Dayanıb','good' if ctx.supervisor_running else '',f'bu gün {restarts} yenidən başlama'),
                         ('Skan',f"{stats.get('scan_scanned',0)}/{stats.get('scan_total',0)}",'',f"{stats.get('scan_signals',0)} siqnal · {float(stats.get('scan_seconds',0) or 0):.1f} san")]),unsafe_allow_html=True)
    st.markdown('#### Son 1 saat: siqnallar niyə əmrə çevrilmədi')
    funnel=queries.get_pipeline_funnel((datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),DB_PATH)
    if funnel.get('available'):
        counts=funnel.get('counts',{})
        stages=[('Skan','symbols_scanned'),('Filtrdən keçib','filters_passed'),('Siqnal','signals'),('Riskdən keçib','passed_risk'),('Açılıb','orders')]
        cols=st.columns(len(stages) if not ctx.phone else 2)
        for i,(label,key) in enumerate(stages): cols[i%len(cols)].metric(label,f"{int(counts.get(key,0)):,}")
    skips=queries.get_top_skip_reasons_since((datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),DB_PATH,8)
    if skips: st.dataframe(pd.DataFrame([{'Səbəb':translate_message(r['skip_reason']),'Say':r['count']} for r in skips]),width='stretch',hide_index=True)
    else: st.caption('Son 1 saatda rədd edilmiş siqnal yoxdur.')
    gates=queries.get_adaptive_gates(DB_PATH)
    if gates:
        st.markdown('#### Müvəqqəti fasilələr')
        st.dataframe(pd.DataFrame([{'Növ':'Strategiya' if r['scope']=='strategy' else 'Alət qrupu','Ad':strategy_label(r['name']) if r['scope']=='strategy' else GROUP_LABELS.get(r['name'],r['name']),'Son nəticələrin ortası':format_money(r.get('expectancy'),signed=True),'Nümunə':r.get('sample_size'),'Nə vaxta qədər':local_time(r['disabled_until'])} for r in gates]),width='stretch',hide_index=True)
    left,right=st.columns(2) if not ctx.phone else (st.container(),st.container())
    with left:
        st.markdown('#### Xəbərdarlıqlar və xətalar')
        events=[e for e in queries.get_recent_events(DB_PATH,200) if e['level'] in {'WARNING','ERROR','CRITICAL'}][:20]
        if events: st.dataframe(pd.DataFrame([{'Vaxt':local_time(e['time'],'%d.%m %H:%M'),'Mənbə':e['source'],'Məlumat':translate_message(e['message'])} for e in events]),width='stretch',hide_index=True)
        else: st.caption('Xəbərdarlıq yoxdur.')
    with right:
        st.markdown('#### Son əmrlər')
        commands=queries.get_recent_commands(DB_PATH,10)
        names={'pause':'Fasilə','resume':'Davam et','stop':'Dayandır','close_all':'Hamısını bağla','close_all_stop':'Hamısını bağla və dayan','restart':'Yenidən başlat'}
        status_names={'pending':'Gözləyir','running':'İcra olunur','done':'Tamamlandı','failed':'Uğursuz','expired':'Vaxtı bitib'}
        if commands: st.dataframe(pd.DataFrame([{'Vaxt':local_time(c.get('created_at'),'%d.%m %H:%M'),'Əmr':names.get(c['command'],c['command'].replace('close_position:','Mövqeni bağla #')),'Vəziyyət':status_names.get(c['status'],c['status']),'Məlumat':translate_message(c.get('message'))} for c in commands]),width='stretch',hide_index=True)
        else: st.caption('Əmr yoxdur.')
    with st.expander('💾 Ehtiyat nüsxələr'):
        db_backups=sorted((DATA_DIR/'backups').glob('*.db'),reverse=True) if (DATA_DIR/'backups').is_dir() else []
        st.caption('Verilənlər bazası: '+(', '.join(p.name for p in db_backups[:7]) if db_backups else 'hələ yoxdur'))
        st.caption(f"Ayarlar: {len(settings_editor.list_backups(DATA_DIR))} nüsxə (Ayarlar → Əvvəlki ayarlara qayıt)")
    with st.expander('✅ Real hesaba hazırlıq (son 7 gün)'):
        now=datetime.now(timezone.utc); start,end=(now-timedelta(days=7)).isoformat(),now.isoformat()
        for label,passed in readiness_checks(start,end,DB_PATH): st.markdown(('✅ ' if passed else '🔴 ')+label)
        dashboard_cfg=ctx.config.get('dashboard',{}); cols=st.columns(2)
        balance=cols[0].number_input('Real hesab məbləği ($)',min_value=1.0,value=float(dashboard_cfg.get('real_balance_default',100)),step=10.0,key='sim_balance')
        risk_pct=cols[1].number_input('Planlaşdırılan risk (%)',min_value=.01,value=float(dashboard_cfg.get('real_risk_pct_default',1.0)),step=.1,key='sim_risk')
        history_dir=Path(ctx.config.get('history',{}).get('data_dir','data/history')); history_dir=history_dir if history_dir.is_absolute() else ROOT/history_dir
        try: specs=json.loads((history_dir/'symbol_specs.json').read_text(encoding='utf-8'))
        except (OSError,ValueError): specs={}
        sim=simulate_small_account(queries.get_signals_period(start,end,DB_PATH),balance,risk_pct,specs)
        st.info(f"Son 7 gündəki {sim['signals']} siqnaldan bu kapital və risklə {sim['possible']} əmr açmaq olardı; {sim['min_lot_rejected']} siqnal minimum lot riskinə görə rədd edilərdi.")
    with st.expander('⚙️ Botun iş axını'):
        st.markdown('MT5 məlumatı → skaner (sessiya, spred, hərəkət) → strategiya siqnalı → xəbər və xərc filtri → risk qapıları → həcm → MT5 əmri → mövqe idarəsi (SL/TP, hərəkətsizlik, vaxt) → hesabat.')


def main():
    st.set_page_config(page_title='Ticarət botu',page_icon='📈',layout='wide',initial_sidebar_state='collapsed')
    st.markdown(ui.CSS,unsafe_allow_html=True)
    if not _authenticated(): return
    top=st.columns([5,1])
    with top[1]: phone=_phone_toggle()
    with top[0]: page=_page()
    # Live pages refresh; forms (settings) and history filters are not interrupted.
    if page in {'overview','positions','system'}:
        st_autorefresh(interval=10000 if phone else 2000,key='dashboard_refresh')
    ctx=Ctx(phone)
    _header(ctx)
    {'overview':_view_overview,'positions':_view_positions,'history':_view_history,'settings':_view_settings,'brain':_view_brain,'system':_view_system}[page](ctx)


if __name__=='__main__': main()
