from quant.execution import ledger as L


def test_batches_are_recorded_per_tranche_and_summed_across_tranches(tmp_path):
    p = tmp_path / "l.json"
    led = L.record_batch({}, 0, {"A": 2.0, "B": 0.0}, p)
    led = L.record_batch(led, 1, {"A": 1.5}, p)
    assert L.load(p) == led and L.tranche(led, 0) == {"A": 2.0} and L.aggregate(led) == {"A": 3.5}
    assert L.tranche(led, 3) == {}
