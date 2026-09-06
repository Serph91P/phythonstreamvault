import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.recording.exceptions import ProcessError
from app.services.recording.process_manager import ProcessManager
from app.utils.streamlink_utils import get_streamlink_command


@pytest.mark.parametrize(
    "output",
    [
        'Unauthorized: The "Authorization" token is invalid.',
        "401 Unauthorized",
        "Twitch OAuth token is invalid",
        "error: invalid oauth token",
    ],
)
def test_auth_rejection_classifier_recognizes_token_failures(output):
    from app.services.recording.recording_auth_policy import is_twitch_auth_rejection

    assert is_twitch_auth_rejection(output) is True


@pytest.mark.parametrize(
    "output",
    [
        "403 Forbidden from proxy.example.com",
        "ProxyError: 401 Unauthorized from proxy.example.com",
        "No playable streams found on this URL",
        "Connection refused by proxy",
        "No space left on device",
        "Permission denied: /recordings/channel.ts",
        "Stream is offline",
    ],
)
def test_auth_rejection_classifier_ignores_unrelated_failures(output):
    from app.services.recording.recording_auth_policy import is_twitch_auth_rejection

    assert is_twitch_auth_rejection(output) is False


class _Process:
    def __init__(self, pid, returncode, stderr=b""):
        self.pid = pid
        self.returncode = returncode
        self.stderr_value = stderr
        self.communicate_calls = 0

    async def communicate(self):
        self.communicate_calls += 1
        return b"", self.stderr_value


class _DelayedProcess(_Process):
    def __init__(self, pid, returncode=None, stderr=b""):
        super().__init__(pid, returncode, stderr)
        self.exited = asyncio.Event()
        if returncode is not None:
            self.exited.set()

    async def communicate(self):
        self.communicate_calls += 1
        await self.exited.wait()
        return b"", self.stderr_value


def _install_start_environment(
    monkeypatch,
    tmp_path: Path,
    processes,
    *,
    token: str | None = "validated-secret",
    source="database_manual",
):
    process_manager_module = importlib.import_module(
        "app.services.recording.process_manager"
    )
    manager = object.__new__(ProcessManager)
    manager.active_processes = {}
    manager.long_stream_processes = {}
    manager.lock = asyncio.Lock()
    manager.rotation_locks = {}
    manager.logging_service = None
    manager._streamlink_output_secrets = {}
    manager._segment_completion_tasks = {}
    manager.AUTH_STARTUP_FALLBACK_WINDOW_SECONDS = 0

    global_settings = SimpleNamespace(
        http_proxy="http://same-route.example:8080",
        https_proxy=None,
        supported_codecs="av1,h265,h264",
    )

    class _Query:
        def __init__(self, model):
            self.model = model

        def first(self):
            if self.model.__name__ == "GlobalSettings":
                return global_settings
            return None

        def filter(self, *_args):
            return self

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def query(self, model):
            return _Query(model)

    stored_version = object()
    token_calls = []
    invalidations = []

    class _TokenService:
        def __init__(self, _db):
            pass

        async def resolve_recording_token(self):
            token_calls.append(True)
            return SimpleNamespace(
                token=token,
                source=source,
                stored_version=stored_version,
            )

        def invalidate_recording_token(self, version):
            invalidations.append(version)
            return True

    commands = []

    def _command(**kwargs):
        commands.append(kwargs)
        return get_streamlink_command(**kwargs)

    pending_processes = list(processes)

    async def _create_subprocess(*_args, **_kwargs):
        return pending_processes.pop(0)

    tracked = []
    manager._track_segment_completion = tracked.append
    notifications = []

    class _Notifications:
        async def send_recording_notification(self, **_kwargs):
            notifications.append(_kwargs)
            return None

    monkeypatch.setattr("app.database.SessionLocal", _Session)
    monkeypatch.setattr(
        "app.services.system.twitch_token_service.TwitchTokenService",
        _TokenService,
    )
    monkeypatch.setattr(process_manager_module, "get_streamlink_command", _command)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess)
    monkeypatch.setattr(
        "app.services.notifications.external_notification_service.ExternalNotificationService",
        _Notifications,
    )
    monkeypatch.setattr(
        process_manager_module,
        "ASYNC_DELAYS",
        SimpleNamespace(PROCESS_START_GRACE=0, RECORDING_ERROR_RECOVERY=1),
    )

    stream = SimpleNamespace(
        id=7,
        streamer_id=3,
        streamer=SimpleNamespace(username="channel", twitch_id=None),
        title="title",
        category_name="category",
    )
    segment_path = str(tmp_path / "recording_part001.ts")
    segment_info = {
        "stream_id": stream.id,
        "segment_count": 1,
        "total_segments": [],
    }
    manager.long_stream_processes["stream_7"] = segment_info
    return SimpleNamespace(
        manager=manager,
        stream=stream,
        segment_path=segment_path,
        segment_info=segment_info,
        commands=commands,
        token_calls=token_calls,
        invalidations=invalidations,
        stored_version=stored_version,
        tracked=tracked,
        notifications=notifications,
        pending_processes=pending_processes,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["validation_invalid", "validation_transient", "credential_unavailable"]
)
async def test_initial_anonymous_resolution_emits_token_free_fallback_event(
    monkeypatch, tmp_path, caplog, reason
):
    child = _Process(101, None)
    env = _install_start_environment(monkeypatch, tmp_path, [child], token=None)
    resolution = SimpleNamespace(
        token=None, source="database_manual", reason=reason, stored_version=None
    )
    env.segment_info["auth_fallback_to_anonymous"] = True

    result = await env.manager._start_segment(
        env.stream,
        env.segment_path,
        "best",
        env.segment_info,
        token_resolution=resolution,
    )

    assert result is child
    assert len(env.commands) == 1
    assert env.commands[0]["anonymous"] is True
    assert "TWITCH_AUTH_FALLBACK_TO_H264" in caplog.text
    assert f"reason={reason} source=database_manual attempt=1" in caplog.text
    assert "Authorization" not in caplog.text


