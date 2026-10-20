"""Model, host state, file helpers and carrier lock of the OpenCode credentials helper.

Every dummy credential value is generated at test runtime (never a literal) and files are
written only below ``tmp_path``.
"""

from __future__ import annotations

import ast
import copy
import errno
import hashlib
import json
import os
import random
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest

from djinn_in_a_box.core import opencode_credentials as oc

MODULE_PATH = Path(oc.__file__)
SALT = os.urandom(8).hex()


def token(name: str) -> str:
    """A runtime-generated dummy value; differs per run and per name."""
    return hashlib.sha256(f"{SALT}:{name}".encode()).hexdigest()[:16]


def entry_dict(
    kind: str = "key", identity: str = "cred_one", integration: str = "prov_one", **value: Any
) -> dict[str, Any]:
    if kind == "key":
        body: dict[str, Any] = {"type": "key", "key": token(identity)}
    elif kind == "oauth":
        body = {
            "type": "oauth",
            "methodID": "m1",
            "refresh": token(identity + "r"),
            "access": token(identity + "a"),
            "expires": 1000,
        }
    else:
        body = {"type": "external", "methodID": "m1"}
    body.update(value)
    return {
        "id": identity,
        "integrationID": integration,
        "label": "label " + identity,
        "active": False,
        "value": body,
    }


def make(*args: Any, **kwargs: Any) -> oc.Entry:
    return oc.validate_entry(entry_dict(*args, **kwargs))


def raw(document: object) -> bytes:
    return json.dumps(document).encode()


# ---------------------------------------------------------------------------------------------
# Merge: properties
# ---------------------------------------------------------------------------------------------

IDS = ["cred_a", "cred_b", "cred_c", "cred_d"]


def random_entry(rng: random.Random) -> oc.Entry:
    identity = rng.choice(IDS)
    integration = rng.choice(["prov_x", "prov_y"])
    kind = rng.choice(["key", "oauth", "external"])
    tag = f"{kind}{rng.randrange(3)}"
    if kind == "key":
        document = entry_dict("key", identity, integration, key=token(tag))
    elif kind == "oauth":
        document = entry_dict(
            "oauth", identity, integration, expires=rng.choice([0, 5, 5, 9]), refresh=token(tag)
        )
    else:
        document = entry_dict("external", identity, integration, methodID=f"m{rng.randrange(2)}")
    if rng.random() < 0.3:
        document["label"] = rng.choice(["one", "two"])
    document["active"] = rng.random() < 0.5
    return oc.validate_entry(document)


def random_group(rng: random.Random) -> list[oc.Entry]:
    chosen: dict[str, oc.Entry] = {}
    for _ in range(rng.randrange(0, 6)):
        candidate = random_entry(rng)
        chosen[candidate.id] = candidate
    return list(chosen.values())


def random_tombstones(rng: random.Random) -> set[str]:
    return {identity for identity in IDS if rng.random() < 0.2}


def snapshot(result: oc.MergeResult) -> tuple[dict[str, str], list[str], tuple[str, ...]]:
    return (
        {identity: entry.canonical for identity, entry in sorted(result.entries.items())},
        sorted(result.tombstones),
        result.conflicts,
    )


def joined(result: oc.MergeResult) -> list[oc.Entry]:
    return list(result.entries.values())


def case_inputs(seed: int) -> tuple[list[list[oc.Entry]], list[set[str]]]:
    rng = random.Random(seed)
    groups = [random_group(rng) for _ in range(rng.randrange(2, 5))]
    tombstones = [random_tombstones(rng) for _ in range(rng.randrange(1, 4))]
    return groups, tombstones


SEEDS = range(300)


@pytest.mark.parametrize("seed", SEEDS)
def test_merge_is_commutative_and_order_independent(seed: int) -> None:
    groups, tombstones = case_inputs(seed)
    expected = snapshot(oc.merge(groups, tombstones))
    rng = random.Random(seed + 10_000)
    shuffled_groups = [rng.sample(group, len(group)) for group in groups]
    rng.shuffle(shuffled_groups)
    shuffled_tombstones = list(tombstones)
    rng.shuffle(shuffled_tombstones)
    assert snapshot(oc.merge(shuffled_groups, shuffled_tombstones)) == expected


@pytest.mark.parametrize("seed", SEEDS)
def test_merge_is_associative(seed: int) -> None:
    groups, tombstones = case_inputs(seed)
    first, second, third = (groups * 3)[:3]
    t_first, t_second, t_third = (tombstones * 3)[:3]
    whole = oc.merge([first, second, third], [t_first, t_second, t_third])
    left = oc.merge([first, second], [t_first, t_second])
    left_assoc = oc.merge([joined(left), third], [left.tombstones, t_third])
    right = oc.merge([second, third], [t_second, t_third])
    right_assoc = oc.merge([first, joined(right)], [t_first, right.tombstones])
    assert snapshot(left_assoc)[:2] == snapshot(whole)[:2]
    assert snapshot(right_assoc)[:2] == snapshot(whole)[:2]


@pytest.mark.parametrize("seed", SEEDS)
def test_merge_is_idempotent(seed: int) -> None:
    groups, tombstones = case_inputs(seed)
    once = oc.merge(groups, tombstones)
    assert snapshot(oc.merge(groups + groups, tombstones + tombstones)) == snapshot(once)
    again = oc.merge([joined(once)], [once.tombstones])
    assert snapshot(again)[:2] == snapshot(once)[:2]


@pytest.mark.parametrize("seed", SEEDS)
def test_tombstones_only_grow_and_remove(seed: int) -> None:
    groups, tombstones = case_inputs(seed)
    base = oc.merge(groups, tombstones)
    extra = random_tombstones(random.Random(seed + 20_000))
    bigger = oc.merge(groups, [*tombstones, extra])
    assert base.tombstones <= bigger.tombstones
    assert extra <= bigger.tombstones
    assert not set(base.entries) & base.tombstones
    assert not set(bigger.entries) & bigger.tombstones
    assert set(bigger.entries) <= set(base.entries)
    more = oc.merge([*groups, random_group(random.Random(seed + 30_000))], tombstones)
    assert set(base.entries) <= set(more.entries)


# ---------------------------------------------------------------------------------------------
# Merge: literal rules
# ---------------------------------------------------------------------------------------------


def test_oauth_later_expires_wins_over_a_larger_canonical_json() -> None:
    later = make("oauth", expires=10, refresh="a" + token("later"))
    earlier = make("oauth", expires=9, refresh="z" + token("earlier"))
    assert earlier.canonical > later.canonical
    for adds in ([[earlier], [later]], [[later], [earlier]]):
        result = oc.merge(adds, [])
        assert result.entries["cred_one"].value["expires"] == 10
        assert result.conflicts == ()


def test_equal_expires_falls_to_the_canonical_json() -> None:
    low = make("oauth", expires=1000, refresh="a" + token("low"))
    high = make("oauth", expires=1000, refresh="b" + token("high"))
    result = oc.merge([[low], [high]], [])
    assert result.entries["cred_one"] == high
    assert result.conflicts == ("cred_one",)


def test_key_conflict_larger_canonical_wins_and_is_reported() -> None:
    low = make("key", key="a" + token("low"))
    high = make("key", key="b" + token("high"))
    for adds in ([[low], [high]], [[high], [low]]):
        result = oc.merge(adds, [])
        assert result.entries["cred_one"].canonical == high.canonical
        assert result.conflicts == ("cred_one",)


