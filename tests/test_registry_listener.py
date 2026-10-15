"""Unit tests for RegistryListener (registry relay + SCD2 change-detection helpers)."""
import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.timescaledb_recorder import registry_listener as registry_listener_module
from custom_components.timescaledb_recorder.registry_listener import RegistryListener, _to_json_safe
from custom_components.timescaledb_recorder.persistent_queue import PersistentQueue


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_meta_queue():
    """Return a PersistentQueue mock with put_async as AsyncMock."""
    q = MagicMock(spec=PersistentQueue)
    q.put_async = AsyncMock()
    return q


@pytest.fixture
def listener(hass, mock_meta_queue):
    """Return a RegistryListener wired to mock_meta_queue."""
    return RegistryListener(hass=hass, meta_queue=mock_meta_queue)


# ---------------------------------------------------------------------------
# _to_json_safe helper
# ---------------------------------------------------------------------------

def test_to_json_safe_converts_datetime_to_iso():
    """datetime values in params must be converted to ISO-8601 strings."""
    t = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
    out = _to_json_safe(("x.y", t, None, [1, 2]))
    assert out is not None
    assert isinstance(out[1], str)
    assert out[1] == "2026-04-21T12:00:00+00:00"
    assert out[0] == "x.y"
    assert out[2] is None
    assert out[3] == [1, 2]


def test_to_json_safe_none_passthrough():
    """_to_json_safe(None) must return None (remove-action path)."""
    assert _to_json_safe(None) is None


def test_to_json_safe_non_datetime_values_unchanged():
    """Non-datetime types (str, int, list, None elements) must pass through unchanged."""
    out = _to_json_safe(("hello", 42, None, ["a", "b"]))
    assert out == ["hello", 42, None, ["a", "b"]]


# ---------------------------------------------------------------------------
# Startup: no DISCARD window, snapshot in event order (issue #19)
# ---------------------------------------------------------------------------

def test_events_are_enqueued_without_any_enable_step(listener, mock_entity_registry):
    """A registry change during startup must be recorded, not dropped."""
    assert not hasattr(listener, "enable")
    listener._entity_reg = mock_entity_registry
    event = MagicMock()
    event.data = {"action": "update", "entity_id": "sensor.living_room_temp",
                  "old_entity_id": None}
    listener._handle_entity_registry_updated(event)
    assert [i["action"] for i in listener._buffer] == ["update"]


@pytest.fixture
def snapshot_listener(listener, mock_area_registry, mock_label_registry,
                      mock_entity_registry, mock_device_registry):
    listener._area_reg = mock_area_registry
    listener._label_reg = mock_label_registry
    listener._entity_reg = mock_entity_registry
    listener._device_reg = mock_device_registry
    return listener


def test_snapshot_items_cover_every_registry_in_dependency_order(snapshot_listener):
    snapshot_listener.enqueue_snapshot()

    items = [i for i in snapshot_listener._buffer if i["action"] == "snapshot"]
    assert [(i["registry"], i["registry_id"]) for i in items] == [
        ("area", "area-living-room"), ("area", "area-kitchen"),
        ("label", "indoor"), ("label", "climate"),
        ("entity", "sensor.living_room_temp"), ("entity", "switch.plug_kettle_power"),
        ("device", "device-001"), ("device", "device-002"), ("device", "device-003"),
    ]
    assert snapshot_listener._buffer_ready.is_set()


def test_snapshot_reads_devices_without_deprecated_access(snapshot_listener):
    """Iterating `devices` is HA's supported API; mapping access and reading
    manufacturer/model off a ChildDeviceEntry both report deprecations."""
    from homeassistant.helpers import device_registry as dr

    with patch.object(dr, "report_usage") as report:
        snapshot_listener.enqueue_snapshot()

    report.assert_not_called()
    child = next(i for i in snapshot_listener._buffer if i["registry_id"] == "device-003")
    assert child["params"][1:4] == ["Kettle Plug USB Port", None, None]


