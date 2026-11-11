"""Build causal labeled samples and train-only reference memory offline."""
from __future__ import annotations

from pathlib import Path
import json
import sys
import time
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
from src.parquet_compat import write_parquet

from src.brain.dataset import build_labeled_outcomes
from src.brain.memory import apply_pca, fit_pca
from src.config import load_settings


def _memory_mb() -> float|None:
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1024**2
    except ImportError:
        if sys.platform != 'win32': return None
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD),('PeakWorkingSetSize',ctypes.c_size_t),('WorkingSetSize',ctypes.c_size_t),('QuotaPeakPagedPoolUsage',ctypes.c_size_t),('QuotaPagedPoolUsage',ctypes.c_size_t),('QuotaPeakNonPagedPoolUsage',ctypes.c_size_t),('QuotaNonPagedPoolUsage',ctypes.c_size_t),('PagefileUsage',ctypes.c_size_t),('PeakPagefileUsage',ctypes.c_size_t)]
        counters=Counters(); counters.cb=ctypes.sizeof(counters)
        kernel=ctypes.WinDLL('kernel32',use_last_error=True); psapi=ctypes.WinDLL('psapi',use_last_error=True)
        kernel.GetCurrentProcess.restype=wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype=wintypes.BOOL
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(counters),counters.cb): return None
        return counters.WorkingSetSize/1024**2


def main() -> int:
    settings=load_settings(require_credentials=False)
    if settings.config['account']['mode']!='demo' or settings.allow_real:
        raise SystemExit('Offline brain build requires DEMO configuration and ALLOW_REAL=false.')
    brain_dir=settings.data_dir/'brain'; brain_dir.mkdir(parents=True,exist_ok=True)
    split_path=brain_dir/'splits.json'; history=settings.data_dir/'history'
    if not split_path.is_file(): raise SystemExit(f'Missing frozen split file: {split_path}')
    split=json.loads(split_path.read_text(encoding='utf-8'))
    specs_path=history/'symbol_specs.json'
    specs=json.loads(specs_path.read_text(encoding='utf-8')) if specs_path.is_file() else {}
    symbols=list(split.get('symbols',{}))
    started=time.perf_counter(); peak=[_memory_mb() or 0.0]; stop_monitor=threading.Event()
    def monitor():
        while not stop_monitor.wait(.5):
            used=_memory_mb()
            if used is not None: peak[0]=max(peak[0],used)
    watcher=threading.Thread(target=monitor,name='brain-memory-monitor',daemon=True); watcher.start()
    outcomes=build_labeled_outcomes(history,symbols,specs,split,settings.config,
                                    int(settings.config.get('brain',{}).get('sample_every_minutes',30)))
    if outcomes.empty: raise SystemExit('No labeled examples were produced from the configured focus/weekend history.')
    outcome_path=brain_dir/'labeled_outcomes.parquet'; write_parquet(outcomes,outcome_path,index=False)
    train=outcomes[outcomes.split=='train'].copy()
    if train.empty: raise SystemExit('Frozen train split has no labeled examples; refusing to fit memory.')
    maximum=int(settings.config.get('brain',{}).get('max_reference_situations',1_500_000))
    if len(train)>maximum:
        # Deterministic, evenly spaced sampling preserves coverage across symbols and time.
        chosen=[]
        for _,group in train.sort_values(['symbol','time','direction']).groupby('symbol'):
            take=max(1,int(maximum*len(group)/len(train)))
            chosen.extend(group.index[np.linspace(0,len(group)-1,min(take,len(group)),dtype=int)].tolist())
        train=outcomes.loc[chosen].copy()
    vectors=np.asarray(train.vector.tolist(),dtype=np.float32)
    pca=fit_pca(vectors,int(settings.config.get('brain',{}).get('pca_components',28)))
    pca_path=brain_dir/'pca.json'
    pca_path.write_text(json.dumps({'mean':pca['mean'].tolist(),'components':pca['components'].tolist()},separators=(',',':')),encoding='utf-8')
    transformed=[]
    source=np.asarray(outcomes.vector.tolist(),dtype=np.float32)
    for start in range(0,len(source),20_000): transformed.extend(apply_pca(source[start:start+20_000],pca).tolist())
    outcomes['vector']=transformed
    # Reference memory contains TRAIN rows only; validation/holdout labels never enter it.
    train_ids=set(train.index)
    reference=outcomes.loc[sorted(train_ids)].copy()
    reference_path=brain_dir/'reference.parquet'; write_parquet(reference,reference_path,index=False)
    write_parquet(reference[reference.weekend.astype(bool)],brain_dir/'weekend_reference.parquet',index=False)
    write_parquet(reference[~reference.weekend.astype(bool)],brain_dir/'weekday_reference.parquet',index=False)
    # Store transformed vectors in the labeled set after keeping a separate full outcome file.
    write_parquet(outcomes,outcome_path,index=False)
    # OOD guard: distance cutoff from VALIDATION rows to the TRAIN memory. Live queries
    # farther than this are "unfamiliar" and the brain abstains from sizing them.
    validation=outcomes[outcomes.split=='validation']
    ood_summary=None
    if len(validation):
        from src.brain.ood import fit_ood_guard_fast
        guard=fit_ood_guard_fast(np.asarray(reference.vector.tolist(),dtype=np.float32),np.asarray(validation.vector.tolist(),dtype=np.float32),
                                 float(settings.config.get('brain',{}).get('ood_percentile',99.0)))
        guard['fitted_at_utc']=pd.Timestamp.now(tz='UTC').isoformat()
        (brain_dir/'ood_guard.json').write_text(json.dumps(guard,indent=2),encoding='utf-8')
        ood_summary={'threshold':guard['threshold'],'percentile':guard['percentile']}
    else:
        print('No validation rows; OOD guard not fitted. Brain cannot size live risk until it is.')
    stats={split_name:int((outcomes.split==split_name).sum()) for split_name in ('train','validation','test','purged')}
    stop_monitor.set(); watcher.join(timeout=2)
    used=_memory_mb()
    if used is not None: peak[0]=max(peak[0],used)
    summary={'built_at_utc':pd.Timestamp.now(tz='UTC').isoformat(),'samples':len(outcomes),
        'outcomes_by_split':stats,'train_reference_rows':len(reference),
        'weekend_reference_rows':int(reference.weekend.astype(bool).sum()),
        'asset_groups':sorted(reference.asset_group.unique().tolist()),
        'symbols':sorted(reference.symbol.unique().tolist()),
        'win_rate_by_split':{name:float(outcomes.loc[outcomes.split==name,'win'].mean()) for name in stats if stats[name]},
        'mean_net_r_by_split':{name:float(outcomes.loc[outcomes.split==name,'net_r'].mean()) for name in stats if stats[name]},
        'pca_dimensions':int(pca['components'].shape[0]),'pca_fit_rows':len(vectors),'ood_guard':ood_summary,
        'elapsed_seconds':round(time.perf_counter()-started,2),'process_memory_mb_peak':round(peak[0],2),
        'label_method':'next-bar entry; configured ATR-scaled SL/TP; stop-first OHLC; spread, slippage, commission; UTC time barrier'}
    (brain_dir/'dataset_summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary,indent=2))
    return 0


if __name__=='__main__': raise SystemExit(main())
