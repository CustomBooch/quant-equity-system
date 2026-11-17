"""Operations: the jobs that run the pipeline and watch it. The top production layer: it may import every layer below and nothing imports it.

    month          the monthly cycle for decision month M: what is done, what is missing, what runs next (the wiki page Home)
    forward        forward tests: the record ledger, its verification, and the reconciliation of the book against its shadow
    borrow_watch   alert on held shorts that are no longer easy to borrow

Nothing here sends an order: a step that would is left to the owner and to quant.execution.engine (the wiki page Home).
"""