def test_value_type_conflict_without_oauth_precedence_is_reported() -> None:
    external = make("external")
    key = make("key")
    result = oc.merge([[external], [key]], [])
    assert result.entries["cred_one"].canonical == max(external.canonical, key.canonical)
    assert result.conflicts == ("cred_one",)


def test_oauth_beats_key_by_expires_without_conflict() -> None:
    oauth = make("oauth", expires=0)
    key = make("key", key="z" * 40)
    result = oc.merge([[key], [oauth]], [])
    assert result.entries["cred_one"].value["type"] == "oauth"
    assert result.conflicts == ()


def test_identical_values_are_no_conflict() -> None:
    result = oc.merge([[make("key")], [make("key")]], [])
    assert list(result.entries) == ["cred_one"]
    assert result.conflicts == ()


def test_tombstone_removes_and_is_kept() -> None:
    result = oc.merge([[make("key"), make("key", "cred_two")]], [{"cred_one"}, {"cred_zzz"}])
    assert list(result.entries) == ["cred_two"]
    assert result.tombstones == {"cred_one", "cred_zzz"}


def test_tombstoned_id_is_neither_winner_nor_conflict() -> None:
    low = make("key", key="a" + token("low"))
    high = make("key", key="b" + token("high"))
    result = oc.merge([[low], [high]], [{"cred_one"}])
    assert result.entries == {}
    assert result.conflicts == ()


def test_active_never_affects_result_or_fingerprint() -> None:
    inactive = entry_dict("key")
    active = copy.deepcopy(inactive)
    active["active"] = True
    first, second = oc.validate_entry(inactive), oc.validate_entry(active)
    assert first.canonical == second.canonical
    assert first.fingerprint == second.fingerprint
    assert first == second
    assert oc.merge([[first], [second]], []).conflicts == ()
    assert "active" not in first.canonical


def test_canonical_json_and_fingerprint_are_exactly_specified() -> None:
    entry = make("key", key="k")
    document = entry_dict("key", key="k")
    del document["active"]
    expected = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert entry.canonical == expected
    assert entry.fingerprint == hashlib.sha256(expected.encode()).hexdigest()
    assert len(entry.fingerprint) == 64


def test_order_key_is_expires_for_oauth_and_minus_one_otherwise() -> None:
    assert make("oauth", expires=7).order == (7, make("oauth", expires=7).canonical)
    assert make("key").order[0] == -1
    assert make("external").order[0] == -1


def test_non_ascii_is_kept_unescaped_and_hashed_as_utf8() -> None:
    document = entry_dict("key")
    document["label"] = "Zugang äö"
    entry = oc.validate_entry(document)
    assert "ä" in entry.canonical
    assert entry.fingerprint == hashlib.sha256(entry.canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------
# Materialize and parsers
# ---------------------------------------------------------------------------------------------


def test_materialize_entries_is_sorted_export_format_with_selection() -> None:
    entries = [
        make("key", "cred_b", "prov_x"),
        make("key", "cred_a", "prov_x"),
        make("key", "c3", "p2"),
    ]
    data = oc.materialize_entries(entries, {"prov_x": "cred_b"})
    document = json.loads(data)
    assert [item["id"] for item in document] == ["c3", "cred_a", "cred_b"]
    assert [item["active"] for item in document] == [False, False, True]
    assert list(document[0]) == ["id", "integrationID", "label", "active", "value"]
    assert data == (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()
    assert oc.parse_entries(data) == sorted(entries, key=lambda entry: entry.id)


def test_materialize_defaults_to_inactive_and_empty_is_three_bytes() -> None:
    document = json.loads(oc.materialize_entries([make("key")], {}))
    assert document[0]["active"] is False
    assert oc.materialize_entries([], {}) == b"[]\n"


def test_materialize_refuses_a_result_over_the_limit() -> None:
    big = make("key", key="x" * 600)
    with pytest.raises(oc.NotUnderstood):
        oc.materialize_entries([big], {}, limit=500)
    assert oc.materialize_entries([big], {}, limit=len(oc.materialize_entries([big], {})))


def test_materialize_tombstones_sorted_unique() -> None:
    assert oc.materialize_tombstones(["b", "a", "b"]) == b'[\n  "a",\n  "b"\n]\n'
    assert oc.materialize_tombstones([]) == b"[]\n"
    with pytest.raises(oc.NotUnderstood):
        oc.materialize_tombstones(["x" * 40], limit=20)


def test_parse_tombstones() -> None:
    assert oc.parse_tombstones(b'["b","a","b"]') == ["a", "b"]
    assert oc.parse_tombstones(b"[]") == []
    for bad in (b'{"a":1}', b"[1]", b'["bad id"]', b'[""]', b'["a","a/\\u00e4"]', b"nope"):
        with pytest.raises(oc.NotUnderstood):
            oc.parse_tombstones(bad)


# ---------------------------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------------------------

REQUIRED_PATHS = [
    ("id",),
    ("integrationID",),
    ("label",),
    ("active",),
    ("value",),
    ("value", "type"),
]


def assert_refused(document: object) -> None:
    with pytest.raises(oc.NotUnderstood):
        oc.parse_entries(raw([document]))


def modified(document: dict[str, Any], path: tuple[str, ...], replacement: Any = ...) -> Any:
    clone = copy.deepcopy(document)
    target = clone
    for step in path[:-1]:
        target = target[step]
    if replacement is ...:
        del target[path[-1]]
    else:
        target[path[-1]] = replacement
    return clone


@pytest.mark.parametrize("kind", ["key", "oauth", "external"])
def test_valid_variants_are_accepted(kind: str) -> None:
    document = entry_dict(kind)
    document["value"]["metadata"] = {"nested": {"deep": [1, 2.5, None]}}
    assert oc.parse_entries(raw([document]))[0].id == "cred_one"
    assert oc.parse_entries(raw([entry_dict(kind)]))[0].id == "cred_one"


def test_key_configuration_forms_are_accepted_or_refused() -> None:
    good = entry_dict("key", configuration={"a": "s", "b": 1, "c": 2.5, "d": True, "e": ["x"]})
    assert oc.parse_entries(raw([good]))
    for bad in ({"a": None}, {"a": {"x": 1}}, {"a": [1]}, ["a"], "a"):
        assert_refused(entry_dict("key", configuration=bad))
    assert_refused(entry_dict("oauth", configuration={}))


STRING_FIELDS = [("key", "key"), ("oauth", "methodID"), ("oauth", "refresh")] + [
    ("oauth", "access"),
    ("external", "methodID"),
]


@pytest.mark.parametrize(("kind", "name"), STRING_FIELDS)
def test_missing_or_mistyped_string_value_fields(kind: str, name: str) -> None:
    base = entry_dict(kind)
    assert_refused(modified(base, ("value", name)))
    for wrong in (1, None, ["x"], {"a": 1}, True):
        assert_refused(modified(base, ("value", name), wrong))


def test_missing_expires() -> None:
    assert_refused(modified(entry_dict("oauth"), ("value", "expires")))


@pytest.mark.parametrize("path", REQUIRED_PATHS)
def test_missing_entry_fields(path: tuple[str, ...]) -> None:
    assert_refused(modified(entry_dict("key"), path))


@pytest.mark.parametrize(
    ("path", "wrong"),
    [
        (("id",), 5),
        (("integrationID",), None),
        (("label",), 5),
        (("label",), None),
        (("active",), "true"),
        (("active",), 1),
        (("active",), None),
        (("value",), "text"),
        (("value",), []),
        (("value", "type"), 5),
        (("value", "type"), "unknown"),
        (("value", "type"), "Key"),
    ],
)
def test_mistyped_entry_fields(path: tuple[str, ...], wrong: Any) -> None:
    assert_refused(modified(entry_dict("key"), path, wrong))


@pytest.mark.parametrize("wrong", [True, False, -1, 1.5, 1.0, "5", None])
def test_expires_must_be_a_nonnegative_integer(wrong: Any) -> None:
    assert_refused(entry_dict("oauth", expires=wrong))


def test_expires_boundary_values() -> None:
    assert oc.parse_entries(raw([entry_dict("oauth", expires=0)]))
    assert oc.parse_entries(raw([entry_dict("oauth", expires=2**60)]))


def test_metadata_must_be_an_object() -> None:
    for wrong in ([], "x", 1, None):
        assert_refused(entry_dict("key", metadata=wrong))


def test_unknown_fields_are_refused() -> None:
    assert_refused({**entry_dict("key"), "extra": 1})
    assert_refused(entry_dict("key", extra=1))
    assert_refused(entry_dict("external", key="x"))
    assert_refused(entry_dict("key", refresh="x"))


def test_non_object_entries_and_non_array_documents() -> None:
    for document in ("x", 5, None, [[]], {"id": "x"}):
        with pytest.raises(oc.NotUnderstood):
            oc.parse_entries(raw(document))


@pytest.mark.parametrize(
    ("identity", "valid"),
    [
        ("a", True),
        ("Cred_1.2:3@4/5-6", True),
        ("x" * 200, True),
        ("x" * 201, False),
        ("", False),
        ("with space", False),
        ("new\nline", False),
        ("trailing\n", False),
        ("ä", False),
        ("semi;colon", False),
        ("back\\slash", False),
        ("quote'", False),
        ("a|b", False),
        ("a,b", False),
        ("a+b", False),
    ],
)
def test_id_charset_for_id_and_integration_id(identity: str, valid: bool) -> None:
    for field_name in ("id", "integrationID"):
        document = modified(entry_dict("key"), (field_name,), identity)
        if valid:
            assert oc.parse_entries(raw([document]))
        else:
            assert_refused(document)
    assert (identity in oc.parse_tombstones(raw([identity]))) if valid else True
    if not valid:
        with pytest.raises(oc.NotUnderstood):
            oc.parse_tombstones(raw([identity]))


def test_duplicate_id_is_refused_with_the_id_only() -> None:
    first = entry_dict("key", "cred_dup")
    second = entry_dict("external", "cred_dup")
    with pytest.raises(oc.NotUnderstood, match="duplicate id cred_dup") as caught:
        oc.parse_entries(raw([first, second]))
    assert token("cred_dup") not in caught.value.reason


def test_duplicate_object_member_is_refused() -> None:
    text = json.dumps(entry_dict("key")).replace('"label"', '"label": "x", "label"', 1)
    with pytest.raises(oc.NotUnderstood):
        oc.parse_entries(f"[{text}]".encode())
    nested = '[{"a": {"b": 1, "b": 2}}]'
    with pytest.raises(oc.NotUnderstood):
        oc.load_strict_json(nested.encode())


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_non_finite_numbers_are_refused(constant: str) -> None:
    text = json.dumps(entry_dict("key", metadata={"n": 0})).replace('"n": 0', f'"n": {constant}')
    with pytest.raises(oc.NotUnderstood):
        oc.parse_entries(f"[{text}]".encode())


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff\xfe[]",
        b"[\xc3(]",
        b"\xef\xbb\xbf[]",
        "[]".encode("utf-16"),
        b"",
        b"[",
        b"[] x",
        b'["\\ud800"]',
        b"[" * 100_000 + b"]" * 100_000,
        b"1" * 5000,
    ],
)
def test_undecodable_or_malformed_content_is_refused(payload: bytes) -> None:
    with pytest.raises(oc.NotUnderstood):
        oc.parse_entries(payload)