@pytest.mark.asyncio
async def test_authenticated_startup_auth_rejection_retries_once_anonymously(
    monkeypatch, tmp_path, caplog
):
    rejected = _Process(
        101,
        1,
        b'Unauthorized: The "Authorization" token is invalid. validated-secret',
    )
    replacement = _Process(102, None)
    fixture = _install_start_environment(monkeypatch, tmp_path, [rejected, replacement])

    with caplog.at_level("INFO"):
        process = await fixture.manager._start_segment(
            fixture.stream,
            fixture.segment_path,
            "best",
            fixture.segment_info,
        )

    assert process is replacement
    assert len(fixture.commands) == 2
    authenticated, anonymous = fixture.commands
    assert authenticated["anonymous"] is False
    assert authenticated["oauth_token"] == "validated-secret"
    assert anonymous["anonymous"] is True
    assert anonymous["oauth_token"] is None
    for name in ("streamer_name", "quality", "output_path", "proxy_settings"):
        assert authenticated[name] == anonymous[name]
    assert fixture.token_calls == [True]
    assert fixture.invalidations == [fixture.stored_version]
    assert fixture.manager.active_processes == {"stream_7": replacement}
    assert fixture.segment_info["total_segments"] == [
        {
            "path": fixture.segment_path,
            "start_time": fixture.segment_info["total_segments"][0]["start_time"],
            "process_pid": 102,
        }
    ]
    assert fixture.tracked == [replacement]
    assert rejected.communicate_calls == 1
    assert rejected not in fixture.manager._streamlink_output_secrets
    assert "TWITCH_AUTH_FALLBACK_TO_H264" in caplog.text
    assert "reason=streamlink_auth_rejection" in caplog.text
    assert "source=database_manual" in caplog.text
    assert "attempt=2" in caplog.text
    assert "validated-secret" not in caplog.text


@pytest.mark.asyncio
async def test_non_auth_startup_failure_does_not_retry(monkeypatch, tmp_path):
    failed = _Process(101, 1, b"No space left on device")
    fixture = _install_start_environment(monkeypatch, tmp_path, [failed])

    with pytest.raises(ProcessError):
        await fixture.manager._start_segment(
            fixture.stream,
            fixture.segment_path,
            "best",
            fixture.segment_info,
        )

    assert len(fixture.commands) == 1
    assert fixture.pending_processes == []
    assert fixture.invalidations == []
    assert fixture.tracked == []
    assert fixture.manager.active_processes == {}