def test_snapshot_ends_with_one_sweep_per_registry(snapshot_listener):
    """Sweeps follow every snapshot item and carry the live ids, JSON-safe."""
    import json

    snapshot_listener.enqueue_snapshot()

    buffer = snapshot_listener._buffer
    sweeps = buffer[-4:]
    assert all(i["action"] == "snapshot" for i in buffer[:-4])
    assert [(i["action"], i["registry"], i["live_ids"]) for i in sweeps] == [
        ("sweep", "area", ["area-kitchen", "area-living-room"]),
        ("sweep", "label", ["climate", "indoor"]),
        ("sweep", "entity", ["sensor.living_room_temp", "switch.plug_kettle_power"]),
        ("sweep", "device", ["device-001", "device-002", "device-003"]),
    ]
    assert {i["enqueued_at"] for i in sweeps} == {buffer[0]["enqueued_at"]}
    assert json.loads(json.dumps(sweeps)) == sweeps


def test_snapshot_items_share_one_stamp(snapshot_listener):
    """valid_from and enqueued_at are the moment the registries were read."""
    snapshot_listener.enqueue_snapshot()
    from custom_components.timescaledb_recorder.meta_worker import _VALID_FROM_INDEX

    stamps = {i["params"][_VALID_FROM_INDEX[i["registry"]]] for i in snapshot_listener._buffer
              if i["action"] == "snapshot"}
    assert stamps == {snapshot_listener._buffer[0]["enqueued_at"]}


def test_snapshot_lands_between_earlier_and_later_events(snapshot_listener):
    """Queue order must equal stamp order across events and the snapshot.

    The snapshot goes through the same buffer as events. Written to the queue
    directly, it could overtake an earlier event still waiting for the drain
    task, and the worker would then refuse that event as out of order.
    """
    event = MagicMock()
    event.data = {"action": "update", "entity_id": "sensor.living_room_temp",
                  "old_entity_id": None}
    snapshot_listener._handle_entity_registry_updated(event)
    snapshot_listener.enqueue_snapshot()
    snapshot_listener._handle_entity_registry_updated(event)

    actions = [i["action"] for i in snapshot_listener._buffer]
    assert actions[0] == "update" and actions[-1] == "update"
    assert set(actions[1:-1]) == {"snapshot", "sweep"}
    stamps = [datetime.fromisoformat(i["enqueued_at"]) for i in snapshot_listener._buffer]
    assert stamps == sorted(stamps)


def test_extra_carries_created_at_the_worker_can_read():
    """The first-version start is read back from `extra`; the round trip must hold."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import label_registry as lr
    from custom_components.timescaledb_recorder.meta_worker import _created_at

    created = datetime(2026, 3, 1, 8, 30, 15, 123456, tzinfo=timezone.utc)
    rl = RegistryListener(hass=MagicMock())
    now = datetime.now(timezone.utc)

    device = dr.DeviceEntry(config_entry_id="e", created_at=created)
    assert _created_at(rl._extract_device_params(device, now)[-1]) == created

    # Dataclass path of _build_extra (areas and labels).
    area = ar.AreaEntry(name="A", aliases=set(), floor_id=None,
                        humidity_entity_id=None, icon=None, id="a", picture=None,
                        temperature_entity_id=None, created_at=created)
    assert _created_at(rl._extract_area_params(area, now)[-1]) == created
    label = lr.LabelEntry(name="L", label_id="l", created_at=created)
    assert _created_at(rl._extract_label_params(label, now)[-1]) == created


# ---------------------------------------------------------------------------
# extra: set fields must not depend on PYTHONHASHSEED
# ---------------------------------------------------------------------------

_HASHSEED_PROBE = """
import dataclasses
import json
import attrs
from datetime import datetime, timezone
from homeassistant.helpers import device_registry as dr, area_registry as ar
from custom_components.timescaledb_recorder import registry_listener as rl

ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
device = dr.DeviceEntry(
    id="d", created_at=ts, modified_at=ts, config_entry_id="e1",
    connections={("mac", "1"), ("zigbee", "2"), ("bt", "3")},
    identifiers={("hue", "1"), ("hue", "2"), ("hue", "3")},
)
area = ar.AreaEntry(name="A", aliases={"x", "y", "z", "w"}, floor_id=None,
                    humidity_entity_id=None, icon=None, id="a", picture=None,
                    temperature_entity_id=None, created_at=ts, modified_at=ts)


def legacy(raw, typed):
    # _build_extra before canonical serialization: sets fell through to str().
    return json.dumps({k: v for k, v in raw.items()
                       if k not in typed and not k.startswith("_")}, default=str)


