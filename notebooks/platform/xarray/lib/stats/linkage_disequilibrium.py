from numba import cuda
import numpy as np
import xarray as xr
import contextlib
from dask.distributed import Lock
from ..method.interval import axis_interval_fields as aif, ChunkInterval
from ..stats import math as gmath

# Axis interval fields (`aif`) cannot be used directly in kernel,
# but constants like this from outer scope will compile
AIF_IDX_INDEX = aif.index('index')
AIF_IDX_START = aif.index('start')
AIF_IDX_STOP = aif.index('stop')
AIF_IDX_COUNT = aif.index('count')

_r = cuda.jit(gmath.r, device=True)


@cuda.jit(device=True)
def _r2(gn0, gn1):
    return _r(gn0, gn1) ** 2

@cuda.jit
def _ld_kernel(x, ais, cts, scores, offset, out_idx, out_res, out_cmp):
    n = x.shape[0]
    ti = cuda.grid(1)

    if ti >= ais.shape[0]:
        return
        
    # Get offset into output array from which all results here are written
    # (this is the sum of count of comparisons for all intervals up to this point)
    oi = 0 if ti == 0 else cts[ti - 1]

    # Alternatively, this could come from ti + chunk_interval['min_index'] but
    # reference explicitly instead for greater readability
    index = ais[ti, AIF_IDX_INDEX]
    start = ais[ti, AIF_IDX_START]
    stop = ais[ti, AIF_IDX_STOP]

    for i, other in enumerate(range(start, stop)):
        # All indexes are specified for global array so they must be offset by the
        # absolute index of the first row provided in x (i.e. `min_start` for the chunk)
        i1, i2 = index - offset, other - offset
        assert 0 <= i1 < n
        assert 0 <= i2 < n
        if i1 == i2:
            res = 1.0
        else:
            res = _r2(x[i1], x[i2])

        idx = oi + i    
        out_idx[idx, 0] = index
        out_idx[idx, 1] = other
        out_res[idx] = res
        if out_cmp.shape[0] > 0:
            if scores[i1] > scores[i2]:
                out_cmp[idx] = 1
            elif scores[i1] < scores[i2]:
                out_cmp[idx] = -1
            else:
                out_cmp[idx] = 0


def _get_result(idx, res):
    pass

def _run_kernel(x, ais, ci, scores, index_dtype, value_dtype):
    ci = ChunkInterval(*ci)
    n_tasks = len(ais)
    n_pairs = ci.count
    offset = ci.min_start
    cts = np.cumsum(ais[:, AIF_IDX_COUNT])

    x = cuda.to_device(x)
    cts = cuda.to_device(cts)
    ais = cuda.to_device(ais)
    if scores is not None:
        scores = cuda.to_device(scores)
        out_cmp = cuda.to_device(np.empty(n_pairs, dtype=np.int8))
    else:
        scores = cuda.to_device(np.empty(0))
        out_cmp = cuda.to_device(np.empty(0))

    out_idx = cuda.to_device(np.empty((n_pairs, 2), dtype=index_dtype))
    # TODO: remove this default value when ready
    out_res = cuda.to_device(np.full(n_pairs, -99, dtype=value_dtype))

    _ld_kernel.forall(n_tasks)(x, ais, cts, scores, offset, out_idx, out_res, out_cmp)

    return out_idx.copy_to_host(), out_res.copy_to_host(), out_cmp.copy_to_host()

def ld_matrix(x, axis_intervals, chunk_interval, scores=None, index_dtype=np.int32, value_dtype=np.float32, min_threshold=None, return_value=True, lock=False):
    # `axis_intervals` have ranges for rows i to j
    # `x` has overlap and should contain more rows than axis intervals
    # `chunk_interval` should have single row
    from ..method.interval import chunk_interval_fields
    assert chunk_interval.shape[0] == 1
    assert chunk_interval.ndim == 2
    assert x.shape[0] >= axis_intervals.shape[0]
    if scores is not None:
        assert x.shape[0] == scores.shape[0]

    ctx = Lock('gpu') if lock else contextlib.suppress()
    with ctx:
        idx, res, cmp = _run_kernel(x, axis_intervals, chunk_interval[0], scores, index_dtype, value_dtype)
        # TODO: remove this
        assert np.all(np.nan_to_num(res) > -99)

    if min_threshold is not None:
        mask = res >= min_threshold
        idx, res = idx[mask], res[mask]
        if scores is not None:
            cmp = cmp[mask]

    data_vars = dict(i=idx[:, 0], j=idx[:, 1])
    if return_value:
        data_vars['value'] = res
    if scores is not None:
        data_vars['cmp'] = cmp
    data_vars = {k: ('pair', v) for k, v in data_vars.items()}
    df = xr.Dataset(data_vars).to_dataframe()
    return df
    