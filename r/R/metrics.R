# Evaluation statistics for signals and books. R owns these for new research (docs/LANGUAGE_ROLES.md); the Python reports used the same conventions, so a result can be
# checked against them: monthly series; means annualised by 12; Sharpe = sqrt(12) * mean / sample sd of the return in excess of the risk-free rate; drawdown on the
# compounded net return; Newey-West t with Bartlett weights and 6 lags (R/stats.R). Missing values are dropped pairwise.

# Mean, Newey-West t and number of observations of a monthly series.
summ <- function(x, lags = 6) {
  x <- x[!is.na(x)]
  list(mean = mean(x), nw_t = nw_t(x, lags), n = length(x))
}

ann_mean <- function(x) 12 * mean(x, na.rm = TRUE)

sharpe <- function(net, rf) {
  ex <- net - rf
  ex <- ex[!is.na(ex)]
  sqrt(12) * mean(ex) / stats::sd(ex)
}

max_drawdown <- function(net) {
  w <- cumprod(1 + net[!is.na(net)])
  min(w / cummax(w) - 1)
}

# Paired monthly difference b - a: annualised mean and Newey-West t.
paired <- function(a, b, lags = 6) {
  d <- b - a
  d <- d[!is.na(d)]
  list(diff_ann = 12 * mean(d), nw_t = nw_t(d, lags), n = length(d))
}

# Rows of a monthly table with month in [lo, hi] (dates as "YYYY-MM-DD").
in_window <- function(df, lo, hi) df[df$month >= as.Date(lo) & df$month <= as.Date(hi), , drop = FALSE]

# Share of the names above quantile q of score a that are also above quantile q of score b (same universe).
top_overlap <- function(a, b, q) {
  ta <- which(rank(a) / length(a) > q)
  tb <- which(rank(b) / length(b) > q)
  length(intersect(ta, tb)) / max(length(ta), 1)
}

# Book summary over a window: annualised net return, Sharpe over the risk-free rate, maximum drawdown, months.
book_summary <- function(net, rf) {
  list(net_ann = ann_mean(net), sharpe = sharpe(net, rf), max_dd = max_drawdown(net), months = sum(!is.na(net)))
}

# Relative or absolute agreement of two numbers (for comparisons with stored Python results).
agrees <- function(r, py, tol = 1e-9) isTRUE(abs(r - py) <= tol * max(1, abs(py)))
