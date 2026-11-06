"""Mapping of an APS properties collection to rows and quarantine items. Pure functions: no database, no network."""
from __future__ import annotations

import dataclasses
import json

import pytest

from aps_stand_in import GUID_3D, URN, obj, sample_collection
from xerlane.aps import (MAX_PROPERTIES_BYTES, MAX_RAW_BYTES, MAX_TEXT_CHARS, ApsReconcileReport, ModelView, ObjectRef, ObjectRow, Reject, TaskRef, content_hash, find_task_code,
                         map_collection, payload_digest)


def run(collection, **kw):
    return map_collection(URN, GUID_3D, collection, **kw)


def codes(mapped):
    return [(r.item_index, r.code) for r in mapped.rejects]


# ---- good rows -----------------------------------------------------------------------------------------------------------------------------------

def test_a_documented_entry_maps_to_one_row():
    m = run([{"objectid": 2, "name": "Model", "externalId": "z4u0zG8ViUOsqUzhb4TUiA", "properties": {"Name": "Model", "File Properties": {"Author": "ADSK"}}}])
    assert not m.rejects
    r = m.rows[0]
    assert (r.model_urn, r.view_guid, r.object_id, r.external_id, r.name) == (URN, GUID_3D, 2, "z4u0zG8ViUOsqUzhb4TUiA", "Model")
    assert r.properties == {"Name": "Model", "File Properties": {"Author": "ADSK"}}
    assert r.category is None and r.task_code is None
    assert len(r.content_hash) == 64


def test_sample_collection_maps_every_entry():
    m = run(sample_collection())
    assert [r.object_id for r in m.rows] == [1, 2, 3, 4] and not m.rejects
    assert [r.task_code for r in m.rows] == [None, "A1000", "A1010", "NOPE-1"]
    assert m.rows[1].category == "Revit Walls"


def test_missing_optional_fields_become_null_and_empty():
    r = run([{"objectid": 5}]).rows[0]
    assert (r.external_id, r.name, r.category, r.task_code, r.properties) == (None, None, None, None, {})
    assert run([{"objectid": 5, "properties": None}]).rows[0].properties == {}


@pytest.mark.parametrize("raw,want", [(0, 0), (7, 7), (7.0, 7), (2**63 - 1, 2**63 - 1)])
def test_object_ids_that_are_whole_numbers_are_accepted(raw, want):
    assert run([{"objectid": raw}]).rows[0].object_id == want


def test_content_hash_ignores_key_order_and_changes_with_every_stored_field():
    base = ("e", "n", "c", "t", {"a": 1, "b": {"x": 1, "y": 2}})
    h = content_hash(*base)
    assert h == content_hash("e", "n", "c", "t", {"b": {"y": 2, "x": 1}, "a": 1})
    variants = [("e2", "n", "c", "t", base[4]), ("e", "n2", "c", "t", base[4]), ("e", "n", "c2", "t", base[4]), ("e", "n", "c", "t2", base[4]),
                ("e", "n", "c", "t", {"a": 2, "b": base[4]["b"]})]
    assert len({h, *(content_hash(*v) for v in variants)}) == 6
    assert content_hash(None, None, None, None, {}) != content_hash("", None, None, None, {})


def test_payload_digest_is_stable_and_sees_a_change():
    a = sample_collection()
    assert payload_digest(a) == payload_digest(json.loads(json.dumps(a)))
    b = sample_collection()
    b[0]["name"] = "Other"
    assert payload_digest(a) != payload_digest(b)


# ---- category and task code ----------------------------------------------------------------------------------------------------------------------

def test_category_prefers_the_group_then_the_top_level_and_ignores_junk():
    assert run([obj(1, **{"__category__": {"Category": " Revit Walls "}})]).rows[0].category == "Revit Walls"
    assert run([obj(1, Category="Doors")]).rows[0].category == "Doors"
    assert run([obj(1, **{"__category__": {"Category": "A"}, "Category": "B"})]).rows[0].category == "A"
    assert run([obj(1, **{"__category__": {"Category": "  "}, "Category": "B"})]).rows[0].category == "B"
    assert run([obj(1, **{"__category__": "oops", "Category": 5})]).rows[0].category is None


