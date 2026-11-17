# Independent R implementations of the lab.stats statistics, written from the sandwich and boot documentation (not ported from the Python code).
# Convention to compare: Bartlett kernel, `lags` = maximum lag, no prewhitening, no small-sample correction, NA dropped, fewer than min_obs -> NA.

nw_t <- function(x, lags = 6, min_obs = 12) {
  x <- x[!is.na(x)]
  if (length(x) < min_obs) return(NA_real_)
  fit <- stats::lm(x ~ 1)
  v <- sandwich::NeweyWest(fit, lag = lags, prewhite = FALSE, adjust = FALSE)
  unname(stats::coef(fit) / sqrt(v[1, 1]))
}

# 95% percentile interval of a statistic under a block bootstrap. kind: "stationary" (geometric blocks, mean `block`) or "moving" (fixed blocks).
block_ci <- function(x, stat = mean, kind = c("stationary", "moving"), reps = 10000, block = 12, seed = 1) {
  kind <- match.arg(kind)
  x <- x[!is.na(x)]
  set.seed(seed)
  b <- boot::tsboot(x, function(z) stat(z), R = reps, l = block, sim = if (kind == "stationary") "geom" else "fixed")
  unname(stats::quantile(b$t, c(0.025, 0.975)))
}
