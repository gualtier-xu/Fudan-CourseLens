"""Contract gate for ``courselens.vpn-connection.v1``.

Reference validator for the campus-connection snapshot contract defined in
``docs/vpn-connection-contract-v1.md``. Standard library only, deterministic,
no product imports: this module is a contract gate, not runtime code. A later
backend package may lift ``validate_snapshot`` verbatim into product code.

Fixture: ``tests/fixtures/vpn_connection_contract_v1.json``.
"""

import copy
import json
import math
import re
from pathlib import Path

import pytest

SCHEMA = "courselens.vpn-connection.v1"

STATES = frozenset(
    {
        "off",
        "checking",
        "ready",
        "login_required",
        "reauthenticating",
        "network_unavailable",
        "challenge_required",
        "expired",
        "degraded",
    }
)
NETWORK_PATHS = frozenset({"direct", "local_proxy", "unknown"})
SCHOOL_ROUTES = frozenset({"webvpn", "icourse_direct", "mixed", "unknown"})
REASONS = frozenset(
    {
        "cold_start",
        "direct_ok",
        "proxy_fallback",
        "session_expired",
        "possible_tun_interference",
        "credentials_rejected",
        "challenge",
        "service_unavailable",
        "unknown",
    }
)
ACTIONS = frozenset(
    {
        "login",
        "reauthenticate",
        "check-network",
        "retry",
        "open-settings",
        "close-tun-and-retry",
    }
)
SERVICE_NAMES = ("webvpn", "icourse")
SERVICE_STATES = frozenset({"unknown", "checking", "ready", "unavailable"})
SERVICE_ROUTES = frozenset({"direct", "local_proxy", "unknown"})
SERVICE_FIELDS = ("state", "route", "verified")

TOP_FIELDS = (
    "schema",
    "state",
    "network_path",
    "school_route",
    "reason",
    "observed_at",
    "expires_at",
    "retry_after",
    "actions",
    "generation",
    "services",
)
_READY_LIKE_STATES = frozenset({"ready", "degraded"})

_MIN_EPOCH = 1
_MAX_EPOCH = 4102444800  # 2100-01-01T00:00:00Z
_MAX_SAFE_INTEGER = 2**53 - 1

REJECTION_CODES = frozenset(
    {
        "not_an_object",
        "missing_field",
        "unknown_field",
        "unknown_service",
        "bad_schema",
        "bad_type",
        "bad_enum",
        "duplicate_action",
        "non_integer_number",
        "non_finite_number",
        "negative_number",
        "number_too_large",
        "bad_time",
        "expires_at_forbidden",
        "expires_at_required",
        "expires_before_observed",
        "retry_after_without_retry",
        "retry_before_observed",
    }
)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "vpn_connection_contract_v1.json"


def _load_fixture():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


_FIXTURE = _load_fixture()


def _check_epoch_field(errors, path, value):
    """Validate one epoch-seconds field. Returns (is_valid_int, value_or_none)."""
    if isinstance(value, bool):
        errors.append(("bad_type", path))
        return False, None
    if isinstance(value, float):
        if not math.isfinite(value):
            errors.append(("non_finite_number", path))
        else:
            errors.append(("non_integer_number", path))
        return False, None
    if not isinstance(value, int):
        errors.append(("bad_type", path))
        return False, None
    if value < 0:
        errors.append(("negative_number", path))
        return False, None
    if value == 0 or value > _MAX_EPOCH:
        errors.append(("bad_time", path))
        return False, None
    return True, value


def _check_enum_field(errors, path, value, allowed):
    if not isinstance(value, str):
        errors.append(("bad_type", path))
        return False
    if value not in allowed:
        errors.append(("bad_enum", path))
        return False
    return True


