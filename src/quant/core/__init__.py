"""Layer 0, core: what every other layer may use. Imports nothing above it.

    paths, config, env              repository paths, dataset locations and credentials, keys read from the environment
    stats                           the statistics reference: Newey-West, block bootstraps, Holm, ranks, the deflated Sharpe ratio
    kernels                         in-process kernels: Rust when built from the current sources, the Python reference otherwise (bit-identical)
    release                         the release contract (write, verify, compare) and the pointer to the current master release
    registry                        the experiment registry: every research test, its verdict and its trial count
    chain, sql, xlsx, xlsx_write    the hash-chained JSON-lines helper, DuckDB helpers, the .xlsx reader and writer
    weights, frozen, licences       capped portfolio weights, the frozen-artefact check, what each data source may be used for
"""
