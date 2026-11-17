"""Broker adapters: one subpackage per broker, each holding its host, its keys and its requests.

    alpaca    the paper account in use (PAPER amendment A10): read-only API and the asset list
    t212      the Trading 212 practice account: read-only API, journal, reconciliation and fill reports

Nothing here sends an order. The one writing request of the repository is in quant.execution.engine, behind its gates
(the wiki page Home; tests/contracts/test_broker_boundary.py).
"""