def validate_snapshot(snapshot):
    """Return all contract violations as (code, json_path) pairs, in fixed order."""
    errors = []
    if not isinstance(snapshot, dict):
        errors.append(("not_an_object", "$"))
        return errors

    present = set(snapshot)
    for field in TOP_FIELDS:
        if field not in present:
            errors.append(("missing_field", "$." + field))
    for field in sorted(present - set(TOP_FIELDS)):
        errors.append(("unknown_field", "$." + field))

    # Per-field checks, canonical field order.
    if "schema" in present:
        schema_value = snapshot["schema"]
        if not isinstance(schema_value, str):
            errors.append(("bad_type", "$.schema"))
        elif schema_value != SCHEMA:
            errors.append(("bad_schema", "$.schema"))

    state_value = snapshot.get("state")
    state_ok = False
    if "state" in present:
        state_ok = _check_enum_field(errors, "$.state", state_value, STATES)

    if "network_path" in present:
        _check_enum_field(errors, "$.network_path", snapshot["network_path"], NETWORK_PATHS)

    if "school_route" in present:
        _check_enum_field(errors, "$.school_route", snapshot["school_route"], SCHOOL_ROUTES)

    if "reason" in present:
        _check_enum_field(errors, "$.reason", snapshot["reason"], REASONS)

    observed_ok = False
    observed_value = None
    if "observed_at" in present:
        observed_ok, observed_value = _check_epoch_field(errors, "$.observed_at", snapshot["observed_at"])

    expires_ok = False
    expires_value = snapshot.get("expires_at")
    if "expires_at" in present and expires_value is not None:
        expires_ok, expires_value = _check_epoch_field(errors, "$.expires_at", expires_value)

    retry_ok = False
    retry_value = snapshot.get("retry_after")
    if "retry_after" in present and retry_value is not None:
        retry_ok, retry_value = _check_epoch_field(errors, "$.retry_after", retry_value)

    actions_value = snapshot.get("actions")
    actions_ok = False
    if "actions" in present:
        if not isinstance(actions_value, list):
            errors.append(("bad_type", "$.actions"))
        else:
            actions_ok = True
            seen = set()
            for index, item in enumerate(actions_value):
                path = "$.actions.%d" % index
                if not isinstance(item, str):
                    errors.append(("bad_type", path))
                    continue
                if item not in ACTIONS:
                    errors.append(("bad_enum", path))
                    continue
                if item in seen:
                    errors.append(("duplicate_action", path))
                    continue
                seen.add(item)

    if "generation" in present:
        generation_value = snapshot["generation"]
        if isinstance(generation_value, bool) or not isinstance(generation_value, int):
            errors.append(("bad_type", "$.generation"))
        elif generation_value < 0:
            errors.append(("negative_number", "$.generation"))
        elif generation_value > _MAX_SAFE_INTEGER:
            errors.append(("number_too_large", "$.generation"))

    if "services" in present:
        services = snapshot["services"]
        if not isinstance(services, dict):
            errors.append(("bad_type", "$.services"))
        else:
            for name in SERVICE_NAMES:
                if name not in services:
                    errors.append(("missing_field", "$.services." + name))
            for name in sorted(services):
                if name not in SERVICE_NAMES:
                    errors.append(("unknown_service", "$.services." + name))
            for name in SERVICE_NAMES:
                if name not in services:
                    continue
                service = services[name]
                if not isinstance(service, dict):
                    errors.append(("bad_type", "$.services." + name))
                    continue
                for key in sorted(service):
                    if key not in SERVICE_FIELDS:
                        errors.append(("unknown_field", "$.services.%s.%s" % (name, key)))
                if "state" in service:
                    _check_enum_field(
                        errors, "$.services.%s.state" % name, service["state"], SERVICE_STATES
                    )
                if "route" in service:
                    _check_enum_field(
                        errors, "$.services.%s.route" % name, service["route"], SERVICE_ROUTES
                    )
                if "verified" in service and not isinstance(service["verified"], bool):
                    errors.append(("bad_type", "$.services.%s.verified" % name))

    # Cross-field rules, guarded so invalid prerequisites add no cascade errors.
    if state_ok:
        if state_value in _READY_LIKE_STATES:
            if "expires_at" in present and expires_value is None:
                errors.append(("expires_at_required", "$.expires_at"))
            elif expires_ok and observed_ok and expires_value < observed_value:
                errors.append(("expires_before_observed", "$.expires_at"))
        else:
            if "expires_at" in present and expires_value is not None:
                errors.append(("expires_at_forbidden", "$.expires_at"))

    if "retry_after" in present and retry_value is not None and retry_ok:
        if actions_ok and "retry" not in actions_value:
            errors.append(("retry_after_without_retry", "$.retry_after"))
        if observed_ok and retry_value < observed_value:
            errors.append(("retry_before_observed", "$.retry_after"))

    return errors


def is_valid_snapshot(snapshot):
    """True when the snapshot satisfies the frozen v1 contract."""
    return not validate_snapshot(snapshot)


