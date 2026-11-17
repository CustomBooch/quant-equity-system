# Release contract v1 (contracts/RELEASE_V1.md): verify a release directory before reading any file in it.
# Same rules as lab/release (Python) and rust_core verify-release: schema, listed files (size + SHA-256), nothing unlisted.

verify_release <- function(dir) {
  manifest <- file.path(dir, "VERSION.json")
  if (!file.exists(manifest)) stop("cannot read VERSION.json in ", dir)
  doc <- jsonlite::read_json(manifest, simplifyVector = FALSE)
  if (!identical(doc$schema, "release/1")) stop("unsupported schema ", deparse(doc$schema), "; expected release/1")
  files <- doc$files
  if (length(files) == 0) stop("manifest lists no files")
  problems <- character()
  for (rel in names(files)) {
    p <- file.path(dir, rel)
    if (!file.exists(p) || dir.exists(p)) {
      problems <- c(problems, paste0("missing: ", rel))
    } else if (file.size(p) != files[[rel]]$bytes) {
      problems <- c(problems, paste0("size differs: ", rel))
    } else if (!identical(digest::digest(p, algo = "sha256", file = TRUE, serialize = FALSE), files[[rel]]$sha256)) {
      problems <- c(problems, paste0("hash differs: ", rel))
    }
  }
  present <- list.files(dir, recursive = TRUE, all.files = TRUE, no.. = TRUE)
  present <- present[basename(present) != "VERSION.json"]
  extra <- setdiff(present, names(files))
  if (length(extra) > 0) problems <- c(problems, paste0("not listed in manifest: ", extra))
  if (length(problems) > 0) stop(paste(problems, collapse = "; "))
  doc
}

# For data-layer releases: a validation block whose report hash is that of a listed file, with every gate exactly "pass".
require_validation <- function(doc) {
  v <- doc$validation
  if (is.null(v)) stop("release has no validation block")
  if (length(v$gates) == 0) stop("validation lists no gates")
  failed <- sort(names(v$gates)[vapply(v$gates, function(g) !identical(g, "pass"), logical(1))])
  if (length(failed) > 0) stop("validation gates not passed: ", paste(failed, collapse = ", "))
  listed <- vapply(doc$files, function(m) m$sha256, character(1))
  if (is.null(v$report_sha256) || !(v$report_sha256 %in% listed)) stop("validation report hash is not that of a listed file")
  doc
}
