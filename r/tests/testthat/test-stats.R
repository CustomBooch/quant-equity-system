source(file.path("..", "..", "R", "stats.R"))
cases <- jsonlite::read_json(file.path("..", "..", "inputs", "frozen_check_v1", "synthetic_nw_cases.json"), simplifyVector = TRUE)

test_that("nw_t matches the Python lab.stats values on synthetic series", {
  for (i in seq_len(nrow(cases))) {
    for (l in c(1, 3, 6)) {
      expect_equal(nw_t(cases$x[[i]], l), cases$nw_t[[as.character(l)]][i], tolerance = 1e-9)
    }
  }
})

test_that("short samples give NA", {
  expect_true(is.na(nw_t(rnorm(11))))
})

test_that("block_ci brackets the mean and widens with more serial dependence", {
  set.seed(3)
  x <- as.numeric(arima.sim(list(ar = 0.5), 200))
  ci <- block_ci(x, reps = 2000, seed = 3)
  expect_lt(ci[1], mean(x)); expect_gt(ci[2], mean(x))
})
