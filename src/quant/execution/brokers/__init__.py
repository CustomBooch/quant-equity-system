"""Broker adapters: one subpackage per broker, each holding its host, its keys and its requests.

    alpaca    the paper account (PAPER amendment A10, the only broker since A14): read-only API and the asset list

Nothing here sends an order. The one writing request of the repository is in quant.execution.engine, behind its gates
(the wiki page Home; tests/contracts/test_broker_boundary.py).
"""