print(json.dumps({
    "device": rl._build_extra(device, rl._DEVICE_TYPED_KEYS),
    "area": rl._build_extra(area, rl._AREA_TYPED_KEYS),
    "legacy_device": legacy(attrs.asdict(device), rl._DEVICE_TYPED_KEYS),
    "legacy_area": legacy({f.name: getattr(area, f.name) for f in dataclasses.fields(area)},
                          rl._AREA_TYPED_KEYS),
}))
"""


def _probe_under_seed(seed: int) -> dict[str, Any]:
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONHASHSEED": str(seed), "PYTHONPATH": str(root)}
    out = subprocess.run([sys.executable, "-c", _HASHSEED_PROBE], env=env, cwd=root,
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_build_extra_is_independent_of_hash_seed():
    """Restarts change PYTHONHASHSEED; the reconcile must not see a change."""
    from custom_components.timescaledb_recorder.registry_listener import _extra_changed

    one, two = _probe_under_seed(1), _probe_under_seed(2)
    # Precondition: these seeds really do order the sets differently.
    assert one["legacy_device"] != two["legacy_device"]
    assert one["device"] == two["device"]
    assert one["area"] == two["area"]
    assert not _extra_changed(one["device"], two["device"])


def test_migration_turns_legacy_extra_into_exactly_the_new_form():
    """canonical_legacy (the migration) and _build_extra must agree exactly,
    across hash seeds, or migrated rows would still read as changed."""
    import json
    from custom_components.timescaledb_recorder.canonical import canonical_legacy

    one, two = _probe_under_seed(1), _probe_under_seed(2)
    for legacy, new in ((one["legacy_device"], two["device"]),
                        (two["legacy_device"], one["device"]),
                        (one["legacy_area"], two["area"])):
        assert legacy != new
        assert canonical_legacy(json.loads(legacy)) == json.loads(new)
    assert canonical_legacy({"aliases": "set()", "x": "frozenset({'b', 'a'})"}) == {
        "aliases": [], "x": ["a", "b"]}


def test_canonical_legacy_leaves_other_strings_alone():
    from custom_components.timescaledb_recorder.canonical import canonical_legacy

    for value in ("{mdi:x}", "{'a': 1}", "{1, 2", "set", "frozenset(x)", "{}"):
        assert canonical_legacy(value) == value


def test_extra_changed_compares_parsed_objects():
    import json
    from custom_components.timescaledb_recorder.registry_listener import _extra_changed

    assert not _extra_changed({"a": [1], "b": "x"}, json.dumps({"b": "x", "a": [1]}))
    assert not _extra_changed('{"a": 1, "modified_at": "t1"}',
                              json.dumps({"a": 1, "modified_at": "t2"}))
    assert _extra_changed({"aliases": ["a", "b"]}, json.dumps({"aliases": ["a"]}))
    assert _extra_changed({"icon": "{mdi:x}"}, json.dumps({"icon": ["mdi:x"]}))
    # Legacy forms are migrated before comparison, not tolerated by it.
    assert _extra_changed({"aliases": "{'a'}"}, json.dumps({"aliases": ["a"]}))


def test_entity_extra_stores_aliases_like_ha_storage():
    """COMPUTED_NAME is null in aliases (HA's own form); compat_aliases is not stored."""
    import json
    from homeassistant.helpers import entity_registry as er

    entry = er.RegistryEntry(
        entity_id="sensor.x", unique_id="u", platform="p",
        aliases=["foo", er.COMPUTED_NAME], compat_aliases=["foo"],
        capabilities=None, config_entry_id=None, config_subentry_id=None,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), device_id=None,
        disabled_by=None, entity_category=None, has_entity_name=False, hidden_by=None,
        id="i", object_id_base=None, options=None, original_device_class=None,
        original_icon=None, original_name=None, suggested_object_id=None,
        supported_features=0, translation_key=None, unit_of_measurement=None,
    )
    extra = json.loads(RegistryListener(hass=MagicMock())._extract_entity_params(
        entry, datetime.now(timezone.utc))[-1])
    assert extra["aliases"] == ["foo", None]
    assert "compat_aliases" not in extra


# ---------------------------------------------------------------------------
# Entity registry callback → dict enqueue (live mode)
# ---------------------------------------------------------------------------

