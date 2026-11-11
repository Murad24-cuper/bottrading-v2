"""Validation-calibrated out-of-distribution abstention helpers."""
from __future__ import annotations
import math


def _vector(value):
    result=tuple(float(item) for item in value)
    if not result or any(not math.isfinite(item) for item in result):
        raise ValueError('OOD vectors must be non-empty and finite')
    return result


def _distance(left,right):
    if len(left)!=len(right):
        raise ValueError('OOD vector dimensions do not match')
    return math.sqrt(sum((a-b)**2 for a,b in zip(left,right)))


def fit_ood_guard(training_vectors,validation_vectors,percentile=99.0):
    """Set the nearest-training-distance cutoff from validation examples only."""
    training=[_vector(row) for row in training_vectors]
    validation=[_vector(row) for row in validation_vectors]
    if not training or not validation:
        raise ValueError('OOD guard requires non-empty train and validation vectors')
    dimension=len(training[0])
    if any(len(row)!=dimension for row in training+validation):
        raise ValueError('OOD training and validation dimensions do not match')
    percentile=float(percentile)
    if not 0.0<percentile<=100.0:
        raise ValueError('percentile must be in (0, 100]')
    distances=sorted(min(_distance(row,reference) for reference in training) for row in validation)
    index=max(0,min(len(distances)-1,math.ceil(percentile/100.0*len(distances))-1))
    return {'threshold':distances[index],'percentile':percentile,'dimension':dimension,
            'training_count':len(training),'validation_count':len(validation),
            'method':'nearest_train_euclidean_validation_percentile'}


def is_out_of_distribution(vector,training_vectors,guard):
    """Abstain when a query is farther from every training row than validation allows."""
    query=_vector(vector); training=[_vector(row) for row in training_vectors]
    if not training:
        return True
    if len(query)!=int(guard['dimension']) or any(len(row)!=len(query) for row in training):
        return True
    nearest=min(_distance(query,row) for row in training)
    return nearest>float(guard['threshold'])


def fit_ood_guard_fast(training_vectors,validation_vectors,percentile=99.0,max_training=20000,max_validation=2000):
    """NumPy version of :func:`fit_ood_guard` for large memories.

    Evenly spaced deterministic subsamples keep it fast; the cutoff is still set from
    validation rows only, so it measures how far genuinely unseen situations sit from
    the training memory."""
    import numpy as np
    train=np.asarray(training_vectors,dtype=np.float32); valid=np.asarray(validation_vectors,dtype=np.float32)
    if train.ndim!=2 or valid.ndim!=2 or not len(train) or not len(valid):
        raise ValueError('OOD guard requires non-empty 2D train and validation vectors')
    if train.shape[1]!=valid.shape[1]: raise ValueError('OOD training and validation dimensions do not match')
    if not (np.isfinite(train).all() and np.isfinite(valid).all()): raise ValueError('OOD vectors must be finite')
    percentile=float(percentile)
    if not 0.0<percentile<=100.0: raise ValueError('percentile must be in (0, 100]')
    if len(train)>max_training: train=train[np.linspace(0,len(train)-1,int(max_training),dtype=int)]
    if len(valid)>max_validation: valid=valid[np.linspace(0,len(valid)-1,int(max_validation),dtype=int)]
    nearest=np.empty(len(valid),dtype=np.float64)
    for start in range(0,len(valid),256):
        block=valid[start:start+256]
        d2=(block**2).sum(1)[:,None]-2*block@train.T+(train**2).sum(1)[None,:]
        nearest[start:start+256]=np.sqrt(np.maximum(d2.min(1),0.0))
    threshold=float(np.percentile(nearest,percentile,method='higher'))
    return {'threshold':threshold,'percentile':percentile,'dimension':int(train.shape[1]),
            'training_count':int(len(train)),'validation_count':int(len(valid)),
            'method':'nearest_train_euclidean_validation_percentile'}


def distance_is_out_of_distribution(nearest_distance,guard):
    """Classify a query from its nearest-neighbour distance.

    Returns True/False, or None when no guard is available (unknown)."""
    if not guard: return None
    if nearest_distance is None: return True
    try: value=float(nearest_distance); threshold=float(guard['threshold'])
    except (KeyError,TypeError,ValueError): return None
    if not math.isfinite(value): return True
    return value>threshold
