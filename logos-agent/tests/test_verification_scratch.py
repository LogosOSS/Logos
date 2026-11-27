"""Verifier disk scratch is monitored, isolated, and removed only after Docker releases it."""

from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app import docker_engine, sessions
from app import verification_scratch as scratch

VOLUME = "logos_agent_verifier_scratch"
MOUNTPOINT = f"/var/lib/docker/volumes/{VOLUME}/_data"
IDENTITY = "a" * 12
MARKER = "a" * 64


@pytest.fixture
def pool(monkeypatch, tmp_path):
    monkeypatch.setattr(scratch, "ROOT", tmp_path)
    monkeypatch.setattr(scratch, "_validate_mount", lambda *_args: None)
    monkeypatch.setattr(scratch, "_available", lambda: (100 * scratch.GIB, 50_000))
    monkeypatch.setattr(scratch, "_validate_daemon_volume", AsyncMock(return_value=Path(MOUNTPOINT)))
    monkeypatch.setattr(scratch.docker_engine, "list_managed_containers", AsyncMock(return_value=[]))
    # The runner is root in Compose, unlike a local pytest process.
    real_os = os

    class RootOs:
        def __getattr__(self, name):
            return getattr(real_os, name)

        @staticmethod
        def chown(*_args):
            pass

        @staticmethod
        def fstat(fd):
            info = real_os.fstat(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink, st_uid=0)

    monkeypatch.setattr(scratch, "os", RootOs())
    return tmp_path


async def test_cancelled_lock_open_closes_the_eventual_descriptor(monkeypatch, tmp_path):
    started = Event()
    finish = Event()
    fd = os.open(tmp_path / "lock", os.O_RDONLY | os.O_CREAT, 0o600)

    def open_slowly():
        started.set()
        finish.wait(timeout=2)
        return fd

    monkeypatch.setattr(scratch, "_open_lock_file", open_slowly)
    waiter = asyncio.create_task(scratch._open_lock())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        finish.set()
    for _ in range(100):
        try:
            os.fstat(fd)
        except OSError:
            break
        await asyncio.sleep(0.01)
    else:
        os.close(fd)
        pytest.fail("cancelled lock opening leaked a descriptor")


async def test_reservations_are_exclusive_fresh_and_cleaned(pool):
    async with scratch.reserve(VOLUME, "20", "10", timeout_s=0) as first:
        local = pool / first.slot
        marker = local / scratch.MARKER
        assert marker.read_text() == first.marker
        assert stat.S_IMODE(local.stat().st_mode) == 0o700
        assert stat.S_IMODE(marker.stat().st_mode) == 0o444
        assert first.volume == VOLUME
        (local / "prover-output").write_text("private")
        with pytest.raises(RuntimeError, match="slot was still busy"):
            async with scratch.reserve(VOLUME, "20", "10", timeout_s=0):
                pytest.fail("must not allow another verifier")
    assert not local.exists()
    async with scratch.reserve(VOLUME, "20", "10", timeout_s=0) as second:
        assert second.slot != first.slot
        assert second.marker != first.marker
        assert not (pool / second.slot / "prover-output").exists()


@pytest.mark.parametrize("labelled", [False, True])
async def test_recovery_retains_directories_owned_by_orphaned_docker_helpers(pool, labelled):
    async with scratch.reserve(VOLUME, "20", "10", timeout_s=0) as first:
        scratch.docker_engine.list_managed_containers.return_value = [
            {"Labels": {scratch.LABEL: "true"}} if labelled else {"Mounts": [{"Source": f"{MOUNTPOINT}/{first.slot}"}]}
        ]
        stale = pool / first.slot
        (stale / "retained").touch()

    assert stale.exists()
    with pytest.raises(RuntimeError, match="previous verification scratch is still"):
        async with scratch.reserve(VOLUME, "20", "10", timeout_s=0):
            pytest.fail("must not clear an orphaned verifier's scratch data")
    assert (stale / "retained").exists()
    scratch.docker_engine.list_managed_containers.return_value = []
    async with scratch.reserve(VOLUME, "20", "10", timeout_s=0):
        assert not stale.exists()


