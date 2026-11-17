"""Capped portfolio weights: the rule every live book uses (the long book's 5% cap, the short basket's 5% cap, the shadow arms).

The algorithm is the one the research recorded results were computed with (`lab.core.econ.capped_weights`, itself a byte-identical copy of the frozen
`legacy_research/signal_batch_03/econ.py`); tools/tests/test_duplicate_parity.py checks that the three give identical arrays. The production path imports it from
here, so it does not depend on the research record (tools/check_imports.py).
"""
from __future__ import annotations

import numpy as np

CAP = 0.10


def capped_weights(me: np.ndarray, cap: float = CAP) -> np.ndarray:
    """Weights proportional to `me`, none above `cap`: the excess of capped names is handed to the uncapped ones in proportion to their weight, repeatedly (at
    most 100 rounds). When fewer than 1/cap names exist no feasible answer exists and every name ends at or near the cap; callers pass max(cap, 1/n)."""
    w = me / me.sum()
    for _ in range(100):
        over = w > cap + 1e-12
        if not over.any():
            break
        excess = (w[over] - cap).sum()
        w[over] = cap
        free = ~over & (w < cap)
        if not free.any():
            break
        w[free] += excess * w[free] / w[free].sum()
    out: np.ndarray = w
    return out