def test_entity_callback_enqueues_dict(listener, mock_entity_registry):
    """_handle_entity_registry_updated must schedule put_async with a correctly-shaped dict."""
    listener._entity_reg = mock_entity_registry

    event = MagicMock()
    event.data = {
        "action": "update",
        "entity_id": "sensor.living_room_temp",
        "old_entity_id": None,
    }
    listener._handle_entity_registry_updated(event)

    assert len(listener._buffer) == 1

    item = listener._buffer[0]
    assert item["registry"] == "entity"
    assert item["action"] == "update"
    assert item["registry_id"] == "sensor.living_room_temp"
    assert item["old_id"] is None
    assert item["params"] is not None, "Non-remove action must extract params"
    assert "enqueued_at" in item


def test_entity_remove_enqueues_params_none(listener):
    """Remove action must enqueue params=None — registry entry is already gone."""
    listener._entity_reg = MagicMock()

    event = MagicMock()
    event.data = {
        "action": "remove",
        "entity_id": "sensor.temp",
        "old_entity_id": None,
    }
    listener._handle_entity_registry_updated(event)

    item = listener._buffer[-1]
    assert item["params"] is None, "Remove action must not fetch params"
    assert item["action"] == "remove"
    listener._entity_reg.async_get.assert_not_called()


def test_entity_rename_sets_old_id(listener, mock_entity_registry):
    """Rename (update with old_entity_id) must populate old_id in the dict."""
    listener._entity_reg = mock_entity_registry

    event = MagicMock()
    event.data = {
        "action": "update",
        "entity_id": "sensor.living_room_temp",
        "old_entity_id": "sensor.old_name",
    }
    listener._handle_entity_registry_updated(event)

    item = listener._buffer[-1]
    assert item["old_id"] == "sensor.old_name"
    assert item["registry_id"] == "sensor.living_room_temp"


# ---------------------------------------------------------------------------
# Area registry callback — reorder skip
# ---------------------------------------------------------------------------

def test_area_reorder_skipped(listener):
    """Reorder events carry no data change and must be silently dropped (Pitfall 6)."""
    event = MagicMock()
    event.data = {"action": "reorder"}
    listener._handle_area_registry_updated(event)
    assert listener._buffer == []


# ---------------------------------------------------------------------------
# Change-detection helpers (sync, called from meta worker thread)
# ---------------------------------------------------------------------------

def test_entity_row_changed_returns_true_when_no_row(listener, mock_psycopg_conn):
    """No existing row → always changed (first write for this entity_id)."""
    _conn, cur = mock_psycopg_conn
    cur.fetchone.return_value = None

    params = (
        "sensor.temp",   # [0]  entity_id
        "uuid-001",      # [1]  ha_entity_uuid
        "Temperature",   # [2]  name
        "sensor",        # [3]  domain
        "zha",           # [4]  platform
        None,            # [5]  device_id
        None,            # [6]  area_id
        [],              # [7]  labels
        "temperature",   # [8]  device_class
        "°C",            # [9]  unit_of_measurement
        None,            # [10] disabled_by
        datetime.now(timezone.utc),  # [11] valid_from
        "{}",            # [12] extra
    )
    assert listener._entity_row_changed(cur, "sensor.temp", params) is True


def test_entity_row_changed_returns_false_when_unchanged(listener, mock_psycopg_conn):
    """Identical row data → no change; helper must return False to suppress spurious SCD2 writes."""
    _conn, cur = mock_psycopg_conn
    cur.fetchone.return_value = {
        "name": "Temperature",
        "platform": "zha",
        "device_id": None,
        "area_id": None,
        "labels": [],
        "device_class": "temperature",
        "unit_of_measurement": "°C",
        "disabled_by": None,
        "extra": "{}",
    }

    params = (
        "sensor.temp",   # [0]  entity_id
        "uuid-001",      # [1]  ha_entity_uuid
        "Temperature",   # [2]  name (matches row)
        "sensor",        # [3]  domain
        "zha",           # [4]  platform (matches row)
        None,            # [5]  device_id (matches row)
        None,            # [6]  area_id (matches row)
        [],              # [7]  labels (matches row)
        "temperature",   # [8]  device_class (matches row)
        "°C",            # [9]  unit_of_measurement (matches row)
        None,            # [10] disabled_by (matches row)
        datetime.now(timezone.utc),  # [11] valid_from
        "{}",            # [12] extra (matches row)
    )
    assert listener._entity_row_changed(cur, "sensor.temp", params) is False