@pytest.mark.asyncio
async def test_failed_anonymous_fallback_never_starts_third_child(
    monkeypatch, tmp_path
):
    rejected = _Process(101, 1, b"401 Unauthorized")
    fallback_failed = _Process(102, 1, b"No playable streams found")
    fixture = _install_start_environment(
        monkeypatch, tmp_path, [rejected, fallback_failed]
    )

    with pytest.raises(ProcessError):
        await fixture.manager._start_segment(
            fixture.stream,
            fixture.segment_path,
            "best",
            fixture.segment_info,
        )

    assert len(fixture.commands) == 2
    assert fixture.pending_processes == []
    assert fixture.invalidations == [fixture.stored_version]
    assert fixture.tracked == []
    assert fixture.segment_info["total_segments"] == []
    assert fixture.manager.active_processes == {}
    assert fixture.manager.long_stream_processes == {}


@pytest.mark.asyncio
async def test_missing_token_starts_directly_in_anonymous_h264(monkeypatch, tmp_path):
    process = _Process(101, None)
    fixture = _install_start_environment(
        monkeypatch, tmp_path, [process], token=None, source=None
    )

    result = await fixture.manager._start_segment(
        fixture.stream,
        fixture.segment_path,
        "best",
        fixture.segment_info,
    )

    assert result is process
    assert len(fixture.commands) == 1
    assert fixture.commands[0]["anonymous"] is True
    assert fixture.commands[0]["oauth_token"] is None
    assert fixture.invalidations == []
    assert fixture.tracked == [process]


@pytest.mark.asyncio
async def test_auth_rejection_after_segment_data_does_not_overwrite_output(
    monkeypatch, tmp_path
):
    rejected = _Process(101, 1, b"401 Unauthorized")
    fixture = _install_start_environment(monkeypatch, tmp_path, [rejected])
    Path(fixture.segment_path).write_bytes(b"segment-data")

    with pytest.raises(ProcessError):
        await fixture.manager._start_segment(
            fixture.stream,
            fixture.segment_path,
            "best",
            fixture.segment_info,
        )

    assert len(fixture.commands) == 1
    assert fixture.invalidations == []
    assert Path(fixture.segment_path).read_bytes() == b"segment-data"


@pytest.mark.asyncio
async def test_fallback_activates_upstream_lease_only_for_replacement(
    monkeypatch, tmp_path
):
    rejected = _Process(101, 1, b"401 Unauthorized")
    replacement = _Process(102, None)
    fixture = _install_start_environment(monkeypatch, tmp_path, [rejected, replacement])
    fixture.segment_info.update(
        upstream_channel_key="channel-id",
        upstream_generation=4,
    )
    inspected = []
    released = []
    reserved = []
    activated = []

    class _Coordinator:
        async def inspect_process_identity(self, pid):
            inspected.append(pid)
            return SimpleNamespace(
                process_group_id=pid,
                started_at=None,
                fingerprint=f"birth-{pid}",
            )

        async def release(self, **values):
            released.append(values)
            return True

        async def reserve(self, **values):
            reserved.append(values)
            return SimpleNamespace(generation=5)

        async def activate(self, **values):
            activated.append(values)
            return SimpleNamespace(
                process_group_id=values["process_group_id"],
                process_start_fingerprint=values["process_start_fingerprint"],
            )

    fixture.manager.upstream_coordinator = _Coordinator()

    process = await fixture.manager._start_segment(
        fixture.stream,
        fixture.segment_path,
        "best",
        fixture.segment_info,
    )

    assert process is replacement
    assert inspected == [101, 102]
    assert released == [
        {
            "channel_key": "channel-id",
            "generation": 4,
            "reason": "recording_auth_fallback",
        }
    ]
    assert reserved == [
        {
            "channel_key": "channel-id",
            "auth_key": None,
            "purpose": "RECORDING",
            "recording_id": None,
        }
    ]
    assert [values["process_pid"] for values in activated] == [102]
    assert activated[0]["generation"] == 5
    assert fixture.segment_info["upstream_generation"] == 5
    assert fixture.segment_info["upstream_activated"] is True


