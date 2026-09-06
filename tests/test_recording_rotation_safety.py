import asyncio
import importlib
import signal
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import (
    GlobalSettings,
    TwitchUpstreamCoordinationState,
    TwitchUpstreamLease,
)
from app.services.recording.process_manager import ProcessManager
from app.services.recording.exceptions import ProcessError
from app.services.twitch_upstream_coordinator import (
    ProcessIdentity,
    TwitchUpstreamCoordinator,
)


class FakeProcess:
    def __init__(
        self,
        *,
        returncode: int | None = None,
        terminate_error: BaseException | None = None,
        release_on_terminate: bool = True,
        release_on_kill: bool = True,
        stdout: bytes = b"",
        stderr: bytes = b"harmless local failure",
    ) -> None:
        self.returncode = returncode
        self.terminate_error = terminate_error
        self.release_on_terminate = release_on_terminate
        self.release_on_kill = release_on_kill
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self.communicate_calls = 0
        self.wait_started = asyncio.Event()
        self.release = asyncio.Event()
        self.stdout = stdout
        self.stderr = stderr
        if returncode is not None:
            self.release.set()

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.terminate_error is not None:
            raise self.terminate_error
        if self.release_on_terminate:
            self.returncode = -15
            self.release.set()

    def kill(self) -> None:
        self.kill_calls += 1
        if self.release_on_kill:
            self.returncode = -9
            self.release.set()

    async def wait(self) -> int:
        self.wait_calls += 1
        self.wait_started.set()
        await self.release.wait()
        assert self.returncode is not None
        return self.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicate_calls += 1
        return self.stdout, self.stderr


def make_manager(process: FakeProcess, stream_id: int = 7) -> ProcessManager:
    manager = object.__new__(ProcessManager)
    manager.active_processes = {f"stream_{stream_id}": process}
    manager.long_stream_processes = {}
    manager.lock = asyncio.Lock()
    manager.rotation_locks = {}
    return manager


def make_segment_info() -> dict:
    return {
        "stream_id": 7,
        "base_output_path": "/tmp/recording.ts",
        "segment_dir": "/tmp/recording_segments",
        "current_segment_path": "/tmp/recording_segments/recording_part001.ts",
        "segment_count": 1,
        "segment_start_time": datetime(2026, 1, 1),
        "total_segments": [],
        "monitor_task": None,
    }


def install_segment_starter(
    manager: ProcessManager,
    started: list[FakeProcess],
) -> None:
    async def start_segment(stream, segment_path, quality, segment_info):
        replacement = FakeProcess()
        manager.active_processes[f"stream_{stream.id}"] = replacement
        started.append(replacement)
        return replacement

    manager._start_segment = start_segment


def install_immediate_exit_start(
    monkeypatch,
    failed_process: FakeProcess,
) -> tuple[ProcessManager, SimpleNamespace, list]:
    process_manager_module = importlib.import_module(
        "app.services.recording.process_manager"
    )
    manager = object.__new__(ProcessManager)
    manager.active_processes = {}
    manager.long_stream_processes = {}
    manager.lock = asyncio.Lock()
    manager.rotation_locks = {}
    manager.logging_service = None
    monitor_coroutines = []

    class FakeQuery:
        def first(self):
            return None

        def filter(self, *args):
            return self

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def query(self, *args):
            return FakeQuery()

    class FakeTokenService:
        def __init__(self, db):
            pass

        async def resolve_recording_token(self):
            return SimpleNamespace(token=None, source=None, stored_version=None)

    async def create_subprocess(*args, **kwargs):
        return failed_process

    original_create_task = asyncio.create_task

    def create_task(coroutine):
        if coroutine.cr_code.co_name == "_monitor_long_stream":
            monitor_coroutines.append(coroutine)
            coroutine.close()
            return SimpleNamespace()
        return original_create_task(coroutine)

    monkeypatch.setattr("app.database.SessionLocal", FakeSession)
    monkeypatch.setattr(
        "app.services.system.twitch_token_service.TwitchTokenService",
        FakeTokenService,
    )
    monkeypatch.setattr(
        process_manager_module,
        "get_streamlink_command",
        lambda **kwargs: ["harmless-local-command"],
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    monkeypatch.setattr(asyncio, "create_task", create_task)
    monkeypatch.setattr(
        process_manager_module,
        "ASYNC_DELAYS",
        SimpleNamespace(PROCESS_START_GRACE=0, RECORDING_ERROR_RECOVERY=1),
    )
    stream = SimpleNamespace(
        id=7,
        streamer_id=3,
        streamer=SimpleNamespace(username="local-test"),
        title="",
        category_name="",
    )
    return manager, stream, monitor_coroutines


@pytest.mark.asyncio
async def test_rotation_reaps_exited_process_before_starting_replacement() -> None:
    old_process = FakeProcess(returncode=0)
    manager = make_manager(old_process)
    segment_info = make_segment_info()
    started = []
    install_segment_starter(manager, started)

    rotated = await manager._rotate_segment(SimpleNamespace(id=7), segment_info, "best")

    assert rotated is True
    assert old_process.wait_calls == 1
    assert old_process.terminate_calls == 0
    assert old_process.kill_calls == 0
    assert manager.active_processes["stream_7"] is started[0]


@pytest.mark.asyncio
async def test_rotation_terminates_and_waits_before_starting_replacement() -> None:
    old_process = FakeProcess()
    manager = make_manager(old_process)
    segment_info = make_segment_info()
    started = []
    install_segment_starter(manager, started)

    rotated = await manager._rotate_segment(SimpleNamespace(id=7), segment_info, "best")

    assert rotated is True
    assert old_process.terminate_calls == 1
    assert old_process.wait_calls == 1
    assert old_process.kill_calls == 0
    assert len(started) == 1


@pytest.mark.asyncio
async def test_rotation_kills_and_waits_after_graceful_timeout(monkeypatch) -> None:
    old_process = FakeProcess(release_on_terminate=False)
    manager = make_manager(old_process)
    segment_info = make_segment_info()
    started = []
    install_segment_starter(manager, started)
    monkeypatch.setattr(
        "app.services.recording.process_manager.ASYNC_DELAYS",
        SimpleNamespace(RECORDING_ERROR_RECOVERY=0.01),
    )

    rotated = await manager._rotate_segment(SimpleNamespace(id=7), segment_info, "best")

    assert rotated is True
    assert old_process.terminate_calls == 1
    assert old_process.kill_calls == 1
    assert old_process.wait_calls == 2
    assert len(started) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "termination_error",
    [ProcessLookupError(), PermissionError()],
)
async def test_rotation_retains_tracking_when_terminate_raises(
    termination_error: BaseException,
) -> None:
    old_process = FakeProcess(terminate_error=termination_error)
    manager = make_manager(old_process)
    segment_info = make_segment_info()
    original_path = segment_info["current_segment_path"]
    started = []
    install_segment_starter(manager, started)

    rotated = await manager._rotate_segment(SimpleNamespace(id=7), segment_info, "best")

    assert rotated is False
    assert manager.active_processes["stream_7"] is old_process
    assert segment_info["current_segment_path"] == original_path
    assert started == []


