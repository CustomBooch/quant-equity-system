source(file.path("..", "..", "R", "stats.R"))
source(file.path("..", "..", "R", "metrics.R"))
source(file.path("..", "..", "R", "paper_rules.R"))

test_that("book statistics follow the stated conventions", {
  net <- c(0.10, -0.20, 0.05, 0.02)
  rf <- rep(0.01, 4)
  expect_equal(ann_mean(net), 12 * mean(net))
  expect_equal(sharpe(net, rf), sqrt(12) * mean(net - rf) / sd(net - rf))
  expect_equal(max_drawdown(net), 1.1 * 0.8 / 1.1 - 1)                    # the trough after the -20% month, relative to the 1.10 peak
  expect_equal(max_drawdown(c(0.01, 0.02)), 0)
})

test_that("paired differences and windows drop missing values", {
  a <- c(0.01, 0.02, NA, 0.04)
  b <- c(0.02, 0.02, 0.01, 0.05)
  p <- paired(a, b)
  expect_equal(p$n, 3)
  expect_equal(p$diff_ann, 12 * mean(c(0.01, 0, 0.01)))
  df <- data.frame(month = as.Date(c("2025-12-01", "2026-01-01", "2026-02-01")), x = 1:3)
  expect_equal(in_window(df, "2026-01-01", "2026-12-01")$x, 2:3)
})

test_that("top overlap counts shared names above the quantile", {
  expect_equal(top_overlap(1:100, 1:100, 0.9), 1)
  expect_equal(top_overlap(1:100, 100:1, 0.9), 0)
})

orders_ok <- function(n_slots = 12, slip = 0.0003) {
  slots <- seq(as.Date("2026-11-03"), by = "week", length.out = n_slots)
  data.frame(slot = rep(slots, each = 5), ticker = "A", status = "filled", fill_price = 100 * (1 + slip), prev_close = 100, has_plan = TRUE, has_token = TRUE)
}
recon_ok <- data.frame(slot = as.Date("2026-11-03"), ticker = "A", difference = 0, business_days_open = 0)
track_ok <- data.frame(month = as.Date(c("2026-11-01", "2026-12-01", "2027-01-01")), account_ret = c(0.01, 0.02, -0.01), model_ret = c(0.012, 0.03, 0.0))
rails_ok <- data.frame(test = c("stale_score", "unmappable_name"), refused = TRUE)

test_that("paper rules pass on a clean record and need enough history", {
  expect_equal(paper_rules(orders_ok(), recon_ok, track_ok, rails_ok)$verdict, "pass")
  expect_equal(paper_rules(orders_ok(11), recon_ok, track_ok, rails_ok)$verdict, "insufficient")
  expect_equal(paper_rules(orders_ok(), recon_ok, track_ok[1:2, ], rails_ok)$verdict, "insufficient")
})

test_that("each paper rule fails on its own breach", {
  o <- orders_ok()
  o$has_token[1] <- FALSE
  r <- paper_rules(o, recon_ok, track_ok, rails_ok)
  expect_equal(r$verdict, "fail"); expect_false(r$rules$operations$pass)
  expect_false(paper_rules(orders_ok(slip = 0.0009), recon_ok, track_ok, rails_ok)$rules$execution$pass)                    # 9 bp > 5 bp median
  t2 <- track_ok; t2$model_ret <- t2$account_ret + c(0.02, 0.02, 0)
  expect_false(paper_rules(orders_ok(), recon_ok, t2, rails_ok)$rules$tracking$pass)                                         # only 1 of 3 months within 1.5 pp
  expect_false(paper_rules(orders_ok(), recon_ok, track_ok, rails_ok[1, ])$rules$rails$pass)
  late <- data.frame(slot = as.Date("2026-11-03"), ticker = "A", difference = 1, business_days_open = 3)
  expect_false(paper_rules(orders_ok(), late, track_ok, rails_ok)$rules$operations$pass)
})