def test_task_code_is_found_at_the_top_level_or_one_group_down():
    assert find_task_code({"task_code": "A1"}) == "A1"
    assert find_task_code({"Schedule": {"Task Code": " A2 "}}) == "A2"
    assert find_task_code({"Schedule": {"deeper": {"task_code": "A3"}}}) is None
    assert find_task_code({"Schedule": {"Activity ID": "A4"}}) is None
    assert find_task_code({"Schedule": {"Activity ID": "A4"}}, keys=("Activity ID",)) == "A4"
    assert find_task_code({"task_code": 1000}) == "1000"
    assert find_task_code({"task_code": True}) is None
    assert find_task_code({"task_code": 1.5}) is None
    assert find_task_code({"task_code": "  "}) is None
    assert find_task_code({"task_code": ""}) is None
    assert find_task_code({}) is None
    assert find_task_code({"x": "task_code"}) is None


def test_task_code_keys_can_be_set_on_the_mapping_call():
    c = [obj(1, Schedule={"Activity ID": "Z9"})]
    assert run(c).rows[0].task_code is None
    assert run(c, task_code_keys=("Activity ID",)).rows[0].task_code == "Z9"


# ---- quarantine reasons --------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("item,code", [
    ("just text", "MALFORMED_OBJECT"), (5, "MALFORMED_OBJECT"), (None, "MALFORMED_OBJECT"), ([1, 2], "MALFORMED_OBJECT"),
    ({"name": "x"}, "NO_OBJECT_ID"), ({"objectid": None, "name": "x"}, "NO_OBJECT_ID"),
    ({"objectid": "7"}, "BAD_OBJECT_ID"), ({"objectid": -1}, "BAD_OBJECT_ID"), ({"objectid": 1.5}, "BAD_OBJECT_ID"), ({"objectid": True}, "BAD_OBJECT_ID"),
    ({"objectid": 2**63}, "BAD_OBJECT_ID"), ({"objectid": [1]}, "BAD_OBJECT_ID"), ({"objectid": float("nan")}, "BAD_OBJECT_ID"),
    ({"objectid": 1, "name": 5}, "BAD_FIELD_TYPE"), ({"objectid": 1, "externalId": ["x"]}, "BAD_FIELD_TYPE"),
    ({"objectid": 1, "properties": "text"}, "BAD_PROPERTIES"), ({"objectid": 1, "properties": [1]}, "BAD_PROPERTIES"),
    ({"objectid": 1, "name": "a\x00b"}, "BAD_CHARACTER"), ({"objectid": 1, "properties": {"k": {"deep": ["a\x00"]}}}, "BAD_CHARACTER"),
    ({"objectid": 1, "properties": {"a\x00": 1}}, "BAD_CHARACTER"), ({"objectid": 1, "name": "lone \ud800 surrogate"}, "BAD_CHARACTER"),
    ({"objectid": 1, "name": "n" * (MAX_TEXT_CHARS + 1)}, "VALUE_TOO_LARGE"), ({"objectid": 1, "externalId": "e" * (MAX_TEXT_CHARS + 1)}, "VALUE_TOO_LARGE"),
    ({"objectid": 1, "properties": {"blob": "x" * MAX_PROPERTIES_BYTES}}, "VALUE_TOO_LARGE"),
])
def test_each_bad_item_is_set_aside_with_its_reason(item, code):
    m = run([obj(100), item, obj(101)])
    assert codes(m) == [(1, code)]
    assert [r.object_id for r in m.rows] == [100, 101]
    assert m.rejects[0].reason


def test_size_limits_are_inclusive():
    ok = {"objectid": 1, "name": "n" * MAX_TEXT_CHARS, "externalId": "e" * MAX_TEXT_CHARS}
    assert not run([ok]).rejects
    overhead = len(json.dumps({"k": ""}, separators=(",", ":")))
    fits = {"objectid": 1, "properties": {"k": "x" * (MAX_PROPERTIES_BYTES - overhead)}}
    assert not run([fits]).rejects
    too = {"objectid": 1, "properties": {"k": "x" * (MAX_PROPERTIES_BYTES - overhead + 1)}}
    assert codes(run([too])) == [(0, "VALUE_TOO_LARGE")]
    assert codes(run([fits], max_properties_bytes=MAX_PROPERTIES_BYTES - 1)) == [(0, "VALUE_TOO_LARGE")]