@pytest.mark.asyncio
async def test_delayed_startup_auth_exit_still_falls_back(monkeypatch, tmp_path):
    rejected = _Process(101, None, b"401 Unauthorized")
    replacement = _Process(102, None)
    fixture = _install_start_environment(monkeypatch, tmp_path, [rejected, replacement])
    fixture.manager.AUTH_STARTUP_FALLBACK_WINDOW_SECONDS = 0.1
    fixture.manager.AUTH_STARTUP_POLL_SECONDS = 0.005

    async def _reject_after_network_round_trip():
        await asyncio.sleep(0.02)
        rejected.returncode = 1

    rejection_task = asyncio.create_task(_reject_after_network_round_trip())
    process = await fixture.manager._start_segment(
        fixture.stream,
        fixture.segment_path,
        "best",
        fixture.segment_info,
    )
    await rejection_task

    assert process is replacement
    assert len(fixture.commands) == 2
    assert fixture.commands[1]["anonymous"] is True


@pytest.mark.asyncio
async def test_auth_exit_after_startup_window_retries_anonymously(
    monkeypatch, tmp_path, caplog
):
    rejected = _DelayedProcess(101, stderr=b"401 Unauthorized")
    replacement = _DelayedProcess(102)
    fixture = _install_start_environment(monkeypatch, tmp_path, [rejected, replacement])
    fixture.manager.AUTH_STARTUP_FALLBACK_WINDOW_SECONDS = 0.01
    fixture.manager.AUTH_STARTUP_POLL_SECONDS = 0.001
    fixture.manager._track_segment_completion = (
        ProcessManager._track_segment_completion.__get__(fixture.manager)
    )
    fixture.segment_info.update(
        current_segment_path=fixture.segment_path,
        monitor_task=None,
    )

    async def _finalize(_segment_info):
        return None

    fixture.manager._finalize_segmented_recording = _finalize

    async def _reject_after_startup_window():
        await asyncio.sleep(0.02)
        rejected.returncode = 1
        rejected.exited.set()

    rejection_task = asyncio.create_task(_reject_after_startup_window())
    process = await fixture.manager._start_segment(
        fixture.stream,
        fixture.segment_path,
        "best",
        fixture.segment_info,
    )
    assert process is rejected
    assert rejected.returncode is None
    await rejection_task
    for _ in range(20):
        if (
            fixture.manager.active_processes.get("stream_7") is replacement
            and fixture.segment_info["total_segments"][0]["process_pid"]
            == replacement.pid
        ):
            break
        await asyncio.sleep(0.01)

    assert len(fixture.commands) == 2
    assert fixture.commands[1]["anonymous"] is True
    assert fixture.commands[1]["oauth_token"] is None
    assert (
        fixture.commands[1]["proxy_settings"] == fixture.commands[0]["proxy_settings"]
    )
    assert fixture.commands[1]["output_path"] == fixture.commands[0]["output_path"]
    assert sum("TWITCH_AUTH_FALLBACK_TO_H264" in r.message for r in caplog.records) == 1
    assert fixture.manager.active_processes == {"stream_7": replacement}
    assert fixture.segment_info["total_segments"] == [
        {
            "path": fixture.segment_path,
            "start_time": fixture.segment_info["total_segments"][0]["start_time"],
            "process_pid": replacement.pid,
        }
    ]
    assert fixture.invalidations == [fixture.stored_version]
    assert len(fixture.notifications) == 1

    monitor_task = fixture.manager._segment_completion_tasks[rejected]
    monitor_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await monitor_task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stderr", "segment_data", "owner_lost"),
    [
        (b"No space left on device", None, False),
        (b"401 Unauthorized", b"segment-data", False),
        (b"401 Unauthorized", None, True),
    ],
)
async def test_late_startup_failure_without_auth_evidence_does_not_retry(
    monkeypatch, tmp_path, stderr, segment_data, owner_lost
):
    failed = _DelayedProcess(101, stderr=stderr)
    fixture = _install_start_environment(monkeypatch, tmp_path, [failed])
    fixture.manager.AUTH_STARTUP_FALLBACK_WINDOW_SECONDS = 0.01
    fixture.manager.AUTH_STARTUP_POLL_SECONDS = 0.001
    fixture.manager._track_segment_completion = (
        ProcessManager._track_segment_completion.__get__(fixture.manager)
    )
    fixture.segment_info.update(
        current_segment_path=fixture.segment_path,
        monitor_task=None,
    )

    async def _finalize(_segment_info):
        return None

    fixture.manager._finalize_segmented_recording = _finalize

    assert (
        await fixture.manager._start_segment(
            fixture.stream, fixture.segment_path, "best", fixture.segment_info
        )
        is failed
    )
    monitor_task = fixture.manager._segment_completion_tasks[failed]
    await asyncio.sleep(0)
    if owner_lost:
        fixture.manager.active_processes.pop("stream_7")
    if segment_data:
        Path(fixture.segment_path).write_bytes(segment_data)
    failed.returncode = 1
    failed.exited.set()
    await asyncio.wait_for(monitor_task, timeout=1)

    assert len(fixture.commands) == 1
    assert fixture.invalidations == []


