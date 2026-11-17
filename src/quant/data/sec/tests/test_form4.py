"""quant.data.sec.form4: a filing's XML gives the data set's fields, the comparison measures agreement both ways, the listing keeps original Form 4s of linked CIKs
once, and reading is resumable with failures recorded. EDGAR is replaced by a fake; nothing else is."""
import pandas as pd
import pytest

from quant.data.sec import form4 as F4

DOC = """<SEC-DOCUMENT>
<TYPE>4
<XML>
<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <issuer><issuerCik>0000320193</issuerCik><issuerName>Apple</issuerName></issuer>
  <reportingOwner><reportingOwnerId><rptOwnerCik>0001214156</rptOwnerCik></reportingOwnerId></reportingOwner>
  <reportingOwner><reportingOwnerId><rptOwnerCik>0001051401</rptOwnerCik></reportingOwnerId></reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeHolding><transactionAmounts><transactionShares><value>9</value></transactionShares></transactionAmounts></nonDerivativeHolding>
    <nonDerivativeTransaction>
      <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>1000.5</value></transactionShares><transactionPricePerShare><value>230.12</value></transactionPricePerShare></transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>50</value></transactionShares><transactionPricePerShare><footnoteId id="F1"/></transactionPricePerShare></transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
  <derivativeTable><derivativeTransaction><transactionCoding><transactionCode>P</transactionCode></transactionCoding></derivativeTransaction></derivativeTable>
</ownershipDocument>
</XML>
</SEC-DOCUMENT>"""


def test_a_filing_gives_its_type_issuer_owners_and_non_derivative_transactions_only():
    f = F4.parse(DOC)
    assert f.doc_type == "4" and f.issuer_cik == 320193 and f.owners == [1051401, 1214156]
    assert f.tx == [("S", 1000.5, 230.12), ("M", 50.0, None)]               # the holding and the derivative transaction are not transactions the engine reads


def test_a_text_without_an_ownership_document_gives_nothing():
    assert F4.parse("<SEC-DOCUMENT>no form here</SEC-DOCUMENT>") is None


def test_numbers_take_the_data_set_s_two_decimals_rounded_half_up():
    assert F4.cents(532.0826) == 532.08 and F4.cents(305.985) == 305.99 and F4.cents(15661.707) == 15661.71 and F4.cents(81.0) == 81.0
    assert F4.cents(None) is None and F4.cents(float("nan")) is None


def test_events_match_after_rounding_and_the_comparison_counts_both_directions():
    ds = [F4.key("a", "P", 100.0, 10.00001), F4.key("a", "S", 5.0, None), F4.key("b", "P", 1.0, 1.0)]
    rd = [F4.key("a", "P", 100.0, 10.0), F4.key("a", "S", 5.0, float("nan")), F4.key("a", "S", 7.0, 3.0), F4.key("c", "P", 1.0, 1.0)]
    meta_ds = {"a": (1, {9}), "b": (2, {8})}
    meta_rd = {"a": (1, {9}), "c": (3, {7})}
    r = F4.compare(ds, rd, meta_ds, meta_rd)
    assert r["accessions_compared"] == 1 and r["dataset_events"] == 2 and r["reader_events"] == 3
    assert r["dataset_found_by_reader"] == 1.0 and r["reader_found_in_dataset"] == pytest.approx(0.6667) and r["issuer_and_owners_agree"] == 1.0


IDX = """Description: Master Index
Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
4           APPLE INC                                                     320193      2026-10-02  edgar/data/320193/0001-26-000001.txt
4           COOK TIMOTHY D                                                1214156     2026-10-02  edgar/data/1214156/0001-26-000001.txt
4/A         APPLE INC                                                     320193      2026-10-03  edgar/data/320193/0001-26-000002.txt
4           APPLE INC                                                     320193      2026-10-31  edgar/data/320193/0001-26-000003.txt
4           SOMEONE ELSE INC                                              999         2026-10-05  edgar/data/999/0001-26-000004.txt
"""


def test_the_listing_keeps_original_form4s_of_linked_ciks_in_the_window_once():
    d = F4.listing(IDX, {320193, 1214156})
    assert d["accession"].tolist() == ["0001-26-000001"] and d["cik"].tolist() == [320193]


def test_reading_is_resumable_and_a_failed_or_empty_filing_is_recorded_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(F4, "OUT", tmp_path)
    calls = []

    def getter(url):
        calls.append(url)
        if "bad" in url:
            raise OSError("HTTP 503")
        return b"nothing" if "empty" in url else DOC.encode()
    todo = [("acc-1", 320193), ("bad-2", 1), ("empty-3", 2)]
    tx, ow, st = F4.read_batches(todo, "t", getter)
    assert len(calls) == 3 and st.set_index("accession")["ok"].to_dict() == {"acc-1": True, "bad-2": False, "empty-3": False}
    assert tx["code"].tolist() == ["S", "M"] and sorted(ow["owner_cik"]) == [1051401, 1214156]
    tx2, _, st2 = F4.read_batches(todo + [("acc-4", 320193)], "t", getter)
    assert len(calls) == 4 and len(st2) == 4 and len(tx2) == 4                # only the new accession is fetched; earlier parts are kept


def test_the_check_compares_the_sample_with_the_data_set_and_records_the_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(F4, "OUT", tmp_path)
    sub = pd.DataFrame({"ACCESSION_NUMBER": ["acc-1", "acc-x"], "FILING_DATE": ["15-SEP-2026", "15-AUG-2026"], "DOCUMENT_TYPE": ["4", "4"], "ISSUERCIK": ["320193", "1"]})
    ntx = pd.DataFrame({"ACCESSION_NUMBER": ["acc-1", "acc-1"], "TRANS_CODE": ["S", "M"], "TRANS_SHARES": ["1000.5", "50"], "TRANS_PRICEPERSHARE": ["230.12", None]})
    own = pd.DataFrame({"ACCESSION_NUMBER": ["acc-1", "acc-1"], "RPTOWNERCIK": ["1214156", "1051401"]})
    monkeypatch.setattr(F4, "dataset_tables", lambda path=None: (sub, ntx, own))
    r = F4.check(getter=lambda url: DOC.encode())
    assert r["sample"] == 1 and r["dataset_events"] == 1 and r["dataset_found_by_reader"] == 1.0 and r["issuer_and_owners_agree"] == 1.0 and r["pass"]
    assert (tmp_path / "form4_check.json").exists()
