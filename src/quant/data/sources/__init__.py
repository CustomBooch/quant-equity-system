"""Data sources as plugins: declare a dataset once, and the pull, the checks, the store and the point-in-time monthly join come with it.

    base        Dataset (the declaration), DataSource (fetch), conform and validate (the point-in-time checks every pull passes)
    registry    @register, discover (imports every module in plugins/), get
    store       pull (fetch -> check -> record/data/derived/sources/<name>/<date>/ with a manifest), load (newest pull, optionally as of a date)
    files       FileSource: a dataset read from a file exported by hand (CSV, Parquet, Excel)
    plugins     one module per source (none registered yet: the wiki page Adding-a-dataset)
    cli         `tools/py -m quant data list | show | pull | check`

The monthly feature join is quant.features.sources (layer 2). Adding a source: the wiki page Adding-a-dataset. A source feeds research until a registered
specification adopts it; nothing here touches the live model.
"""