async def test_cleanup_does_not_follow_verifier_symlinks(pool):
    outside = pool / "lost+found"
    outside.mkdir()
    protected = outside / "keep"
    protected.touch()
    async with scratch.reserve(VOLUME, "20", "10", timeout_s=0) as slot:
        (pool / slot.slot / "malicious").symlink_to(outside)
    assert protected.exists()


@pytest.mark.parametrize("value", ["bad", "0", "-1", "20GiB", "99999"])
async def test_rejects_invalid_explicit_cap(pool, value):
    with pytest.raises(RuntimeError, match="positive integers"):
        async with scratch.reserve(VOLUME, value, "10", timeout_s=0):
            pytest.fail("invalid limits must fail closed")


async def test_rejects_extraneous_files_on_shared_device(pool):
    (pool / "other-service").mkdir()
    with pytest.raises(RuntimeError, match="only runner files"):
        async with scratch.reserve(VOLUME, "20", "10", timeout_s=0):
            pytest.fail("must not use shared storage")


async def test_binds_must_match_daemon_inspection(monkeypatch):
    monkeypatch.setattr(scratch.socket, "gethostname", lambda: IDENTITY)
    mount = {"Type": "volume", "Destination": str(scratch.ROOT), "Name": VOLUME, "Source": MOUNTPOINT, "RW": True}
    inspect = AsyncMock(return_value={"Id": IDENTITY + "b" * 52, "Mounts": [mount]})
    monkeypatch.setattr(docker_engine, "inspect_container", inspect)
    info = {
        "Name": VOLUME,
        "Driver": "local",
        "Mountpoint": MOUNTPOINT,
        "Options": None,
    }
    volume = AsyncMock(return_value=info.copy())
    monkeypatch.setattr(docker_engine, "inspect_volume", volume)
    assert await scratch._validate_daemon_volume(VOLUME) == Path(MOUNTPOINT)
    for change in ({"Type": "bind"}, {"Source": "/wrong/path"}, {"RW": False}, {"Name": "other-volume"}):
        inspect.return_value["Mounts"] = [{**mount, **change}]
        with pytest.raises(RuntimeError, match="Docker volume mount"):
            await scratch._validate_daemon_volume(VOLUME)
    for options in ({"type": "tmpfs"}, {"device": "/", "type": "none", "o": "bind"}):
        volume.return_value = {**info, "Options": options}
        with pytest.raises(RuntimeError, match="plain local Docker volume"):
            await scratch._validate_daemon_volume(VOLUME)


@pytest.mark.parametrize(
    ("filesystem", "dev", "options", "superopts", "nested", "valid"),
    [
        ("ext4", "253:1", "rw,relatime", "rw", False, True),
        ("xfs", "253:1", "rw,relatime", "rw", False, True),
        ("tmpfs", "253:1", "rw,nosuid,nodev", "rw", False, False),
        ("ext4", "253:2", "rw,nosuid,nodev", "rw", False, False),
        ("ext4", "253:1", "ro,relatime", "rw", False, False),
        ("ext4", "253:1", "rw,relatime", "ro", False, False),
        ("ext4", "253:1", "rw,nosuid,nodev,noexec", "rw", False, False),
        ("ext4", "253:1", "rw,nosuid,nodev", "rw", True, False),
    ],
)
def test_mount_requires_a_writable_disk_backed_docker_volume(
    monkeypatch, filesystem, dev, options, superopts, nested, valid
):
    target = Path("/agent/scratch")
    info = f"123 456 {dev} /var/lib/docker/volumes/vol/_data {target} {options} - {filesystem} /dev/disk {superopts}\n"
    if nested:
        info += f"124 123 0:3 / {target}/subdir rw - tmpfs tmpfs rw\n"
    metadata = SimpleNamespace(st_dev=os.makedev(253, 1), st_mode=stat.S_IFDIR | 0o700, st_uid=0)
    monkeypatch.setattr(Path, "lstat", lambda path: metadata)
    monkeypatch.setattr(Path, "resolve", lambda path: path)
    monkeypatch.setattr(Path, "read_text", lambda path: info)
    if valid:
        scratch._validate_mount(target)
    else:
        with pytest.raises(RuntimeError, match="verification scratch"):
            scratch._validate_mount(target)


