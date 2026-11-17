# Pass rules of the paper-trading specification (lab/paper/SPEC.md, "Pass rules"; thresholds fixed there on 2026-10-19). R owns this evaluation (forward-test analysis).
# Input tables, one row each (exported from the paper engine's audit log and reconciliation):
#   orders:   slot (Date), ticker, status ("filled" | "rejected"), fill_price, prev_close, has_plan (logical), has_token (logical)
#   recon:    slot (Date), ticker, difference (shares, after the batch), business_days_open (integer)
#   tracking: month (Date), account_ret, model_ret (monthly returns as fractions)
#   rails:    test ("stale_score" | "unmappable_name" | ...), refused (logical)
# Evaluated only after at least 3 months and 12 slots; before that the verdict is "insufficient". Any failure means no live trading (SPEC: re-register, never relax).

PAPER_RULES <- list(min_months = 3, min_slots = 12, max_reject_rate = 0.02, recon_days = 2, slip_median_bp = 5, slip_mean_bp = 8, track_pp = 1.5, track_months_needed = 2)

slippage_bp <- function(orders) {
  f <- orders[orders$status == "filled" & !is.na(orders$fill_price) & !is.na(orders$prev_close) & orders$prev_close > 0, ]
  abs(f$fill_price / f$prev_close - 1) * 1e4
}

paper_rules <- function(orders, recon, tracking, rails, rules = PAPER_RULES) {
  slots <- length(unique(orders$slot))
  months <- nrow(tracking)
  enough <- months >= rules$min_months && slots >= rules$min_slots
  open_late <- sum(recon$difference != 0 & recon$business_days_open > rules$recon_days)
  reject_rate <- if (nrow(orders)) mean(orders$status == "rejected") else NA_real_
  unauthorised <- sum(!(orders$has_plan & orders$has_token))
  r1 <- list(pass = open_late == 0 && !is.na(reject_rate) && reject_rate < rules$max_reject_rate && unauthorised == 0,
             unreconciled_after_two_days = open_late, rejection_rate = reject_rate, orders_without_plan_and_token = unauthorised)
  s <- slippage_bp(orders)
  r2 <- list(pass = length(s) > 0 && stats::median(s) <= rules$slip_median_bp && mean(s) <= rules$slip_mean_bp,
             median_bp = if (length(s)) stats::median(s) else NA_real_, mean_bp = if (length(s)) mean(s) else NA_real_, fills = length(s))
  last3 <- utils::tail(tracking[order(tracking$month), ], 3)
  gap <- abs(last3$account_ret - last3$model_ret) * 100
  r3 <- list(pass = nrow(last3) == 3 && sum(gap <= rules$track_pp) >= rules$track_months_needed, gap_pp_last3 = gap)
  need <- c("stale_score", "unmappable_name")
  r4 <- list(pass = all(vapply(need, function(t) any(rails$test == t & rails$refused), logical(1))), tests = need)
  rules_out <- list(operations = r1, execution = r2, tracking = r3, rails = r4)
  verdict <- if (!enough) "insufficient" else if (all(vapply(rules_out, function(r) isTRUE(r$pass), logical(1)))) "pass" else "fail"
  list(verdict = verdict, months = months, slots = slots, rules = rules_out)
}
