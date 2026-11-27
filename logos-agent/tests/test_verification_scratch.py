"""Verifier disk scratch is bounded, isolated, and removed only after Docker releases it."""

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

HOST = "/srv/logos-verifier-scratch"
VOLUME = "logos_agent_verifier_scratch_disk"
MOUNTPOINT = f"/var/lib/docker/volumes/{VOLUME}/_data"
IDENTITY = "a" * 12
MARKER = "a" * 64


@pytest.fixture
def pool(monkeypatch, tmp_path):
    monkeypatch.setattr(scratch, "ROOT", tmp_path)
    monkeypatch.setattr(scratch, "_validate_mount", lambda *_args: None)
    monkeypatch.setattr(scratch, "_validate_daemon_bind", AsyncMock(return_value=Path(MOUNTPOINT)))
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
    async with scratch.reserve(VOLUME, HOST, "20", timeout_s=0) as first:
        local = pool / Path(first.host_path).name
        marker = local / scratch.MARKER
        assert marker.read_text() == first.marker
        assert stat.S_IMODE(local.stat().st_mode) == 0o700
        assert stat.S_IMODE(marker.stat().st_mode) == 0o444
        assert Path(first.host_path).parent == Path(MOUNTPOINT)
        (local / "prover-output").write_text("private")
        with pytest.raises(RuntimeError, match="slot was still busy"):
            async with scratch.reserve(VOLUME, HOST, "20", timeout_s=0):
                pytest.fail("must not allow another verifier")
    assert not local.exists()
    async with scratch.reserve(VOLUME, HOST, "20", timeout_s=0) as second:
        assert second.host_path != first.host_path
        assert second.marker != first.marker
        assert not (pool / Path(second.host_path).name / "prover-output").exists()


@pytest.mark.parametrize("labelled", [False, True])
async def test_recovery_retains_directories_owned_by_orphaned_docker_helpers(pool, labelled):
    async with scratch.reserve(VOLUME, HOST, "20", timeout_s=0) as first:
        scratch.docker_engine.list_managed_containers.return_value = [
            {"Labels": {scratch.LABEL: "true"}} if labelled else {"Mounts": [{"Source": first.host_path}]}
        ]
        stale = pool / Path(first.host_path).name
        (stale / "retained").touch()

    assert stale.exists()
    with pytest.raises(RuntimeError, match="previous verification scratch is still"):
        async with scratch.reserve(VOLUME, HOST, "20", timeout_s=0):
            pytest.fail("must not clear an orphaned verifier's scratch data")
    assert (stale / "retained").exists()
    scratch.docker_engine.list_managed_containers.return_value = []
    async with scratch.reserve(VOLUME, HOST, "20", timeout_s=0):
        assert not stale.exists()


async def test_cleanup_does_not_follow_verifier_symlinks(pool):
    outside = pool / "lost+found"
    outside.mkdir()
    protected = outside / "keep"
    protected.touch()
    async with scratch.reserve(VOLUME, HOST, "20", timeout_s=0) as slot:
        (pool / Path(slot.host_path).name / "malicious").symlink_to(outside)
    assert protected.exists()


@pytest.mark.parametrize("value", ["bad", "0", "-1", "20GiB", "99999"])
async def test_rejects_invalid_explicit_cap(pool, value):
    with pytest.raises(RuntimeError, match="positive integer"):
        async with scratch.reserve(VOLUME, HOST, value, timeout_s=0):
            pytest.fail("invalid limits must fail closed")


