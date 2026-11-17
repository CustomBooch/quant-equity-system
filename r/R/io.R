# Reading releases and writing results. Every table is read through a verified release (R/release.R); results go to outputs/ as JSON.

# A Parquet table from a release directory that `verify_release` has already accepted. Date-time columns named `month` become Date.
read_release_table <- function(dir, file) {
  d <- as.data.frame(arrow::read_parquet(file.path(dir, file)))
  if ("month" %in% names(d)) d$month <- as.Date(d$month)
  d
}

write_result <- function(obj, path) {
  dir.create(dirname(path), showWarnings = FALSE, recursive = TRUE)
  jsonlite::write_json(obj, path, auto_unbox = TRUE, digits = 12, pretty = TRUE, na = "null")
}