@pytest.mark.asyncio
async def test_late_anonymous_replacement_never_retries_a_second_auth_exit(
    monkeypatch, tmp_path
):
    rejected = _DelayedProcess(101, stderr=b"401 Unauthorized")
    replacement = _DelayedProcess(102, stderr=b"401 Unauthorized")
    fixture = _install_start_environment(monkeypatch, tmp_path, [rejected, replacement])
    fixture.manager.AUTH_STARTUP_FALLBACK_WINDOW_SECONDS = 0.01
    fixture.manager.AUTH_STARTUP_POLL_SECONDS = 0.001
    fixture.manager._track_segment_completion = (
        ProcessManager._track_segment_completion.__get__(fixture.manager)
    )
    fixture.segment_info.update(
        current_segment_path=fixture.segment_path,
        monitor_task=None,
    )

    async def _finalize(_segment_info):
        return None

    fixture.manager._finalize_segmented_recording = _finalize
    assert (
        await fixture.manager._start_segment(
            fixture.stream, fixture.segment_path, "best", fixture.segment_info
        )
        is rejected
    )
    monitor_task = fixture.manager._segment_completion_tasks[rejected]
    rejected.returncode = 1
    rejected.exited.set()
    for _ in range(20):
        if fixture.manager.active_processes.get("stream_7") is replacement:
            break
        await asyncio.sleep(0.01)
    replacement.returncode = 1
    replacement.exited.set()
    await asyncio.wait_for(monitor_task, timeout=1)

    assert len(fixture.commands) == 2
    assert fixture.pending_processes == []


@pytest.mark.asyncio
async def test_initial_anonymous_resolution_is_reused_without_second_lookup():
    manager = object.__new__(ProcessManager)
    manager.active_processes = {}
    manager.long_stream_processes = {}
    manager.lock = asyncio.Lock()
    manager.rotation_locks = {}
    token_resolution = SimpleNamespace(
        token=None,
        source="database_manual",
        stored_version=None,
    )
    calls = []

    async def _resolve():
        calls.append("resolve")
        return token_resolution

    async def _initialize(*_args):
        segment_info = {
            "current_segment_path": "/tmp/segment.ts",
            "monitor_task": None,
        }
        manager.long_stream_processes["stream_7"] = segment_info
        return segment_info

    async def _start(
        _stream,
        _path,
        _quality,
        segment_info,
        *,
        token_resolution,
    ):
        calls.append((segment_info.copy(), token_resolution))
        return None

    manager._resolve_recording_token = _resolve
    manager._initialize_segmented_recording = _initialize
    manager._start_segment = _start
    manager.upstream_coordinator = SimpleNamespace()
    stream = SimpleNamespace(id=7, streamer=SimpleNamespace(twitch_id=None))

    assert (
        await manager.start_recording_process(stream, "/tmp/output.ts", "best") is None
    )
    assert calls[0] == "resolve"
    segment_info, passed_resolution = calls[1]
    assert segment_info["auth_fallback_to_anonymous"] is True
    assert passed_resolution is token_resolution