def test_lone_surrogate_inside_an_entry_is_refused() -> None:
    text = json.dumps(entry_dict("key")).replace("label cred_one", "\\ud800")
    assert "\\ud800" in text
    with pytest.raises(oc.NotUnderstood):
        oc.parse_entries(f"[{text}]".encode())


def test_error_reasons_never_carry_values() -> None:
    secret = token("must-not-leak")
    cases: list[bytes] = [
        raw([entry_dict("key", key=5, metadata={"x": secret})]),
        raw([entry_dict("oauth", expires=secret)]),
        raw([entry_dict("key", **{secret: 1})]),
        raw([{**entry_dict("key"), secret: 1}]),
        raw([modified(entry_dict("key"), ("value", "type"), secret)]),
        raw([modified(entry_dict("key"), ("id",), secret + " bad")]),
        raw([modified(entry_dict("key"), ("label",), 5)]).replace(b"5", secret.encode()),
        b'[{"' + secret.encode() + b'": 1, "' + secret.encode() + b'": 2}]',
        secret.encode(),
    ]
    for payload in cases:
        with pytest.raises(oc.NotUnderstood) as caught:
            oc.parse_entries(payload)
        assert secret not in caught.value.reason
        assert secret not in str(caught.value)
        assert secret not in repr(caught.value)


@pytest.mark.parametrize("limit", [200, 4096, oc.FILE_LIMIT])
def test_parse_size_bound_is_exact(limit: int) -> None:
    exact = b"[]" + b" " * (limit - 2)
    assert len(exact) == limit
    assert oc.parse_entries(exact, limit) == []
    assert oc.parse_tombstones(exact, limit) == []
    with pytest.raises(oc.NotUnderstood):
        oc.parse_entries(exact + b" ", limit)
    with pytest.raises(oc.NotUnderstood):
        oc.parse_tombstones(exact + b" ", limit)


def test_default_size_bounds() -> None:
    assert oc.FILE_LIMIT == 1024 * 1024
    assert oc.EXPORT_LIMIT == 16 * 1024 * 1024
    assert oc.parse_entries(b"[]" + b" " * (oc.FILE_LIMIT - 2)) == []
    with pytest.raises(oc.NotUnderstood):
        oc.parse_entries(b"[]" + b" " * (oc.FILE_LIMIT - 1))
    export = b"[]" + b" " * (oc.EXPORT_LIMIT - 2)
    assert oc.parse_entries(export, oc.EXPORT_LIMIT) == []
    with pytest.raises(oc.NotUnderstood):
        oc.parse_entries(export + b" ", oc.EXPORT_LIMIT)


# ---------------------------------------------------------------------------------------------
# Host state
# ---------------------------------------------------------------------------------------------