def test_size_is_counted_in_bytes_not_characters():
    item = {"objectid": 1, "properties": {"k": "é" * 40}}                       # 80 bytes of UTF-8, 40 characters
    assert not run([item], max_properties_bytes=100).rejects
    assert codes(run([item], max_properties_bytes=60)) == [(0, "VALUE_TOO_LARGE")]


def test_a_duplicate_objectid_sets_aside_every_copy_and_stores_none():
    c = [obj(1), obj(2, "first"), obj(3), obj(2, "second"), obj(2, "third")]
    m = run(c)
    assert [r.object_id for r in m.rows] == [1, 3]
    assert codes(m) == [(1, "DUP_OBJECT_ID"), (3, "DUP_OBJECT_ID"), (4, "DUP_OBJECT_ID")]
    assert "3 times" in m.rejects[0].reason


def test_an_invalid_entry_does_not_make_a_valid_one_a_duplicate():
    m = run([obj(2), {"objectid": 2, "name": 5}])
    assert [r.object_id for r in m.rows] == [2] and codes(m) == [(1, "BAD_FIELD_TYPE")]


def test_float_and_int_ids_that_are_equal_collide():
    m = run([obj(2), {"objectid": 2.0}])
    assert not m.rows and len(m.rejects) == 2


def test_nothing_is_lost_every_item_is_a_row_or_a_reject_once():
    c = sample_collection() + [{"name": "no id"}, obj(1), "junk"]
    m = run(c)
    assert len(m.rows) + len(m.rejects) == len(c)
    assert sorted(r.item_index for r in m.rejects) == [0, 4, 5, 6]            # the dup pair (0 and 5), the id-less entry, the junk


# ---- what the quarantine keeps -------------------------------------------------------------------------------------------------------------------

def test_a_small_bad_item_is_kept_whole():
    item = {"objectid": "7", "name": "x"}
    assert run([item]).rejects[0].raw == item


def test_a_huge_bad_item_is_kept_as_a_stub_with_its_size_and_id():
    item = {"objectid": 9, "properties": {"blob": "x" * (MAX_RAW_BYTES * 3)}, "name": None, "externalId": 5}
    raw = run([item]).rejects[0].raw
    assert raw["truncated"] is True and raw["objectid"] == 9 and raw["bytes"] > MAX_RAW_BYTES
    assert "blob" not in json.dumps(raw)


def test_raw_at_exactly_the_limit_is_kept_whole():
    pad = MAX_RAW_BYTES - len(json.dumps({"objectid": "7", "p": ""}, separators=(",", ":")))
    item = {"objectid": "7", "p": "x" * pad}
    assert len(json.dumps(item, separators=(",", ":"))) == MAX_RAW_BYTES
    assert run([item]).rejects[0].raw == item
    item["p"] += "x"
    assert run([item]).rejects[0].raw.get("truncated") is True


def test_an_item_with_unstorable_text_is_kept_as_a_stub_with_no_text():
    raw = run([{"objectid": 4, "name": "a\x00b"}]).rejects[0].raw
    assert raw == {"unstorable": True, "objectid": 4}
    raw = run([{"objectid": "4", "name": "a\x00b"}]).rejects[0].raw          # also bad for another reason first
    assert raw == {"unstorable": True}
    assert run(["a\x00b"]).rejects[0].raw == {"unstorable": True}


# ---- value types ---------------------------------------------------------------------------------------------------------------------------------

def test_value_types_are_immutable_so_they_can_be_shared_safely():
    samples = [run([obj(1)]).rows[0], run([{"name": "x"}]).rejects[0], ModelView("n", "3d", "g", False), ObjectRef("u", "g", 1, None, None, None), TaskRef(1, 1, "A", None)]
    for sample in samples:
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(sample, dataclasses.fields(sample)[0].name, "changed")
    assert isinstance(samples[0], ObjectRow) and isinstance(samples[1], Reject)


@pytest.mark.parametrize("counts", [(1, 0, 0), (0, 1, 0), (0, 0, 1), (3, 2, 1)])
def test_a_report_is_clean_only_when_all_three_mismatch_counts_are_zero(counts):
    no_code, no_task, orphans = counts
    rep = ApsReconcileReport(objects=5, matched_objects=5 - no_code - no_task, no_task_code=no_code, no_task=no_task, tasks=5, tasks_without_object=orphans)
    assert not rep.clean
    assert ApsReconcileReport(5, 5, 0, 0, 5, 0).clean
