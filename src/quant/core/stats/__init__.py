"""Canonical statistics for monthly return series (Newey-West t, block bootstraps).

New code imports from here. Existing definitions in hash-frozen or already-run modules are left untouched; `tests/test_stats.py` proves this module reproduces them
(exactly for the hand-written ones, to numerical tolerance against statsmodels and arch).
"""
from quant.core.stats.bootstrap import moving_block_ci, moving_block_draws, stationary_ci, stationary_draws
from quant.core.stats.multiple import crank, holm_adjusted, holm_reject, holm_rejections_in_order, holm_steps, monthly_rank_ic
from quant.core.stats.hac import hac_mean_ols, nw_mean_t, nw_p_normal, nw_t, nw_t_matrix

__all__ = ["crank", "hac_mean_ols", "holm_adjusted", "holm_reject", "holm_rejections_in_order", "holm_steps", "monthly_rank_ic", "moving_block_ci", "moving_block_draws", "nw_mean_t", "nw_p_normal", "nw_t", "nw_t_matrix", "stationary_ci", "stationary_draws"]