def fp(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def state_with(**parts: Any) -> oc.HostState:
    return oc.HostState(
        inode=parts.get("inode", 7),
        seen=parts.get("seen", {}),
        hidden=parts.get("hidden", {}),
        reactivate=parts.get("reactivate", {}),
    )


def test_bind_keeps_the_same_inode_and_resets_otherwise() -> None:
    state = state_with(
        seen={"cred_a": fp("a")}, hidden={"cred_a": (fp("a"),)}, reactivate={"p": "cred_a"}
    )
    assert oc.bind(state, 7) is state
    for other in (8, 0, None):
        reset = oc.bind(state, other)
        assert reset == oc.HostState(inode=other)
        assert not reset.seen
        assert not reset.hidden
        assert not reset.reactivate
    assert oc.bind(oc.HostState(), 5) == oc.HostState(inode=5)


def test_vanished_reports_seen_entries_missing_from_the_export() -> None:
    present = make("key", "cred_here")
    seen = {"cred_here": present.fingerprint, "cred_gone": fp("g"), "cred_also": fp("h")}
    assert oc.vanished(seen, [present]) == [("cred_also", fp("h")), ("cred_gone", fp("g"))]
    assert oc.vanished(seen, [make("key", "cred_here", key="z")]) == [
        ("cred_also", fp("h")),
        ("cred_gone", fp("g")),
    ]
    assert oc.vanished({}, [present]) == []


def test_hide_only_grows_and_is_hidden_checks_the_pair() -> None:
    state = oc.hide(state_with(), [("cred_a", fp("1")), ("cred_a", fp("2")), ("cred_b", fp("3"))])
    state = oc.hide(state, [("cred_a", fp("1"))])
    assert set(state.hidden["cred_a"]) == {fp("1"), fp("2")}
    assert oc.is_hidden(state, "cred_a", fp("1"))
    assert oc.is_hidden(state, "cred_a", fp("2"))
    assert not oc.is_hidden(state, "cred_a", fp("3"))
    assert not oc.is_hidden(state, "cred_c", fp("1"))
    assert not oc.is_hidden(state_with(), "cred_a", fp("1"))


def test_unhide_present_removes_exactly_the_returned_pair() -> None:
    back = make("key", "cred_a")
    state = state_with(
        hidden={"cred_a": (back.fingerprint, fp("old")), "cred_b": (fp("keep"),)},
    )
    after = oc.unhide_present(state, [back, make("key", "cred_zzz")])
    assert after.hidden == {"cred_a": (fp("old"),), "cred_b": (fp("keep"),)}
    assert not oc.is_hidden(after, "cred_a", back.fingerprint)
    final = oc.unhide_present(after, [make("key", "cred_a", key="other")])
    assert final.hidden == after.hidden
    emptied = oc.unhide_present(state_with(hidden={"cred_a": (back.fingerprint,)}), [back])
    assert emptied.hidden == {}


def test_a_different_fingerprint_for_the_same_id_stays_importable() -> None:
    state = oc.hide(state_with(), [("cred_a", make("key", "cred_a").fingerprint)])
    fresher = make("key", "cred_a", key=token("fresh"))
    assert not oc.is_hidden(state, "cred_a", fresher.fingerprint)


def test_set_reactivate_first_target_per_integration_wins() -> None:
    state = oc.set_reactivate(state_with(), "prov_x", "cred_a")
    state = oc.set_reactivate(state, "prov_x", "cred_b")
    state = oc.set_reactivate(state, "prov_y", "cred_c")
    assert state.reactivate == {"prov_x": "cred_a", "prov_y": "cred_c"}


def test_confirm_sets_seen_from_the_export_and_clears_reactivate() -> None:
    first, second = make("key", "cred_a"), make("external", "cred_b")
    state = state_with(
        seen={"cred_old": fp("o")},
        hidden={"cred_h": (fp("h"),)},
        reactivate={"prov": "cred_a"},
    )
    done = oc.confirm(state, [first, second])
    assert done.seen == {"cred_a": first.fingerprint, "cred_b": second.fingerprint}
    assert done.reactivate == {}
    assert done.hidden == state.hidden
    assert done.inode == 7


def test_state_functions_do_not_mutate_their_input() -> None:
    state = state_with(seen={"cred_a": fp("a")}, hidden={"cred_a": (fp("a"),)})
    before = copy.deepcopy(state)
    oc.hide(state, [("cred_a", fp("b"))])
    oc.unhide_present(state, [make("key", "cred_a")])
    oc.set_reactivate(state, "p", "cred_a")
    oc.confirm(state, [])
    oc.bind(state, 99)
    assert state == before


def good_state_document() -> dict[str, Any]:
    return {
        "database": {"inode": 12},
        "seen": {"cred_a": fp("a")},
        "hidden": {"cred_b": [fp("b"), fp("c")]},
        "reactivate": {"prov_x": "cred_a"},
    }


def test_host_state_round_trip() -> None:
    state = oc.parse_host_state(raw(good_state_document()))
    assert state == state_with(
        inode=12,
        seen={"cred_a": fp("a")},
        hidden={"cred_b": tuple(sorted([fp("b"), fp("c")]))},
        reactivate={"prov_x": "cred_a"},
    )
    dumped = oc.dump_host_state(state)
    assert oc.parse_host_state(dumped) == state
    assert dumped == oc.dump_host_state(oc.parse_host_state(dumped))
    assert dumped.endswith(b"\n")


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.pop("database"),
        lambda d: d.pop("seen"),
        lambda d: d.pop("hidden"),
        lambda d: d.pop("reactivate"),
        lambda d: d.update(extra=1),
        lambda d: d.update(database={}),
        lambda d: d.update(database={"inode": -1}),
        lambda d: d.update(database={"inode": True}),
        lambda d: d.update(database={"inode": "1"}),
        lambda d: d.update(database={"inode": 1, "dev": 2}),
        lambda d: d.update(seen=[]),
        lambda d: d.update(seen={"bad id": fp("a")}),
        lambda d: d.update(seen={"cred_a": fp("a").upper()}),
        lambda d: d.update(seen={"cred_a": fp("a")[:-1]}),
        lambda d: d.update(seen={"cred_a": 5}),
        lambda d: d.update(hidden={"cred_b": []}),
        lambda d: d.update(hidden={"cred_b": fp("b")}),
        lambda d: d.update(hidden={"cred_b": ["nothex"]}),
        lambda d: d.update(hidden={"bad id": [fp("b")]}),
        lambda d: d.update(reactivate={"prov": 5}),
        lambda d: d.update(reactivate={"bad id": "cred_a"}),
        lambda d: d.update(reactivate={"prov": "bad id"}),
        lambda d: d.update(reactivate=[]),
    ],
)
def test_host_state_is_strictly_validated(change: Callable[[dict[str, Any]], Any]) -> None:
    document = good_state_document()
    change(document)
    with pytest.raises(oc.NotUnderstood):
        oc.parse_host_state(raw(document))


def test_host_state_rejects_garbage_and_oversize() -> None:
    for payload in (b"[]", b"nope", b"", b'{"database":{"inode":1},"database":{"inode":2}}'):
        with pytest.raises(oc.NotUnderstood):
            oc.parse_host_state(payload)
    with pytest.raises(oc.NotUnderstood):
        oc.dump_host_state(oc.HostState())