@pytest.mark.asyncio
async def test_rotation_retains_tracking_when_forced_wait_times_out(
    monkeypatch,
) -> None:
    old_process = FakeProcess(
        release_on_terminate=False,
        release_on_kill=False,
    )
    manager = make_manager(old_process)
    segment_info = make_segment_info()
    started = []
    install_segment_starter(manager, started)
    monkeypatch.setattr(
        "app.services.recording.process_manager.ASYNC_DELAYS",
        SimpleNamespace(RECORDING_ERROR_RECOVERY=0.01),
    )

    rotated = await manager._rotate_segment(SimpleNamespace(id=7), segment_info, "best")

    assert rotated is False
    assert manager.active_processes["stream_7"] is old_process
    assert old_process.kill_calls == 1
    assert started == []


@pytest.mark.asyncio
async def test_rotation_cancellation_retains_tracking() -> None:
    old_process = FakeProcess(release_on_terminate=False)
    manager = make_manager(old_process)
    segment_info = make_segment_info()
    started = []
    install_segment_starter(manager, started)

    rotation = asyncio.create_task(
        manager._rotate_segment(SimpleNamespace(id=7), segment_info, "best")
    )
    await old_process.wait_started.wait()
    rotation.cancel()

    with pytest.raises(asyncio.CancelledError):
        await rotation
    assert manager.active_processes["stream_7"] is old_process
    assert started == []


@pytest.mark.asyncio
async def test_rotation_does_not_replace_newer_tracked_process() -> None:
    old_process = FakeProcess(release_on_terminate=False)
    newer_process = FakeProcess()
    manager = make_manager(old_process)
    segment_info = make_segment_info()
    started = []
    install_segment_starter(manager, started)

    rotation = asyncio.create_task(
        manager._rotate_segment(SimpleNamespace(id=7), segment_info, "best")
    )
    await old_process.wait_started.wait()
    manager.active_processes["stream_7"] = newer_process
    old_process.returncode = -15
    old_process.release.set()

    assert await rotation is False
    assert manager.active_processes["stream_7"] is newer_process
    assert started == []


@pytest.mark.asyncio
async def test_concurrent_rotation_requests_start_one_replacement() -> None:
    old_process = FakeProcess(release_on_terminate=False)
    manager = make_manager(old_process)
    first_segment_info = make_segment_info()
    second_segment_info = make_segment_info()
    started = []
    install_segment_starter(manager, started)

    first_rotation = asyncio.create_task(
        manager._rotate_segment(SimpleNamespace(id=7), first_segment_info, "best")
    )
    await old_process.wait_started.wait()
    second_rotation = asyncio.create_task(
        manager._rotate_segment(SimpleNamespace(id=7), second_segment_info, "best")
    )
    await asyncio.sleep(0)
    old_process.returncode = -15
    old_process.release.set()

    results = await asyncio.gather(first_rotation, second_rotation)
    assert results == [True, False]
    assert old_process.terminate_calls == 1
    assert len(started) == 1
    assert manager.active_processes["stream_7"] is started[0]


@pytest.mark.asyncio
async def test_monitor_finalization_and_rotation_cannot_both_win() -> None:
    old_process = FakeProcess(returncode=0)
    manager = make_manager(old_process)
    segment_info = make_segment_info()
    manager.long_stream_processes["stream_7"] = segment_info
    started = []
    install_segment_starter(manager, started)
    finalization_started = asyncio.Event()
    release_finalization = asyncio.Event()

    async def finalize_segmented_recording(info):
        assert info is segment_info
        finalization_started.set()
        await release_finalization.wait()

    manager._finalize_segmented_recording = finalize_segmented_recording
    monitor = asyncio.create_task(manager.monitor_process(old_process))
    await finalization_started.wait()
    rotation = asyncio.create_task(
        manager._rotate_segment(SimpleNamespace(id=7), segment_info, "best")
    )

    try:
        rotated_while_finalizing = await asyncio.wait_for(
            asyncio.shield(rotation), timeout=0.05
        )
    except TimeoutError:
        rotated_while_finalizing = None
    owner_while_finalizing = manager.active_processes.get("stream_7")
    release_finalization.set()
    monitor_result, rotation_result = await asyncio.gather(monitor, rotation)

    assert (
        rotated_while_finalizing is not True and owner_while_finalizing not in started
    ), (
        "rotation and finalization both won: "
        f"rotation={rotated_while_finalizing!r}, "
        f"replacement_owned={owner_while_finalizing in started}"
    )
    assert monitor_result == 0
    assert rotation_result is False
    assert started == []


