"""Layer 5, execution: the order path and its records. Imports every layer below.

    preflight    go / no-go checks before a batch
    engine       the one order path: gates, send, reconcile
    audit        the append-only, hash-chained audit log
    ledger       what each tranche holds, written only after a reconciliation with zero differences
    brokers      one adapter per broker: alpaca (the paper account in use), t212 (the Trading 212 practice account, read only)

quant.execution.engine is the only code in the repository that writes to a broker, behind its gates (the wiki page Home;
tests/contracts/test_broker_boundary.py). A new broker is a new subpackage of brokers.
"""