def test_host_state_file_round_trip_with_compare_before_rename(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    directory.mkdir()
    fd = oc.open_dir(directory)
    try:
        state, expected = oc.read_host_state(fd, "djinn-credentials-host.json")
        assert state == oc.HostState()
        assert expected is None
        state = oc.bind(state, 41)
        written = oc.write_host_state(fd, "djinn-credentials-host.json", state, expected)
        again, expected = oc.read_host_state(fd, "djinn-credentials-host.json")
        assert again == state
        assert expected == written
        assert stat.S_IMODE((directory / "djinn-credentials-host.json").stat().st_mode) == 0o600
        before = (directory / "djinn-credentials-host.json").stat()
        assert oc.write_host_state(fd, "djinn-credentials-host.json", again, expected) == written
        after = (directory / "djinn-credentials-host.json").stat()
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
        newer = oc.hide(state, [("cred_a", fp("a"))])
        (directory / "djinn-credentials-host.json").write_bytes(b"{}")
        with pytest.raises(oc.ChangedOnDisk):
            oc.write_host_state(fd, "djinn-credentials-host.json", newer, expected)
        assert (directory / "djinn-credentials-host.json").read_bytes() == b"{}"
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def directory(tmp_path: Path) -> Iterable[tuple[Path, int]]:
    path = tmp_path / "carrier"
    path.mkdir()
    fd = oc.open_dir(path)
    try:
        yield path, fd
    finally:
        os.close(fd)


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_read_private_returns_content_and_identity(directory: tuple[Path, int]) -> None:
    path, fd = directory
    target = path / "file.json"
    content = token("content").encode()
    target.write_bytes(content)
    target.chmod(0o600)
    result = oc.read_private(fd, "file.json", 100)
    assert result is not None
    status = target.stat()
    assert result == (content, status.st_ino, status.st_mtime_ns, len(content))
    assert oc.read_private(fd, "missing.json", 100) is None


def test_read_private_size_bound_is_exact(directory: tuple[Path, int]) -> None:
    path, fd = directory
    (path / "file.json").write_bytes(b"x" * 100)
    result = oc.read_private(fd, "file.json", 100)
    assert result is not None
    assert len(result.data) == 100
    with pytest.raises(oc.NotUnderstood):
        oc.read_private(fd, "file.json", 99)
    (path / "file.json").write_bytes(b"x" * 101)
    with pytest.raises(oc.NotUnderstood):
        oc.read_private(fd, "file.json", 100)


def test_read_private_checks_the_size_before_reading(
    directory: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, fd = directory
    (path / "file.json").write_bytes(b"x" * 101)

    def forbidden(*_args: object) -> bytes:
        raise AssertionError("content was read although the size exceeds the limit")

    monkeypatch.setattr(os, "read", forbidden)
    with pytest.raises(oc.NotUnderstood):
        oc.read_private(fd, "file.json", 100)


def test_read_private_bounds_the_read_when_the_file_grows(
    directory: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, fd = directory
    (path / "file.json").write_bytes(b"x" * 10)
    real_fstat = os.fstat

    def lying_fstat(descriptor: int) -> os.stat_result:
        status = real_fstat(descriptor)
        return os.stat_result((status.st_mode, status.st_ino, status.st_dev, status.st_nlink,
                               status.st_uid, status.st_gid, 5, 0, 0, 0))  # fmt: skip

    monkeypatch.setattr(os, "fstat", lying_fstat)
    with pytest.raises(oc.NotUnderstood):
        oc.read_private(fd, "file.json", 8)


def test_read_private_refuses_a_symlink(directory: tuple[Path, int]) -> None:
    path, fd = directory
    (path / "real.json").write_bytes(b"[]")
    (path / "link.json").symlink_to("real.json")
    with pytest.raises(oc.Refused):
        oc.read_private(fd, "link.json", 100)
    (path / "dangling.json").symlink_to("nowhere.json")
    with pytest.raises(oc.Refused):
        oc.read_private(fd, "dangling.json", 100)


def test_read_private_refuses_a_fifo_without_blocking(directory: tuple[Path, int]) -> None:
    path, fd = directory
    os.mkfifo(path / "fifo.json", 0o600)
    outcome: list[BaseException | None] = []

    def attempt() -> None:
        try:
            oc.read_private(fd, "fifo.json", 100)
            outcome.append(None)
        except BaseException as error:  # noqa: BLE001 - recorded and asserted below
            outcome.append(error)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(5)
    if worker.is_alive():
        release = os.open(path / "fifo.json", os.O_WRONLY | os.O_NONBLOCK)
        worker.join(5)
        os.close(release)
        pytest.fail("read_private blocked on a FIFO")
    assert isinstance(outcome[0], oc.Refused)


def test_read_private_refuses_a_directory(directory: tuple[Path, int]) -> None:
    path, fd = directory
    (path / "sub.json").mkdir()
    with pytest.raises(oc.Refused):
        oc.read_private(fd, "sub.json", 100)


def test_read_private_refuses_a_foreign_owner(directory: tuple[Path, int]) -> None:
    path, fd = directory
    (path / "file.json").write_bytes(b"[]")
    with pytest.raises(oc.Refused):
        oc.read_private(fd, "file.json", 100, euid=os.geteuid() + 1)
    assert oc.read_private(fd, "file.json", 100, euid=os.geteuid()) is not None


def test_refusal_and_not_understood_are_distinct_errors() -> None:
    assert not issubclass(oc.Refused, oc.NotUnderstood)
    assert not issubclass(oc.NotUnderstood, oc.Refused)
    for error in (oc.Refused, oc.NotUnderstood, oc.ChangedOnDisk, oc.LockTimeout):
        assert issubclass(error, oc.CredentialsError)


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "../x", "a\x00b"])
def test_entry_names_must_be_plain(directory: tuple[Path, int], name: str) -> None:
    _, fd = directory
    with pytest.raises(oc.Refused):
        oc.read_private(fd, name, 10)
    with pytest.raises(oc.Refused):
        oc.write_atomic(fd, name, b"x", None)


@pytest.mark.parametrize("loose", [0o644, 0o640, 0o666, 0o604, 0o660, 0o700])
def test_loose_mode_is_fixed_without_rewriting(directory: tuple[Path, int], loose: int) -> None:
    path, fd = directory
    target = path / "file.json"
    target.write_bytes(b"[]\n")
    target.chmod(loose)
    os.utime(target, ns=(1_000_000_000, 2_000_000_000))
    result = oc.read_private(fd, "file.json", 100)
    assert result is not None
    assert result.data == b"[]\n"
    assert mode_of(target) == 0o600
    assert target.stat().st_mtime_ns == 2_000_000_000
    assert result.mtime_ns == 2_000_000_000


def test_a_stricter_mode_is_left_alone(directory: tuple[Path, int]) -> None:
    path, fd = directory
    target = path / "file.json"
    target.write_bytes(b"[]")
    target.chmod(0o400)
    assert oc.read_private(fd, "file.json", 100) is not None
    assert mode_of(target) == 0o400


def test_open_dir_refuses_symlinks_and_files(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to("real")
    (tmp_path / "file").write_bytes(b"")
    for name in ("link", "file"):
        with pytest.raises(oc.Refused):
            oc.open_dir(tmp_path / name)
    with pytest.raises(FileNotFoundError):
        oc.open_dir(tmp_path / "absent")
    fd = oc.open_dir(tmp_path / "real")
    os.close(fd)


def test_open_dir_follows_symlinks_in_parent_components(tmp_path: Path) -> None:
    (tmp_path / "parent" / "child").mkdir(parents=True)
    (tmp_path / "alias").symlink_to("parent")
    os.close(oc.open_dir(tmp_path / "alias" / "child"))


def listing(path: Path) -> list[str]:
    return sorted(entry.name for entry in path.iterdir())


def test_write_atomic_creates_and_replaces_with_mode_0600(directory: tuple[Path, int]) -> None:
    path, fd = directory
    old = token("old").encode()
    new = token("new").encode() * 5000
    oc.write_atomic(fd, "file.json", old, None)
    assert (path / "file.json").read_bytes() == old
    assert mode_of(path / "file.json") == 0o600
    (path / "file.json").chmod(0o644)
    oc.write_atomic(fd, "file.json", new, old)
    assert (path / "file.json").read_bytes() == new
    assert mode_of(path / "file.json") == 0o600
    assert listing(path) == ["file.json"]


def test_write_atomic_temp_is_private_from_its_creation(
    directory: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fd = directory
    modes: list[int] = []
    real = oc.ops.open

    def spy(name: str, flags: int, mode: int, dir_fd: int) -> int:
        descriptor = real(name, flags, mode, dir_fd)
        if name.endswith(".tmp"):
            modes.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
        return descriptor

    monkeypatch.setattr(oc.ops, "open", spy)
    previous = os.umask(0)
    try:
        oc.write_atomic(fd, "file.json", b"[]", None)
    finally:
        os.umask(previous)
    assert modes == [0o600]


def test_write_atomic_never_reuses_an_existing_temp_name(
    directory: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, fd = directory
    monkeypatch.setattr(os, "urandom", lambda count: b"\x01" * count)
    squatter = path / f".file.json.{'01' * 8}.tmp"
    squatter.write_bytes(b"not ours")
    with pytest.raises(FileExistsError):
        oc.write_atomic(fd, "file.json", b"[]", None)
    assert squatter.read_bytes() == b"not ours"
    assert listing(path) == [squatter.name]


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_an_interrupt_still_removes_the_own_temp(
    directory: tuple[Path, int], monkeypatch: pytest.MonkeyPatch, error: type[BaseException]
) -> None:
    path, fd = directory

    def interrupt(*_args: object) -> None:
        raise error

    monkeypatch.setattr(oc.ops, "fsync_file", interrupt)
    with pytest.raises(error):
        oc.write_atomic(fd, "file.json", b"[]", None)
    assert listing(path) == []


def test_write_atomic_survives_a_restrictive_umask(directory: tuple[Path, int]) -> None:
    path, fd = directory
    previous = os.umask(0o277)
    try:
        oc.write_atomic(fd, "file.json", b"[]", None)
    finally:
        os.umask(previous)
    assert mode_of(path / "file.json") == 0o600


def test_write_atomic_loops_over_partial_writes(
    directory: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, fd = directory
    real = oc.ops.write
    monkeypatch.setattr(oc.ops, "write", lambda descriptor, data: real(descriptor, data[:3]))
    content = token("partial").encode() * 7
    oc.write_atomic(fd, "file.json", content, None)
    assert (path / "file.json").read_bytes() == content


def test_write_atomic_stalled_write_fails_and_cleans(
    directory: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, fd = directory
    calls: list[int] = []

    def stalled(descriptor: int, data: memoryview) -> int:
        calls.append(descriptor)
        if len(calls) > 100:
            raise AssertionError("the write loop does not stop on a stalled write")
        return 0

    monkeypatch.setattr(oc.ops, "write", stalled)
    with pytest.raises(OSError):
        oc.write_atomic(fd, "file.json", b"data", None)
    assert listing(path) == []


def test_compare_before_rename_mismatch_leaves_destination_and_removes_temp(
    directory: tuple[Path, int],
) -> None:
    path, fd = directory
    (path / "file.json").write_bytes(b"theirs")
    for expected in (b"mine", None, b"their", b"theirsx"):
        with pytest.raises(oc.ChangedOnDisk):
            oc.write_atomic(fd, "file.json", b"new", expected)
    assert (path / "file.json").read_bytes() == b"theirs"
    assert listing(path) == ["file.json"]
    with pytest.raises(oc.ChangedOnDisk):
        oc.write_atomic(fd, "absent.json", b"new", b"expected")
    assert listing(path) == ["file.json"]
    oc.write_atomic(fd, "file.json", b"new", b"theirs")
    assert (path / "file.json").read_bytes() == b"new"


def test_compare_before_rename_catches_a_change_during_the_write(
    directory: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, fd = directory
    (path / "file.json").write_bytes(b"before")
    real = oc.ops.fsync_file

    def racing(descriptor: int) -> None:
        real(descriptor)
        (path / "file.json").write_bytes(b"racer")

    monkeypatch.setattr(oc.ops, "fsync_file", racing)
    with pytest.raises(oc.ChangedOnDisk):
        oc.write_atomic(fd, "file.json", b"mine", b"before")
    assert (path / "file.json").read_bytes() == b"racer"
    assert listing(path) == ["file.json"]


def test_compare_before_rename_refuses_a_destination_turned_symlink(
    directory: tuple[Path, int],
) -> None:
    path, fd = directory
    (path / "elsewhere").write_bytes(b"x")
    (path / "file.json").symlink_to("elsewhere")
    with pytest.raises(oc.Refused):
        oc.write_atomic(fd, "file.json", b"new", b"x")
    assert (path / "elsewhere").read_bytes() == b"x"
    assert listing(path) == ["elsewhere", "file.json"]


class BoomError(Exception):
    pass


@pytest.mark.parametrize("failing", ["write", "fsync_file", "rename", "fsync_dir"])
def test_injected_failure_never_leaves_a_partial_destination(
    directory: tuple[Path, int], monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    path, fd = directory
    old, new = b"old content", b"new content that is longer"
    (path / "file.json").write_bytes(old)

    def fail(*_args: object) -> Any:
        raise BoomError

    monkeypatch.setattr(oc.ops, failing, fail)
    with pytest.raises(BoomError):
        oc.write_atomic(fd, "file.json", new, old)
    assert (path / "file.json").read_bytes() in {old, new}
    if failing != "fsync_dir":
        assert (path / "file.json").read_bytes() == old
    assert listing(path) == ["file.json"]


def test_a_partial_write_followed_by_failure_does_not_reach_the_destination(
    directory: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, fd = directory
    (path / "file.json").write_bytes(b"old")
    real = oc.ops.write
    calls: list[int] = []

    def half(descriptor: int, data: memoryview) -> int:
        calls.append(1)
        if len(calls) > 1:
            raise OSError(errno.ENOSPC, "full")
        return real(descriptor, data[:2])

    monkeypatch.setattr(oc.ops, "write", half)
    with pytest.raises(OSError):
        oc.write_atomic(fd, "file.json", b"longer new content", b"old")
    assert (path / "file.json").read_bytes() == b"old"
    assert listing(path) == ["file.json"]


def test_a_failing_temp_open_is_reported_and_leaves_nothing(
    directory: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, fd = directory

    def deny(*_args: object) -> int:
        raise PermissionError

    monkeypatch.setattr(oc.ops, "open", deny)
    with pytest.raises(PermissionError):
        oc.write_atomic(fd, "file.json", b"x", None)
    assert listing(path) == []


def test_a_failing_cleanup_does_not_mask_the_original_error(
    directory: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fd = directory

    def fail(*_args: object) -> Any:
        raise BoomError

    def cannot_unlink(*_args: object) -> None:
        raise PermissionError

    monkeypatch.setattr(oc.ops, "fsync_file", fail)
    monkeypatch.setattr(oc.ops, "unlink", cannot_unlink)
    with pytest.raises(BoomError):
        oc.write_atomic(fd, "file.json", b"x", None)


def record_events(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    events: list[str] = []
    for name in ("write", "fsync_file", "rename", "fsync_dir", "unlink"):
        original = getattr(oc.ops, name)

        def wrapper(*args: Any, _name: str = name, _original: Any = original) -> Any:
            events.append(_name)
            return _original(*args)

        monkeypatch.setattr(oc.ops, name, wrapper)
    return events


def test_event_order_write_fsync_file_rename_fsync_dir(
    directory: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fd = directory
    events = record_events(monkeypatch)
    oc.write_atomic(fd, "file.json", b"data", None)
    assert events == ["write", "fsync_file", "rename", "fsync_dir"]


def test_event_order_for_a_replace_and_for_a_mismatch(
    directory: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, fd = directory
    (path / "file.json").write_bytes(b"old")
    events = record_events(monkeypatch)
    oc.write_atomic(fd, "file.json", b"new", b"old")
    assert events == ["write", "fsync_file", "rename", "fsync_dir"]
    events.clear()
    with pytest.raises(oc.ChangedOnDisk):
        oc.write_atomic(fd, "file.json", b"newer", b"old")
    assert events == ["write", "fsync_file", "unlink"]


def test_event_order_a_failed_fsync_never_renames(
    directory: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fd = directory
    events = record_events(monkeypatch)

    def fail(*_args: object) -> Any:
        raise BoomError

    monkeypatch.setattr(oc.ops, "fsync_file", fail)
    with pytest.raises(BoomError):
        oc.write_atomic(fd, "file.json", b"x", None)
    assert "rename" not in events
    assert "fsync_dir" not in events
    assert events[-1] == "unlink"


def make_temp(path: Path, base: str, suffix: str, age: float) -> Path:
    temp = path / f".{base}.{suffix}.tmp"
    temp.write_bytes(b"x")
    stamp = time.time() - age
    os.utime(temp, (stamp, stamp))
    return temp


def test_stale_cleanup_removes_only_this_modules_old_temps(directory: tuple[Path, int]) -> None:
    path, fd = directory
    old_a = make_temp(path, "credentials.json", "0123456789abcdef", 600)
    old_b = make_temp(path, "credentials.removed.json", "fedcba9876543210", 600)
    fresh = make_temp(path, "credentials.json", "1111111111111111", 5)
    foreign_name = make_temp(path, "other.json", "0123456789abcdef", 600)
    short_random = make_temp(path, "credentials.json", "0123", 600)
    upper_random = make_temp(path, "credentials.json", "0123456789ABCDEF", 600)
    wildcard = make_temp(path, "credentialsXjson", "0123456789abcdef", 600)
    no_dot = path / "credentials.json.0123456789abcdef.tmp"
    no_dot.write_bytes(b"x")
    keep = path / "credentials.json"
    keep.write_bytes(b"[]")
    other_suffix = path / ".credentials.json.0123456789abcdef.tmp.bak"
    other_suffix.write_bytes(b"x")
    os.utime(other_suffix, (1, 1))
    directory_like = path / ".credentials.json.aaaaaaaaaaaaaaaa.tmp"
    directory_like.mkdir()
    os.utime(directory_like, (1, 1))
    link = path / ".credentials.json.bbbbbbbbbbbbbbbb.tmp"
    link.symlink_to("credentials.json")
    removed = oc.remove_stale_own_temps(fd, ["credentials.json", "credentials.removed.json"], 300)
    assert sorted(removed) == sorted([old_a.name, old_b.name])
    survivors = set(listing(path))
    assert survivors == {
        fresh.name,
        foreign_name.name,
        short_random.name,
        upper_random.name,
        wildcard.name,
        no_dot.name,
        keep.name,
        other_suffix.name,
        directory_like.name,
        link.name,
    }
    assert keep.read_bytes() == b"[]"


def test_stale_cleanup_respects_the_age_threshold_and_the_names(
    directory: tuple[Path, int],
) -> None:
    path, fd = directory
    temp = make_temp(path, "credentials.json", "0123456789abcdef", 100)
    assert oc.remove_stale_own_temps(fd, ["credentials.json"], 1000) == []
    assert oc.remove_stale_own_temps(fd, [], 0) == []
    assert oc.remove_stale_own_temps(fd, ["credentials.json.x"], 0) == []
    assert temp.exists()
    assert oc.remove_stale_own_temps(fd, ["credentials.json"], 50) == [temp.name]


def test_stale_cleanup_leaves_foreign_owned_temps(directory: tuple[Path, int]) -> None:
    path, fd = directory
    temp = make_temp(path, "credentials.json", "0123456789abcdef", 600)
    assert oc.remove_stale_own_temps(fd, ["credentials.json"], 0, euid=os.geteuid() + 1) == []
    assert temp.exists()


def test_stale_cleanup_matches_the_temp_names_the_writer_creates(
    directory: tuple[Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, fd = directory
    real = oc.ops.rename

    def abandon(*_args: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(oc.ops, "rename", abandon)
    monkeypatch.setattr(oc.ops, "unlink", lambda *_args: None)
    with pytest.raises(KeyboardInterrupt):
        oc.write_atomic(fd, "credentials.json", b"x", None)
    monkeypatch.setattr(oc.ops, "rename", real)
    assert len(listing(path)) == 1
    leftover = path / listing(path)[0]
    stamp = time.time() - 600
    os.utime(leftover, (stamp, stamp))
    monkeypatch.undo()
    assert oc.remove_stale_own_temps(fd, ["credentials.json"], 300) == [leftover.name]


# ---------------------------------------------------------------------------------------------
# Carrier lock
# ---------------------------------------------------------------------------------------------

LOADER = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("oc_by_path", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules["oc_by_path"] = module
spec.loader.exec_module(module)
"""

HOLDER = (
    LOADER
    + """
with module.carrier_lock(sys.argv[2]):
    print("locked", flush=True)
    sys.stdin.readline()
"""
)


def start_holder(path: Path) -> subprocess.Popen[str]:
    holder = subprocess.Popen(
        [sys.executable, "-I", "-c", HOLDER, str(MODULE_PATH), str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "locked"
    return holder


def stop_holder(holder: subprocess.Popen[str]) -> None:
    assert holder.stdin is not None
    holder.stdin.write("\n")
    holder.stdin.close()
    assert holder.wait(timeout=20) == 0
    assert holder.stdout is not None
    holder.stdout.close()


@pytest.fixture
def carrier_with_alias(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "parent" / "carrier").mkdir(parents=True)
    (tmp_path / "alias").symlink_to("parent")
    return tmp_path / "parent" / "carrier", tmp_path / "alias" / "carrier"


def test_two_processes_exclude_each_other_through_different_paths(
    carrier_with_alias: tuple[Path, Path],
) -> None:
    real, alias = carrier_with_alias
    assert real.resolve() == alias.resolve()
    assert str(real) != str(alias)
    holder = start_holder(real)
    try:
        started = time.monotonic()
        with pytest.raises(oc.LockTimeout), oc.carrier_lock(alias, timeout=0.4):
            pytest.fail("lock was granted while another process holds it")
        assert time.monotonic() - started >= 0.35
        with pytest.raises(oc.LockTimeout), oc.carrier_lock(real, timeout=0):
            pytest.fail("lock was granted while another process holds it")
    finally:
        stop_holder(holder)
    with oc.carrier_lock(alias, timeout=5):
        pass


def test_a_waiting_process_gets_the_lock_when_the_holder_exits(
    carrier_with_alias: tuple[Path, Path],
) -> None:
    real, alias = carrier_with_alias
    holder = start_holder(real)
    try:
        assert holder.stdin is not None
        holder.stdin.write("\n")
        holder.stdin.flush()
        with oc.carrier_lock(alias, timeout=20):
            pass
    finally:
        holder.wait(timeout=20)
        assert holder.stdout is not None
        holder.stdout.close()


def test_the_lock_is_not_reentrant_in_one_process(carrier_with_alias: tuple[Path, Path]) -> None:
    real, alias = carrier_with_alias
    with oc.carrier_lock(real) as lease:
        for again in (real, alias):
            started = time.monotonic()
            with pytest.raises(oc.LockReentered), oc.carrier_lock(again, timeout=30):
                pytest.fail("re-entrant acquisition succeeded")
            assert time.monotonic() - started < 2
        assert lease.held
        with pytest.raises(oc.LockReentered), oc.carrier_lock(real):
            pytest.fail("a failed attempt must not release the first lock")
    with oc.carrier_lock(real, timeout=1):
        pass


def test_the_lock_is_released_on_exit_and_on_error(carrier_with_alias: tuple[Path, Path]) -> None:
    real, alias = carrier_with_alias
    with oc.carrier_lock(real) as lease:
        assert lease.held
    assert not lease.held
    with pytest.raises(BoomError), oc.carrier_lock(real):
        raise BoomError
    holder = start_holder(alias)
    stop_holder(holder)
    with oc.carrier_lock(real, timeout=1):
        pass


def test_two_different_directories_do_not_exclude_each_other(tmp_path: Path) -> None:
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    with oc.carrier_lock(tmp_path / "one"), oc.carrier_lock(tmp_path / "two"):
        pass


def test_a_failed_acquisition_does_not_block_a_later_one(tmp_path: Path) -> None:
    (tmp_path / "carrier").mkdir()
    holder = start_holder(tmp_path / "carrier")
    try:
        with pytest.raises(oc.LockTimeout), oc.carrier_lock(tmp_path / "carrier", timeout=0.1):
            pytest.fail("granted")
    finally:
        stop_holder(holder)
    with oc.carrier_lock(tmp_path / "carrier", timeout=1):
        pass


def test_the_lock_creates_no_file_and_the_lease_exposes_the_directory(
    carrier_with_alias: tuple[Path, Path],
) -> None:
    real, _ = carrier_with_alias
    with oc.carrier_lock(real) as lease:
        oc.write_atomic(lease.dir_fd, "file.json", b"[]", None)
        assert lease.inode == real.stat().st_ino
        assert listing(real) == ["file.json"]
    with pytest.raises(oc.CredentialsError):
        _ = lease.dir_fd
    (real / "file.json").unlink()
    with oc.carrier_lock(real):
        assert listing(real) == []
    assert listing(real) == []


def test_the_lock_refuses_a_symlinked_or_missing_directory(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to("real")
    with pytest.raises(oc.Refused), oc.carrier_lock(tmp_path / "link"):
        pytest.fail("granted")
    with pytest.raises(FileNotFoundError), oc.carrier_lock(tmp_path / "absent"):
        pytest.fail("granted")


# ---------------------------------------------------------------------------------------------
# Script compatibility and module guards
# ---------------------------------------------------------------------------------------------

SYSTEM_PYTHON = Path("/usr/bin/python3")


@pytest.mark.skipif(not SYSTEM_PYTHON.exists(), reason="no system python3")
def test_the_module_runs_as_a_standalone_script_on_the_system_python(tmp_path: Path) -> None:
    document = [entry_dict("oauth", "cred_script"), entry_dict("key", "cred_two")]
    script = (
        LOADER
        + """
import json
entries = module.parse_entries(sys.stdin.buffer.read())
result = module.merge([entries], [["cred_two"]])
print(json.dumps({
    "version": list(sys.version_info[:2]),
    "fingerprints": {k: v.fingerprint for k, v in result.entries.items()},
    "conflicts": list(result.conflicts),
}))
"""
    )
    completed = subprocess.run(
        [str(SYSTEM_PYTHON), "-I", "-c", script, str(MODULE_PATH)],
        input=raw(document),
        capture_output=True,
        cwd=tmp_path,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    answer = json.loads(completed.stdout)
    expected = oc.parse_entries(raw(document))[0]
    assert answer["fingerprints"] == {"cred_script": expected.fingerprint}
    assert answer["conflicts"] == []


def module_tree() -> ast.Module:
    return ast.parse(MODULE_PATH.read_text(), feature_version=(3, 11))


def test_the_module_parses_with_python_311_grammar() -> None:
    tree = module_tree()
    assert not [node for node in ast.walk(tree) if type(node).__name__ == "TypeAlias"]


def test_the_module_imports_only_the_standard_library() -> None:
    allowed = set(sys.stdlib_module_names)
    for node in ast.walk(module_tree()):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            modules = [node.module or ""]
        else:
            continue
        for module in modules:
            assert module.split(".")[0] in allowed, module


def test_no_exception_message_formats_a_value_variable() -> None:
    """Every `raise` of the module's errors builds its message from literals and ids only."""
    allowed_names = {"where", "what", "name", "index", "identity", "entry"}
    for node in ast.walk(module_tree()):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        for argument in node.exc.args:
            if isinstance(argument, ast.JoinedStr):
                used = {
                    name.id
                    for part in argument.values
                    if isinstance(part, ast.FormattedValue)
                    for name in ast.walk(part)
                    if isinstance(name, ast.Name)
                }
                assert used <= allowed_names, (node.lineno, used)


def test_empty_destination_is_not_absent(directory: tuple[Path, int]) -> None:
    path, fd = directory
    (path / "file.json").write_bytes(b"")
    with pytest.raises(oc.ChangedOnDisk):
        oc.write_atomic(fd, "file.json", b"new", None)
    assert (path / "file.json").read_bytes() == b""
    oc.write_atomic(fd, "file.json", b"new", b"")
    assert (path / "file.json").read_bytes() == b"new"


def test_invalid_utf8_inside_a_string_is_refused() -> None:
    text = json.dumps(entry_dict("key")).replace("label cred_one", "label \x00")
    payload = f"[{text}]".encode().replace(b"\\u0000", b"\xff").replace(b"\x00", b"\xff")
    assert b"\xff" in payload
    with pytest.raises(oc.NotUnderstood):
        oc.parse_entries(payload)


def test_materialize_keeps_non_ascii_unescaped() -> None:
    document = entry_dict("key")
    document["label"] = "Zugang \u00e4"
    data = oc.materialize_entries([oc.validate_entry(document)], {})
    assert "\u00e4".encode() in data
    assert b"\\u00e4" not in data
