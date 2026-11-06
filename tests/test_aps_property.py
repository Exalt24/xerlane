"""Property test for the APS loader: many seeded random worlds, each a sequence of loads into several tenants, models and views, with edits, bad items, duplicates,
replays and empty payloads mixed together. The loader is stateful (what it does depends on what is stored), so single examples cannot reach the cases where two
dimensions cross, for instance a duplicate pair appearing for an object another tenant also holds.

Rules checked after EVERY step against an oracle written separately from xerlane.aps (it only knows the shapes this generator makes):
  1. a view's stored rows equal the valid, non-duplicated items of the last payload sent to it, field by field
  2. the counts a load reports equal the difference between the previous and the new expected rows
  3. sending the same payload again writes nothing at all
  4. no other tenant, model or view changes
  5. replaying the change log of a view from the start rebuilds exactly the stored object ids and content hashes
  6. a payload with no valid object is refused when rows are stored, and then nothing changes
  7. the quarantine holds exactly the bad items of each distinct payload, once
"""
from __future__ import annotations

import hashlib
import json
import random

import pytest

from aps_dbfix import aadmin, aconn, aps_admin_dsn, aps_app_dsn, aps_clean, table_state  # noqa: F401  (fixtures)
from xerlane.aps import ApsPayloadError, load_view
from xerlane.db import tenant_tx

pytestmark = pytest.mark.db

LIMIT = 2000
TENANTS = ("acme", "globex")
URNS = ("bW9kZWwtYQ", "bW9kZWwtYg")
GUIDS = ("g-3d", "g-2d")
SLOTS = [(t, u, g) for t in TENANTS for u in URNS for g in GUIDS]


