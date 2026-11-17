source(file.path("..", "..", "R", "release.R"))
example <- normalizePath(file.path("..", "..", "..", "contracts", "examples", "release_example_v1"))

scratch <- function() {
  d <- tempfile("rel_")
  dir.create(d)
  file.copy(list.files(example, full.names = TRUE), d, recursive = TRUE)
  d
}

test_that("a Python-written release verifies", {
  doc <- verify_release(example)
  expect_equal(doc$release, "release_example_v1")
})

test_that("every kind of tampering is refused", {
  d <- scratch(); b <- readBin(file.path(d, "series.txt"), "raw", 1000); b[length(b)] <- as.raw(bitwXor(as.integer(b[length(b)]), 1L)); writeBin(b, file.path(d, "series.txt"))
  expect_error(verify_release(d), "hash differs")
  d <- scratch(); writeLines("short", file.path(d, "series.txt"))
  expect_error(verify_release(d), "size differs")
  d <- scratch(); unlink(file.path(d, "data", "note.txt"))
  expect_error(verify_release(d), "missing")
  d <- scratch(); writeLines("x", file.path(d, "sneaked.csv"))
  expect_error(verify_release(d), "not listed")
  d <- scratch(); writeLines(sub("release/1", "release/2", readLines(file.path(d, "VERSION.json"))), file.path(d, "VERSION.json"))
  expect_error(verify_release(d), "unsupported schema")
  expect_error(verify_release(tempfile()), "cannot read")
})

test_that("validation is required only when asked, and every gate must pass", {
  ex <- function(n) normalizePath(file.path("..", "..", "..", "contracts", "examples", n))
  expect_equal(require_validation(verify_release(ex("release_validated_v1")))$release, "release_validated_v1")
  expect_error(require_validation(verify_release(ex("release_failed_gate_v1"))), "gates not passed")
  expect_error(require_validation(verify_release(example)), "no validation block")
})
