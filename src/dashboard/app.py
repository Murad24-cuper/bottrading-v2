"""Single-user local Streamlit dashboard backed solely by SQLite."""
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
from src.dashboard.overview import brain_overview, risk_overview, runtime_label, stop_state
from src.dashboard.presentation import (AZ_TZ, close_reason_label,
                                        direction_label, format_duration,
                                        feature_label, format_money, local_time,
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



def _table(frame,phone=False,phone_columns=None,money=None):
    """Scrollable table; on phones only the most important columns are shown."""
    if phone and phone_columns: frame=frame[[c for c in phone_columns if c in frame.columns]]
    if money and money in frame.columns:
        frame=frame.style.format({money:lambda value:format_money(value,signed=True)}).map(_money_color,subset=[money])
    st.dataframe(frame,use_container_width=True,hide_index=True)

def _position_chart_card(row,timeframe,phone):
    from src.dashboard.charts import position_chart, r_multiple
    from src.position_charts import read_position_chart
    payload=read_position_chart(DATA_DIR,row['ticket'])
    r_value=r_multiple(payload) if payload else None
    result=float(row.get('net_result') or 0)
    color='#16a34a' if result>0 else '#dc2626' if result<0 else '#64748b'
    r_text=f' · {r_value:+.2f}R' if r_value is not None else ''
    st.markdown(f"<div style='display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;font-weight:650'><span>{row['symbol']} · {row['direction_label']} · {row['volume']} lot</span><span style='color:{color}'>{format_money(result,signed=True)}{r_text}</span></div>",unsafe_allow_html=True)
    chart=position_chart(payload,timeframe,height=240 if phone else 300) if payload else None
    if chart is None:
        st.caption('Qrafik hələ hazır deyil: bot növbəti dövrdə (≈15 san) yazacaq.')
        return
    st.altair_chart(chart,use_container_width=True)
    try: age=(datetime.now(timezone.utc)-_as_utc(payload['updated_at'])).total_seconds()
    except (KeyError,ValueError,TypeError): age=None
    stale=' · ⚠ köhnədir' if age is not None and age>120 else ''
    st.caption(f"Mavi: giriş · qırmızı: SL · yaşıl: TP · tünd nöqtəli: cari qiymət · {row['duration']} açıqdır"+(f' · yenilənib {age:.0f} san əvvəl' if age is not None else '')+stale)

def _position_charts(opened,phone):
    """Price chart per open position, drawn from snapshots the bot writes to data/charts."""
    st.markdown('##### Mövqe qrafikləri')
    labels={'M1 · son 2 saat':'M1','M5 · son 10 saat':'M5'}
    choice=st.radio('Zaman çərçivəsi',list(labels),horizontal=True,key='chart_timeframe',label_visibility='collapsed')
    timeframe=labels[choice]
    if phone:
        picked=st.selectbox('Mövqe',opened,key='chart_position',format_func=lambda row:f"{row['symbol']} · {row['direction_label']} · {format_money(row['net_result'],signed=True)}")
        _position_chart_card(picked,timeframe,phone)
        return
    for start in range(0,len(opened),2):
        cols=st.columns(2)
        for col,row in zip(cols,opened[start:start+2]):
            with col,st.container(border=True): _position_chart_card(row,timeframe,phone)

def _phone_mode():
    """Phone view: remembered in the URL (?view=phone) so a home-screen bookmark opens it directly."""
    current=st.query_params.get('view')=='phone'
    phone=st.toggle('📱 Telefon görünüşü',value=current,key='phone_view',help='Kiçik ekran üçün sadə görünüş; ünvanı əlfəcinə əlavə etsəniz, həmişə belə açılacaq.')
    if phone!=current:
        if phone: st.query_params['view']='phone'
        else: st.query_params.pop('view',None)
    return phone

def main():
    st.set_page_config(page_title='Ticarət botu',page_icon='📈',layout='wide',initial_sidebar_state='collapsed')
    st.markdown(ui.CSS,unsafe_allow_html=True)
    if not _authenticated(): return
    phone=_phone_mode()
    # Phones refresh less often: less data and battery; the desktop keeps the 2 s live view.
    st_autorefresh(interval=10000 if phone else 2000,key='dashboard_refresh')
    now_local=datetime.now(AZ_TZ); today=now_local.date()
    today_start=datetime.combine(today,dt_time.min,tzinfo=AZ_TZ).astimezone(timezone.utc).isoformat()
    today_end=(datetime.combine(today,dt_time.min,tzinfo=AZ_TZ)+timedelta(days=1)).astimezone(timezone.utc).isoformat()
    selected=st.session_state.get('detail_date',today)
    detail_start=datetime.combine(selected,dt_time.min,tzinfo=AZ_TZ).astimezone(timezone.utc).isoformat()
    detail_end=(datetime.combine(selected,dt_time.min,tzinfo=AZ_TZ)+timedelta(days=1)).astimezone(timezone.utc).isoformat()
    status=queries.get_bot_status(DB_PATH); snapshot=queries.get_latest_snapshot(DB_PATH)
    summary=queries.get_day_summary(today_start,today_end,DB_PATH)
    stale=not _heartbeat_fresh(status); pid=_lock_pid(); supervisor_running=_supervisor_running()
    process_running=(supervisor_running or _heartbeat_fresh(status)) if _APP_CONFIG.get('runtime',{}).get('location','local')=='server' else (_pid_running(pid) or supervisor_running)
    account=(status or {}).get('account_type','UNKNOWN').upper(); paused=(ROOT/'STOP').exists(); stop_mode=stop_state(ROOT)
    mt5_connected=bool((status or {}).get('mt5_connected',0)) and not stale
    if stop_mode=='halt' and not process_running:
        status_text='⛔ Tam dayandırılıb (HALT) · avtomatik başlamayacaq'; status_kind='bad'
    elif not process_running:
        status_text='🔴 Bot dayanıb'; status_kind='bad'
    elif stale:
        status_text='🔴 Bot cavab vermir · son ürək döyüntüsü 60 saniyədən köhnədir'; status_kind='bad'
    elif not mt5_connected:
        status_text='🔴 MT5 ilə əlaqə yoxdur'; status_kind='bad'
    elif status and not bool(status.get('trade_allowed',1)):
        status_text='🔴 MT5-də avtomatik ticarət icazəsi bağlıdır'; status_kind='bad'
    elif paused:
        status_text='🟡 Fasilədə · yeni əmr açılmır, açıq mövqelər idarə olunur'; status_kind='warn'
    else:
        status_text=f'🟢 Bot işləyir · {account if account == "DEMO" else "Hesab"} hesab'; status_kind='good'
    try:
        raw_groups=json.loads((status or {}).get('scan_groups_open') or '{}')
        group_labels={'forex':'Valyuta','metals':'Metallar','indices':'İndekslər','energy':'Enerji','crypto':'Kripto','stocks':'Səhmlər','other':'Digər'}
        opened_groups=', '.join(group_labels.get(group,group) for group,count in raw_groups.items() if int(count)>0) or 'Hazırda açıq qrup yoxdur'
    except (ValueError,TypeError): opened_groups='Bazar qrupları məlum deyil'
    st.markdown(ui.hero(status_text,status_kind,f'Açıq bazarlar: {opened_groups}'),unsafe_allow_html=True)
    restart_count=queries.get_supervisor_restart_count(today_start,today_end,DB_PATH)
    runtime=runtime_label(_APP_CONFIG)
    brain_state=brain_overview(_APP_CONFIG,DATA_DIR)
    st.markdown(ui.pills([(f'Hesab: {account if account in {"DEMO","REAL"} else "naməlum"}','info' if account=='DEMO' else 'warn'),
                          (runtime['text'],runtime['state']),
                          ('MT5 qoşulub' if mt5_connected else 'MT5 qoşulmayıb','good' if mt5_connected else 'bad'),
                          (f'Nəzarətçi {"işləyir" if supervisor_running else "dayanıb"} · bu gün {restart_count} restart','good' if supervisor_running else 'neutral'),
                          (brain_state['title'],brain_state['state']),
                          (('Telegram bildirişləri aktivdir','good') if os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID') else ('Telegram bildirişləri söndürülüb','warn'))]),unsafe_allow_html=True)
    if runtime['place']=='Linux server' and not os.getenv('DASHBOARD_PASSWORD'):
        st.warning('Panel serverdə parolsuz işləyir. Telefondan və ya internetdən açmazdan əvvəl .env faylında DASHBOARD_PASSWORD təyin edin.')
    news_events=[row for row in queries.get_recent_events(DB_PATH,100) if row.get('source')=='news']
    active_calendar=next((row for row in news_events if 'Calendar source active' in row.get('message','')),None)
    news_warning=next((row for row in news_events if row.get('level')=='WARNING' and ('calendar' in row.get('message','').lower() or 'təqvim' in row.get('message','').lower())),None)
    if active_calendar:
        st.caption(f"Xəbər təqvimi: {active_calendar['message'].split('Calendar source active · ',1)[-1]} · son yenilənmə {local_time(active_calendar['time'])}")
    if news_warning:
        st.warning('MT5 təqvimi və internet təqvimi əlçatan deyil. USD/EUR/GBP/JPY ilə əlaqəli alətlərdə 12:30–14:30 UTC aralığında yeni əməliyyatlar bloklanır.')

    tab_main, tab_report, tab_analysis, tab_prepare, tab_detail, tab_brain, tab_pipeline = st.tabs(['Əsas', 'Hesabat', 'Analiz', 'Hazırlıq', 'Ətraflı', 'Beyin', 'Necə işləyir'])
    with tab_pipeline:
        st.subheader('Botun iş axını')
        st.graphviz_chart('''digraph pipeline {
          graph [rankdir=TB, bgcolor="transparent", pad="0.2", nodesep="0.28", ranksep="0.38"];
          node [shape=box, style="rounded,filled", color="#6b7280", fillcolor="#eff6ff", fontname="Segoe UI", fontsize=10];
          edge [color="#64748b", arrowsize=0.7];
          data [label="MT5 · kotirovka / şam / tarixçə"];
          scan [label="Skaner · sessiya / spred / hərəkət"];
          features [label="Xüsusiyyətlər · bazar konteksti"];
          strategy [label="Strategiyalar · MTF siqnalları"];
          filters [label="Xəbər · xərc · confluence · sıralama"];
          gates [label="Bot qapıları · brain shadow · qoruma"];
          sizing [label="Gərgin stop həcmi · portfel riski · marja"];
          order [label="İcraçı · broker yoxlaması · MT5 sifarişi"];
          manage [label="Mövqe sinxronu · stop / hədəf / çıxış"];
          review [label="Nəticə və proses icmalı · DB / hesabat"];
          schedule [label="Supervisor · backup / log təmizliyi / watchdog"];
          weekly [label="Həftəlik icmal · model təlimi təxirə salınıb · bandit skan sırasını dəyişir"];
          data -> scan -> features -> strategy -> filters -> gates -> sizing -> order -> manage -> review;
          review -> data [label="yeni dövr", fontsize=8];
          schedule -> data [style=dashed, label="botu nəzarətdə saxlayır", fontsize=8];
          review -> weekly [style=dashed];
        }''', width='stretch', alt='MT5 bazar məlumatından sifarişə və sonrakı icmala qədər botun axını')
        st.markdown('''**Qısa izah.** Bot öz MT5 sessiyasından bazar məlumatı alır və tarixçəni dövrlər ərzində kiçik hissələrlə yeniləyir. Skaner alətləri və strategiya siqnallarını yoxlayır; beyin hazırda müşahidə rejimindədir. Giriş sağlamlıq, xərc, qoruma və portfel risk qapılarından keçəndə həcm gərgin stop xərci ilə hesablanır və icraçı brokerə sifariş göndərir. Açıq mövqelər idarə olunur, bağlanmış əməliyyatlar nəticə və qərar prosesi üzrə qeydə alınır. Bot gündəlik yekun və həftəlik performans icmalı yaradır; supervisor ehtiyat nüsxə, log təmizliyi, yaddaş və heartbeat yoxlamalarını aparır. Həftəlik model yenidən təlimi təxirə salınıb.''')

        st.subheader('Son 1 saatın canlı funnel-i')
        funnel=queries.get_pipeline_funnel((datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),DB_PATH)
        if not funnel.get('available'):
            st.info('Skan funnel qeydləri hələ yaradılmayıb; botun yenilənmiş prosesi başladıqdan sonra görünəcək.')
        else:
            counts=funnel.get('counts',{})
            stages=[('Rəmzlər skan edilib','symbols_scanned'),('İlkin filtri keçib','filters_passed'),('Siqnal yaradılıb','signals'),('Riskdən keçib · icra cəhdi','passed_risk'),('Sifariş açılıb','orders')]
            cols=st.columns(len(stages))
            for col,(label,key) in zip(cols,stages): col.metric(label,f"{int(counts.get(key,0)):,}")
            st.caption(f"Tamamlanmış skan dövrləri: {funnel.get('cycles',0)} · Riskdən keçib sayı botun giriş qapılarını keçərək icraçıya çatan namizədlərdir; broker yenə də sifarişi rədd edə bilər.")
            rejection_labels=[('Filtrlər','filters'),('Siqnal mərhələsi','signals'),('Bot risk qapıları','risk'),('Broker / icraçı','orders')]
            reason_cols=st.columns(4)
            for col,(label,key) in zip(reason_cols,rejection_labels):
                reason_counts=funnel.get('rejections',{}).get(key,{})
                top=max(reason_counts.items(),key=lambda item:item[1]) if reason_counts else None
                col.markdown(f"**{label} · əsas imtina səbəbi**")
                col.write(f"{translate_message(top[0])} · {top[1]}" if top else 'Son 1 saatda imtina yoxdur.')

    with tab_main:
        opened=queries.get_open_positions(DB_PATH); now_utc=datetime.now(timezone.utc)
        for row in opened:
            row['duration']=format_duration((now_utc-_as_utc(row['open_time'])).total_seconds()/60)
            row['extension_status']='Uzadılıb' if int(row.get('extended_minutes') or 0)>0 else '—'
            row['direction_label']=direction_label(row['direction']); row['strategy_label']=strategy_label(row['strategy'])
            row['net_result']=float(row.get('profit') or 0)+float(row.get('commission') or 0)+float(row.get('swap') or 0)
        floating=sum(row['net_result'] for row in opened)
        # Loss limits use UTC days/weeks exactly like the bot does.
        utc_day=datetime.combine(now_utc.date(),dt_time.min,tzinfo=timezone.utc); utc_week=utc_day-timedelta(days=utc_day.weekday())
        magic=_APP_CONFIG.get('magic_number')
        pnl_utc_day=queries.get_pnl_since(utc_day.isoformat(),DB_PATH,magic)['pnl']
        pnl_utc_week=queries.get_pnl_since(utc_week.isoformat(),DB_PATH,magic)['pnl']
        balance=snapshot.get('balance') if snapshot else None; equity=snapshot.get('equity') if snapshot else None
        risk=risk_overview(_APP_CONFIG,account,balance,pnl_utc_day,pnl_utc_week,floating)
        today_all=queries.get_pnl_since(today_start,DB_PATH,magic)   # Baku day, every mode incl. exploration
        daily_result=float(today_all['pnl'])+floating
        tone=lambda value:'good' if value>0 else 'bad' if value<0 else ''
        st.markdown(ui.kpis([('Balans',format_money(balance),'',f'Kapital {format_money(equity)}'),
                             ('Bu gün',format_money(daily_result,signed=True),tone(daily_result),f'{today_all["trades"]} bağlanıb · {today_all["win_rate"]:.0%} qazanc · açıqlar daxil'),
                             ('Açıq mövqelər',str(len(opened)),'',f'Cari nəticə {format_money(floating,signed=True)}'),
                             ('Hər əmrdə risk',f'{risk["risk_pct"]:g}%','',f'≈ {format_money(risk["risk_money"])} · {risk["account"]} hesab')]),unsafe_allow_html=True)
        limit_cols=st.columns(1 if phone else 2)
        for col,(label,key) in zip(limit_cols*2,[('Gündəlik zərər limiti (UTC)','daily'),('Həftəlik zərər limiti','weekly')]):
            item=risk[key]
            with col:
                st.markdown(ui.limit_bar(label,item['used_share'],f'{item["used_share"]:.0%} istifadə olunub',
                    f'Zərər {format_money(item["loss_money"])} / limit {format_money(item["limit_money"])} ({item["limit_pct"]:g}%) · qalıb {format_money(item["left_money"])}'),unsafe_allow_html=True)
        if not risk['known']: st.caption('Hesab növü hələ məlum deyil; limitlər bot qoşulandan sonra dəqiqləşəcək.')
        elif risk['account']=='DEMO':
            st.caption(f"Kiçik balans: {risk['risk_pct']:g}% minimum lota çatmırsa, bot minimum lotla girir, əgər onun riski balansın {float(_APP_CONFIG.get('demo_risk',{}).get('min_lot_max_risk_pct',3)):g}%-ni keçmirsə.")
        elif balance and risk['risk_money']<1:
            st.info(f"Real hesabda hər əmrə cəmi {format_money(risk['risk_money'])} risk düşür: adi hesabda minimum lot bundan böyükdür və bot əmr açmayacaq. Kiçik real balans üçün XM Micro hesabı istifadə edin.")
        controls=st.columns([1.4,1.15,1.4])
        if st.session_state.get('action_message'):
            message=st.session_state.pop('action_message'); level=st.session_state.pop('action_level','info')
            getattr(st,level if level in {'success','warning','error','info'} else 'info')(message)
        with controls[0]:
            if st.button('⏹ Botu dayandır' if process_running else '▶ Botu başlat',key='main_bot_toggle',use_container_width=True):
                if process_running:
                    try:
                        queries.enqueue_command('stop',DB_PATH,ttl_seconds=_command_ttl())
                        st.session_state.action_message='Dayandırma əmri göndərildi.'; st.session_state.action_level='success'
                    except Exception as exc:
                        st.session_state.action_message=f'Dayandırma əmri göndərilmədi: {translate_message(exc)}'; st.session_state.action_level='error'
                else:
                    ok,message=_start_bot()
                    st.session_state.action_message='Botun başladılması istənildi.' if ok else f'Bot başlamadı: {translate_message(message)}'
                    st.session_state.action_level='success' if ok else 'error'
                st.rerun()
        with controls[1]:
            paused=(ROOT/'STOP').exists()
            if st.button('▶ Davam et' if paused else '⏸ Fasilə',key='pause_toggle',disabled=not process_running,use_container_width=True):
                command='resume' if paused else 'pause'
                try:
                    queries.enqueue_command(command,DB_PATH,ttl_seconds=_command_ttl())
                    st.session_state.action_message='Davam etmə əmri göndərildi.' if paused else 'Fasilə əmri göndərildi.'
                    st.session_state.action_level='success'
                except Exception as exc:
                    st.session_state.action_message=f'Əmr göndərilmədi: {translate_message(exc)}'; st.session_state.action_level='error'
                st.rerun()
        with controls[2]:
            with st.expander('Hamısını bağla və dayan',expanded=False):
                confirm=st.checkbox('Bütün açıq mövqelərin bağlanmasını təsdiq edirəm',key='confirm_close_all')
                if st.button('Hamısını bağla və dayan',key='close_all_stop',disabled=not (confirm and process_running),use_container_width=True):
                    try:
                        queries.enqueue_command('close_all_stop',DB_PATH,ttl_seconds=_command_ttl())
                        st.session_state.action_message='Bütün mövqeləri bağlama və dayandırma əmri göndərildi.'; st.session_state.action_level='success'
                    except Exception as exc:
                        st.session_state.action_message=f'Əmr göndərilmədi: {translate_message(exc)}'; st.session_state.action_level='error'
                    st.rerun()

        with st.expander('Yaxın xəbərlər',expanded=False):
            calendar_rows=[row for row in queries.get_recent_events(DB_PATH,100) if row.get('source')=='news' and 'Yaxın xəbər ·' in row.get('message','')]
            if calendar_rows:
                st.dataframe(pd.DataFrame([{'Təqvim yenilənib':local_time(row['time']),'Xəbər':row['message'].replace('Yaxın xəbər · ','')} for row in calendar_rows[:20]]),use_container_width=True,hide_index=True)
            else: st.caption('Növbəti 24 saat üçün təzə xəbər təqvimi yoxdur.')

        diversity=queries.get_today_diversity(today_start,today_end,DB_PATH)
        st.caption(f"Bu gün: {diversity['symbols']} fərqli alət, {diversity['groups']} qrup · istifadə olunan marja {format_money(snapshot.get('margin') if snapshot else None)}")

        st.subheader('Açıq mövqelər')
        if opened:
            open_frame=pd.DataFrame([{'Bilet':r['ticket'],'Alət':r['symbol'],'Həcm':r['volume'],'SL':r['sl'],'TP':r['tp'],'İstiqamət':r['direction_label'],'Nəticə':r['net_result'],'Açıq müddət':r['duration'],'Strategiya':r['strategy_label'],'Rejim':r['mode'],'Status':r['extension_status']} for r in opened])
            _table(open_frame,phone,['Alət','İstiqamət','Nəticə','Açıq müddət'],money='Nəticə')
            _position_charts(opened,phone)
        else: st.caption('Açıq bot mövqeyi yoxdur.')

        st.subheader('Bu gün bağlanan əməliyyatlar')
        closed=queries.get_closed_trades(today_start,today_end,DB_PATH)
        if closed:
            closed_frame=pd.DataFrame([{'Saat':local_time(r['close_time']),'Alət':r['symbol'],'İstiqamət':direction_label(r['direction']),'Nəticə':r['net_profit'],'Səbəb':close_reason_label(r['close_reason'])} for r in closed])
            _table(closed_frame,phone,['Saat','Alət','Nəticə'],money='Nəticə')
        else: st.caption('Bu gün bağlanan əməliyyat yoxdur.')

        external=queries.get_external_open_positions(DB_PATH)
        with st.expander(f'Xarici mövqelər ({len(external)})',expanded=bool(external) and not phone):
            if external:
                external_frame=pd.DataFrame([{'Bilet':r['ticket'],'Alət':r['symbol'],'Magic':r['magic'],'Həcm':r['volume'],'SL':r['sl'],'TP':r['tp'],'İstiqamət':direction_label(r['direction']),'Nəticə':float(r.get('profit') or 0)+float(r.get('swap') or 0),'Şərh':r.get('comment') or ''} for r in external])
                st.caption('Bu mövqelər başqa magic nömrəsi ilə açılıb və yalnız izləmə üçün göstərilir.')
                _table(external_frame,phone,['Alət','İstiqamət','Nəticə'],money='Nəticə')
            else: st.caption('Xarici açıq mövqe yoxdur.')

        exploration=queries.get_exploration_trades(today_start,today_end,DB_PATH)
        st.subheader('Kəşf əməliyyatları')
        if exploration:
            exploration_frame=pd.DataFrame([{'Saat':local_time(row['open_time']),'Alət':row['symbol'],'İstiqamət':direction_label(row['direction']),'Nəticə':row['net_profit'],'Status':'Açıqdır' if row['status']=='open' else close_reason_label(row.get('close_reason'))} for row in exploration])
            _table(exploration_frame,phone,['Saat','Alət','Nəticə'],money='Nəticə')
            closed_exploration=[row for row in exploration if row['status']=='closed']
            if closed_exploration:
                st.caption(f"Kəşf əməliyyatlarının nəticəsi: {sum(row['net_profit'] for row in closed_exploration):+.2f} $ · {len(closed_exploration)} bağlanmış əməliyyat")
        else: st.caption('Bu gün kəşf əməliyyatı yoxdur.')
        fwd_stats=queries.get_exploration_forward_tracking(DB_PATH)
        if fwd_stats:
            st.markdown('##### Kəşf Ailələri: Canlı Nəticələr vs Backtest Meyarları')
            fwd_df=pd.DataFrame([{'Ailə':s['name'],'Cəmi/Açıq':f"{s['total_trades']}/{s['open_trades']}",'Canlı Win%':s['win_rate'],'Canlı EV':s['ev_r'],'Backtest EV':s['bt_ev'],'Backtest Win%':s['bt_win']} for s in fwd_stats])
            st.dataframe(fwd_df,use_container_width=True,hide_index=True)
        weekend_control=queries.get_weekend_control(DB_PATH)
        if weekend_control and weekend_control.get('disabled_until'):
            st.warning(f"Həftəsonu rejimi mənfi nəticəyə görə {local_time(weekend_control['disabled_until'])} tarixinədək söndürülüb.")

        st.subheader('Bu günkü balans dəyişməsi')
        curve=queries.get_equity_curve(today_start,today_end,DB_PATH)
        if curve:
            curve_frame=pd.DataFrame(curve); curve_frame['Saat']=pd.to_datetime(curve_frame.time,utc=True).dt.tz_convert(AZ_TZ)
            st.area_chart(curve_frame.set_index('Saat')[['equity']].rename(columns={'equity':'Kapital'}),height=160 if phone else 200)
        else: st.caption('Bu gün üçün kapital qeydi yoxdur.')

    with tab_report:
        monthly_path=ROOT/'docs'/'MONTHLY_REPORT.md'
        st.subheader('Aylıq DEMO araşdırma hesabatı')
        if monthly_path.exists(): st.markdown(monthly_path.read_text(encoding='utf-8'))
        else: st.caption('Aylıq hesabat növbəti ayın 1-də yaradılacaq.')
        tracking=queries.get_exploration_forward_tracking(DB_PATH)
        if tracking:
            st.subheader('Canlı DEMO nəticələri və backtest müqayisəsi · forex + qızıl')
            st.dataframe(pd.DataFrame(tracking),use_container_width=True,hide_index=True)
        now_utc=datetime.now(timezone.utc); seven_start=(now_utc-timedelta(days=7)).isoformat(); all_start='1970-01-01T00:00:00+00:00'
        for label,period_start,period_end in [('Bu gün',today_start,today_end),('Son 7 gün',seven_start,now_utc.isoformat()),('Bütün tarix',all_start,now_utc.isoformat())]:
            report=queries.get_performance_report(period_start,period_end,DB_PATH); overall=report['overall']
            st.subheader(label)
            cards=st.columns(6)
            cards[0].metric('Əməliyyatlar',overall['trades']); cards[1].metric('Qazanma faizi',f"{overall['win_rate']:.1%}")
            cards[2].metric('Orta qazanc',format_money(overall['average_win'])); cards[3].metric('Orta zərər',format_money(overall['average_loss']))
            cards[4].metric('Gözlənilən nəticə',format_money(overall['expectancy'],signed=True)); cards[5].metric('Cəmi nəticə',format_money(overall['total_pnl'],signed=True))
            drawdown=f"{overall['max_drawdown_pct']:.2f}%" if overall['max_drawdown_pct'] is not None else 'Məlumat yoxdur'
            st.caption(f"Maksimum azalma: {drawdown} · təxmini əməliyyat xərcləri: {format_money(overall['estimated_costs'])}")
            for title,key in [('Strategiyaya görə','by_strategy'),('Aktiv qrupuna görə','by_asset_group'),('Bağlanma səbəbinə görə','by_close_reason')]:
                rows=report[key]
                if rows:
                    st.markdown(f'**{title}**')
                    st.dataframe(pd.DataFrame([{'Ad':name,'Əməliyyat':v['trades'],'Qazanma %':f"{v['win_rate']:.1%}",'Orta qazanc':format_money(v['average_win']),'Orta zərər':format_money(v['average_loss']),'Gözlənilən nəticə':format_money(v['expectancy'],signed=True),'Nəticə':format_money(v['total_pnl'],signed=True),'Komissiya':format_money(v['estimated_costs'])} for name,v in rows.items()]),use_container_width=True,hide_index=True)
        st.subheader('Son 30 günün günlük nəticəsi')
        daily=queries.get_daily_pnl(DB_PATH,30)
        if daily:
            daily_frame=pd.DataFrame(daily); daily_frame['Tarix']=pd.to_datetime(daily_frame.date)
            st.bar_chart(daily_frame.set_index('Tarix')[['pnl']].rename(columns={'pnl':'Nəticə ($)'}),height=220)
        else: st.caption('Günlük nəticə məlumatı yoxdur.')
        stored=queries.get_daily_report_rows(DB_PATH,30)
        if stored:
            st.subheader('Saxlanmış günlük hesabatlar')
            st.dataframe(pd.DataFrame([{'Tarix':r['date'],'Nəticə':format_money(r['payload'].get('pnl'),signed=True),'Əməliyyat':r['payload'].get('trades_count',0)} for r in stored]),use_container_width=True,hide_index=True)

    with tab_analysis:
        st.subheader('Xüsusiyyətlərin müşahidə nəticələri')
        st.info('Bunlar bağlanmış əməliyyatlardan hesablanan müşahidələrdir. Hazırda heç biri ticarət qərarlarında istifadə olunmur.')
        impact=queries.get_feature_impact(DB_PATH)
        if impact:
            st.dataframe(pd.DataFrame([{'Xüsusiyyət':feature_label(row['feature']),'Əməliyyat sayı':row['trades'],'Qazanma faizi':f"{row['win_rate']:.1%}",'Fərq (baseline ilə)':f"{row['win_rate_difference']:+.1%}",'Orta nəticə':format_money(row['expectancy'],signed=True),'Sübut olunub?':'Bəli' if row['meaningful'] else 'Hələ az məlumat' if row['trades']<30 else 'Xeyr'} for row in impact]),use_container_width=True,hide_index=True)
        else: st.caption('Xüsusiyyətlər üzrə müqayisə üçün hələ bağlanmış əməliyyat yoxdur.')

    with tab_prepare:
        now_utc=datetime.now(timezone.utc); seven_start=(now_utc-timedelta(days=7)).isoformat(); seven_end=now_utc.isoformat()
        st.subheader('Son 7 gün üzrə hazırlıq yoxlaması')
        for label,passed in readiness_checks(seven_start,seven_end,DB_PATH):
            st.markdown(('✅ ' if passed else '🔴 ')+label)
        dashboard_cfg=yaml.safe_load((ROOT/'config.yaml').read_text(encoding='utf-8')).get('dashboard',{})
        sim_cols=st.columns(2)
        balance=sim_cols[0].number_input('Real hesab məbləği ($)',min_value=1.0,value=float(dashboard_cfg.get('real_balance_default',100)),step=10.0)
        risk_pct=sim_cols[1].number_input('Planlaşdırılan risk (%)',min_value=.01,value=float(dashboard_cfg.get('real_risk_pct_default',1.0)),step=.1)
        raw=yaml.safe_load((ROOT/'config.yaml').read_text(encoding='utf-8'))
        history_dir=Path(raw.get('history',{}).get('data_dir','data/history')); history_dir=history_dir if history_dir.is_absolute() else ROOT/history_dir
        try: specs=json.loads((history_dir/'symbol_specs.json').read_text(encoding='utf-8'))
        except (OSError,ValueError): specs={}
        signal_rows=queries.get_signals_period(seven_start,seven_end,DB_PATH)
        sim=simulate_small_account(signal_rows,balance,risk_pct,specs)
        st.info(f"Son 7 gündəki {sim['signals']} siqnaldan bu kapital və risklə {sim['possible']} əməliyyat açmaq olardı; {sim['min_lot_rejected']} siqnal minimum lot riskə görə rədd edilərdi. {sim['unavailable']} siqnal üçün broker spesifikasiyası və ya düzgün SL yoxdur.")

    with tab_detail:
        with st.expander('Ətraflı məlumat',expanded=False):
            stats=status or {}; heartbeat_age=(datetime.now(timezone.utc)-_as_utc(status['time'])).total_seconds() if status and status.get('time') else None
            st.subheader('Hesab və skanlama')
            diversity=queries.get_today_diversity(today_start,today_end,DB_PATH)
            group_labels={'forex':'Valyuta','metals':'Metallar','indices':'İndekslər','energy':'Enerji','crypto':'Kripto','stocks':'Səhmlər','other':'Digər'}
            st.caption('Bu gün qruplar üzrə əməliyyatlar')
            if diversity['per_group']:
                st.table(pd.DataFrame([{'Qrup':group_labels.get(group,group),'Əməliyyat sayı':count} for group,count in sorted(diversity['per_group'].items(),key=lambda item:-item[1])]))
                st.caption('Bu gün ən çox işlənən alətlər: '+', '.join(f'{name} ({count})' for name,count in diversity['top_symbols']))
            else: st.caption('Bu gün hələ bağlanmış əməliyyat yoxdur.')
            detail_metrics=st.columns(4)
            detail_metrics[0].metric('Kapital',format_money(snapshot.get('equity') if snapshot else None))
            detail_metrics[1].metric('Sərbəst marja',format_money(snapshot.get('free_margin') if snapshot else None))
            detail_metrics[2].metric('MT5 əlaqəsi','Aktivdir' if mt5_connected else 'Yoxdur')
            detail_metrics[3].metric('Ürək döyüntüsü',f'{heartbeat_age:.0f} san' if heartbeat_age is not None else 'Məlumat yoxdur')
            st.caption(f'Skanlama: cəmi {stats.get("scan_total",0)} · yoxlanıb {stats.get("scan_scanned",0)} · ticarətə uyğun {stats.get("scan_stage1_passed",0)} · təhlil edilən alətlər {stats.get("scan_top_n",0)} · siqnallar {stats.get("scan_signals",0)} · {float(stats.get("scan_seconds",0) or 0):.2f} san')
            st.subheader('Gündəlik iş qrafiki')
            schedule_rows=queries.get_scheduler_status(DB_PATH)
            task_names={'hourly_history':'Saatlıq tarixçə','daily_backup':'Gündəlik ehtiyat nüsxə','weekly_retrain':'Həftəlik beyin yenilənməsi','weekly_vacuum':'Həftəlik DB təmizliyi'}
            st.table(pd.DataFrame([{'İş':task_names.get(row['task'],row['task']),'Son icra':local_time(row['last_run']) if row['last_run'] else 'Hələ icra edilməyib','Növbəti icra (UTC)':row['next_run'],'Vəziyyət':row['status']} for row in schedule_rows]))
            target=int(_APP_CONFIG.get('scanner',{}).get('target_open_positions',15))
            st.caption(f'Açıq bot mövqeləri: {len(opened)} / {target} hədəf (hədəf məcburi əməliyyat sayı deyil)')
            skips=queries.get_top_skip_reasons_since((datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),DB_PATH,5)
            st.subheader('Son 1 saatın əsas imtina səbəbləri')
            if skips: st.dataframe(pd.DataFrame([{'Səbəb':translate_message(r['skip_reason']),'Say':r['count']} for r in skips]),use_container_width=True,hide_index=True)
            else: st.caption('Son 1 saatda imtina edilmiş siqnal yoxdur.')
            adaptive=queries.get_adaptive_gates(DB_PATH)
            st.subheader('Müvəqqəti söndürülən strategiyalar və qruplar')
            if adaptive:
                group_names={'forex':'Valyuta','metals':'Metallar','indices':'İndekslər','energy':'Enerji','crypto':'Kripto','stocks':'Səhmlər','other':'Digər'}
                st.table(pd.DataFrame([{'Növ':'Strategiya' if r['scope']=='strategy' else 'Alət qrupu','Ad':strategy_label(r['name']) if r['scope']=='strategy' else group_names.get(r['name'],r['name']),'Son 20 əməliyyatın gözlənilən nəticəsi':format_money(r['expectancy'],signed=True),'Səbəb':'Son bağlanmış əməliyyatlardakı orta nəticə mənfidir','Nümunə':r['sample_size'],'Söndürülüb':'Bəli · '+local_time(r['disabled_until'])} for r in adaptive]))
            else: st.caption('Hazırda müvəqqəti söndürülən strategiya və qrup yoxdur.')
            try:
                import ast
                rejections=stats.get('scan_rejections') or '{}'; rejections=ast.literal_eval(rejections) if isinstance(rejections,str) else rejections
            except (ValueError,SyntaxError): rejections={}
            if rejections: st.table(pd.DataFrame([{'Səbəb':translate_message(k),'Say':v} for k,v in rejections.items()]))
            try:
                diagnostics=stats.get('strategy_diagnostics') or '{}'; diagnostics=json.loads(diagnostics) if isinstance(diagnostics,str) else diagnostics
            except (ValueError,TypeError): diagnostics={}
            rows=[{'Strategiya':strategy_label(name),'Səbəb':translate_message(reason),'Say':count} for name,counts in diagnostics.items() for reason,count in counts.items()]
            if rows: st.table(pd.DataFrame(rows))

            st.subheader('Ətraflı əməliyyat məlumatı')
            selected=st.date_input('Tarix · Bakı vaxtı',value=selected,key='detail_date',format='DD.MM.YYYY')
            detail_start=datetime.combine(selected,dt_time.min,tzinfo=AZ_TZ).astimezone(timezone.utc).isoformat()
            detail_end=(datetime.combine(selected,dt_time.min,tzinfo=AZ_TZ)+timedelta(days=1)).astimezone(timezone.utc).isoformat()
            detail_closed=queries.get_closed_trades(detail_start,detail_end,DB_PATH)
            if detail_closed:
                full_closed=pd.DataFrame([{'Saat':local_time(r['close_time']),'Alət':r['symbol'],'İstiqamət':direction_label(r['direction']),'Həcm':r['volume'],'Açılış qiyməti':r['open_price'],'Bağlanış qiyməti':r['close_price'],'SL':r.get('sl'),'TP':r.get('tp'),'Nəticə':r['net_profit'],'Proses':('Yaxşı' if r.get('process_review',{}).get('good_process') else 'Naməlum'),'Nəticə (R)':r.get('process_review',{}).get('outcome_r'),'Səbəb':close_reason_label(r['close_reason']),'Strategiya':strategy_label(r['strategy'])} for r in detail_closed])
                st.table(full_closed.style.format({'Nəticə':lambda value:format_money(value,signed=True)}).map(_money_color,subset=['Nəticə']))
            else: st.caption('Seçilən tarixdə bağlanan əməliyyat yoxdur.')
            if opened:
                full_open=pd.DataFrame([{'Alət':r['symbol'],'İstiqamət':direction_label(r['direction']),'Həcm':r['volume'],'Açılış qiyməti':r['open_price'],'Nəticə':r['net_result'],'SL':r['sl'],'TP':r['tp'],'Açıq vaxt':local_time(r['open_time']),'Strategiya':r['strategy_label']} for r in opened])
                st.table(full_open.style.format({'Nəticə':lambda value:format_money(value,signed=True)}).map(_money_color,subset=['Nəticə']))
            st.subheader('Qismən bağlamalar')
            partials=queries.get_partial_closes(detail_start,detail_end,DB_PATH)
            if partials:
                partial_frame=pd.DataFrame([{'Saat':local_time(r['time']),'Alət':r['symbol'],'Həcm':r['volume'],'Qiymət':r['price'],'Nəticə':float(r['profit'] or 0)+float(r['commission'] or 0)+float(r['swap'] or 0),'Səbəb':close_reason_label(r['reason'])} for r in partials])
                st.table(partial_frame.style.format({'Nəticə':lambda value:format_money(value,signed=True)}).map(_money_color,subset=['Nəticə']))
            else: st.caption('Seçilən tarixdə qismən bağlama yoxdur.')

            st.subheader('Siqnallar')
            signals=queries.get_latest_signals(DB_PATH,50)
            if signals:
                signal_rows=[{'Saat':local_time(r['time']),'Alət':r['symbol'],'Strategiya':strategy_label(r['strategy']),'İstiqamət':direction_label(r['direction']),'Bal':r.get('score',0),'Əminlik':f'{float(r.get("confidence") or 0):.1%}','İcra edildi':'Bəli' if r.get('taken') else 'Xeyr','Səbəb':translate_message(r.get('skip_reason'))} for r in signals]
                st.table(pd.DataFrame(signal_rows))
            else: st.caption('Qeydə alınmış siqnal yoxdur.')

            st.subheader('Son əmrlər')
            commands=queries.get_recent_commands(DB_PATH,10)
            command_names={'pause':'Fasilə','resume':'Davam et','stop':'Dayandır','close_all':'Hamısını bağla','close_all_stop':'Hamısını bağla və dayan'}
            command_status={'pending':'Gözləyir','running':'İcra olunur','done':'Tamamlandı','failed':'Uğursuz','expired':'Vaxtı bitib'}
            if commands:
                st.table(pd.DataFrame([{'Vaxt':local_time(c.get('created_at') or c.get('time')),'Əmr':command_names.get(c['command'],c['command']),'Vəziyyət':command_status.get(c['status'],c['status']),'Məlumat':translate_message(c.get('message'))} for c in commands]))
            else: st.caption('Son əmr yoxdur.')

            st.subheader('Bot hadisələri')
            events=queries.get_recent_events(DB_PATH,30)
            level_names={'INFO':'MƏLUMAT','WARNING':'XƏBƏRDARLIQ','ERROR':'XƏTA','CRITICAL':'CİDDİ XƏTA'}
            source_names={'bot':'Bot','scanner':'Skaner','executor':'İcraçı','mt5_client':'MT5'}
            if events: st.table(pd.DataFrame([{'Vaxt':local_time(e['time']),'Səviyyə':level_names.get(e['level'],e['level']),'Mənbə':source_names.get(e['source'],e['source']),'Məlumat':translate_message(e['message'])} for e in events]))
            else: st.caption('Bot hadisəsi qeydə alınmayıb.')

            st.subheader('Son 30 günün nəticəsi')
            daily_stats=queries.get_daily_pnl(DB_PATH,30)
            if daily_stats:
                daily_frame=pd.DataFrame(daily_stats); daily_frame['Tarix']=pd.to_datetime(daily_frame.date)
                st.bar_chart(daily_frame.set_index('Tarix')[['pnl']].rename(columns={'pnl':'Nəticə ($)'}),height=220)
            else: st.caption('Günlük statistika hələ yoxdur.')

    with tab_brain:
        weekly_learning=next((row for row in queries.get_recent_events(DB_PATH,100) if 'Bu həftə öyrəndiklərim' in row.get('message','')),None)
        st.subheader('Bu həftə öyrəndiklərim')
        if weekly_learning: st.info(translate_message(weekly_learning['message']))
        else: st.caption('Həftəlik yekun hesabat hələ yoxdur.')
        brain_cfg=_APP_CONFIG.get('brain',{}); gate_path=DATA_DIR/'brain'/'gate.json'
        try: gate=json.loads(gate_path.read_text(encoding='utf-8'))
        except (OSError,ValueError): gate={'status':'FAIL','reasons':['Yekun test qiymətləndirməsi hələ aparılmayıb.']}
        live_gate_path=DATA_DIR/'brain'/'live_gate.json'
        try: live_gate=json.loads(live_gate_path.read_text(encoding='utf-8'))
        except (OSError,ValueError): live_gate={}
        if brain_state['state']=='good': st.success(f"{brain_state['title']} · {brain_state['text']}")
        else:
            st.warning(f"{brain_state['title']} · {brain_state['text']}")
            for reason in brain_state['reasons']: st.caption('• '+reason)
            for reason in gate.get('reasons',[]): st.caption('• '+translate_message(reason))
        brain_risk=risk_overview(_APP_CONFIG,account,None,0,0,0)
        if brain_risk['brain_max_pct']>0:
            st.caption(f"Beyin aktiv olanda risk aralığı ({brain_risk['account']} hesab): {brain_risk['brain_min_pct']:g}% – {brain_risk['brain_max_pct']:g}% · ardıcıl {int(brain_cfg.get('loss_streak_base_risk',3))} zərərdən sonra baza riskə qayıdır")
        if live_gate.get('mode')=='shadow': st.error('Canlı sapma qoruyucusu beyni müşahidə rejiminə qaytardı.')
        if gate.get('evaluated_at'): st.caption('Son qiymətləndirmə: '+local_time(gate['evaluated_at']))
        calibration=gate.get('metrics',{}).get('calibration',[])
        st.subheader('Ehtimal kalibrlənməsi')
        if calibration:
            cal=pd.DataFrame(calibration).rename(columns={'predicted':'Proqnoz','actual':'Fakt','bucket':'Ehtimal aralığı','samples':'Nümunə sayı'}).set_index('Ehtimal aralığı')
            st.line_chart(cal[['Proqnoz','Fakt']],height=230)
        else: st.caption('Test və canlı kalibrlənmə məlumatı yoxdur.')
        st.subheader('Müqayisə və qatların töhfəsi')
        ablation=gate.get('metrics',{}).get('ablation',{})
        if ablation:
            st.table(pd.DataFrame([{'Model':name,'Əməliyyat':row['trades'],'Qazanma faizi':f"{row['win_rate']:.1%}",'Gözlənilən nəticə (R)':row['expectancy_r'],'Mənfəət əmsalı':row['profit_factor']} for name,row in ablation.items()]))
        else: st.caption('Ablasiya nəticəsi mövcud deyil.')
        scanned=int((status or {}).get('scan_scanned',0)); total=int((status or {}).get('scan_total',0)); interval=float(_APP_CONFIG.get('loop',{}).get('interval_seconds',15))
        rotation=total/max(1,scanned)*interval if total else 0
        st.metric('Dövriyyə təhlili',f"{int((status or {}).get('scan_top_n',0))} alət təhlil edildi · təxmini tam dövr {rotation:.0f} san")
        st.subheader('Açıq mövqe üzrə oxşar hallar')
        if opened:
            selected_open=st.selectbox('Alət',opened,key='brain_open_symbol',format_func=lambda row:f"{row['symbol']} · {direction_label(row['direction'])}")
            try:
                stored=json.loads(selected_open.get('features') or '{}'); cases=stored.get('brain',{}).get('memory',{}).get('cases',[])
            except (ValueError,TypeError): cases=[]
            if cases: st.table(pd.DataFrame([{'Alət':case.get('symbol'),'Vaxt':local_time(case.get('time')),'Oxşarlıq məsafəsi':case.get('distance')} for case in cases[:3]]))
            else: st.caption('Bu mövqe üçün keçmişdə uyğun oxşar hal tapılmayıb.')
        else: st.caption('Hazırda açıq bot mövqeyi yoxdur.')

if __name__=='__main__': main()