def test_entity_row_changed_returns_true_when_name_differs(listener, mock_psycopg_conn):
    """Changed name must trigger a new SCD2 row."""
    _conn, cur = mock_psycopg_conn
    cur.fetchone.return_value = {
        "name": "Old Name",
        "platform": "zha",
        "device_id": None,
        "area_id": None,
        "labels": [],
        "device_class": "temperature",
        "unit_of_measurement": "°C",
        "disabled_by": None,
        "extra": "{}",
    }

    params = (
        "sensor.temp",   # [0]
        "uuid-001",      # [1]
        "New Name",      # [2]  name differs from row
        "sensor",        # [3]
        "zha",           # [4]
        None,            # [5]
        None,            # [6]
        [],              # [7]
        "temperature",   # [8]
        "°C",            # [9]
        None,            # [10]
        datetime.now(timezone.utc),  # [11]
        "{}",            # [12]
    )
    assert listener._entity_row_changed(cur, "sensor.temp", params) is True


def test_device_row_changed_returns_true_when_no_row(listener, mock_psycopg_conn):
    """No existing device row → always changed."""
    _conn, cur = mock_psycopg_conn
    cur.fetchone.return_value = None
    params = (
        "device-001",   # [0] device_id
        "My Device",    # [1] name
        "IKEA",         # [2] manufacturer
        "Model X",      # [3] model
        None,           # [4] area_id
        [],             # [5] labels
        datetime.now(timezone.utc),  # [6] valid_from
        "{}",           # [7] extra
    )
    assert listener._device_row_changed(cur, "device-001", params) is True


# ---------------------------------------------------------------------------
# Ordered hand-off to the meta queue (issue #17)
# ---------------------------------------------------------------------------

def _accept_any_entity_id(reg):
    """Make the registry mock resolve any entity_id to a real-shaped entry.

    The shared fixture only knows two ids; these tests fire bursts across many,
    and an unresolvable id makes the handler skip the event entirely.
    """
    entry = reg.async_get("sensor.living_room_temp")
    reg.async_get = MagicMock(return_value=entry)
    return reg


async def test_buffer_preserves_event_order(listener, mock_entity_registry):
    """The buffer must record events in the order they fired.

    Previously each event became its own task calling put_async, which offloads
    the append to the default multi-threaded executor; concurrent appends raced
    and queue order stopped matching event order. valid_from is stamped at event
    time, so a reordered queue made the worker apply versions out of sequence and
    corrupt the SCD2 intervals.
    """
    listener._entity_reg = _accept_any_entity_id(mock_entity_registry)
    for i in range(50):
        event = MagicMock()
        event.data = {"action": "update", "entity_id": f"sensor.e{i}",
                      "old_entity_id": None}
        listener._handle_entity_registry_updated(event)

    ids = [item["registry_id"] for item in listener._buffer]
    assert ids == [f"sensor.e{i}" for i in range(50)]


async def test_valid_from_is_monotonic_in_buffer_order(listener, mock_entity_registry):
    """Queue position must agree with the valid_from stamps the items carry.

    This is the property the SCD2 close+insert depends on: if position disagrees
    with valid_from, a version gets applied against the wrong predecessor.
    """
    listener._entity_reg = _accept_any_entity_id(mock_entity_registry)
    for _ in range(20):
        event = MagicMock()
        event.data = {"action": "update", "entity_id": "sensor.x", "old_entity_id": None}
        listener._handle_entity_registry_updated(event)

    stamps = [item["params"][11] for item in listener._buffer]
    assert stamps == sorted(stamps)


async def test_flush_sends_one_batch_and_clears(listener, mock_meta_queue,
                                                mock_entity_registry):
    """Draining hands the whole buffer over in a single put_many_async."""
    listener._entity_reg = _accept_any_entity_id(mock_entity_registry)
    for i in range(5):
        event = MagicMock()
        event.data = {"action": "update", "entity_id": f"sensor.e{i}",
                      "old_entity_id": None}
        listener._handle_entity_registry_updated(event)

    await listener._flush_buffer()

    mock_meta_queue.put_many_async.assert_awaited_once()
    batch = mock_meta_queue.put_many_async.await_args.args[0]
    assert [i["registry_id"] for i in batch] == [f"sensor.e{i}" for i in range(5)]
    assert listener._buffer == []


