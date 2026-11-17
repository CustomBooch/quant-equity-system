"""quant.execution.brokers.t212.schema: strict JSON decoding and the field reader that turns documented objects into typed records. Pure functions: no boundary is replaced."""
from __future__ import annotations

import datetime as dt

import pytest

from quant.execution.brokers.t212 import schema as S


class TestLoads:
    def test_a_json_body_is_decoded(self):
        assert S.loads(b'{"a": [1, 2.5, "x", null, true]}') == {"a": [1, 2.5, "x", None, True]}

    @pytest.mark.parametrize("body,message", [
        (b"\xff\xfe{}", "body is not UTF-8"),
        (b"{'a': 1}", "body is not JSON"),
        (b'{"a": NaN}', "NaN is not a JSON number"),
        (b'{"a": -Infinity}', "-Infinity is not a JSON number"),
    ])
    def test_a_body_that_is_not_strict_json_is_refused_by_name(self, body, message):
        with pytest.raises(S.NotJson, match=message):
            S.loads(body)

    def test_an_object_that_repeats_a_key_is_refused_instead_of_keeping_the_last_value(self):
        with pytest.raises(S.DuplicateKey, match="repeats the key 'quantity'"):
            S.loads(b'[{"quantity": 1, "quantity": 2}]')


class TestScalars:
    @pytest.mark.parametrize("convert,value,error,message", [
        (S.as_text, 5, S.WrongType, "expected string, got number"),
        (S.as_text, "", S.BadValue, "empty string"),
        (S.as_number, "1.5", S.WrongType, "expected number, got string"),
        (S.as_number, True, S.WrongType, "expected number, got boolean"),
        (S.as_number, 10 ** 400, S.BadValue, "number out of range"),
        (S.as_integer, 3.0, S.WrongType, "got number 3.0"),
        (S.as_integer, False, S.WrongType, "expected integer, got boolean"),
        (S.as_integer, None, S.WrongType, "expected integer, got null"),
        (S.as_flag, "true", S.WrongType, "expected boolean, got string"),
        (S.as_timestamp, "yesterday", S.BadValue, "not an ISO 8601 date-time"),
        (S.as_timestamp, "2026-10-27T14:30:00", S.BadValue, "without a UTC offset"),
        (S.as_currency, "usd", S.BadValue, "not a three-letter currency code"),
    ])
    def test_a_value_of_the_wrong_type_or_domain_is_refused_with_its_path(self, convert, value, error, message):
        with pytest.raises(error, match=message) as e:
            convert(value, "$.x")
        assert e.value.path == "$.x" and str(e.value).startswith("$.x: ")

    def test_numbers_integers_and_flags_keep_their_value(self):
        assert S.as_number(7, "$") == 7.0 and isinstance(S.as_number(7, "$"), float) and S.as_integer(12, "$") == 12 and S.as_flag(False, "$") is False

    def test_a_number_that_is_not_finite_is_refused(self):
        with pytest.raises(S.BadValue, match="not finite"):
            S.as_number(float("inf"), "$.q")

    def test_a_timestamp_becomes_an_aware_utc_datetime_whatever_its_offset(self):
        t = S.as_timestamp("2026-10-27T16:30:00.123456789+02:00", "$")
        assert t == dt.datetime(2026, 10, 27, 14, 30, 0, 123456, tzinfo=dt.UTC) and t.tzinfo is dt.UTC

    def test_a_value_outside_an_enumeration_is_refused_and_long_lists_are_cut_short(self):
        with pytest.raises(S.BadValue, match=r"'DONE' is not a documented value \(FILLED, NEW\)"):
            S.as_choice("DONE", "$.status", {"NEW", "FILLED"})
        with pytest.raises(S.BadValue, match=r"\.\.\. \(20 in all\)"):
            S.as_choice("Z", "$.t", {f"V{i:02d}" for i in range(20)})


def money(f: S.Fields) -> tuple[float, str]:
    return f.number("amount"), f.currency("currency")