@pytest.mark.asyncio
@pytest.mark.parametrize("finalization_outcome", ["success", "error", "cancelled"])
async def test_segmented_monitor_drains_logs_and_releases_exact_secret_context(
    finalization_outcome: str,
) -> None:
    class PipeBoundProcess(FakeProcess):
        async def wait(self) -> int:
            raise AssertionError("wait() cannot drain full child pipes")

    process = PipeBoundProcess(
        returncode=0,
        stdout=b"local stdout",
        stderr=b"Authorization=OAuth fixture-auth-value-807",
    )
    process.pid = 401
    replacement = FakeProcess()
    manager = make_manager(process)
    segment_info = make_segment_info()
    replacement_segment_info = {"attempt": "replacement"}
    manager.long_stream_processes["stream_7"] = segment_info
    manager._streamlink_output_secrets = {
        process: ("fixture-auth-value-807",),
        replacement: ("replacement-secret",),
    }
    logged = []
    releases = []

    class LoggingService:
        def log_streamlink_output(self, *args, **kwargs):
            logged.append((args, kwargs))

    class Coordinator:
        async def release(self, **values):
            releases.append(values)

    async def finalize(info):
        assert info is segment_info
        manager.active_processes["stream_7"] = replacement
        manager.long_stream_processes["stream_7"] = replacement_segment_info
        if finalization_outcome == "error":
            raise RuntimeError("injected finalization failure")
        if finalization_outcome == "cancelled":
            raise asyncio.CancelledError

    segment_info.update(
        {"upstream_channel_key": "stable-channel", "upstream_generation": 4}
    )
    manager.logging_service = LoggingService()
    manager.upstream_coordinator = Coordinator()
    manager._finalize_segmented_recording = finalize

    if finalization_outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await manager.monitor_process(process)
        result = None
    else:
        result = await manager.monitor_process(process)

    assert (
        result == {"success": 0, "error": -1, "cancelled": None}[finalization_outcome]
    )
    assert process.wait_calls == 0
    assert process.communicate_calls == 1
    assert logged == [
        (
            (
                "stream_7",
                b"local stdout",
                b"Authorization=OAuth fixture-auth-value-807",
                0,
            ),
            {"known_secrets": ("fixture-auth-value-807",)},
        )
    ]
    assert process not in manager._streamlink_output_secrets
    assert manager._streamlink_output_secrets[replacement] == ("replacement-secret",)
    assert manager.active_processes["stream_7"] is replacement
    assert manager.long_stream_processes["stream_7"] is replacement_segment_info
    assert releases == (
        [
            {
                "channel_key": "stable-channel",
                "generation": 4,
                "reason": "recording_process_exited",
            }
        ]
        if finalization_outcome == "success"
        else []
    )