def _iter_strings(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _iter_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_strings(item)
    elif isinstance(node, str):
        yield node


def _apply_mutations(base_snapshot, entry):
    snapshot = copy.deepcopy(base_snapshot)
    for path, value in (entry.get("set") or {}).items():
        keys = path.split(".")
        target = snapshot
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value
    for path in entry.get("remove") or []:
        keys = path.split(".")
        target = snapshot
        for key in keys[:-1]:
            target = target[key]
        del target[keys[-1]]
    return snapshot


def _snapshot_for_entry(entry):
    if "explicit_snapshot" in entry:
        return copy.deepcopy(entry["explicit_snapshot"])
    return _apply_mutations(_FIXTURE["valid"][entry["base"]], entry)


def _reject_non_finite_constant(name):
    raise ValueError("fixture must be strict JSON, found constant: %s" % name)


def test_fixture_is_strict_json():
    raw = FIXTURE_PATH.read_text(encoding="utf-8")
    parsed = json.loads(raw, parse_constant=_reject_non_finite_constant)
    assert parsed == _FIXTURE


def test_fixture_hygiene_no_url_or_credential_like_strings():
    url_pattern = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
    secret_pattern = re.compile(
        r"lck=|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|[A-Fa-f0-9]{24,}|[A-Za-z0-9+/]{32,}={0,2}"
    )
    for text in _iter_strings(_FIXTURE):
        assert not url_pattern.search(text), text
        assert not secret_pattern.search(text), text


def test_required_valid_examples_present():
    assert set(_FIXTURE["valid"]) == {
        "ready_direct",
        "checking_cold_start",
        "degraded_proxy_fallback",
        "login_required",
        "expired_session",
    }


def test_valid_examples_validate_clean():
    for name, snapshot in _FIXTURE["valid"].items():
        errors = validate_snapshot(snapshot)
        assert errors == [], (name, errors)


@pytest.mark.parametrize(
    "entry",
    sorted(_FIXTURE["invalid"], key=lambda item: item["name"]),
    ids=lambda item: item["name"],
)
def test_invalid_examples_rejected_with_expected_code(entry):
    snapshot = _snapshot_for_entry(entry)
    errors = validate_snapshot(snapshot)
    assert errors, (entry["name"], "expected a rejection, got none")
    assert (entry["expect_code"], entry["expect_path"]) in errors, (entry["name"], errors)
    for code, _path in errors:
        assert code in REJECTION_CODES, (entry["name"], code)


def test_every_rejection_code_except_non_finite_is_in_fixture():
    exercised = {entry["expect_code"] for entry in _FIXTURE["invalid"]}
    assert exercised == REJECTION_CODES - {"non_finite_number"}


def test_non_finite_times_rejected():
    base = _FIXTURE["valid"]["ready_direct"]
    for bad in (float("nan"), float("inf"), float("-inf")):
        snapshot = copy.deepcopy(base)
        snapshot["observed_at"] = bad
        assert ("non_finite_number", "$.observed_at") in validate_snapshot(snapshot)
        snapshot = copy.deepcopy(base)
        snapshot["expires_at"] = bad
        assert ("non_finite_number", "$.expires_at") in validate_snapshot(snapshot)


def test_boolean_is_rejected_where_number_required():
    base = _FIXTURE["valid"]["ready_direct"]
    for field in ("observed_at", "expires_at", "generation"):
        snapshot = copy.deepcopy(base)
        snapshot[field] = True
        codes = {code for code, _path in validate_snapshot(snapshot)}
        assert "bad_type" in codes, (field, codes)


def test_non_object_snapshots_rejected():
    for bad in ([], "off", 3, None, True):
        assert validate_snapshot(copy.deepcopy(bad)) == [("not_an_object", "$")]


def test_validator_is_deterministic():
    snapshot = _apply_mutations(
        _FIXTURE["valid"]["degraded_proxy_fallback"],
        {"set": {"state": "nope", "generation": -2}, "remove": ["school_route"]},
    )
    first = validate_snapshot(snapshot)
    second = validate_snapshot(copy.deepcopy(snapshot))
    assert first == second
    assert [code for code, _path in first] == [
        "missing_field",
        "bad_enum",
        "negative_number",
    ]


def test_closed_set_sizes_are_pinned():
    assert len(STATES) == 9
    assert len(NETWORK_PATHS) == 3
    assert len(SCHOOL_ROUTES) == 4
    assert len(REASONS) == 9
    assert len(ACTIONS) == 6
    assert len(TOP_FIELDS) == 11
    assert len(REJECTION_CODES) == 18