@pytest.mark.parametrize(("free", "inodes"), [(11, 30_000), (100, 16_384)])
async def test_pressure_refuses_a_slot_before_verifier_start(pool, monkeypatch, free, inodes):
    monkeypatch.setattr(scratch, "_available", lambda: (free * scratch.GIB, inodes))
    with pytest.raises(RuntimeError, match="insufficient free space or inodes"):
        async with scratch.reserve(VOLUME, "20", "10", timeout_s=0):
            pytest.fail("must not admit a verifier")
    assert list(pool.glob("verifier-*")) == []


@pytest.mark.parametrize(("start", "after", "inodes"), [(100, 80, 50_000), (12, 10, 50_000), (100, 99, 16_384)])
async def test_pressure_stops_an_in_flight_operation(pool, monkeypatch, start, after, inodes):
    available = [start * scratch.GIB, 50_000]
    monkeypatch.setattr(scratch, "_available", lambda: tuple(available))
    monkeypatch.setattr(scratch, "CHECK_INTERVAL_S", 0.001)
    cancelled = asyncio.Event()

    async def running():
        available[:] = [after * scratch.GIB, inodes]
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with scratch.reserve(VOLUME, "20", "10", timeout_s=0) as slot:
        with pytest.raises(RuntimeError, match="disk pressure limit reached"):
            await asyncio.wait_for(slot.monitor(running), timeout=1)
        assert cancelled.is_set()


async def test_pressure_detected_at_successful_exit_still_fails(pool, monkeypatch):
    async with scratch.reserve(VOLUME, "20", "10", timeout_s=0) as slot:

        async def done():
            monkeypatch.setattr(scratch, "_available", lambda: (80 * scratch.GIB, 50_000))
            return 0

        with pytest.raises(RuntimeError, match="disk pressure limit reached"):
            await slot.monitor(done)


async def test_bad_provisioning_fails_verification_without_tmpfs_fallback(monkeypatch):
    manager = sessions.SessionManager()
    helper = AsyncMock()
    monkeypatch.setattr(manager, "_run_helper_container", helper)
    monkeypatch.setattr(
        sessions,
        "settings",
        replace(sessions.settings, verification_scratch_volume=VOLUME),
    )
    monkeypatch.setattr(scratch, "_validate_daemon_volume", AsyncMock(return_value=Path(MOUNTPOINT)))
    monkeypatch.setattr(scratch, "_validate_mount", lambda *_args: (_ for _ in ()).throw(RuntimeError("not mounted")))

    code = await manager._run_helper(
        phase="verify", session_id=42, env={}, workspace_volume="workspace", artifact_host_path="/artifacts/42"
    )
    assert code == 1
    assert "disk scratch unavailable: not mounted" in manager._last_helper_output[42]
    helper.assert_not_awaited()


async def test_unprovisioned_runner_fails_verification_but_still_runs_normal_helpers(monkeypatch):
    manager = sessions.SessionManager()
    helper = AsyncMock(return_value=0)
    monkeypatch.setattr(manager, "_run_helper_container", helper)
    monkeypatch.setattr(
        sessions,
        "settings",
        replace(sessions.settings, verification_scratch_volume=""),
    )
    options = dict(session_id=42, env={}, workspace_volume="workspace", artifact_host_path="/artifacts/42")
    assert await manager._run_helper(phase="verify", **options) == 1
    assert "requires the name of its local Docker volume" in manager._last_helper_output[42]
    helper.assert_not_awaited()
    assert await manager._run_helper(phase="prepare", **options) == 0
    helper.assert_awaited_once_with(phase="prepare", cache_volume=None, **options)