@pytest.mark.asyncio
async def test_rotation_cancellation_tracks_or_reaps_created_replacement(
    monkeypatch,
) -> None:
    process_manager_module = importlib.import_module(
        "app.services.recording.process_manager"
    )
    old_process = FakeProcess(returncode=0)
    old_process.pid = 401
    manager = make_manager(old_process)
    manager.logging_service = None
    segment_info = make_segment_info()
    segment_info.update(
        {
            "upstream_channel_key": "stable-channel",
            "upstream_generation": 4,
            "upstream_process_group_id": 401,
            "upstream_process_start_fingerprint": "birth-401",
        }
    )
    replacement = FakeProcess()
    replacement.pid = 1234
    child_created = asyncio.Event()
    calls = []

    class Coordinator:
        async def begin_rotation(self, **values):
            calls.append(("begin", values))
            return SimpleNamespace(generation=5)

        async def assert_stop_authorized(self, **values):
            calls.append(("authorize", values))

        async def inspect_process_identity(self, process_pid):
            return ProcessIdentity(
                process_pid,
                process_pid,
                datetime.now(timezone.utc),
                f"birth-{process_pid}",
            )

        async def release(self, **values):
            calls.append(("release", values))
            return True

    class FakeQuery:
        def first(self):
            return None

        def filter(self, *args):
            return self

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def query(self, *args):
            return FakeQuery()

    class FakeTokenService:
        def __init__(self, db):
            pass

        async def resolve_recording_token(self):
            return SimpleNamespace(token=None, source=None, stored_version=None)

    async def create_subprocess(*args, **kwargs):
        child_created.set()
        return replacement

    monkeypatch.setattr("app.database.SessionLocal", FakeSession)
    monkeypatch.setattr(
        "app.services.system.twitch_token_service.TwitchTokenService",
        FakeTokenService,
    )
    monkeypatch.setattr(
        process_manager_module,
        "get_streamlink_command",
        lambda **kwargs: ["harmless-local-command"],
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    monkeypatch.setattr(
        process_manager_module,
        "ASYNC_DELAYS",
        SimpleNamespace(PROCESS_START_GRACE=3600, RECORDING_ERROR_RECOVERY=1),
    )
    manager.upstream_coordinator = Coordinator()

    async def terminate_process_group(
        process, process_group_id, timeout, process_start_fingerprint=None
    ):
        if process is old_process:
            await process.wait()
            return True
        assert process is replacement
        assert process_group_id == replacement.pid
        process.returncode = -15
        process.release.set()
        await process.wait()
        return True

    manager._terminate_process_group = terminate_process_group
    stream = SimpleNamespace(
        id=7,
        streamer_id=3,
        streamer=SimpleNamespace(username="local-test"),
        title="",
        category_name="",
    )

    rotation = asyncio.create_task(
        manager._rotate_segment(stream, segment_info, "best")
    )
    await child_created.wait()
    rotation.cancel()

    with pytest.raises(asyncio.CancelledError):
        await rotation

    assert manager.active_processes.get("stream_7") is not old_process
    assert replacement.returncode == -15
    assert replacement.wait_calls == 1
    assert [name for name, _values in calls] == [
        "begin",
        "authorize",
        "authorize",
        "release",
    ]
    assert calls[-1][1] == {
        "channel_key": "stable-channel",
        "generation": 5,
        "reason": "rotation_cancelled",
    }


@pytest.mark.asyncio
async def test_rotation_immediate_child_exit_releases_rotating_generation(
    monkeypatch,
) -> None:
    create_task = asyncio.create_task
    failed_process = FakeProcess(returncode=1)
    failed_process.pid = 402
    manager, stream, _monitor_coroutines = install_immediate_exit_start(
        monkeypatch, failed_process
    )
    monkeypatch.setattr(asyncio, "create_task", create_task)
    old_process = FakeProcess(returncode=0)
    old_process.pid = 401
    manager.active_processes["stream_7"] = old_process
    segment_info = make_segment_info()
    segment_info.update(
        {
            "upstream_channel_key": "stable-channel",
            "upstream_generation": 4,
            "upstream_process_group_id": 401,
            "upstream_process_start_fingerprint": "birth-401",
        }
    )
    calls = []

    class Coordinator:
        async def begin_rotation(self, **values):
            calls.append(("begin", values))
            return SimpleNamespace(generation=5)

        async def assert_stop_authorized(self, **values):
            calls.append(("authorize", values))

        async def release(self, **values):
            calls.append(("release", values))
            return True

    manager.upstream_coordinator = Coordinator()

    assert await manager._rotate_segment(stream, segment_info, "best") is False
    assert [name for name, _values in calls] == ["begin", "authorize", "release"]
    assert calls[-1][1] == {
        "channel_key": "stable-channel",
        "generation": 5,
        "reason": "rotation_start_failed",
    }
    assert manager.active_processes == {}


@pytest.mark.asyncio
async def test_rotation_failed_handoff_reaps_only_authorized_replacement() -> None:
    old_process = FakeProcess(returncode=0)
    old_process.pid = 401
    replacement = FakeProcess()
    replacement.pid = 402
    manager = make_manager(old_process)
    segment_info = make_segment_info()
    segment_info.update(
        {
            "upstream_channel_key": "stable-channel",
            "upstream_generation": 4,
            "upstream_process_group_id": 401,
            "upstream_process_start_fingerprint": "birth-401",
        }
    )
    calls = []

    class Coordinator:
        async def begin_rotation(self, **values):
            calls.append(("begin", values))
            return SimpleNamespace(generation=5)

        async def assert_stop_authorized(self, **values):
            calls.append(("authorize", values))

        async def handoff_rotation(self, **values):
            calls.append(("handoff", values))
            raise RuntimeError("local handoff failure")

        async def inspect_process_identity(self, process_pid):
            return ProcessIdentity(
                process_pid,
                process_pid,
                datetime.now(timezone.utc),
                f"birth-{process_pid}",
            )

        async def release(self, **values):
            calls.append(("release", values))
            return True

    async def start_segment(stream, segment_path, quality, info):
        manager.active_processes[f"stream_{stream.id}"] = replacement
        return replacement

    terminated = []

    async def terminate_process_group(
        process, process_group_id, timeout, process_start_fingerprint=None
    ):
        terminated.append((process, process_group_id))
        process.returncode = -15
        process.release.set()
        await process.wait()
        return True

    manager.upstream_coordinator = Coordinator()
    manager._start_segment = start_segment
    manager._terminate_process_group = terminate_process_group

    assert (
        await manager._rotate_segment(SimpleNamespace(id=7), segment_info, "best")
        is False
    )
    assert [name for name, _values in calls] == [
        "begin",
        "authorize",
        "handoff",
        "authorize",
        "release",
    ]
    assert terminated == [(replacement, replacement.pid)]
    assert replacement.kill_calls == 0
    assert calls[-1][1] == {
        "channel_key": "stable-channel",
        "generation": 5,
        "reason": "rotation_handoff_failed",
    }


@pytest.mark.asyncio
async def test_start_recording_cleans_up_immediately_exited_child(
    monkeypatch,
    tmp_path,
) -> None:
    failed_process = FakeProcess(returncode=1)
    failed_process.pid = 1234
    manager, stream, monitor_coroutines = install_immediate_exit_start(
        monkeypatch,
        failed_process,
    )

    with pytest.raises(ProcessError):
        await manager.start_recording_process(
            stream,
            str(tmp_path / "recording.ts"),
            "best",
        )

    assert failed_process.communicate_calls == 1
    assert (manager.active_processes, manager.long_stream_processes) == ({}, {})
    assert monitor_coroutines == []


@pytest.mark.asyncio
async def test_segment_start_and_rotation_own_one_exact_completion_drain_task(
    monkeypatch,
    tmp_path,
) -> None:
    class PipeBoundProcess(FakeProcess):
        def __init__(self, pid: int) -> None:
            super().__init__()
            self.pid = pid
            self.drain_started = asyncio.Event()

        async def communicate(self) -> tuple[bytes, bytes]:
            self.communicate_calls += 1
            self.drain_started.set()
            await self.release.wait()
            return self.stdout, self.stderr

    process = PipeBoundProcess(1234)
    manager, stream, monitor_coroutines = install_immediate_exit_start(
        monkeypatch,
        process,
    )

    async def finalize(_segment_info):
        return None

    manager._finalize_segmented_recording = finalize

    started_process = await manager.start_recording_process(
        stream,
        str(tmp_path / "recording.ts"),
        "best",
    )
    await asyncio.wait_for(process.drain_started.wait(), timeout=0.1)

    completion_task = manager._segment_completion_tasks[process]
    assert started_process is process
    assert completion_task is manager._track_segment_completion(process)
    assert len(manager._segment_completion_tasks) == 1
    assert process.communicate_calls == 1
    assert monitor_coroutines

    replacement = PipeBoundProcess(1235)

    async def create_replacement(*args, **kwargs):
        return replacement

    process_manager_module = importlib.import_module(
        "app.services.recording.process_manager"
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_replacement)
    monkeypatch.setattr(
        process_manager_module,
        "get_streamlink_command",
        lambda **kwargs: [
            "streamlink",
            "--twitch-api-header=Authorization=OAuth replacement-secret-807",
        ],
    )

    segment_info = manager.long_stream_processes["stream_7"]
    assert await manager._rotate_segment(stream, segment_info, "best") is True
    await asyncio.wait_for(replacement.drain_started.wait(), timeout=0.1)
    assert await completion_task == -15
    await asyncio.sleep(0)

    assert process not in manager._segment_completion_tasks
    assert process not in manager._streamlink_output_secrets
    replacement_task = manager._segment_completion_tasks[replacement]
    assert replacement_task is manager._track_segment_completion(replacement)
    assert len(manager._segment_completion_tasks) == 1
    assert replacement.communicate_calls == 1
    assert manager._streamlink_output_secrets[replacement] == (
        "replacement-secret-807",
    )

    replacement.returncode = 0
    replacement.release.set()
    assert await replacement_task == 0
    await asyncio.sleep(0)

    assert replacement not in manager._segment_completion_tasks
    assert replacement not in manager._streamlink_output_secrets


@pytest.mark.asyncio
async def test_immediate_exit_uses_child_bound_sanitization_context(
    monkeypatch, tmp_path, caplog
) -> None:
    failed_process = FakeProcess(
        returncode=1,
        stderr=b"Authorization=OAuth fixture-auth-value-807",
    )
    failed_process.pid = 1234
    manager, stream, _monitor_coroutines = install_immediate_exit_start(
        monkeypatch, failed_process
    )
    calls = []

    class LoggingService:
        def get_streamlink_log_path(self, streamer_name):
            return str(tmp_path / "streamlink-child.log")

        def log_streamlink_output(self, *args, **kwargs):
            calls.append((args, kwargs))

    manager.logging_service = LoggingService()
    process_manager_module = importlib.import_module(
        "app.services.recording.process_manager"
    )
    monkeypatch.setattr(
        process_manager_module,
        "get_streamlink_command",
        lambda **kwargs: [
            "streamlink",
            "--twitch-api-header=Authorization=OAuth fixture-auth-value-807",
        ],
    )

    with pytest.raises(ProcessError):
        await manager.start_recording_process(
            stream, str(tmp_path / "recording.ts"), "best"
        )

    assert calls
    assert calls[0][1]["known_secrets"] == ("fixture-auth-value-807",)
    assert "fixture-auth-value-807" not in caplog.text
    assert failed_process not in manager._streamlink_output_secrets


@pytest.mark.asyncio
async def test_immediate_exit_cleanup_preserves_newer_owners(
    monkeypatch,
    tmp_path,
) -> None:
    failed_process = FakeProcess(returncode=1)
    failed_process.pid = 1234
    manager, stream, monitor_coroutines = install_immediate_exit_start(
        monkeypatch,
        failed_process,
    )
    newer_process = FakeProcess()
    newer_segment_info = {"attempt": "newer"}

    class ReplacingLock:
        def __init__(self):
            self.enter_calls = 0

        async def __aenter__(self):
            self.enter_calls += 1
            if self.enter_calls == 2:
                manager.active_processes["stream_7"] = newer_process
                manager.long_stream_processes["stream_7"] = newer_segment_info

        async def __aexit__(self, *args):
            return None

    manager.lock = ReplacingLock()

    with pytest.raises(ProcessError):
        await manager.start_recording_process(
            stream,
            str(tmp_path / "recording.ts"),
            "best",
        )

    assert manager.active_processes["stream_7"] is newer_process
    assert manager.long_stream_processes["stream_7"] is newer_segment_info
    assert failed_process.communicate_calls == 1
    assert monitor_coroutines == []


@pytest.mark.asyncio
async def test_rotation_uses_generation_fenced_handoff() -> None:
    old_process = FakeProcess()
    old_process.pid = 401
    manager = make_manager(old_process)
    segment_info = make_segment_info()
    segment_info.update(
        {
            "upstream_channel_key": "stable-channel",
            "upstream_generation": 4,
            "upstream_process_group_id": 401,
            "upstream_process_start_fingerprint": "birth-401",
        }
    )
    calls = []

    class Coordinator:
        async def begin_rotation(self, **values):
            calls.append(("begin", values))
            return SimpleNamespace(generation=5)

        async def assert_stop_authorized(self, **values):
            calls.append(("authorize", values))

        async def handoff_rotation(self, **values):
            calls.append(("handoff", values))
            return SimpleNamespace(
                generation=5,
                process_group_id=402,
                process_start_fingerprint="birth-402",
            )

    replacement = FakeProcess()
    replacement.pid = 402

    async def start_segment(stream, segment_path, quality, info):
        manager.active_processes[f"stream_{stream.id}"] = replacement
        return replacement

    async def terminate_process_group(
        process, process_group_id, timeout, process_start_fingerprint=None
    ):
        assert process is old_process
        process.returncode = -15
        process.release.set()
        await process.wait()
        return True

    manager.upstream_coordinator = Coordinator()
    manager._start_segment = start_segment
    manager._terminate_process_group = terminate_process_group

    rotated = await manager._rotate_segment(SimpleNamespace(id=7), segment_info, "best")

    assert rotated is True
    assert [name for name, _values in calls] == ["begin", "authorize", "handoff"]
    assert calls[1][1]["generation"] == 5
    assert calls[2][1]["generation"] == 5
    assert segment_info["upstream_generation"] == 5


@pytest.mark.asyncio
async def test_rotation_real_coordinator_hands_off_replacement(
    monkeypatch, tmp_path
) -> None:
    process_manager_module = importlib.import_module(
        "app.services.recording.process_manager"
    )
    engine = create_engine(
        f"sqlite:///{tmp_path / 'rotation.db'}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(
        engine,
        tables=[
            TwitchUpstreamCoordinationState.__table__,
            TwitchUpstreamLease.__table__,
            GlobalSettings.__table__,
        ],
    )
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    started_at = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

    class Inspector:
        def inspect(self, pid):
            return ProcessIdentity(pid, pid, started_at, f"birth-{pid}")

        def is_exact_process_alive(self, **identity):
            return True

    coordinator = TwitchUpstreamCoordinator(
        Session,
        utc_clock=lambda: started_at,
        monotonic_clock=lambda: 1.0,
        process_inspector=Inspector(),
    )
    reservation = await coordinator.reserve(
        channel_key="stable-rotation-channel",
        auth_key=None,
        purpose="RECORDING",
        recording_id=12,
    )
    active = await coordinator.activate(
        channel_key=reservation.channel_key,
        generation=reservation.generation,
        process_pid=401,
        process_group_id=401,
        process_started_at=started_at,
        process_start_fingerprint="birth-401",
    )

    old_process = FakeProcess()
    old_process.pid = 401
    replacement = FakeProcess()
    replacement.pid = 402
    manager = make_manager(old_process)
    manager.logging_service = None
    manager.upstream_coordinator = coordinator
    segment_info = make_segment_info()
    segment_info.update(
        {
            "upstream_channel_key": active.channel_key,
            "upstream_generation": active.generation,
            "upstream_process_group_id": active.process_group_id,
            "upstream_process_start_fingerprint": active.process_start_fingerprint,
        }
    )
    events = []

    class FakeQuery:
        def first(self):
            return None

        def filter(self, *args):
            return self

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def query(self, *args):
            return FakeQuery()

    class FakeTokenService:
        def __init__(self, db):
            pass

        async def resolve_recording_token(self):
            return SimpleNamespace(token=None, source=None, stored_version=None)

    class FakeNotificationService:
        async def send_recording_notification(self, **kwargs):
            return None

    async def create_subprocess(*args, **kwargs):
        events.append("replacement_started")
        return replacement

    async def stop_process_group(
        process, process_group_id, timeout, process_start_fingerprint=None
    ):
        assert process is old_process
        assert process_group_id == 401
        events.append("old_group_stopped")
        process.returncode = -15
        process.release.set()
        await process.wait()
        return True

    monkeypatch.setattr("app.database.SessionLocal", FakeSession)
    monkeypatch.setattr(
        "app.services.system.twitch_token_service.TwitchTokenService",
        FakeTokenService,
    )
    monkeypatch.setattr(
        "app.services.notifications.external_notification_service.ExternalNotificationService",
        FakeNotificationService,
    )
    monkeypatch.setattr(
        process_manager_module,
        "get_streamlink_command",
        lambda **kwargs: ["harmless-local-command"],
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    monkeypatch.setattr(
        process_manager_module,
        "ASYNC_DELAYS",
        SimpleNamespace(PROCESS_START_GRACE=0, RECORDING_ERROR_RECOVERY=1),
    )
    manager._terminate_process_group = stop_process_group
    stream = SimpleNamespace(
        id=7,
        streamer_id=3,
        streamer=SimpleNamespace(username="local-test"),
        title="",
        category_name="",
    )

    rotated = await manager._rotate_segment(stream, segment_info, "best")

    assert rotated is True
    assert events == ["old_group_stopped", "replacement_started"]
    with Session() as db:
        lease = db.query(TwitchUpstreamLease).one()
        assert (lease.state, lease.generation, lease.process_pid) == (
            "ACTIVE",
            2,
            402,
        )
        assert lease.process_start_fingerprint == "birth-402"
    engine.dispose()


@pytest.mark.asyncio
async def test_recording_immediate_exit_releases_reserved_generation(
    monkeypatch, tmp_path
) -> None:
    failed_process = FakeProcess(returncode=1)
    failed_process.pid = 501
    manager, stream, _monitor_coroutines = install_immediate_exit_start(
        monkeypatch, failed_process
    )
    stream.streamer.twitch_id = "stable-recording-channel"
    calls = []

    class Coordinator:
        async def reserve(self, **values):
            calls.append(("reserve", values))
            return SimpleNamespace(channel_key=values["channel_key"], generation=1)

        async def activate(self, **values):
            calls.append(("activate", values))
            return SimpleNamespace(
                generation=1,
                process_group_id=501,
                process_start_fingerprint="birth-501",
            )

        async def inspect_process_identity(self, process_pid):
            return ProcessIdentity(
                process_pid,
                process_pid,
                datetime.now(timezone.utc),
                f"birth-{process_pid}",
            )

        async def release(self, **values):
            calls.append(("release", values))
            return True

    manager.upstream_coordinator = Coordinator()

    with pytest.raises(ProcessError):
        await manager.start_recording_process(
            stream,
            str(tmp_path / "recording.ts"),
            "best",
            recording_id=12,
        )

    assert [name for name, _values in calls] == ["reserve", "release"]
    assert calls[0][1]["channel_key"] == "stable-recording-channel"
    assert calls[0][1]["recording_id"] == 12
    assert calls[0][1]["auth_key"] is None
    assert manager.active_processes == {}


@pytest.mark.asyncio
async def test_recording_activation_failure_reaps_exact_child_before_durable_release(
    monkeypatch, tmp_path
) -> None:
    process_manager_module = importlib.import_module(
        "app.services.recording.process_manager"
    )
    engine = create_engine(
        f"sqlite:///{tmp_path / 'recording-activation-failure.db'}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(
        engine,
        tables=[
            TwitchUpstreamCoordinationState.__table__,
            TwitchUpstreamLease.__table__,
            GlobalSettings.__table__,
        ],
    )
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    started_at = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)

    class Inspector:
        def inspect(self, pid):
            return ProcessIdentity(pid, pid + 1000, started_at, f"birth-{pid}")

        def is_exact_process_alive(self, **identity):
            return True

    coordinator = TwitchUpstreamCoordinator(
        Session,
        utc_clock=lambda: started_at,
        monotonic_clock=lambda: 1.0,
        process_inspector=Inspector(),
    )
    activation_values = []

    async def fail_activation(**values):
        activation_values.append(values)
        raise RuntimeError("injected activation failure")

    coordinator.activate = fail_activation
    process = FakeProcess()
    process.pid = 501
    manager = object.__new__(ProcessManager)
    manager.active_processes = {}
    manager.long_stream_processes = {}
    manager.lock = asyncio.Lock()
    manager.rotation_locks = {}
    manager.logging_service = None
    manager.upstream_coordinator = coordinator

    class FakeQuery:
        def first(self):
            return None

        def filter(self, *args):
            return self

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def query(self, *args):
            return FakeQuery()

    class FakeTokenService:
        def __init__(self, db):
            pass

        async def resolve_recording_token(self):
            return SimpleNamespace(token=None, source=None, stored_version=None)

    async def create_subprocess(*args, **kwargs):
        return process

    signaled = []

    def kill_process_group(process_group_id, sig):
        signaled.append((process_group_id, sig))
        process.returncode = -sig
        process.release.set()

    original_release = coordinator.release
    release_observations = []

    async def release_after_reap(**values):
        release_observations.append((values, process.returncode, tuple(signaled)))
        return await original_release(**values)

    coordinator.release = release_after_reap
    monkeypatch.setattr("app.database.SessionLocal", FakeSession)
    monkeypatch.setattr(
        "app.services.system.twitch_token_service.TwitchTokenService",
        FakeTokenService,
    )
    monkeypatch.setattr(
        process_manager_module,
        "get_streamlink_command",
        lambda **kwargs: ["harmless-local-command"],
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    monkeypatch.setattr(process_manager_module.os, "killpg", kill_process_group)
    monkeypatch.setattr(
        process_manager_module,
        "ASYNC_DELAYS",
        SimpleNamespace(PROCESS_START_GRACE=0, RECORDING_ERROR_RECOVERY=1),
    )
    stream = SimpleNamespace(
        id=7,
        streamer_id=3,
        streamer=SimpleNamespace(
            username="local-test", twitch_id="recording-activation-channel"
        ),
        title="",
        category_name="",
    )

    with pytest.raises(ProcessError):
        await manager.start_recording_process(
            stream,
            str(tmp_path / "recording.ts"),
            "best",
            recording_id=12,
        )

    assert activation_values == [
        {
            "channel_key": "recording-activation-channel",
            "generation": 1,
            "process_pid": 501,
            "process_group_id": 1501,
            "process_started_at": started_at,
            "process_start_fingerprint": "birth-501",
        }
    ]
    assert release_observations == [
        (
            {
                "channel_key": "recording-activation-channel",
                "generation": 1,
                "reason": "recording_start_failed",
            },
            -15,
            ((1501, signal.SIGTERM),),
        )
    ]
    assert (manager.active_processes, manager.long_stream_processes) == ({}, {})
    with Session() as db:
        lease = db.query(TwitchUpstreamLease).one()
        assert (lease.state, lease.release_reason) == (
            "RELEASED",
            "recording_start_failed",
        )
    engine.dispose()


async def make_real_failed_handoff_rotation(tmp_path, name):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(
        engine,
        tables=[
            TwitchUpstreamCoordinationState.__table__,
            TwitchUpstreamLease.__table__,
            GlobalSettings.__table__,
        ],
    )
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    started_at = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

    class Inspector:
        def inspect(self, pid):
            return ProcessIdentity(pid, pid, started_at, f"birth-{pid}")

        def is_exact_process_alive(self, **identity):
            pid = identity["process_pid"]
            return identity["process_start_fingerprint"] == f"birth-{pid}"

    coordinator = TwitchUpstreamCoordinator(
        Session,
        utc_clock=lambda: started_at,
        monotonic_clock=lambda: 1.0,
        process_inspector=Inspector(),
    )
    reservation = await coordinator.reserve(
        channel_key="failed-handoff-channel",
        auth_key=None,
        purpose="RECORDING",
        recording_id=12,
    )
    active = await coordinator.activate(
        channel_key=reservation.channel_key,
        generation=reservation.generation,
        process_pid=401,
        process_group_id=401,
        process_started_at=started_at,
        process_start_fingerprint="birth-401",
    )
    old_process = FakeProcess()
    old_process.pid = 401
    replacement = FakeProcess()
    replacement.pid = 402
    manager = make_manager(old_process)
    manager.upstream_coordinator = coordinator
    segment_info = make_segment_info()
    segment_info.update(
        {
            "upstream_channel_key": active.channel_key,
            "upstream_generation": active.generation,
            "upstream_process_group_id": active.process_group_id,
            "upstream_process_start_fingerprint": active.process_start_fingerprint,
        }
    )

    async def start_segment(stream, segment_path, quality, info):
        manager.active_processes[f"stream_{stream.id}"] = replacement
        return replacement

    stopped = []

    async def stop_process_group(
        process, process_group_id, timeout, process_start_fingerprint=None
    ):
        with Session() as db:
            lease = db.query(TwitchUpstreamLease).one()
            stopped.append((process.pid, lease.state))
        process.returncode = -15
        process.release.set()
        await process.wait()
        return True

    manager._start_segment = start_segment
    manager._terminate_process_group = stop_process_group
    return engine, Session, coordinator, manager, segment_info, replacement, stopped


@pytest.mark.asyncio
async def test_real_coordinator_failed_handoff_reaps_replacement_before_release(
    tmp_path,
) -> None:
    (
        engine,
        Session,
        coordinator,
        manager,
        segment_info,
        replacement,
        stopped,
    ) = await make_real_failed_handoff_rotation(tmp_path, "failed-handoff.db")

    async def fail_handoff(**values):
        raise RuntimeError("injected local handoff failure")

    coordinator.handoff_rotation = fail_handoff

    assert (
        await manager._rotate_segment(SimpleNamespace(id=7), segment_info, "best")
        is False
    )
    assert stopped == [(401, "ROTATING"), (402, "ROTATING")]
    assert replacement.returncode == -15
    with Session() as db:
        lease = db.query(TwitchUpstreamLease).one()
        assert (lease.state, lease.release_reason) == (
            "RELEASED",
            "rotation_handoff_failed",
        )
    engine.dispose()


@pytest.mark.asyncio
async def test_real_coordinator_cancelled_handoff_reaps_replacement_before_release(
    tmp_path,
) -> None:
    (
        engine,
        Session,
        coordinator,
        manager,
        segment_info,
        replacement,
        stopped,
    ) = await make_real_failed_handoff_rotation(tmp_path, "cancelled-handoff.db")
    handoff_started = asyncio.Event()
    finish_handoff = asyncio.Event()

    async def cancel_handoff(**values):
        handoff_started.set()
        await finish_handoff.wait()
        raise asyncio.CancelledError

    coordinator.handoff_rotation = cancel_handoff
    rotation = asyncio.create_task(
        manager._rotate_segment(SimpleNamespace(id=7), segment_info, "best")
    )
    await handoff_started.wait()
    rotation.cancel()
    finish_handoff.set()

    with pytest.raises(asyncio.CancelledError):
        await rotation

    assert stopped == [(401, "ROTATING"), (402, "ROTATING")]
    assert replacement.returncode == -15
    with Session() as db:
        lease = db.query(TwitchUpstreamLease).one()
        assert (lease.state, lease.release_reason) == (
            "RELEASED",
            "rotation_cancelled",
        )
    engine.dispose()


@pytest.mark.asyncio
async def test_recording_stop_revalidates_persisted_identity_before_sigterm(
    monkeypatch,
) -> None:
    process_manager_module = importlib.import_module(
        "app.services.recording.process_manager"
    )
    process = FakeProcess()
    process.pid = 401
    manager = make_manager(process)
    segment_info = make_segment_info()
    segment_info.update(
        {
            "upstream_channel_key": "recording-stop-channel",
            "upstream_generation": 3,
            "upstream_process_group_id": 401,
            "upstream_process_start_fingerprint": "birth-401",
        }
    )
    manager.long_stream_processes["stream_7"] = segment_info
    releases = []

    class Coordinator:
        async def assert_stop_authorized(self, **values):
            return None

        async def inspect_process_identity(self, process_pid):
            return ProcessIdentity(
                process_pid,
                9401,
                datetime.now(timezone.utc),
                "foreign-birth-401",
            )

        async def release(self, **values):
            releases.append(values)
            return True

    async def finalize(info):
        return None

    manager.upstream_coordinator = Coordinator()
    manager._finalize_segmented_recording = finalize
    signaled = []

    def kill_process_group(process_group_id, sig):
        signaled.append((process_group_id, sig))
        process.returncode = -sig
        process.release.set()

    monkeypatch.setattr(process_manager_module.os, "killpg", kill_process_group)

    assert await manager.terminate_process("stream_7") is False
    assert signaled == []
    assert releases == []
    assert manager.active_processes["stream_7"] is process


@pytest.mark.asyncio
async def test_recording_stop_revalidates_persisted_identity_before_sigkill(
    monkeypatch,
) -> None:
    process_manager_module = importlib.import_module(
        "app.services.recording.process_manager"
    )
    process = FakeProcess(release_on_terminate=False)
    process.pid = 401
    manager = make_manager(process)
    segment_info = make_segment_info()
    segment_info.update(
        {
            "upstream_channel_key": "recording-stop-channel",
            "upstream_generation": 3,
            "upstream_process_group_id": 401,
            "upstream_process_start_fingerprint": "birth-401",
        }
    )
    manager.long_stream_processes["stream_7"] = segment_info
    identity_changed = False
    releases = []

    class Coordinator:
        async def assert_stop_authorized(self, **values):
            return None

        async def inspect_process_identity(self, process_pid):
            return ProcessIdentity(
                process_pid,
                401,
                datetime.now(timezone.utc),
                "foreign-birth-401" if identity_changed else "birth-401",
            )

        async def release(self, **values):
            releases.append(values)
            return True

    manager.upstream_coordinator = Coordinator()
    signaled = []

    def kill_process_group(process_group_id, sig):
        signaled.append((process_group_id, sig))
        if sig == process_manager_module.signal.SIGKILL:
            process.returncode = -sig
            process.release.set()

    async def expire_wait(awaitable, timeout):
        nonlocal identity_changed
        awaitable.close()
        identity_changed = True
        raise asyncio.TimeoutError

    monkeypatch.setattr(process_manager_module.os, "killpg", kill_process_group)
    monkeypatch.setattr(process_manager_module.asyncio, "wait_for", expire_wait)

    assert await manager.terminate_process("stream_7") is False
    assert signaled == [(401, process_manager_module.signal.SIGTERM)]
    assert releases == []
    assert manager.active_processes["stream_7"] is process


@pytest.mark.asyncio
async def test_recording_stop_authorizes_unactivated_startup_child_locally() -> None:
    process = FakeProcess()
    process.pid = 701
    manager = make_manager(process)
    segment_info = make_segment_info()
    segment_info.update(
        {
            "upstream_channel_key": "startup-channel",
            "upstream_generation": 3,
            "upstream_process_group_id": 701,
            "upstream_process_start_fingerprint": "birth-701",
            "upstream_activated": False,
        }
    )
    manager.long_stream_processes["stream_7"] = segment_info
    released = []

    class Coordinator:
        async def assert_stop_authorized(self, **_values):
            raise AssertionError("unactivated reservation has no durable PID yet")

        async def release(self, **values):
            released.append(values)
            return True

    async def terminate_process_group(*_args, **_kwargs):
        process.returncode = -15
        process.release.set()
        return True

    async def finalize(_segment_info):
        return None

    manager.upstream_coordinator = Coordinator()
    manager._terminate_process_group = terminate_process_group
    manager._finalize_segmented_recording = finalize

    assert await manager.terminate_process("stream_7") is True
    assert manager.active_processes == {}
    assert manager.long_stream_processes == {}
    assert released == [
        {
            "channel_key": "startup-channel",
            "generation": 3,
            "reason": "recording_stopped",
        }
    ]