async def test_flush_requeues_batch_on_failure(listener, mock_meta_queue,
                                               mock_entity_registry):
    """A failed enqueue must not silently drop registry changes — losing them
    leaves a permanent gap in SCD2 history."""
    listener._entity_reg = _accept_any_entity_id(mock_entity_registry)
    event = MagicMock()
    event.data = {"action": "update", "entity_id": "sensor.x", "old_entity_id": None}
    listener._handle_entity_registry_updated(event)

    mock_meta_queue.put_many_async.side_effect = OSError("disk full")
    with pytest.raises(OSError):
        await listener._flush_buffer()

    assert len(listener._buffer) == 1


async def test_drain_loop_survives_an_enqueue_failure(listener, mock_meta_queue,
                                                     mock_entity_registry):
    """The drainer is the only path from callback to disk.

    If it exits on the first error, every later registry change accumulates in
    memory unpersisted while nothing reports a problem. It must retry instead.
    """
    listener._entity_reg = _accept_any_entity_id(mock_entity_registry)
    calls = []

    async def flaky(batch):
        calls.append(list(batch))
        if len(calls) == 1:
            raise OSError("disk full")

    mock_meta_queue.put_many_async = AsyncMock(side_effect=flaky)

    with patch.object(registry_listener_module, "_DRAIN_RETRY_MIN_S", 0.01), \
         patch.object(registry_listener_module, "_DRAIN_RETRY_MAX_S", 0.01):
        task = asyncio.get_running_loop().create_task(listener._drain_loop())
        event = MagicMock()
        event.data = {"action": "update", "entity_id": "sensor.x", "old_entity_id": None}
        listener._handle_entity_registry_updated(event)

        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(calls) >= 2:
                break

        assert not task.done(), "drain loop died on the first enqueue error"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert len(calls) >= 2, "the failed batch was never retried"
    assert calls[1][0]["registry_id"] == "sensor.x", "retry lost the item"
    assert listener._buffer == []


async def test_cancellation_then_failed_append_returns_the_batch(listener,
                                                                 mock_meta_queue,
                                                                 mock_entity_registry):
    """Cancelled mid-flush and the append then fails: the items must come back.

    The buffer is swapped out before awaiting the executor, so without settling
    the in-flight append these items would belong to neither the buffer nor the
    queue — silently lost.
    """
    listener._entity_reg = _accept_any_entity_id(mock_entity_registry)
    event = MagicMock()
    event.data = {"action": "update", "entity_id": "sensor.x", "old_entity_id": None}
    listener._handle_entity_registry_updated(event)

    started = asyncio.Event()

    async def slow_failure(_batch):
        started.set()
        await asyncio.sleep(0.05)
        raise OSError("disk full")

    mock_meta_queue.put_many_async = AsyncMock(side_effect=slow_failure)

    flush = asyncio.get_running_loop().create_task(listener._flush_buffer())
    await started.wait()
    flush.cancel()
    with pytest.raises(asyncio.CancelledError):
        await flush

    # Shielded, so still in flight: neither lost nor yet known to have succeeded.
    assert listener._inflight is not None
    await listener._settle_inflight()
    assert len(listener._buffer) == 1, "batch was lost on cancellation"


async def test_cancellation_does_not_append_twice(listener, mock_meta_queue,
                                                  mock_entity_registry):
    """Cancelling the awaiter does not stop an append already in the executor.

    Requeuing unconditionally would then write the same items a second time. The
    batch must be requeued only if the append actually failed.
    """
    listener._entity_reg = _accept_any_entity_id(mock_entity_registry)
    event = MagicMock()
    event.data = {"action": "update", "entity_id": "sensor.x", "old_entity_id": None}
    listener._handle_entity_registry_updated(event)

    appended = []
    started = asyncio.Event()

    async def slow_but_successful(batch):
        started.set()
        await asyncio.sleep(0.05)
        appended.extend(batch)      # succeeds despite the awaiter being cancelled

    mock_meta_queue.put_many_async = AsyncMock(side_effect=slow_but_successful)

    flush = asyncio.get_running_loop().create_task(listener._flush_buffer())
    await started.wait()
    flush.cancel()
    with pytest.raises(asyncio.CancelledError):
        await flush

    await listener._settle_inflight()

    assert len(appended) == 1, "the shielded append should have completed"
    assert listener._buffer == [], "a successful append must not be requeued"

    await listener._flush_buffer()
    assert len(appended) == 1, "the batch was appended twice"