async def test_rejects_extraneous_files_on_shared_device(pool):
    (pool / "other-service").mkdir()
    with pytest.raises(RuntimeError, match="only runner files"):
        async with scratch.reserve(VOLUME, HOST, "20", timeout_s=0):
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
        "Options": {"device": HOST, "type": "none", "o": "bind"},
    }
    volume = AsyncMock(return_value=info.copy())
    monkeypatch.setattr(docker_engine, "inspect_volume", volume)
    assert await scratch._validate_daemon_bind(VOLUME, HOST) == Path(MOUNTPOINT)
    for change in ({"Type": "bind"}, {"Source": "/wrong/path"}, {"RW": False}, {"Name": "other-volume"}):
        inspect.return_value["Mounts"] = [{**mount, **change}]
        with pytest.raises(RuntimeError, match="Docker volume mount"):
            await scratch._validate_daemon_bind(VOLUME, HOST)
    for options in (None, {}, {**info["Options"], "device": "/"}):
        volume.return_value = {**info, "Options": options}
        with pytest.raises(RuntimeError, match="explicitly provisioned"):
            await scratch._validate_daemon_bind(VOLUME, HOST)


@pytest.mark.parametrize(
    ("filesystem", "root", "dev", "options", "size_gib", "backed", "nested", "shared", "valid"),
    [
        ("ext4", "/", "253:1", "rw,nosuid,nodev", 20, True, False, False, True),
        ("xfs", "/", "253:1", "rw,nosuid,nodev", 20, True, False, False, True),
        ("ext4", "/", "253:1", "rw,nosuid,nodev", 21, True, False, False, False),
        ("ext4", "/", "253:1", "rw,nosuid,nodev", 0, True, False, False, False),
        ("tmpfs", "/", "253:1", "rw,nosuid,nodev", 2, True, False, False, False),
        ("ext4", "/shared", "253:1", "rw,nosuid,nodev", 20, True, False, False, False),
        ("ext4", "/", "253:2", "rw,nosuid,nodev", 20, True, False, False, False),
        ("ext4", "/", "253:1", "rw,nosuid", 20, True, False, False, False),
        ("ext4", "/", "253:1", "rw,nosuid,nodev,noexec", 20, True, False, False, False),
        ("ext4", "/", "253:1", "rw,nosuid,nodev", 20, False, False, False, False),
        ("ext4", "/", "253:1", "rw,nosuid,nodev", 20, True, True, False, False),
        ("ext4", "/", "253:1", "rw,nosuid,nodev", 20, True, False, True, False),
    ],
)
def test_mount_requires_a_bounded_dedicated_block_filesystem(
    monkeypatch, filesystem, root, dev, options, size_gib, backed, nested, shared, valid
):
    target = Path("/agent/scratch")
    info = f"123 456 {dev} {root} {target} {options} - {filesystem} /dev/mapper/disk rw\n"
    if nested:
        info += f"124 123 0:3 / {target}/subdir rw - tmpfs tmpfs rw\n"
    metadata = SimpleNamespace(st_dev=os.makedev(253, 1), st_mode=stat.S_IFDIR | 0o700, st_uid=0)
    monkeypatch.setattr(Path, "lstat", lambda path: metadata)
    monkeypatch.setattr(Path, "resolve", lambda path: path)
    monkeypatch.setattr(Path, "read_text", lambda path: info)
    monkeypatch.setattr(
        os, "stat", lambda path: SimpleNamespace(st_dev=metadata.st_dev if shared else os.makedev(8, 1))
    )
    monkeypatch.setattr(os, "statvfs", lambda path: SimpleNamespace(f_blocks=size_gib, f_frsize=scratch.GIB))
    monkeypatch.setattr(scratch, "_disk_device", lambda dev_id: backed)
    if valid:
        scratch._validate_mount(target, 20)
    else:
        with pytest.raises(RuntimeError, match="verification scratch"):
            scratch._validate_mount(target, 20)


def test_devices_backed_by_a_loop_are_rejected(tmp_path):
    dm = tmp_path / "253:1"
    (dm / "slaves").mkdir(parents=True)
    (dm / "dev").write_text("253:1")
    disk = tmp_path / "7:1"
    disk.mkdir()
    (disk / "dev").write_text("8:1")
    (dm / "slaves" / "inner").symlink_to(disk)
    assert scratch._disk_device("253:1", tmp_path)
    (disk / "dev").write_text("7:1")
    assert not scratch._disk_device("253:1", tmp_path)


