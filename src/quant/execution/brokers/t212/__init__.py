"""The Trading 212 practice account, read only: the GET client (api) and the typed client of every documented read (client, schema, records,
ratelimit), the hash-chained journal of every response (journal, snapshots), the engine's records read as typed views (engine_records),
reconciliation (reconcile), fill and cost analysis (fills), reports (reports), the read and health commands (pull, pull_instruments, health).
Specification: lab/t212/INTEGRATION_SPEC.md. The instrument mapping is quant.data.instruments.t212; the one order path is quant.execution.engine."""