async def test_a_failed_flush_is_requeued_exactly_once(listener,
                                                       mock_meta_queue,
                                                       mock_entity_registry):
    """A failed append restores its batch, and _settle_inflight must not do it again.

    _flush_buffer requeues on failure; if it also left the batch recorded as
    in-flight, shutdown would settle the already-failed append and requeue the
    same items a second time, and the final flush would write both copies.
    """
    listener._entity_reg = _accept_any_entity_id(mock_entity_registry)
    event = MagicMock()
    event.data = {"action": "update", "entity_id": "sensor.x", "old_entity_id": None}
    listener._handle_entity_registry_updated(event)

    mock_meta_queue.put_many_async = AsyncMock(side_effect=OSError("disk full"))
    with pytest.raises(OSError):
        await listener._flush_buffer()

    assert len(listener._buffer) == 1
    assert listener._inflight is None, (
        "a batch already back in the buffer is not in flight")

    # This is what async_stop does next.
    await listener._settle_inflight()
    assert len(listener._buffer) == 1, "the batch was requeued twice"

    appended = []
    mock_meta_queue.put_many_async = AsyncMock(side_effect=appended.extend)
    await listener._flush_buffer()
    assert len(appended) == 1, "the final flush wrote a duplicate"


async def test_stop_flushes_remaining_buffer(listener, mock_meta_queue,
                                             mock_entity_registry):
    """Shutdown must drain what is still buffered."""
    listener._entity_reg = _accept_any_entity_id(mock_entity_registry)
    event = MagicMock()
    event.data = {"action": "update", "entity_id": "sensor.x", "old_entity_id": None}
    listener._handle_entity_registry_updated(event)

    await listener.async_stop()

    mock_meta_queue.put_many_async.assert_awaited_once()
    assert listener._buffer == []


async def test_stop_falls_back_to_a_blocking_append(listener, mock_meta_queue,
                                                    mock_entity_registry):
    """A failed final flush must not be the end of the item.

    The async path appends through an executor. When that fails there is still
    one thing left to try — appending synchronously — and by then the drainer is
    gone, so nothing else can touch the buffer. Giving up instead threw away
    every registry change of the last drain interval.
    """
    listener._entity_reg = _accept_any_entity_id(mock_entity_registry)
    event = MagicMock()
    event.data = {"action": "update", "entity_id": "sensor.x", "old_entity_id": None}
    listener._handle_entity_registry_updated(event)

    mock_meta_queue.put_many_async = AsyncMock(side_effect=OSError("executor gone"))
    appended: list[dict[str, Any]] = []
    mock_meta_queue.put_many = MagicMock(side_effect=appended.extend)

    await listener.async_stop()

    assert len(appended) == 1, "the item must reach the queue file synchronously"
    assert appended[0]["registry_id"] == "sensor.x"
    assert listener._buffer == []


async def test_stop_reports_the_loss_when_even_the_blocking_append_fails(
    listener, mock_meta_queue, mock_entity_registry, caplog
):
    """If both paths fail the change really is gone, and that must be said.

    Unload still has to succeed — blocking HA shutdown over this is worse — so
    the log line is the only thing standing between a lost registry change and
    nobody knowing.
    """
    listener._entity_reg = _accept_any_entity_id(mock_entity_registry)
    event = MagicMock()
    event.data = {"action": "update", "entity_id": "sensor.x", "old_entity_id": None}
    listener._handle_entity_registry_updated(event)

    mock_meta_queue.put_many_async = AsyncMock(side_effect=OSError("executor gone"))
    mock_meta_queue.put_many = MagicMock(side_effect=OSError("disk full"))

    await listener.async_stop()

    assert "Dropped 1 buffered registry item" in caplog.text
    assert listener._buffer, "the batch is kept for whatever inspects it"