async def test_bad_provisioning_fails_verification_without_tmpfs_fallback(monkeypatch):
    manager = sessions.SessionManager()
    helper = AsyncMock()
    monkeypatch.setattr(manager, "_run_helper_container", helper)
    monkeypatch.setattr(
        sessions,
        "settings",
        replace(sessions.settings, verification_scratch_host_path=HOST, verification_scratch_volume=VOLUME),
    )
    monkeypatch.setattr(scratch, "_validate_daemon_bind", AsyncMock(return_value=Path(MOUNTPOINT)))
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
        replace(sessions.settings, verification_scratch_host_path="", verification_scratch_volume=""),
    )
    options = dict(session_id=42, env={}, workspace_volume="workspace", artifact_host_path="/artifacts/42")
    assert await manager._run_helper(phase="verify", **options) == 1
    assert "provision dedicated verification storage" in manager._last_helper_output[42]
    helper.assert_not_awaited()
    assert await manager._run_helper(phase="prepare", **options) == 0
    helper.assert_awaited_once_with(phase="prepare", cache_volume=None, **options)


async def test_runner_verifier_starts_with_real_reservation_and_cleans_after_removal(monkeypatch, pool):
    manager = sessions.SessionManager()
    monkeypatch.setattr(
        sessions,
        "settings",
        replace(sessions.settings, verification_scratch_host_path=HOST, verification_scratch_volume=VOLUME),
    )
    monkeypatch.setattr(sessions.db, "get_session", AsyncMock(return_value={"status": "finalizing"}))
    monkeypatch.setattr(docker_engine, "start_container", AsyncMock())
    monkeypatch.setattr(docker_engine, "wait_container", AsyncMock(return_value=0))
    monkeypatch.setattr(manager, "_what_it_printed", AsyncMock(return_value="verified"))
    mounted = []

    async def create(**kwargs):
        path, identity = kwargs["verification_scratch"]
        local = pool / Path(path).name
        assert (local / scratch.MARKER).read_text() == identity
        assert kwargs["network"] == "none"
        mounted.append(local)
        return "verifier"

    async def remove(_container):
        assert mounted[0].is_dir()

    monkeypatch.setattr(docker_engine, "create_session_container", create)
    monkeypatch.setattr(docker_engine, "remove_container", remove)
    assert (
        await manager._run_helper(
            phase="verify",
            session_id=42,
            env={"LOGOS_SESSION_PHASE": "verify"},
            workspace_volume="workspace",
            artifact_host_path="/artifacts/42",
        )
        == 0
    )
    assert len(mounted) == 1 and not mounted[0].exists()
    assert manager._last_helper_output[42] == "verified"


async def test_scratch_is_mounted_only_as_a_private_verifier_tmp(monkeypatch):
    payloads = []

    async def request(_method, _path, **kwargs):
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
        verification_scratch=(f"{HOST}/sample", MARKER),
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
    assert host_config["LogConfig"] == {"Type": "local", "Config": {"max-size": "10m", "max-file": "2"}}
    assert host_config["Mounts"] == [
        {
            "Type": "bind",
            "Source": f"{HOST}/sample",
            "Target": "/logos-verifier-scratch",
            "ReadOnly": False,
            "BindOptions": {"CreateMountpoint": False, "NonRecursive": True, "Propagation": "rprivate"},
        }
    ]
    assert f"LOGOS_VERIFICATION_SCRATCH_MARKER={MARKER}" in payloads[0]["Env"]
    assert "TMPDIR=/logos-verifier-scratch" in payloads[0]["Env"]
    for phase in ("agent", "prepare", "finalize"):
        with pytest.raises(ValueError, match="only a verifier"):
            await docker_engine.create_session_container(env={"LOGOS_SESSION_PHASE": phase}, **options)
