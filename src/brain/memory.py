"""Small, causal nearest-situation memory helpers (NumPy only)."""
from __future__ import annotations
from datetime import datetime,timezone,timedelta
import numpy as np

def situation_vector(candles: dict, context: dict | None=None, direction: str | None=None) -> np.ndarray:
    """Encode recent closed-bar returns and context without using future rows."""
    values=[]
    for timeframe,count in (('M5',48),('M15',32)):
        frame=candles.get(timeframe)
        if frame is None or len(frame)<count+1:
            values.extend([0.0]*count); continue
        close=frame['close'].astype(float).iloc[-count-1:]
        returns=np.diff(np.log(np.maximum(close.to_numpy(),1e-12)))
        atr=float(frame['high'].astype(float).sub(frame['low'].astype(float)).tail(14).mean())
        price=float(close.iloc[-1]); scale=max(atr/max(price,1e-12),1e-8)
        values.extend(np.clip(returns/scale,-5,5).tolist())
    context=context or {}
    for key in ('checklist_score','trend','momentum','volatility','support_distance_atr','resistance_distance_atr','usd_strength','risk_on'):
        raw=context.get(key,0.0)
        try: value=float(raw)
        except (TypeError,ValueError): value=0.0
        values.append(float(np.clip(value,-5,5)) if np.isfinite(value) else 0.0)
    minutes=context.get('minutes_to_next_event')
    values.append(float(np.clip(float(minutes)/1440.0,0.0,7.0)) if minutes is not None and np.isfinite(float(minutes)) else 7.0)
    values.append(1.0 if direction=='buy' else -1.0 if direction=='sell' else 0.0)
    return np.asarray(values,dtype=np.float32)

def fit_pca(training_vectors: np.ndarray, components: int=28) -> dict:
    """Fit a compact PCA transform using only the supplied training rows."""
    data=np.asarray(training_vectors,dtype=np.float64)
    if data.ndim!=2 or not len(data): raise ValueError('training_vectors must be a non-empty 2D array')
    mean=data.mean(axis=0); centered=data-mean
    _,_,vectors=np.linalg.svd(centered,full_matrices=False)
    count=max(1,min(int(components),vectors.shape[0],data.shape[1]))
    return {'mean':mean.astype(np.float32),'components':vectors[:count].astype(np.float32)}

def apply_pca(vectors: np.ndarray, model: dict) -> np.ndarray:
    data=np.asarray(vectors,dtype=np.float32)
    return (data-np.asarray(model['mean'],dtype=np.float32))@np.asarray(model['components'],dtype=np.float32).T

def prepare_memory(rows: list[dict]) -> dict:
    """Build reusable NumPy arrays for fast causal neighbor queries."""
    if not rows: return {'rows':[],'vectors':np.empty((0,0),dtype=np.float32),'times':np.empty(0,dtype='datetime64[ns]'),'symbols':np.empty(0,dtype=object),'wins':np.empty(0,dtype=np.float32),'net_r':np.empty(0,dtype=np.float32),'base_rate':.5}
    valid=[]
    for row in rows:
        try:
            vector=np.asarray(row['vector'],dtype=np.float32)
            parsed=datetime.fromisoformat(str(row['time']).replace('Z','+00:00'))
            parsed=parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
            stamp=np.datetime64(parsed.replace(tzinfo=None),'ns')
            if vector.ndim!=1 or not np.isfinite(vector).all(): continue
            valid.append((stamp,str(row.get('symbol','')),float(row.get('win',0)),float(row.get('net_r',0)),vector,row))
        except (KeyError,TypeError,ValueError,OverflowError): continue
    if not valid: return prepare_memory([])
    dimension=valid[0][4].shape[0]; valid=[item for item in valid if item[4].shape[0]==dimension]
    return {'rows':[item[5] for item in valid],'vectors':np.stack([item[4] for item in valid]),'times':np.asarray([item[0] for item in valid],dtype='datetime64[ns]'),'symbols':np.asarray([item[1] for item in valid],dtype=object),'wins':np.asarray([item[2] for item in valid],dtype=np.float32),'net_r':np.asarray([item[3] for item in valid],dtype=np.float32),'base_rate':float(np.mean([item[2] for item in valid]))}

def query_memory(vector, rows: list[dict], query_time: datetime, symbol: str,
                 k: int=300, exclude_same_symbol_days: int=1) -> dict:
    """Query only strictly earlier examples and optionally purge nearby same-symbol rows."""
    query_time=query_time.replace(tzinfo=timezone.utc) if query_time.tzinfo is None else query_time.astimezone(timezone.utc)
    query=np.asarray(vector,dtype=np.float32); purge=np.timedelta64(int(exclude_same_symbol_days),'D'); cutoff=np.datetime64(query_time.replace(tzinfo=None),'ns')
    index=rows if isinstance(rows,dict) and 'vectors' in rows else prepare_memory(rows)
    if not len(index['rows']): return {'count':0,'probability':None,'avg_win_r':0.0,'avg_loss_r':0.0,'cases':[],'mean_distance':None}
    vectors=index['vectors']; mask=(index['times']<cutoff)&~((index['symbols']==str(symbol))&((cutoff-index['times'])<=purge))
    if vectors.shape[1]!=query.size: return {'count':0,'probability':None,'avg_win_r':0.0,'avg_loss_r':0.0,'cases':[],'mean_distance':None}
    candidate_ids=np.flatnonzero(mask)
    if not len(candidate_ids): return {'count':0,'probability':None,'avg_win_r':0.0,'avg_loss_r':0.0,'cases':[],'mean_distance':None}
    distances=np.linalg.norm(vectors[candidate_ids]-query,axis=1)
    take=min(max(1,int(k)),len(candidate_ids)); nearest_local=np.argpartition(distances,take-1)[:take]; nearest_local=nearest_local[np.argsort(distances[nearest_local])]
    ids=candidate_ids[nearest_local]; dists=distances[nearest_local]
    weights=1/np.maximum(dists,1e-6); wins=index['wins'][ids]; net=index['net_r'][ids]
    base=float(np.mean(index['wins'][candidate_ids])) if len(candidate_ids) else float(index.get('base_rate',.5))
    effective=float(weights.sum()**2/max(np.sum(weights**2),1e-12)); distance_shrink=float(np.exp(-float(np.mean(dists))/max(1.0,np.sqrt(query.size))))
    shrink=min(1.0,effective/50.0)*distance_shrink
    probability=shrink*float(np.average(wins,weights=weights))+(1-shrink)*base
    win_r=net[net>0]; loss_r=-net[net<0]
    cases=[{'symbol':index['rows'][int(idx)].get('symbol'),'time':str(index['times'][idx])+'Z','distance':float(distance)} for idx,distance in zip(ids[:3],dists[:3])]
    return {'count':len(ids),'probability':probability,'avg_win_r':float(np.mean(win_r)) if len(win_r) else 0.0,'avg_loss_r':float(np.mean(loss_r)) if len(loss_r) else 0.0,'cases':cases,'mean_distance':float(np.mean(dists)),'nearest_distance':float(dists[0])}