def props_size(props) -> int:
    return len(json.dumps(props, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def oracle(payload):
    """(expected rows {id: (external_id, name, props)}, number of bad items) for a payload made by this generator."""
    ok: dict[int, list] = {}
    bad = 0
    for item in payload:
        valid = (isinstance(item, dict) and isinstance(item.get("objectid"), int) and not isinstance(item["objectid"], bool) and item["objectid"] >= 0
                 and all(item.get(k) is None or isinstance(item.get(k), str) for k in ("name", "externalId"))
                 and (item.get("properties") is None or isinstance(item["properties"], dict))
                 and props_size(item.get("properties") or {}) <= LIMIT)
        if valid:
            ok.setdefault(item["objectid"], []).append(item)
        else:
            bad += 1
    rows = {}
    for oid, items in ok.items():
        if len(items) == 1:
            rows[oid] = (items[0].get("externalId"), items[0].get("name"), items[0].get("properties") or {})
        else:
            bad += len(items)
    return rows, bad


def good_item(rng: random.Random, oid: int) -> dict:
    props = {"Name": rng.choice(["Wall", "Slab", "Door"]), "Rev": rng.randint(1, 3), "Flags": {"a": rng.random() < 0.5}}
    if rng.random() < 0.5:
        props["Schedule"] = {"task_code": f"T{rng.randint(1, 4)}"}
    return {"objectid": oid, "name": props["Name"], "externalId": f"e{oid}-{rng.randint(1, 2)}", "properties": props}


def edit(rng: random.Random, payload: list) -> list:
    payload = json.loads(json.dumps(payload))
    for _ in range(rng.randint(1, 4)):
        op = rng.choice(["add", "add", "drop", "rename", "prop", "bad", "dup", "unbad", "shuffle", "reid"])
        goods = [i for i, it in enumerate(payload) if isinstance(it, dict) and isinstance(it.get("objectid"), int)]
        if op == "add":
            payload.append(good_item(rng, rng.randint(1, 12)))
        elif op == "drop" and payload:
            payload.pop(rng.randrange(len(payload)))
        elif op == "rename" and goods:
            payload[rng.choice(goods)]["name"] = rng.choice(["A", "B", None])
        elif op == "prop" and goods:
            it = payload[rng.choice(goods)]
            if not isinstance(it.get("properties"), dict):
                it["properties"] = {}
            it["properties"]["Rev"] = rng.randint(1, 9)
        elif op == "bad":
            payload.append(rng.choice([{"name": "no id"}, {"objectid": "7"}, {"objectid": 8, "name": 5}, {"objectid": 9, "properties": [1]}, "junk", None,
                                       {"objectid": 10, "properties": {"blob": "x" * (LIMIT + 5)}}, {"objectid": -1}, {"objectid": True}]))
        elif op == "dup" and goods:
            payload.append(json.loads(json.dumps(payload[rng.choice(goods)])))
        elif op == "unbad":
            payload = [it for it in payload if isinstance(it, dict) and "objectid" in it]
        elif op == "shuffle":
            rng.shuffle(payload)
        elif op == "reid" and goods:
            payload[rng.choice(goods)]["objectid"] = rng.randint(1, 12)
    return payload


def stored(conn, tenant, urn, guid):
    with tenant_tx(conn, tenant) as cur:
        cur.execute("select object_id, external_id, name, properties, content_hash from aps_object where model_urn = %s and view_guid = %s order by 1", (urn, guid))
        return cur.fetchall()


def replay_log(admin, tenant, urn, guid):
    state: dict[int, str] = {}
    events = admin.execute("select kind, object_id, old_hash, new_hash from aps_change_event where tenant_id = %s and model_urn = %s and view_guid = %s order by event_id",
                           (tenant, urn, guid)).fetchall()
    for kind, oid, old, new in events:
        if kind == "OBJECT_ADDED":
            assert oid not in state and old is None
            state[oid] = new
        elif kind == "OBJECT_CHANGED":
            assert state.get(oid) == old and new != old
            state[oid] = new
        else:
            assert state.pop(oid, None) == old and new is None
    return state


@pytest.mark.parametrize("seed", range(10))
def test_random_worlds_keep_every_rule(seed, aconn, aadmin):
    rng = random.Random(seed)
    sent: dict[tuple, list] = {s: [] for s in SLOTS}                    # last payload per slot
    expect: dict[tuple, dict] = {s: {} for s in SLOTS}
    quarantine_keys: dict[tuple, int] = {}                              # (slot, payload digest) -> bad item count
    for step in range(14):
        slot = rng.choice(SLOTS)
        tenant, urn, guid = slot
        action = rng.choice(["edit", "edit", "edit", "replay", "empty", "fresh"])
        if action == "replay":
            payload = sent[slot]
        elif action == "empty":
            payload = []
        elif action == "fresh":
            payload = [good_item(rng, i) for i in rng.sample(range(1, 13), rng.randint(1, 6))]
        else:
            payload = edit(rng, sent[slot])
        want, bad = oracle(payload)
        before_other = {s: stored(aconn, s[0], s[1], s[2]) for s in SLOTS if s != slot}
        before_state = table_state(aadmin, tenant)
        prev = expect[slot]
        refused = bool(prev) and not want
        if refused:                                                      # rule 6
            with pytest.raises(ApsPayloadError):
                load_view(aconn, tenant, urn, guid, payload, max_properties_bytes=LIMIT)
            assert table_state(aadmin, tenant) == before_state, f"seed {seed} step {step}: a refused load wrote something"
            continue
        res = load_view(aconn, tenant, urn, guid, payload, max_properties_bytes=LIMIT)
        sent[slot], expect[slot] = payload, want
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        quarantine_keys.setdefault((slot, digest), bad)
        ctx = f"seed {seed} step {step} {action} {slot}"
        rows = stored(aconn, tenant, urn, guid)                          # rule 1
        assert {r[0]: (r[1], r[2], r[3]) for r in rows} == {k: v for k, v in want.items()}, ctx
        added, removed = want.keys() - prev.keys(), prev.keys() - want.keys()          # rule 2
        changed = {k for k in want.keys() & prev.keys() if want[k] != prev[k]}
        assert (res.added, res.removed, res.changed, res.unchanged) == (len(added), len(removed), len(changed), len(want) - len(added) - len(changed)), ctx
        assert res.quarantined == bad, ctx
        for s, rows_before in before_other.items():                      # rule 4
            assert stored(aconn, s[0], s[1], s[2]) == rows_before, f"{ctx}: slot {s} changed"
        mid = table_state(aadmin, tenant)                                # rule 3
        again = load_view(aconn, tenant, urn, guid, payload, max_properties_bytes=LIMIT)
        assert again.status == "unchanged" and again.run_id is None and again.quarantined_new == 0, ctx
        assert table_state(aadmin, tenant) == mid, ctx
        assert replay_log(aadmin, tenant, urn, guid) == {r[0]: r[4] for r in rows}, ctx          # rule 5
    for slot in SLOTS:                                                   # rule 7, once at the end
        n = aadmin.execute("select count(*) from aps_quarantine where tenant_id = %s and model_urn = %s and view_guid = %s", slot).fetchone()[0]
        assert n == sum(b for (s, _), b in quarantine_keys.items() if s == slot), f"seed {seed}: quarantine rows of {slot}"
