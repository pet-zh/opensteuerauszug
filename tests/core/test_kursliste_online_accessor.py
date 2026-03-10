import pytest
from decimal import Decimal
from enum import Enum
from pathlib import Path
from opensteuerauszug.core.kursliste_online_accessor import OnlineKurslisteAccessor
from opensteuerauszug.model.kursliste import Kursliste


def _normalize_for_diff(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        normalized = format(value.normalize(), "f")
        if "." in normalized:
            normalized = normalized.rstrip("0").rstrip(".")
        return "0" if normalized in {"-0", ""} else normalized
    if isinstance(value, dict):
        return {k: _normalize_for_diff(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_for_diff(v) for v in value]
    return value


def _filter_deleted_payments(security_dump: dict):
    payments = security_dump.get("payment")
    if not isinstance(payments, list):
        return security_dump

    filtered_payments = [
        payment
        for payment in payments
        if not (
            isinstance(payment, dict)
            and str(payment.get("deleted", "")).lower() in {"1", "true"}
        )
    ]

    if len(filtered_payments) == len(payments):
        return security_dump

    filtered_dump = dict(security_dump)
    filtered_dump["payment"] = filtered_payments
    return filtered_dump


def _collect_diffs(path, online_value, local_value, diffs):
    if type(online_value) is not type(local_value):
        diffs.append((path, online_value, local_value))
        return

    if isinstance(online_value, dict):
        keys = sorted(set(online_value) | set(local_value))
        for key in keys:
            child_path = f"{path}.{key}" if path else key
            if key not in online_value:
                diffs.append((child_path, None, local_value[key]))
            elif key not in local_value:
                diffs.append((child_path, online_value[key], None))
            else:
                _collect_diffs(child_path, online_value[key], local_value[key], diffs)
        return

    if isinstance(online_value, list):
        if len(online_value) != len(local_value):
            diffs.append((f"{path}.length", len(online_value), len(local_value)))
        for idx, (online_item, local_item) in enumerate(zip(online_value, local_value)):
            _collect_diffs(f"{path}[{idx}]", online_item, local_item, diffs)
        return

    if online_value != local_value:
        diffs.append((path, online_value, local_value))


def _is_ignored_difference_path(path: str) -> bool:
    return (path in {"institutionId", "nominalValue", "capitalKey"}
            or path.endswith((".id", ".paymentIdSIX", ".coupon"))
            )


def _load_local_security_for_isin(isin: str):
    sample_file = (
        Path(__file__).resolve().parents[1]
        / "samples"
        / "kursliste"
        / "kursliste_mini_2025.xml"
    )
    if sample_file.exists():
        sample_kursliste = Kursliste.from_xml_file(sample_file, denylist=set())
        sample_security = sample_kursliste.find_security_by_isin(isin)
        if sample_security is not None:
            return sample_security

    return None


@pytest.mark.integration
@pytest.mark.parametrize(
    "isin",
    [
        "US9220427424",  # Vanguard Total World Stock ETF (Fund)
        "DE0007236101",  # Siemens Aktiengesellschaft (Share)
        "CH0226976816",  # iShares Core CHF Corporate Bond ETF (Fund)
        "IE00B8BVCK12",  # iShares MSCI World CHF Hedged UCITS ETF (Fund)
        "CH0038863350",  # Nestle S.A. (Share)
        "US0378331005",  # Apple Inc. (Share)
    ],
)
def test_online_kursliste_instruments_match_local_with_generic_id_ignores(isin):
    local_security = _load_local_security_for_isin(isin)
    if local_security is None:
        pytest.skip(f"Mini Kursliste 2025 does not contain ISIN {isin}")

    online_accessor = OnlineKurslisteAccessor(2025)
    online_security = online_accessor.get_security_by_isin(isin)
    if online_security is None:
        pytest.skip("ICTax online API unavailable or security not returned")

    online_dump = _filter_deleted_payments(
        _normalize_for_diff(online_security.model_dump(exclude_none=True))
    )
    local_dump = _filter_deleted_payments(
        _normalize_for_diff(local_security.model_dump(exclude_none=True))
    )

    differences = []
    _collect_diffs("", online_dump, local_dump, differences)

    expected_non_id_differences = {
        "IE00B8BVCK12": {"payment[0].paymentValueCHF"}, # small rounding diff: online='0.79016' | local='0.790169119'
    }
    expected_paths_for_isin = expected_non_id_differences.get(isin, set())

    unexpected = sorted(
        d[0]
        for d in differences
        if not _is_ignored_difference_path(d[0])
        and d[0] not in expected_paths_for_isin
    )

    difference_lines = [
        f"- {path}: online={online_value!r} | local={local_value!r}"
        for path, online_value, local_value in differences
    ]
    debug_output = "\n".join(difference_lines)

    assert not unexpected, (
        f"Online vs local comparison drifted for {isin}.\n"
        f"Unexpected difference paths: {unexpected}\n"
        "Full differences:\n"
        f"{debug_output}"
    )
