"""
Test Streamlink command formatting to ensure arguments are properly escaped.

This test validates the fix for the OAuth token parsing issue where Streamlink
was receiving the token as separate arguments instead of a single argument.

ISSUE: Streamlink was receiving:
  --twitch-api-header Authorization=OAuth token
  (3 separate arguments, parsed as tuple)

FIX: Now sends:
  --twitch-api-header=Authorization=OAuth token
  (1 single argument, properly parsed)
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import asyncio
import importlib
import logging
from pathlib import Path
import subprocess
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from app.utils import streamlink_utils
from app.utils.streamlink_utils import (
    get_streamlink_clip_command,
    get_streamlink_command,
    get_streamlink_vod_command,
)


def _authenticated_proxy_url(
    scheme: str, username: str, password: str, endpoint: str
) -> str:
    return f"{scheme}://{username}{chr(58)}{password}{chr(64)}{endpoint}"


def test_oauth_token_format():
    """Test that OAuth token is passed as single argument with ="""
    cmd = get_streamlink_command(
        streamer_name="test_streamer",
        quality="best",
        output_path="/tmp/test.ts",
        oauth_token="test_token_123",
    )

    # Find the OAuth argument
    oauth_arg = None
    for arg in cmd:
        if arg.startswith("--twitch-api-header="):
            oauth_arg = arg
            break

    # Verify format
    assert oauth_arg is not None, "OAuth argument not found in command"
    assert oauth_arg == "--twitch-api-header=Authorization=OAuth test_token_123"

    # Verify it's NOT split into separate arguments
    assert "--twitch-api-header" not in cmd or any(
        "=" in arg for arg in cmd if "--twitch-api-header" in arg
    ), "OAuth token must use = format to avoid shell parsing issues"


def test_codec_format():
    """Test that codecs are passed as single argument with ="""
    cmd = get_streamlink_command(
        streamer_name="test_streamer",
        quality="best",
        output_path="/tmp/test.ts",
        supported_codecs="h264,h265,av1",
    )

    # Find the codec argument
    codec_arg = None
    for arg in cmd:
        if arg.startswith("--twitch-supported-codecs="):
            codec_arg = arg
            break

    # Verify format
    assert codec_arg is not None, "Codec argument not found in command"
    assert codec_arg == "--twitch-supported-codecs=h264,h265,av1"


def test_proxy_format():
    """Test that proxy URLs are passed as single argument with ="""
    cmd = get_streamlink_command(
        streamer_name="test_streamer",
        quality="best",
        output_path="/tmp/test.ts",
        proxy_settings={"https": "https://user:pass@proxy.example.com:8443"},
    )

    http_proxy_arg = None
    for arg in cmd:
        if arg.startswith("--http-proxy="):
            http_proxy_arg = arg

    # Verify format
    assert http_proxy_arg == "--http-proxy=https://user:pass@proxy.example.com:8443"
    assert not any(arg.startswith("--https-proxy=") for arg in cmd)


@pytest.mark.parametrize(
    ("factory", "kwargs"),
    [
        (
            get_streamlink_command,
            {
                "streamer_name": "test_streamer",
                "quality": "best",
                "output_path": "/tmp/recording.ts",
            },
        ),
        (
            get_streamlink_vod_command,
            {
                "video_id": "123456",
                "quality": "best",
                "output_path": "/tmp/vod.ts",
            },
        ),
        (
            get_streamlink_clip_command,
            {
                "clip_url": "https://clips.twitch.tv/FixtureClip",
                "quality": "best",
                "output_path": "/tmp/clip.ts",
            },
        ),
    ],
)
def test_new_child_commands_use_one_effective_http_proxy(factory, kwargs):
    proxy_url = _authenticated_proxy_url(
        "http", "fixture-child", "fixture-value", "child.invalid:8080"
    )

    command = factory(proxy_settings={"http": proxy_url}, **kwargs)

    assert [arg for arg in command if arg.startswith("--http-proxy=")] == [
        f"--http-proxy={proxy_url}"
    ]
    assert "--http-proxy" not in command
    assert not any(
        arg == "--https-proxy" or arg.startswith("--https-proxy=") for arg in command
    )


def test_add_proxy_settings_replaces_existing_proxy_idempotently():
    old_http_proxy = _authenticated_proxy_url(
        "http", "fixture-old", "fixture-value", "old.invalid:8080"
    )
    old_https_proxy = _authenticated_proxy_url(
        "https", "fixture-old", "fixture-value", "old.invalid:8443"
    )
    new_http_proxy = _authenticated_proxy_url(
        "http", "fixture-new", "fixture-value", "new.invalid:8080"
    )
    fallback_proxy = _authenticated_proxy_url(
        "https", "fixture-fallback", "fixture-value", "fallback.invalid:8443"
    )
    command = [
        "streamlink",
        "--http-proxy",
        old_http_proxy,
        f"--https-proxy={old_https_proxy}",
        "--stream-timeout",
        "42",
    ]
    proxy_settings = {
        "http": new_http_proxy,
        "https": fallback_proxy,
    }

    for _ in range(2):
        command = streamlink_utils._add_proxy_settings(
            command, proxy_settings, force_mode=False
        )

    assert [arg for arg in command if arg.startswith("--http-proxy=")] == [
        f"--http-proxy={new_http_proxy}"
    ]
    assert "--http-proxy" not in command
    assert not any(
        arg == "--https-proxy" or arg.startswith("--https-proxy=") for arg in command
    )
    timeout_index = command.index("--stream-timeout")
    assert command[timeout_index + 1] == "42"
    for option in (
        "--stream-segment-timeout",
        "--stream-timeout",
        "--stream-segmented-queue-deadline",
        "--stream-segment-attempts",
        "--ringbuffer-size",
        "--hls-segment-stream-data",
        "--hls-playlist-reload-time",
    ):
        assert command.count(option) == 1


@pytest.mark.parametrize(
    ("proxy_settings", "expected_proxy"),
    [
        (
            {
                "http": "http://preferred.example:8080",
                "https": "https://fallback.example:8443",
            },
            "http://preferred.example:8080",
        ),
        (
            {"https": "https://fallback.example:8443"},
            "https://fallback.example:8443",
        ),
    ],
)
def test_proxy_probe_and_stream_info_use_one_http_proxy_argument(
    monkeypatch, proxy_settings, expected_proxy
):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(streamlink_utils.subprocess, "run", fake_run)

    assert streamlink_utils.check_proxy_connectivity(proxy_settings) == (True, "")
    assert streamlink_utils.get_stream_info("test_streamer", proxy_settings) == (
        True,
        {},
    )

    for command in commands:
        assert [arg for arg in command if arg.startswith("--http-proxy=")] == [
            f"--http-proxy={expected_proxy}"
        ]
        assert not any(arg.startswith("--https-proxy=") for arg in command)


def test_proxy_diagnostics_retain_only_host_port(monkeypatch, caplog, tmp_path: Path):
    proxy_url = (
        "https://user:password@proxy.example:8443/signed/path?token=secret#fragment"
    )
    with caplog.at_level(logging.DEBUG):
        get_streamlink_command(
            streamer_name="test_streamer",
            quality="best",
            output_path="/tmp/test.ts",
            proxy_settings={"https": proxy_url},
            log_path="/tmp/streamlink.log",
        )

        with pytest.raises(ValueError) as exc_info:
            streamlink_utils._add_proxy_settings(
                [], {"http": proxy_url.replace("https://", "socks5://")}, False
            )

        with patch("pathlib.Path.exists", return_value=True):
            from app.services.system.streamlink_config_service import (
                StreamlinkConfigService,
            )

        service = StreamlinkConfigService.__new__(StreamlinkConfigService)
        service.config_dir = tmp_path
        service.twitch_config_path = tmp_path / "config.twitch"
        assert service.generate_twitch_config(https_proxy=proxy_url)

    diagnostics = "\n".join(
        record.getMessage()
        for record in caplog.records
        if "proxy" in record.getMessage().lower()
    )
    diagnostics = f"{diagnostics}\n{exc_info.value}"
    assert "proxy.example:8443" in diagnostics
    for secret in (
        "user",
        "password",
        "signed",
        "path",
        "token",
        "secret",
        "fragment",
    ):
        assert secret not in diagnostics


def test_proxy_failure_details_retain_only_host_port(monkeypatch):
    proxy_url = (
        "https://user:password@proxy.example:8443/signed/path?token=secret#fragment"
    )
    monkeypatch.setattr(
        streamlink_utils,
        "check_proxy_connectivity",
        lambda settings: (False, "Proxy connectivity check failed"),
    )

    success, details = streamlink_utils.get_stream_info(
        "test_streamer", {"https": proxy_url}
    )

    assert not success
    assert details["proxy_settings"] == {"https": "proxy.example:8443"}


@pytest.mark.parametrize("force_mode", [False, True])
def test_proxy_request_profile_uses_one_bounded_block(force_mode: bool):
    cmd = get_streamlink_command(
        streamer_name="test_streamer",
        quality="best",
        output_path="/tmp/test.ts",
        proxy_settings={
            "http": "http://proxy.example.com:8080",
            "https": "https://proxy.example.com:8443",
        },
        force_mode=force_mode,
        log_path="/tmp/streamlink.log",
    )

    assert [arg for arg in cmd if arg.startswith("--http-proxy=")] == [
        "--http-proxy=http://proxy.example.com:8080"
    ]
    assert not any(arg.startswith("--https-proxy=") for arg in cmd)
    assert cmd.count("--stream-segment-attempts") == 1
    attempts_index = cmd.index("--stream-segment-attempts")
    assert cmd[attempts_index + 1] == "5"
    assert "--hls-live-edge" not in cmd
    assert "--stream-segment-threads" not in cmd


@pytest.mark.parametrize(
    ("recording_settings", "pool_proxy", "stored_http", "stored_https", "expected"),
    [
        (
            SimpleNamespace(enable_proxy=True, fallback_to_direct_connection=True),
            _authenticated_proxy_url(
                "http", "fixture-pool", "fixture-value", "pool.invalid:8080"
            ),
            _authenticated_proxy_url(
                "http", "fixture-stored", "fixture-value", "stored.invalid:8080"
            ),
            _authenticated_proxy_url(
                "https", "fixture-stored", "fixture-value", "stored.invalid:8443"
            ),
            _authenticated_proxy_url(
                "http", "fixture-pool", "fixture-value", "pool.invalid:8080"
            ),
        ),
        (
            SimpleNamespace(enable_proxy=True, fallback_to_direct_connection=True),
            None,
            _authenticated_proxy_url(
                "http", "fixture-stored", "fixture-value", "stored.invalid:8080"
            ),
            _authenticated_proxy_url(
                "https", "fixture-stored", "fixture-value", "stored.invalid:8443"
            ),
            None,
        ),
        (
            SimpleNamespace(enable_proxy=False, fallback_to_direct_connection=True),
            None,
            _authenticated_proxy_url(
                "http", "fixture-stored", "fixture-value", "stored.invalid:8080"
            ),
            _authenticated_proxy_url(
                "https", "fixture-stored", "fixture-value", "stored.invalid:8443"
            ),
            _authenticated_proxy_url(
                "http", "fixture-stored", "fixture-value", "stored.invalid:8080"
            ),
        ),
        (
            None,
            None,
            None,
            _authenticated_proxy_url(
                "https", "fixture-stored", "fixture-value", "stored.invalid:8443"
            ),
            _authenticated_proxy_url(
                "https", "fixture-stored", "fixture-value", "stored.invalid:8443"
            ),
        ),
    ],
)
def test_recording_new_child_selects_pool_or_stored_proxy(
    monkeypatch,
    recording_settings,
    pool_proxy,
    stored_http,
    stored_https,
    expected,
):
    from app import database
    from app.models import GlobalSettings, RecordingSettings, StreamerRecordingSettings
    from app.services.notifications import external_notification_service
    from app.services.proxy import proxy_health_service as proxy_health_module
    from app.services.system import twitch_token_service

    process_manager_module = importlib.import_module(
        "app.services.recording.process_manager"
    )
    global_settings = SimpleNamespace(
        http_proxy=stored_http,
        https_proxy=stored_https,
        supported_codecs="h264",
    )
    original_settings = vars(global_settings).copy()

    class Query:
        def __init__(self, model):
            self.model = model

        def filter(self, *_args):
            return self

        def first(self):
            if self.model is RecordingSettings:
                return recording_settings
            if self.model is GlobalSettings:
                return global_settings
            if self.model is StreamerRecordingSettings:
                return None
            raise AssertionError(f"Unexpected model query: {self.model}")

    class Database:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def query(self, model):
            return Query(model)

    class TokenService:
        def __init__(self, _db):
            pass

        async def resolve_recording_token(self):
            return SimpleNamespace(token=None, source=None, stored_version=None)

    class NotificationService:
        async def send_recording_notification(self, **_kwargs):
            return None

    class Process:
        returncode = None
        pid = 123

    pool_calls = 0

    async def get_best_proxy():
        nonlocal pool_calls
        pool_calls += 1
        return pool_proxy

    commands = []

    async def create_subprocess_exec(*command, **_kwargs):
        commands.append(list(command))
        return Process()

    async def immediate_sleep(_delay):
        return None

    monkeypatch.setattr(database, "SessionLocal", Database)
    monkeypatch.setattr(twitch_token_service, "TwitchTokenService", TokenService)
    monkeypatch.setattr(proxy_health_module, "get_best_proxy", get_best_proxy)
    monkeypatch.setattr(
        external_notification_service,
        "ExternalNotificationService",
        NotificationService,
    )
    monkeypatch.setattr(
        process_manager_module.asyncio,
        "create_subprocess_exec",
        create_subprocess_exec,
    )
    monkeypatch.setattr(process_manager_module.asyncio, "sleep", immediate_sleep)

    manager = object.__new__(process_manager_module.ProcessManager)
    manager.logging_service = None
    manager.active_processes = {}
    manager.long_stream_processes = {}
    manager.lock = asyncio.Lock()
    manager._track_segment_completion = lambda _process: None
    stream = SimpleNamespace(
        id=7,
        streamer_id=11,
        streamer=SimpleNamespace(username="streamer"),
        title="fixture title",
        category_name="fixture category",
    )
    segment_info = {"segment_count": 1, "total_segments": []}

    asyncio.run(manager._start_segment(stream, "/tmp/segment.ts", "best", segment_info))

    proxy_args = [arg for arg in commands[0] if arg.startswith("--http-proxy=")]
    assert proxy_args == ([f"--http-proxy={expected}"] if expected else [])
    assert not any(arg.startswith("--https-proxy=") for arg in commands[0])
    assert pool_calls == (
        1 if recording_settings and recording_settings.enable_proxy else 0
    )
    assert vars(global_settings) == original_settings


def test_streamlink_840_segment_attempts_cap_http_requests(monkeypatch):
    streamlink = pytest.importorskip("streamlink")
    hls = pytest.importorskip("streamlink.stream.hls")
    streamlink_http = pytest.importorskip("streamlink.session.http")
    assert streamlink.__version__ == "8.4.0"

    cmd = get_streamlink_command(
        streamer_name="test_streamer",
        quality="best",
        output_path="/tmp/test.ts",
        proxy_settings={"http": "http://proxy.example.com:8080"},
        log_path="/tmp/streamlink.log",
    )
    attempts_index = cmd.index("--stream-segment-attempts")
    segment_attempts = int(cmd[attempts_index + 1])
    segment_requests = 0

    class HLSHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal segment_requests
            if self.path == "/playlist.m3u8":
                body = (
                    b"#EXTM3U\n"
                    b"#EXT-X-TARGETDURATION:1\n"
                    b"#EXT-X-MEDIA-SEQUENCE:0\n"
                    b"#EXTINF:1,\n"
                    b"segment.ts\n"
                    b"#EXT-X-ENDLIST\n"
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if self.path == "/segment.ts":
                segment_requests += 1
                self.send_response(503)
                self.end_headers()
                return

            self.send_response(404)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), HLSHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        session = streamlink.Streamlink()
        session.set_option("stream-segment-attempts", segment_attempts)
        session.set_option("stream-segment-threads", 1)
        requested_hosts = []
        request = session.http.request

        def local_only_request(method, url, *args, **kwargs):
            host = urlsplit(url).hostname
            requested_hosts.append(host)
            if host != "127.0.0.1":
                raise RuntimeError(f"Blocked non-loopback request to {host}")
            return request(method, url, *args, **kwargs)

        monkeypatch.setattr(session.http, "request", local_only_request)
        monkeypatch.setattr(streamlink_http.time, "sleep", lambda _delay: None)

        playlist_url = f"http://127.0.0.1:{server.server_port}/playlist.m3u8"
        reader = hls.HLSStream(session, playlist_url).open()
        try:
            assert reader.read(8192) == b""
        finally:
            reader.close()

        assert set(requested_hosts) == {"127.0.0.1"}
        assert segment_requests <= 6
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join()


def test_streamlink_840_retry_max_caps_missing_stream_resolution_attempts(
    monkeypatch, tmp_path: Path
):
    streamlink = pytest.importorskip("streamlink")
    streamlink_cli = pytest.importorskip("streamlink_cli.main")
    plugin_module = pytest.importorskip("streamlink.plugin")
    assert streamlink.__version__ == "8.4.0"

    with patch("pathlib.Path.exists", return_value=True):
        from app.services.system.streamlink_config_service import (
            StreamlinkConfigService,
        )

    service = StreamlinkConfigService.__new__(StreamlinkConfigService)
    service.config_dir = tmp_path
    service.twitch_config_path = tmp_path / "config.twitch"

    assert service.generate_twitch_config()

    config = dict(
        line.split("=", 1)
        for line in service.twitch_config_path.read_text().splitlines()
        if "=" in line
    )
    assert config["retry-streams"] == "10"
    assert config["retry-max"] == "2"
    resolution_attempts = 0

    class MissingStreamHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal resolution_attempts
            resolution_attempts += 1
            self.send_response(404)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    class MissingStreamPlugin(plugin_module.Plugin):
        def _get_streams(self):
            response = self.session.http.get(self.url, timeout=1)
            response.raise_for_status()
            return {}

    server = ThreadingHTTPServer(("127.0.0.1", 0), MissingStreamHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        session = streamlink.Streamlink()
        request = session.http.request

        def local_only_request(method, url, *args, **kwargs):
            if urlsplit(url).hostname != "127.0.0.1":
                raise RuntimeError(f"Blocked non-loopback request to {url}")
            return request(method, url, *args, **kwargs)

        monkeypatch.setattr(session.http, "request", local_only_request)
        monkeypatch.setattr(streamlink_cli, "sleep", lambda _delay: None)
        monkeypatch.setattr(
            streamlink_cli,
            "args",
            SimpleNamespace(stream_types=None, stream_sorting_excludes=None),
        )
        plugin = MissingStreamPlugin(
            session,
            f"http://127.0.0.1:{server.server_port}/missing-stream",
        )

        assert (
            streamlink_cli.fetch_streams_with_retry(
                plugin,
                int(config["retry-streams"]),
                int(config["retry-max"]),
            )
            is None
        )
        assert resolution_attempts == 3
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join()


def test_generated_config_uses_bounded_central_request_profile(tmp_path: Path):
    with patch("pathlib.Path.exists", return_value=True):
        from app.services.system.streamlink_config_service import (
            StreamlinkConfigService,
        )

    service = StreamlinkConfigService.__new__(StreamlinkConfigService)
    service.config_dir = tmp_path
    service.twitch_config_path = tmp_path / "config.twitch"

    assert service.generate_twitch_config()

    config_lines = set(service.twitch_config_path.read_text().splitlines())
    assert {
        "hls-live-edge=3",
        "stream-segment-threads=5",
        "retry-streams=10",
        "retry-max=2",
    } <= config_lines


def test_generated_config_replaces_legacy_credentials_with_static_options(
    tmp_path: Path,
):
    with patch("pathlib.Path.exists", return_value=True):
        from app.services.system.streamlink_config_service import (
            StreamlinkConfigService,
        )

    service = StreamlinkConfigService.__new__(StreamlinkConfigService)
    service.config_dir = tmp_path
    service.twitch_config_path = tmp_path / "config.twitch"
    old_http_proxy = _authenticated_proxy_url(
        "http", "fixture-old", "fixture-old-value", "proxy.invalid:8080"
    )
    old_https_proxy = _authenticated_proxy_url(
        "https", "fixture-old", "fixture-old-value", "proxy.invalid:8443"
    )
    current_http_proxy = _authenticated_proxy_url(
        "http", "fixture-current", "fixture-value", "proxy.invalid:8080/path"
    )
    fallback_proxy = _authenticated_proxy_url(
        "https", "fixture-fallback", "fixture-value", "proxy.invalid:8443"
    )
    service.twitch_config_path.write_text(
        f"http-proxy={old_http_proxy}\n"
        f"https-proxy={old_https_proxy}\n"
        "twitch-api-header=Authorization=OAuth fixture-old-oauth\n"
    )

    assert service.generate_twitch_config(
        oauth_token="fixture-current-oauth",
        http_proxy=current_http_proxy,
        https_proxy=fallback_proxy,
    )

    config = service.twitch_config_path.read_text()
    assert "http-proxy" not in config
    assert "https-proxy" not in config
    assert "twitch-api-header=" not in config
    assert "fixture-old" not in config
    assert "fixture-current" not in config
    assert "fixture-fallback" not in config
    assert "fixture-value" not in config


def test_generated_anonymous_config_cannot_inherit_static_authentication(
    tmp_path: Path,
):
    with patch("pathlib.Path.exists", return_value=True):
        from app.services.system.streamlink_config_service import (
            StreamlinkConfigService,
        )

    service = StreamlinkConfigService.__new__(StreamlinkConfigService)
    service.config_dir = tmp_path
    service.twitch_config_path = tmp_path / "config.twitch"
    anonymous_path = tmp_path / "config.twitch-anonymous"
    anonymous_path.write_text(
        "twitch-api-header=Authorization=OAuth stale-secret\n"
        "twitch-supported-codecs=av1,h265,h264\n"
    )

    assert service.generate_twitch_config(oauth_token="current-secret")

    config = anonymous_path.read_text()
    assert not any(
        line.startswith("twitch-api-header=") for line in config.splitlines()
    )
    assert "stale-secret" not in config
    assert "current-secret" not in config
    assert "twitch-supported-codecs=h264" in config
    assert "retry-streams=10" in config


def test_no_space_in_critical_arguments():
    """Ensure no critical arguments are split into separate list items"""
    cmd = get_streamlink_command(
        streamer_name="test_streamer",
        quality="best",
        output_path="/tmp/test.ts",
        oauth_token="test_token_123",
        supported_codecs="h264,h265",
        proxy_settings={"http": "http://proxy.example.com:8080"},
    )

    # Critical arguments that should use = format
    critical_args = [
        "--twitch-api-header",
        "--twitch-supported-codecs",
        "--http-proxy",
        "--https-proxy",
    ]

    # Check each critical argument
    for arg_name in critical_args:
        # Find all occurrences
        matches = [arg for arg in cmd if arg_name in arg]

        for match in matches:
            # Verify it uses = format (single argument)
            assert "=" in match, f"{arg_name} must use '=' format, found: {match}"

            # Verify it's not followed by a value argument
            idx = cmd.index(match)
            if idx + 1 < len(cmd):
                next_arg = cmd[idx + 1]
                # Next argument should be another option or the URL
                assert (
                    next_arg.startswith("--")
                    or "twitch.tv" in next_arg
                    or next_arg in ["best", "/tmp/test.ts"]
                ), f"{arg_name} appears to have value as separate argument: {next_arg}"


def test_command_structure():
    """Test overall command structure"""
    cmd = get_streamlink_command(
        streamer_name="test_streamer",
        quality="best",
        output_path="/tmp/test.ts",
        oauth_token="test_token_123",
    )

    # Basic structure checks
    assert cmd[0] == "streamlink"
    assert "--config" in cmd
    assert "twitch.tv/test_streamer" in cmd
    assert "best" in cmd
    assert "-o" in cmd or "--output" in [arg.split("=")[0] for arg in cmd]

    # Verify output path
    output_idx = cmd.index("-o") if "-o" in cmd else None
    if output_idx:
        assert cmd[output_idx + 1] == "/tmp/test.ts"


def test_streamlink_children_do_not_receive_logfile_arguments():
    commands = [
        get_streamlink_command("test_streamer", "best", "/tmp/test.ts"),
        get_streamlink_vod_command("123456", "best", "/tmp/vod.ts"),
        get_streamlink_clip_command(
            "https://clips.twitch.tv/FixtureClip", "best", "/tmp/clip.ts"
        ),
    ]

    assert all("--logfile" not in command for command in commands)
    assert all(not any("streamlink_" in arg for arg in command) for command in commands)


def test_closed_world_streamlink_sinks_use_shared_boundary():
    root = Path(__file__).parents[1]
    logging_source = (root / "app/services/system/logging_service.py").read_text()
    command_source = (root / "app/utils/streamlink_utils.py").read_text()
    streamlink_sinks = logging_source[
        logging_source.index("    def log_streamlink_output(") : logging_source.index(
            "    def log_ffmpeg_start("
        )
    ]

    assert logging_source.count("sanitize_streamlink_output(") >= 3
    assert "stdout_text}" not in streamlink_sinks
    assert "stderr_text}" not in streamlink_sinks
    assert "{error_message}" not in streamlink_sinks
    assert "--logfile" not in command_source


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
