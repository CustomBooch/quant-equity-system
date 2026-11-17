"""quant: a point-in-time equity research and paper-trading system, one package in layers (the wiki page Architecture).

    core        L0  paths, statistics, Rust kernels, the release contract and pointer, the experiment registry, licences, file and DuckDB helpers
    data        L1  data coming in: data-source plugins (quant.data.sources), SEC EDGAR, Yahoo, Capital IQ exports, broker instrument lists
    features    L2  features on the data: monthly point-in-time joins of any source, price features from daily bars, the bridge-month fill
    models      L3  scoring: the model definition (quant.models.definition) applied to a verified release
    portfolio   L4  the book: parameters, targets, the tranche schedule, the order plan
    execution   L5  the order path: preflight, the engine (the one place an order is sent), the audit log, the tranche ledger; broker adapters
    ops         L6  monitors (the borrow watch)

A module of layer N imports only layers 0..N (tools/check_layout.py), and the production commands load only production code (tools/check_imports.py).
"""