@pytest.mark.parametrize("pressure", [False, True])
async def test_runner_verifier_cleans_after_removal_even_on_pressure(monkeypatch, pool, pressure):
    manager = sessions.SessionManager()
    monkeypatch.setattr(
        sessions,
        "settings",
        replace(sessions.settings, verification_scratch_volume=VOLUME),
    )
    monkeypatch.setattr(sessions.db, "get_session", AsyncMock(return_value={"status": "finalizing"}))
    monkeypatch.setattr(docker_engine, "start_container", AsyncMock())

    async def wait(*_args, **_kwargs):
        if pressure:
            monkeypatch.setattr(scratch, "_available", lambda: (80 * scratch.GIB, 50_000))
            await asyncio.Event().wait()
        return 0

    monkeypatch.setattr(scratch, "CHECK_INTERVAL_S", 0.001)
    monkeypatch.setattr(docker_engine, "wait_container", wait)
    monkeypatch.setattr(manager, "_what_it_printed", AsyncMock(return_value="verified"))
    mounted = []

    async def create(**kwargs):
        volume, subpath, identity = kwargs["verification_scratch"]
        assert volume == VOLUME
        local = pool / subpath
        assert (local / scratch.MARKER).read_text() == identity
        assert kwargs["network"] == "none"
        mounted.append(local)
        return "verifier"

    async def remove(_container):
        assert mounted[0].is_dir()

    monkeypatch.setattr(docker_engine, "create_session_container", create)
    monkeypatch.setattr(docker_engine, "remove_container", remove)
    assert await asyncio.wait_for(
        manager._run_helper(
            phase="verify",
            session_id=42,
            env={"LOGOS_SESSION_PHASE": "verify"},
            workspace_volume="workspace",
            artifact_host_path="/artifacts/42",
        ),
        timeout=1,
    ) == (1 if pressure else 0)
    assert len(mounted) == 1 and not mounted[0].exists()
    assert manager._last_helper_output[42].endswith("verification aborted" if pressure else "verified")


async def test_scratch_is_mounted_only_as_a_private_verifier_tmp(monkeypatch):
    payloads = []

    async def request(_method, _path, **kwargs):
        assert kwargs["verifier_subpath"] is True
        payloads.append(kwargs["json"])
        return SimpleNamespace(json=lambda: {"Id": "verify"})

    monkeypatch.setattr(docker_engine, "_request", request)
    options = dict(
        name="verify",
        image="workspace",
        workspace_volume="workspace",
        artifact_host_path="/artifacts/7",
        cache_volume="cache",
        session_id=7,
        verification_scratch=(VOLUME, "verifier-" + "b" * 32, MARKER),
        memory_mb=3513,
    )
    await docker_engine.create_session_container(env={"LOGOS_SESSION_PHASE": "verify"}, **options)
    host_config = payloads[0]["HostConfig"]
    assert host_config["Binds"] == ["workspace:/workspace:ro", "/artifacts/7:/artifacts:ro", "cache:/cache:ro"]
    assert host_config["NetworkMode"] == "none"
    assert host_config["ReadonlyRootfs"]
    assert host_config["CapDrop"] == ["ALL"]
    assert host_config["SecurityOpt"] == ["no-new-privileges:true"]
    assert host_config["Memory"] == host_config["MemorySwap"] == 3513 * 1024 * 1024
    assert "Tmpfs" not in host_config
    assert host_config["Ulimits"] == [{"Name": "fsize", "Soft": 8 * scratch.GIB, "Hard": 8 * scratch.GIB}]
    assert host_config["LogConfig"] == {"Type": "local", "Config": {"max-size": "10m", "max-file": "2"}}
    assert host_config["Mounts"] == [
        {
            "Type": "volume",
            "Source": VOLUME,
            "Target": "/logos-verifier-scratch",
            "ReadOnly": False,
            "VolumeOptions": {"Subpath": "verifier-" + "b" * 32, "NoCopy": True},
        }
    ]
    assert f"LOGOS_VERIFICATION_SCRATCH_MARKER={MARKER}" in payloads[0]["Env"]
    assert "TMPDIR=/logos-verifier-scratch" in payloads[0]["Env"]
    for phase in ("agent", "prepare", "finalize"):
        with pytest.raises(ValueError, match="only a verifier"):
            await docker_engine.create_session_container(env={"LOGOS_SESSION_PHASE": phase}, **options)


@pytest.mark.parametrize("slot", ["", "..", "../other-slot", "/verifier-slot", "verifier-invalid"])
async def test_docker_verifier_rejects_non_private_volume_subpaths(monkeypatch, slot):
    request = AsyncMock()
    monkeypatch.setattr(docker_engine, "_request", request)
    with pytest.raises(ValueError, match="private slot name"):
        await docker_engine.create_session_container(
            name="verify",
            image="workspace",
            env={"LOGOS_SESSION_PHASE": "verify"},
            workspace_volume="workspace",
            artifact_host_path="/artifacts/7",
            session_id=7,
            verification_scratch=(VOLUME, slot, MARKER),
        )
    request.assert_not_awaited()