class TestFields:
    def test_a_required_field_that_is_absent_or_null_is_refused_and_the_message_says_which(self):
        with pytest.raises(S.MissingField, match=r"\$\.amount: required field is absent"):
            S.parse_object({"currency": "USD"}, money)
        with pytest.raises(S.MissingField, match=r"\$\.amount: required field is null"):
            S.parse_object({"amount": None, "currency": "USD"}, money)

    def test_every_undocumented_field_is_named_in_one_refusal(self):
        with pytest.raises(S.UnexpectedField, match=r"\$: undocumented field\(s\) \['bonus', 'note'\]"):
            S.parse_object({"amount": 1, "currency": "USD", "note": "x", "bonus": 2}, money)

    def test_optional_fields_may_be_absent_or_null_but_not_of_another_type(self):
        def read(f: S.Fields) -> tuple[float | None, str | None]:
            return f.number_or_none("fx"), f.text_or_none("link")

        assert S.parse_object({}, read) == (None, None) and S.parse_object({"fx": None, "link": None}, read) == (None, None)
        assert S.parse_object({"fx": 1.25, "link": "x"}, read) == (1.25, "x")
        with pytest.raises(S.WrongType, match=r"\$\.fx: expected number, got string"):
            S.parse_object({"fx": "1.25"}, read)

    def test_an_optional_enumeration_may_be_absent_but_not_undocumented(self):
        def validity(f: S.Fields) -> str | None:
            return f.choice_or_none("timeInForce", {"DAY", "GOOD_TILL_CANCEL"})

        assert S.parse_object({}, validity) is None and S.parse_object({"timeInForce": "DAY"}, validity) == "DAY"
        with pytest.raises(S.BadValue, match=r"\$\.timeInForce: 'WEEK' is not a documented value"):
            S.parse_object({"timeInForce": "WEEK"}, validity)

    def test_a_key_documented_as_always_present_is_required_even_when_null_is_allowed(self):
        def page(f: S.Fields) -> str | None:
            f.require_key("nextPagePath")
            return f.text_or_none("nextPagePath")

        assert S.parse_object({"nextPagePath": None}, page) is None
        with pytest.raises(S.MissingField, match=r"\$\.nextPagePath"):
            S.parse_object({}, page)

    def test_nested_records_and_arrays_carry_their_path_into_errors(self):
        def wallet(f: S.Fields) -> tuple[tuple[float, str], tuple[tuple[float, str], ...]]:
            return f.record("cash", money), f.items("taxes", money)

        assert S.parse_object({"cash": {"amount": 1, "currency": "GBP"}, "taxes": []}, wallet) == ((1.0, "GBP"), ())
        with pytest.raises(S.WrongType, match=r"\$\.taxes\[1\]\.amount: expected number"):
            S.parse_object({"cash": {"amount": 1, "currency": "GBP"}, "taxes": [{"amount": 1, "currency": "GBP"}, {"amount": "x", "currency": "GBP"}]}, wallet)
        with pytest.raises(S.UnexpectedField, match=r"\$\.cash: undocumented field\(s\) \['extra'\]"):
            S.parse_object({"cash": {"amount": 1, "currency": "GBP", "extra": 0}, "taxes": []}, wallet)

    def test_an_optional_record_may_be_null(self):
        def maybe(f: S.Fields) -> tuple[float, str] | None:
            return f.record_or_none("fill", money)

        assert S.parse_object({"fill": None}, maybe) is None and S.parse_object({"fill": {"amount": 2, "currency": "USD"}}, maybe) == (2.0, "USD")

    def test_a_value_that_is_not_an_object_or_array_where_one_is_documented_is_refused(self):
        with pytest.raises(S.WrongType, match=r"\$: expected object, got array"):
            S.parse_object([], money)
        with pytest.raises(S.WrongType, match=r"\$: expected array, got object"):
            S.parse_array({}, money)
        with pytest.raises(S.WrongType, match=r"\$\[0\]: expected object, got number"):
            S.parse_array([3], money)

    def test_a_contradiction_between_fields_is_refused_at_the_object(self):
        def consistent(f: S.Fields) -> None:
            if f.text("ticker") != f.text("other"):
                f.fail("ticker and other differ")

        with pytest.raises(S.BadValue, match=r"\$\[0\]: ticker and other differ"):
            S.parse_array([{"ticker": "A", "other": "B"}], consistent)


class TestMoreAccessors:
    def test_dates_integers_and_holdings_are_read_or_refused_with_their_path(self):
        def plan(f: S.Fields) -> tuple[object, ...]:
            return f.date_or_none("slot"), f.integer_or_none("tranche"), f.number_map("held"), f.number_map_or_none("before")

        assert S.parse_object({"slot": "2026-10-27", "tranche": 2, "held": {"AAA_US_EQ": 1.5}}, plan) == (dt.date(2026, 10, 27), 2, {"AAA_US_EQ": 1.5}, None)
        assert S.parse_object({"slot": None, "tranche": None, "held": {}, "before": {"B": 2}}, plan) == (None, None, {}, {"B": 2.0})
        with pytest.raises(S.BadValue, match=r"\$\.slot: not an ISO 8601 date: 'Tuesday'"):
            S.parse_object({"slot": "Tuesday", "held": {}}, plan)
        with pytest.raises(S.WrongType, match=r"\$\.held\.AAA_US_EQ: expected number, got string"):
            S.parse_object({"held": {"AAA_US_EQ": "1"}}, plan)
        with pytest.raises(S.WrongType, match=r"\$\.held: expected object, got array"):
            S.parse_object({"held": []}, plan)

    def test_a_value_taken_as_it_is_must_still_be_present(self):
        def answer(f: S.Fields) -> object:
            return f.value("response")

        assert S.parse_object({"response": {"id": 1}}, answer) == {"id": 1}
        with pytest.raises(S.MissingField, match=r"\$\.response: required field is null"):
            S.parse_object({"response": None}, answer)

    def test_skipped_fields_are_accepted_and_others_still_refused(self):
        def info(f: S.Fields) -> str:
            f.skip("note", "usd_total")
            return f.text("id")

        assert S.parse_object({"id": "x", "note": "anything", "usd_total": 3}, info) == "x" and S.parse_object({"id": "x"}, info) == "x"
        with pytest.raises(S.UnexpectedField, match=r"\['other'\]"):
            S.parse_object({"id": "x", "other": 1}, info)
