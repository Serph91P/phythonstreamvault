"""
Process management for recording service.

This module handles subprocess creation and management, specifically for streamlink recording processes.
Includes support for 24h+ streams through segment splitting and automatic process rotation.

Dependency Injection:
    The ProcessManager can accept a post_processing_callback in its constructor or via
    set_post_processing_callback() to avoid circular imports and follow proper architecture.

    Example usage:
        async def post_processing_callback(recording_id: int, file_path: str):
            # Handle post-processing for completed recording
            pass

        process_manager = ProcessManager(post_processing_callback=post_processing_callback)
"""

import logging
import asyncio
import os
import re
import signal
import shutil
from datetime import datetime, timedelta
from typing import Dict, Optional, Callable, Awaitable
from pathlib import Path
from sqlalchemy.orm import joinedload

try:
    import psutil  # noqa: F401

    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# Import utilities
from app.utils.streamlink_utils import get_streamlink_command
from app.services.recording.exceptions import ProcessError
from app.models import Stream
from app.utils import async_file
from app.utils.security import (
    get_streamlink_command_secret_values,
    sanitize_streamlink_output,
)
from app.services.recording.recording_auth_policy import is_twitch_auth_rejection
from app.config.constants import ASYNC_DELAYS
from app.services.twitch_upstream_coordinator import (
    AUTHENTICATED_TWITCH_ACCOUNT,
    twitch_upstream_coordinator,
)

logger = logging.getLogger("streamvault")
_UNRESOLVED_RECORDING_TOKEN = object()

# ProcessMonitor integration temporarily disabled for stability
process_monitor = None
ProcessType = None
ProcessStatus = None


class ProcessManager:
    """Manages subprocess execution and cleanup for recording processes

    SINGLETON PATTERN: Use `process_manager` global instance.
    All RecordingOrchestrator instances share the same ProcessManager
    to ensure consistent process tracking across the application.
    """

    # Constants for segment file patterns (must match RecordingLifecycleManager)
    SEGMENT_PART_IDENTIFIER = "_part"
    AUTH_STARTUP_FALLBACK_WINDOW_SECONDS = 5.0
    AUTH_STARTUP_POLL_SECONDS = 0.1

    # Singleton instance tracking
    _instance: Optional["ProcessManager"] = None
    _initialized: bool = False

    def __new__(
        cls,
        config_manager=None,
        post_processing_callback: Optional[
            Callable[[int, str], Awaitable[None]]
        ] = None,
        upstream_coordinator=None,
    ):
        """Singleton pattern - return existing instance if available"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        config_manager=None,
        post_processing_callback: Optional[
            Callable[[int, str], Awaitable[None]]
        ] = None,
        upstream_coordinator=None,
    ):
        # Only initialize once (singleton)
        if ProcessManager._initialized:
            # Update config_manager and callback if provided (for compatibility)
            if config_manager is not None:
                self.config_manager = config_manager
            if post_processing_callback is not None:
                self.post_processing_callback = post_processing_callback
            if upstream_coordinator is not None:
                self.upstream_coordinator = upstream_coordinator
            return

        self.active_processes = {}
        self.long_stream_processes = {}  # Track processes that need segmentation
        self.lock = asyncio.Lock()
        self.rotation_locks = {}
        self._streamlink_output_secrets = {}
        self._segment_completion_tasks = {}
        self.ASYNC_DELAYS = ASYNC_DELAYS
        self.config_manager = config_manager
        self.post_processing_callback = post_processing_callback  # Injected dependency
        self.upstream_coordinator = upstream_coordinator or twitch_upstream_coordinator

        # Configuration for long stream handling (avoid streamlink 24h cutoff)
        self.segment_duration_hours = (
            23.98  # Split streams at 23h59min to avoid streamlink cutoff
        )
        self.max_file_size_gb = 100  # Start new segment if file exceeds this size
        self.monitor_interval_seconds = 600  # Check every 10 minutes (less frequent)

        # Initialize structured logging service
        try:
            from app.services.system.logging_service import logging_service

            self.logging_service = logging_service
            logger.info(
                f"✅ ProcessManager: Logging service initialized - {logging_service.logs_base_dir}"
            )
        except Exception as e:
            logger.warning(
                f"❌ ProcessManager: Could not initialize logging service: {e}"
            )
            self.logging_service = None

        self.psutil_available = HAS_PSUTIL
        if not self.psutil_available:
            logger.warning("psutil not available - process monitoring will be limited")

        # Shutdown management
        self._is_shutting_down = False

        ProcessManager._initialized = True
        logger.debug("ProcessManager singleton initialized")

    def _get_streamlink_output_secrets(self, process) -> tuple[str, ...]:
        return getattr(self, "_streamlink_output_secrets", {}).get(process, ())

    def _remember_streamlink_output_secrets(self, process, command: list) -> None:
        contexts = getattr(self, "_streamlink_output_secrets", None)
        if contexts is None:
            contexts = self._streamlink_output_secrets = {}
        contexts[process] = get_streamlink_command_secret_values(command)

    def _release_streamlink_output_secrets(self, process) -> None:
        contexts = getattr(self, "_streamlink_output_secrets", None)
        if contexts is not None:
            contexts.pop(process, None)

    async def _resolve_recording_token(self):
        from app.database import SessionLocal
        from app.services.system.twitch_token_service import TwitchTokenService

        try:
            with SessionLocal() as db:
                return await TwitchTokenService(db).resolve_recording_token()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "Failed to resolve Twitch authentication (%s)", type(e).__name__
            )
            return None

    @staticmethod
    def _segment_has_recording_data(segment_path: str) -> bool:
        try:
            path = Path(segment_path)
            return path.exists() and path.stat().st_size > 0
        except OSError:
            return True

    async def _wait_for_auth_startup(self, process, segment_path: str) -> None:
        await asyncio.sleep(ASYNC_DELAYS.PROCESS_START_GRACE)
        deadline = (
            asyncio.get_running_loop().time()
            + self.AUTH_STARTUP_FALLBACK_WINDOW_SECONDS
        )
        while process.returncode is None and not self._segment_has_recording_data(
            segment_path
        ):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(self.AUTH_STARTUP_POLL_SECONDS, remaining))

    async def _replace_startup_reservation_with_anonymous(
        self, segment_info: Dict
    ) -> None:
        channel_key = segment_info["upstream_channel_key"]
        generation = segment_info["upstream_generation"]
        release_task = asyncio.create_task(
            self.upstream_coordinator.release(
                channel_key=channel_key,
                generation=generation,
                reason="recording_auth_fallback",
            )
        )
        try:
            released = await asyncio.shield(release_task)
        except asyncio.CancelledError:
            await release_task
            raise
        if not released:
            raise ProcessError("Authenticated recording reservation was fenced")

        reserve_task = asyncio.create_task(
            self.upstream_coordinator.reserve(
                channel_key=channel_key,
                auth_key=None,
                purpose="RECORDING",
                recording_id=segment_info.get("recording_id"),
            )
        )
        try:
            reservation = await asyncio.shield(reserve_task)
        except asyncio.CancelledError:
            reservation = await reserve_task
            segment_info["upstream_generation"] = reservation.generation
            raise
        segment_info["upstream_generation"] = reservation.generation

    def _track_segment_completion(self, process) -> asyncio.Task:
        tasks = getattr(self, "_segment_completion_tasks", None)
        if tasks is None:
            tasks = self._segment_completion_tasks = {}
        existing_task = tasks.get(process)
        if existing_task is not None:
            return existing_task

        task = asyncio.create_task(self.monitor_process(process))
        tasks[process] = task

        def release_task(completed_task):
            if tasks.get(process) is completed_task:
                tasks.pop(process, None)

        task.add_done_callback(release_task)
        return task

    async def start_recording_process(
        self,
        stream: Stream,
        output_path: str,
        quality: str,
        recording_id: Optional[int] = None,
        resume_segments_dir: Optional[str] = None,
        recovery_generation: Optional[int] = None,
    ) -> Optional[asyncio.subprocess.Process]:
        """Start a streamlink recording process for a specific stream

        Args:
            stream: Stream object to record
            output_path: Path where the recording should be saved
            quality: Quality setting for the stream
            recording_id: ID of the recording entry (optional, for segmented recording tracking)
            resume_segments_dir: Existing segments directory to resume into (for app restart recovery)

        Returns:
            Process object or None if failed
        """
        reservation = None
        try:
            initial_token_resolution = await self._resolve_recording_token()
            streamer = getattr(stream, "streamer", None)
            channel_key = getattr(streamer, "twitch_id", None)
            if channel_key:
                reservation = await self.upstream_coordinator.reserve(
                    channel_key=channel_key,
                    auth_key=(
                        AUTHENTICATED_TWITCH_ACCOUNT
                        if getattr(initial_token_resolution, "token", None)
                        else None
                    ),
                    purpose="RECOVERY"
                    if recovery_generation is not None
                    else "RECORDING",
                    recording_id=recording_id,
                    expected_generation=recovery_generation,
                )
            # Initialize segmented recording for long streams
            segment_info = await self._initialize_segmented_recording(
                stream, output_path, quality, recording_id, resume_segments_dir
            )
            segment_info["auth_fallback_to_anonymous"] = not bool(
                getattr(initial_token_resolution, "token", None)
            )
            if reservation:
                segment_info["upstream_channel_key"] = reservation.channel_key
                segment_info["upstream_generation"] = reservation.generation

            # Start the first segment
            process = await self._start_segment(
                stream,
                segment_info["current_segment_path"],
                quality,
                segment_info,
                token_resolution=initial_token_resolution,
            )

            if process:
                # Start monitoring task for long stream management
                monitor_task = asyncio.create_task(
                    self._monitor_long_stream(stream, segment_info, quality)
                )
                segment_info["monitor_task"] = monitor_task

            return process

        except BaseException as e:
            if reservation:
                process_id = f"stream_{stream.id}"
                owned_process = self.active_processes.get(process_id)
                stop_authorized = bool(
                    owned_process
                    and owned_process.returncode is None
                    and not segment_info.get("upstream_activated", False)
                    and segment_info.get("upstream_process_group_id") is not None
                    and segment_info.get("upstream_process_start_fingerprint")
                )
                if (
                    owned_process
                    and owned_process.returncode is None
                    and segment_info.get("upstream_activated", False)
                ):
                    try:
                        await self.upstream_coordinator.assert_stop_authorized(
                            channel_key=reservation.channel_key,
                            generation=segment_info["upstream_generation"],
                            process_pid=owned_process.pid,
                            process_group_id=segment_info["upstream_process_group_id"],
                            process_start_fingerprint=segment_info[
                                "upstream_process_start_fingerprint"
                            ],
                        )
                        stop_authorized = True
                    except (KeyError, PermissionError):
                        pass
                cleanup_complete = (
                    owned_process is None or owned_process.returncode is not None
                )
                if (
                    owned_process
                    and owned_process.returncode is None
                    and stop_authorized
                ):
                    cleanup_complete = await self._terminate_process_group(
                        owned_process,
                        segment_info.get(
                            "upstream_process_group_id", owned_process.pid
                        ),
                        ASYNC_DELAYS.RECORDING_ERROR_RECOVERY,
                        segment_info.get("upstream_process_start_fingerprint"),
                    )
                    if cleanup_complete:
                        if self.active_processes.get(process_id) is owned_process:
                            del self.active_processes[process_id]
                        if self.long_stream_processes.get(process_id) is segment_info:
                            del self.long_stream_processes[process_id]
                if cleanup_complete:
                    self._release_streamlink_output_secrets(owned_process)
                    await self.upstream_coordinator.release(
                        channel_key=reservation.channel_key,
                        generation=segment_info.get(
                            "upstream_generation", reservation.generation
                        )
                        if "segment_info" in locals()
                        else reservation.generation,
                        reason="recording_start_failed",
                    )
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.error(
                "Failed to start recording process for stream %s (%s)",
                stream.id,
                type(e).__name__,
                exc_info=True,
            )
            raise ProcessError("Failed to start recording") from e

    async def _initialize_segmented_recording(
        self,
        stream: Stream,
        output_path: str,
        quality: str,
        recording_id: Optional[int] = None,
        resume_segments_dir: Optional[str] = None,
    ) -> Dict:
        """Initialize segmented recording structure for long streams

        Args:
            stream: Stream object to record
            output_path: Path where the final recording should be saved
            quality: Quality setting for the stream
            recording_id: ID of the recording entry (optional)
            resume_segments_dir: Existing segments directory to resume into (for app restart recovery)
                                 If provided, new segments will be added to this directory with
                                 the next available segment number.
        """
        base_path = Path(output_path)

        if resume_segments_dir and Path(resume_segments_dir).exists():
            # RESUME MODE: Use existing segments directory
            segment_dir = Path(resume_segments_dir)

            # Find the next segment number by checking existing files
            existing_segments = list(
                segment_dir.glob(f"*{self.SEGMENT_PART_IDENTIFIER}*.ts")
            )
            if existing_segments:
                # Parse existing segment numbers
                segment_numbers = []
                for seg in existing_segments:
                    match = re.search(
                        rf"{re.escape(self.SEGMENT_PART_IDENTIFIER)}(\d+)\.ts$",
                        seg.name,
                    )
                    if match:
                        segment_numbers.append(int(match.group(1)))
                next_segment_num = max(segment_numbers) + 1 if segment_numbers else 1
            else:
                next_segment_num = 1

            # Derive base stem from existing segments or directory name
            if existing_segments:
                # Use stem from first segment file
                first_seg = existing_segments[0].name
                base_stem = first_seg.rsplit(self.SEGMENT_PART_IDENTIFIER, 1)[0]
            else:
                # Fallback to directory name without _segments suffix
                base_stem = segment_dir.name.replace("_segments", "")

            segment_filename = (
                f"{base_stem}{self.SEGMENT_PART_IDENTIFIER}{next_segment_num:03d}.ts"
            )
            current_segment_path = segment_dir / segment_filename

            logger.info(
                f"🔄 RESUME_SEGMENTS: Found {len(existing_segments)} existing segments in {segment_dir}, "
                f"starting new segment {next_segment_num}"
            )
        else:
            # NORMAL MODE: Create new segments directory
            segment_dir = base_path.parent / f"{base_path.stem}_segments"
            segment_dir.mkdir(parents=True, exist_ok=True)

            # Create first segment path
            segment_filename = f"{base_path.stem}{self.SEGMENT_PART_IDENTIFIER}001.ts"
            current_segment_path = segment_dir / segment_filename
            next_segment_num = 1

        segment_info = {
            "stream_id": stream.id,
            "recording_id": recording_id,  # Store recording_id for proper post-processing
            "base_output_path": str(output_path),
            "segment_dir": str(segment_dir),
            "current_segment_path": str(current_segment_path),
            "segment_count": next_segment_num,
            "segment_start_time": datetime.now(),
            "total_segments": [],
            "monitor_task": None,
        }

        process_id = f"stream_{stream.id}"
        async with self.lock:
            self.long_stream_processes[process_id] = segment_info

        logger.info(
            f"Initialized segmented recording for stream {stream.id}: {segment_dir}"
        )
        return segment_info

    async def _start_segment(
        self,
        stream: Stream,
        segment_path: str,
        quality: str,
        segment_info: Dict,
        token_resolution=_UNRESOLVED_RECORDING_TOKEN,
        _monitor_handoff: bool = False,
        _handoff_owner=None,
        _proxy_settings=_UNRESOLVED_RECORDING_TOKEN,
        _supported_codecs=_UNRESOLVED_RECORDING_TOKEN,
    ) -> Optional[asyncio.subprocess.Process]:
        """Start recording a single segment"""
        try:
            # Get streamer info via relationship or database
            streamer_name = None
            if hasattr(stream, "streamer") and stream.streamer:
                # Use preloaded relationship if available (better performance)
                streamer_name = stream.streamer.username
            else:
                # Fallback to DB query if not preloaded
                from app.database import SessionLocal
                from app.models import Streamer

                with SessionLocal() as db:
                    streamer = (
                        db.query(Streamer)
                        .filter(Streamer.id == stream.streamer_id)
                        .first()
                    )
                    if not streamer:
                        raise Exception(f"Streamer {stream.streamer_id} not found")
                    streamer_name = streamer.username

            if not streamer_name:
                raise Exception(
                    f"Could not resolve streamer name for stream {stream.id}"
                )

            # Debug logging to track potential mismatches
            logger.info(
                f"🔍 PROCESS_DEBUG: stream_id={stream.id}, stream.streamer_id={stream.streamer_id}, streamer_name={streamer_name}"
            )

            # ===== MULTI-PROXY SYSTEM: Get best available proxy =====
            # CRITICAL: This prevents recording failures when proxies go down
            # Uses health checks and automatic failover to select best proxy
            proxy_settings = None
            use_stored_proxy = False

            from app.database import SessionLocal
            from app.models import RecordingSettings

            with SessionLocal() as db:
                recording_settings = db.query(RecordingSettings).first()

                # Check if proxy system is enabled
                if (
                    not _monitor_handoff
                    and recording_settings
                    and hasattr(recording_settings, "enable_proxy")
                    and recording_settings.enable_proxy
                ):
                    from app.services.proxy.proxy_health_service import (
                        proxy_health_service,
                    )

                    # Get best available proxy from health service
                    best_proxy_url = await proxy_health_service.get_best_proxy()

                    if best_proxy_url:
                        # Use selected proxy
                        proxy_settings = {
                            "http": best_proxy_url,
                            "https": best_proxy_url,
                        }
                        # SECURITY: Sanitize proxy URL to hide credentials - CWE-532
                        from app.utils.security import sanitize_proxy_url_for_logging

                        logger.info(
                            f"✅ Using proxy for recording: {sanitize_proxy_url_for_logging(best_proxy_url)}"
                        )
                    else:
                        # No healthy proxies available
                        fallback_enabled = (
                            hasattr(recording_settings, "fallback_to_direct_connection")
                            and recording_settings.fallback_to_direct_connection
                        )

                        if fallback_enabled:
                            logger.warning(
                                "⚠️ No healthy proxies available - using direct connection (fallback enabled)"
                            )
                            proxy_settings = None  # Direct connection
                        else:
                            error_msg = f"Cannot start recording for {streamer_name}: No healthy proxies available and fallback disabled"
                            logger.error(f"🔴 {error_msg}")
                            raise ProcessError(
                                "No healthy proxies available. Please check proxy settings or enable fallback to direct connection."
                            )
                else:
                    logger.info(
                        "ℹ️ Proxy system disabled - checking stored proxy settings"
                    )
                    use_stored_proxy = not _monitor_handoff

            # Get codec preferences (H.265/AV1 support - Streamlink 8.0.0+)
            # Priority: Streamer-specific > Global default
            supported_codecs = None
            oauth_token = None

            from app.models import GlobalSettings, StreamerRecordingSettings
            from app.services.system.twitch_token_service import TwitchTokenService

            with SessionLocal() as db:
                if not segment_info.get("auth_fallback_to_anonymous"):
                    try:
                        if token_resolution is _UNRESOLVED_RECORDING_TOKEN:
                            token_service = TwitchTokenService(db)
                            token_resolution = (
                                await token_service.resolve_recording_token()
                            )
                        oauth_token = getattr(token_resolution, "token", None)
                        if oauth_token:
                            logger.info(
                                "Using live-validated Twitch authentication for %s",
                                streamer_name,
                            )
                        else:
                            logger.info(
                                "Using anonymous H.264 recording for %s",
                                streamer_name,
                            )
                    except Exception as e:
                        logger.warning(
                            "Failed to resolve Twitch authentication (%s)",
                            type(e).__name__,
                        )
                        oauth_token = None

                global_settings = db.query(GlobalSettings).first()
                if use_stored_proxy and global_settings:
                    stored_proxy = (global_settings.http_proxy or "").strip() or (
                        global_settings.https_proxy or ""
                    ).strip()
                    if stored_proxy:
                        proxy_settings = {"http": stored_proxy}

                # === STEP 2: Get codec preferences ===
                # Try to get per-streamer codec preference first
                streamer_settings = (
                    db.query(StreamerRecordingSettings)
                    .filter(StreamerRecordingSettings.streamer_id == stream.streamer_id)
                    .first()
                )

                if streamer_settings and streamer_settings.supported_codecs:
                    # Per-streamer override
                    supported_codecs = streamer_settings.supported_codecs
                    logger.info(
                        f"🎨 Using per-streamer codec preference for {streamer_name}: {supported_codecs}"
                    )
                else:
                    # Fallback to global default
                    if global_settings and hasattr(global_settings, "supported_codecs"):
                        supported_codecs = global_settings.supported_codecs
                        logger.debug(
                            f"🎨 Using global codec preference: {supported_codecs}"
                        )

            if _proxy_settings is not _UNRESOLVED_RECORDING_TOKEN:
                proxy_settings = _proxy_settings
            if _supported_codecs is not _UNRESOLVED_RECORDING_TOKEN:
                supported_codecs = _supported_codecs

            # NOTE: Proxy connectivity is now handled by ProxyHealthService
            # The health check system continuously monitors proxy status and only
            # returns healthy proxies. No need for manual connectivity check here.

            logger.info(
                f"🎬 PROCESS_START_SEGMENT: stream_id={stream.id}, streamer={streamer_name}"
            )

            # Generate streamlink command for this segment
            # Credentials and proxy selection stay process-local on the CLI.
            # Codec CLI parameters override the static config for this streamer.
            anonymous = oauth_token is None
            if anonymous and not _monitor_handoff:
                logger.warning(
                    "TWITCH_AUTH_FALLBACK_TO_H264 reason=%s source=%s attempt=1",
                    getattr(token_resolution, "reason", "credential_unavailable"),
                    getattr(token_resolution, "source", None) or "unknown",
                )
            cmd = get_streamlink_command(
                streamer_name=streamer_name,
                quality=quality,
                output_path=segment_path,
                proxy_settings=proxy_settings,  # Per-recording proxy override (from health check)
                supported_codecs=supported_codecs,  # Per-streamer codec preference (overrides global)
                oauth_token=oauth_token,  # Auto-refreshed OAuth token (overrides config)
                anonymous=anonymous,
            )

            logger.info(
                f"🎬 Starting segment {segment_info['segment_count']} for {streamer_name}"
            )
            logger.debug(f"🎬 Segment path: {segment_path}")
            # SECURITY: Command logging disabled to prevent token exposure (CWE-532)
            # Full command details are available in structured logs if needed
            logger.debug(f"🎬 Streamlink process starting with quality: {quality}")

            # Log to structured logging service
            if self.logging_service:
                streamlink_log_path = self.logging_service.get_streamlink_log_path(
                    streamer_name
                )

                logger.info(
                    f"📂 Streamlink logs for {streamer_name} written to: {streamlink_log_path}"
                )

                # Create additional streamer logger for direct output
                streamer_logger = logging.getLogger(f"streamlink.{streamer_name}")

                # Remove any existing FileHandler instances for this logger to prevent memory leaks
                for handler in list(streamer_logger.handlers):
                    if isinstance(handler, logging.FileHandler):
                        streamer_logger.removeHandler(handler)
                        handler.close()

                # Add a new FileHandler pointing to the streamer-specific log
                file_handler = logging.FileHandler(
                    streamlink_log_path, mode="a", encoding="utf-8"
                )
                file_handler.setFormatter(
                    logging.Formatter(
                        "%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
                    )
                )
                streamer_logger.addHandler(file_handler)
                streamer_logger.setLevel(logging.INFO)
                streamer_logger.propagate = False

                streamer_logger.info(
                    f"Starting streamlink recording for {streamer_name}"
                )
                streamer_logger.info(f"Quality: {quality}")
                streamer_logger.info(f"Output: {segment_path}")
                # SECURITY: Command details omitted to prevent token exposure (CWE-532)
                streamer_logger.info("Recording started with configured authentication")
                streamer_logger.info(f"Segment: {segment_info['segment_count']}")
                streamer_logger.info("=" * 80)

            process_id = f"stream_{stream.id}"
            rotation_generation = segment_info.get("upstream_rotation_generation")
            activation_required = (
                segment_info.get("upstream_channel_key")
                and rotation_generation != segment_info["upstream_generation"]
            )
            process_identity = None
            fallback_attempted = False
            fallback_owner = _handoff_owner

            while True:
                if fallback_owner is not None:
                    try:
                        async with self.lock:
                            if (
                                self.active_processes.get(process_id)
                                is not fallback_owner
                                or self.long_stream_processes.get(process_id)
                                is not segment_info
                            ):
                                raise ProcessError("Recording stopped during fallback")
                            process = await asyncio.create_subprocess_exec(
                                *cmd,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                                start_new_session=True,
                            )
                            self._remember_streamlink_output_secrets(process, cmd)
                            self.active_processes[process_id] = process
                    except BaseException:
                        async with self.lock:
                            if self.active_processes.get(process_id) is fallback_owner:
                                del self.active_processes[process_id]
                            if (
                                self.long_stream_processes.get(process_id)
                                is segment_info
                            ):
                                del self.long_stream_processes[process_id]
                        raise
                    fallback_owner = None
                else:
                    process = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        start_new_session=True,
                    )
                    self._remember_streamlink_output_secrets(process, cmd)

                    # Publish ownership before any cancellable post-creation work.
                    self.active_processes[process_id] = process

                if activation_required:
                    identity_task = asyncio.create_task(
                        self.upstream_coordinator.inspect_process_identity(process.pid)
                    )
                    try:
                        process_identity = await asyncio.shield(identity_task)
                    except asyncio.CancelledError:
                        process_identity = await identity_task
                        segment_info["upstream_process_group_id"] = (
                            process_identity.process_group_id
                        )
                        segment_info["upstream_process_started_at"] = (
                            process_identity.started_at
                        )
                        segment_info["upstream_process_start_fingerprint"] = (
                            process_identity.fingerprint
                        )
                        segment_info["upstream_activated"] = False
                        raise

                    segment_info["upstream_process_group_id"] = (
                        process_identity.process_group_id
                    )
                    segment_info["upstream_process_started_at"] = (
                        process_identity.started_at
                    )
                    segment_info["upstream_process_start_fingerprint"] = (
                        process_identity.fingerprint
                    )
                    segment_info["upstream_activated"] = False

                if anonymous:
                    await asyncio.sleep(ASYNC_DELAYS.PROCESS_START_GRACE)
                else:
                    await self._wait_for_auth_startup(process, segment_path)
                if process.returncode is None:
                    if not anonymous and not self._segment_has_recording_data(
                        segment_path
                    ):
                        # The completion monitor owns no-byte auth failures that
                        # arrive after this bounded startup observation.
                        segment_info["auth_startup_handoff"] = {
                            "process": process,
                            "stream": stream,
                            "quality": quality,
                            "stored_version": getattr(
                                token_resolution, "stored_version", None
                            ),
                            "source": getattr(token_resolution, "source", None),
                            "proxy_settings": proxy_settings,
                            "supported_codecs": supported_codecs,
                        }
                    break

                stdout, stderr = await process.communicate()
                known_secrets = self._get_streamlink_output_secrets(process)
                safe_output = "\n".join(
                    (
                        sanitize_streamlink_output(stdout, known_secrets),
                        sanitize_streamlink_output(stderr, known_secrets),
                    )
                )
                logger.error(
                    "PROCESS_FAILED_IMMEDIATELY: stream=%s exit_code=%s",
                    stream.id,
                    process.returncode,
                )
                logger.error("Streamlink exited during recording startup")

                if self.logging_service:
                    self.logging_service.log_streamlink_output(
                        streamer_name,
                        stdout,
                        stderr,
                        process.returncode,
                        known_secrets=known_secrets,
                    )
                    streamer_logger = logging.getLogger(f"streamlink.{streamer_name}")
                    try:
                        streamer_logger.error(
                            "PROCESS FAILED IMMEDIATELY (exit code: %s)",
                            process.returncode,
                        )
                    except Exception as e:
                        logger.warning(
                            "Could not write to streamlink log file (%s)",
                            type(e).__name__,
                        )

                self._release_streamlink_output_secrets(process)
                if (
                    segment_info.get("auth_startup_handoff", {}).get("process")
                    is process
                ):
                    segment_info.pop("auth_startup_handoff", None)
                segment_has_data = self._segment_has_recording_data(segment_path)

                should_fallback = (
                    not anonymous
                    and not fallback_attempted
                    and not segment_has_data
                    and is_twitch_auth_rejection(safe_output)
                )
                if should_fallback:
                    fallback_attempted = True
                    fallback_owner = process
                    anonymous = True
                    oauth_token = None
                    segment_info["auth_fallback_to_anonymous"] = True
                    stored_version = getattr(token_resolution, "stored_version", None)
                    if stored_version is not None:
                        try:
                            with SessionLocal() as db:
                                TwitchTokenService(db).invalidate_recording_token(
                                    stored_version
                                )
                        except Exception:
                            logger.warning(
                                "Could not invalidate runtime-rejected Twitch token"
                            )
                    if activation_required:
                        try:
                            await self._replace_startup_reservation_with_anonymous(
                                segment_info
                            )
                        except BaseException:
                            async with self.lock:
                                if self.active_processes.get(process_id) is process:
                                    del self.active_processes[process_id]
                                if (
                                    self.long_stream_processes.get(process_id)
                                    is segment_info
                                ):
                                    del self.long_stream_processes[process_id]
                            raise
                    logger.warning(
                        "TWITCH_AUTH_FALLBACK_TO_H264 "
                        "reason=streamlink_auth_rejection source=%s attempt=2",
                        getattr(token_resolution, "source", None) or "unknown",
                    )
                    cmd = get_streamlink_command(
                        streamer_name=streamer_name,
                        quality=quality,
                        output_path=segment_path,
                        proxy_settings=proxy_settings,
                        supported_codecs=supported_codecs,
                        oauth_token=None,
                        anonymous=True,
                    )
                    continue

                async with self.lock:
                    if self.active_processes.get(process_id) is process:
                        del self.active_processes[process_id]
                async with self.lock:
                    if self.long_stream_processes.get(process_id) is segment_info:
                        del self.long_stream_processes[process_id]
                raise ProcessError("Streamlink process failed immediately")

            if activation_required:
                assert process_identity is not None
                activation_task = asyncio.create_task(
                    self.upstream_coordinator.activate(
                        channel_key=segment_info["upstream_channel_key"],
                        generation=segment_info["upstream_generation"],
                        process_pid=process.pid,
                        process_group_id=process_identity.process_group_id,
                        process_started_at=process_identity.started_at,
                        process_start_fingerprint=process_identity.fingerprint,
                    )
                )
                try:
                    active_lease = await asyncio.shield(activation_task)
                except asyncio.CancelledError:
                    active_lease = await activation_task
                    segment_info["upstream_process_group_id"] = (
                        active_lease.process_group_id
                    )
                    segment_info["upstream_process_started_at"] = getattr(
                        active_lease,
                        "process_started_at",
                        process_identity.started_at,
                    )
                    segment_info["upstream_process_start_fingerprint"] = (
                        active_lease.process_start_fingerprint
                    )
                    segment_info["upstream_activated"] = True
                    raise
                segment_info["upstream_process_group_id"] = (
                    active_lease.process_group_id
                )
                segment_info["upstream_process_started_at"] = getattr(
                    active_lease,
                    "process_started_at",
                    process_identity.started_at,
                )
                segment_info["upstream_process_start_fingerprint"] = (
                    active_lease.process_start_fingerprint
                )
                segment_info["upstream_activated"] = True

            # A monitor-owned retry replaces the process for this segment rather
            # than adding another segment or completion monitor.
            if _monitor_handoff:
                for segment in reversed(segment_info["total_segments"]):
                    if segment["path"] == segment_path:
                        segment["process_pid"] = process.pid
                        break
                else:
                    segment_info["total_segments"].append(
                        {
                            "path": segment_path,
                            "start_time": datetime.now(),
                            "process_pid": process.pid,
                        }
                    )
            else:
                segment_info["total_segments"].append(
                    {
                        "path": segment_path,
                        "start_time": datetime.now(),
                        "process_pid": process.pid,
                    }
                )
                self._track_segment_completion(process)

            # Register process with ProcessMonitor - temporarily disabled
            # if process_monitor and ProcessType:
            #     await process_monitor.register_process(
            #         process_id=f"streamlink_{stream.id}_{segment_info['segment_count']}",
            #         process_type=ProcessType.STREAMLINK,
            #         pid=process.pid,
            #         command=' '.join(cmd),
            #         streamer_id=stream.streamer_id,
            #         stream_id=stream.id,
            #         metadata={
            #             'segment_path': segment_path,
            #             'segment_count': segment_info['segment_count'],
            #             'quality': quality
            #         }
            #     )

            logger.info(
                f"Started segment recording for stream {stream.id} with PID {process.pid}"
            )

            if _monitor_handoff:
                return process

            # Send Apprise notification for recording_started (NEW)
            try:
                from app.services.notifications.external_notification_service import (
                    ExternalNotificationService,
                )

                notification_service = ExternalNotificationService()

                await notification_service.send_recording_notification(
                    streamer_name=streamer_name,
                    event_type="recording_started",
                    details={
                        "quality": quality,
                        "stream_title": stream.title or "N/A",
                        "category": stream.category_name or "N/A",
                    },
                )

                logger.info(
                    f"📧 Apprise notification sent: recording_started for {streamer_name}"
                )

            except Exception as apprise_error:
                logger.error(
                    f"Failed to send Apprise notification for recording_started: {apprise_error}"
                )

            return process

        except Exception as e:
            logger.error(
                "Failed to start segment recording for stream %s (%s)",
                stream.id,
                type(e).__name__,
                exc_info=True,
            )
            raise ProcessError("Failed to start segment recording") from e

    async def _monitor_long_stream(
        self, stream: Stream, segment_info: Dict, quality: str
    ):
        """Monitor a long stream and handle segmentation"""
        process_id = f"stream_{stream.id}"

        try:
            while process_id in self.active_processes:
                await asyncio.sleep(min(self.monitor_interval_seconds, 10))

                if segment_info.get("upstream_channel_key"):
                    heartbeat_ok = await self.upstream_coordinator.heartbeat(
                        channel_key=segment_info["upstream_channel_key"],
                        generation=segment_info["upstream_generation"],
                    )
                    if not heartbeat_ok:
                        logger.warning(
                            "Recording lease heartbeat was fenced for stream %s",
                            stream.id,
                        )
                        break

                # Check if we need to start a new segment
                should_rotate = await self._should_rotate_segment(segment_info)

                if should_rotate:
                    logger.info(f"Rotating segment for stream {stream.id}")
                    rotated = await self._rotate_segment(stream, segment_info, quality)
                    if not rotated:
                        logger.error(
                            f"Segment rotation failed for stream {stream.id}; "
                            "current process ownership retained"
                        )

        except asyncio.CancelledError:
            logger.info(f"Long stream monitoring cancelled for stream {stream.id}")
        except Exception as e:
            logger.error(
                f"Error in long stream monitoring for stream {stream.id}: {e}",
                exc_info=True,
            )

    async def _should_rotate_segment(self, segment_info: Dict) -> bool:
        """Check if we should start a new segment"""
        try:
            # Check duration
            duration = datetime.now() - segment_info["segment_start_time"]
            if duration >= timedelta(hours=self.segment_duration_hours):
                logger.info(f"Segment duration limit reached: {duration}")
                return True

            # Check file size
            current_path = segment_info["current_segment_path"]
            if await async_file.exists(current_path):
                file_size_gb = await async_file.getsize(current_path) / (1024**3)
                if file_size_gb >= self.max_file_size_gb:
                    logger.info(
                        f"Segment file size limit reached: {file_size_gb:.2f} GB"
                    )
                    return True

            return False

        except Exception as e:
            logger.error(f"Error checking segment rotation: {e}", exc_info=True)
            return False

    async def _rotate_segment(
        self, stream: Stream, segment_info: Dict, quality: str
    ) -> bool:
        """Rotate to a new segment file"""
        process_id = f"stream_{stream.id}"
        rotation_locks = getattr(self, "rotation_locks", None)
        if rotation_locks is None:
            rotation_locks = self.rotation_locks = {}
        rotation_lock = rotation_locks.setdefault(process_id, asyncio.Lock())
        captured_process = self.active_processes.get(process_id)

        if captured_process is None:
            return False

        async with rotation_lock:
            if self.active_processes.get(process_id) is not captured_process:
                return False

            upstream_channel_key = segment_info.get("upstream_channel_key")
            rotation_generation = None
            replacement_process = None
            rotation_succeeded = False
            failure_reason = "rotation_failed"
            try:
                if upstream_channel_key:
                    try:
                        begin_task = asyncio.create_task(
                            self.upstream_coordinator.begin_rotation(
                                channel_key=upstream_channel_key,
                                generation=segment_info["upstream_generation"],
                            )
                        )
                        try:
                            rotation = await asyncio.shield(begin_task)
                        except asyncio.CancelledError:
                            rotation = await begin_task
                            rotation_generation = rotation.generation
                            segment_info["upstream_generation"] = rotation.generation
                            segment_info["upstream_rotation_generation"] = (
                                rotation.generation
                            )
                            raise
                        rotation_generation = rotation.generation
                        segment_info["upstream_generation"] = rotation.generation
                        segment_info["upstream_rotation_generation"] = (
                            rotation.generation
                        )
                        await self.upstream_coordinator.assert_stop_authorized(
                            channel_key=upstream_channel_key,
                            generation=rotation.generation,
                            process_pid=captured_process.pid,
                            process_group_id=segment_info["upstream_process_group_id"],
                            process_start_fingerprint=segment_info[
                                "upstream_process_start_fingerprint"
                            ],
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as coordinator_error:
                        logger.error(
                            "Segment rotation fencing failed for stream %s (%s)",
                            stream.id,
                            type(coordinator_error).__name__,
                        )
                        return False

                wait_timeout = ASYNC_DELAYS.RECORDING_ERROR_RECOVERY
                if captured_process.returncode is None:
                    if upstream_channel_key:
                        stopped = await self._terminate_process_group(
                            captured_process,
                            segment_info["upstream_process_group_id"],
                            wait_timeout,
                            segment_info["upstream_process_start_fingerprint"],
                        )
                        if not stopped:
                            return False
                    else:
                        captured_process.terminate()
                        logger.debug(f"Sent SIGTERM to stream {stream.id} process")
                        try:
                            await asyncio.wait_for(
                                captured_process.wait(), timeout=wait_timeout
                            )
                        except TimeoutError:
                            captured_process.kill()
                            logger.debug(f"Sent SIGKILL to stream {stream.id} process")
                            await asyncio.wait_for(
                                captured_process.wait(), timeout=wait_timeout
                            )
                else:
                    await asyncio.wait_for(
                        captured_process.wait(), timeout=wait_timeout
                    )

                if captured_process.returncode is None:
                    logger.error(
                        f"Segment rotation could not confirm exit for stream {stream.id}; "
                        "retaining current ownership"
                    )
                    return False

                async with self.lock:
                    if self.active_processes.get(process_id) is not captured_process:
                        return False
                    del self.active_processes[process_id]
                segment_info["segment_count"] += 1
                base_path = Path(segment_info["base_output_path"])
                segment_filename = f"{base_path.stem}{self.SEGMENT_PART_IDENTIFIER}{segment_info['segment_count']:03d}.ts"
                next_segment_path = Path(segment_info["segment_dir"]) / segment_filename
                segment_info["current_segment_path"] = str(next_segment_path)
                segment_info["segment_start_time"] = datetime.now()

                failure_reason = "rotation_start_failed"
                try:
                    replacement_process = await self._start_segment(
                        stream, str(next_segment_path), quality, segment_info
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as start_error:
                    logger.error(
                        f"Failed to start rotated segment for stream {stream.id} "
                        f"({type(start_error).__name__})"
                    )
                    return False

                if not replacement_process:
                    logger.error(f"Failed to start new segment for stream {stream.id}")
                    return False

                if upstream_channel_key:
                    failure_reason = "rotation_handoff_failed"
                    try:
                        handoff_task = asyncio.create_task(
                            self.upstream_coordinator.handoff_rotation(
                                channel_key=upstream_channel_key,
                                generation=segment_info["upstream_generation"],
                                process_pid=replacement_process.pid,
                                process_group_id=replacement_process.pid,
                                purpose="RECORDING",
                            )
                        )
                        try:
                            handoff = await asyncio.shield(handoff_task)
                        except asyncio.CancelledError:
                            handoff = await handoff_task
                            segment_info["upstream_generation"] = handoff.generation
                            segment_info["upstream_process_group_id"] = (
                                handoff.process_group_id
                            )
                            segment_info["upstream_process_start_fingerprint"] = (
                                handoff.process_start_fingerprint
                            )
                            segment_info.pop("upstream_rotation_generation", None)
                            raise
                        segment_info["upstream_generation"] = handoff.generation
                        segment_info["upstream_process_group_id"] = (
                            handoff.process_group_id
                        )
                        segment_info["upstream_process_start_fingerprint"] = (
                            handoff.process_start_fingerprint
                        )
                        segment_info.pop("upstream_rotation_generation", None)
                    except asyncio.CancelledError:
                        raise
                    except Exception as coordinator_error:
                        logger.error(
                            "Segment rotation handoff failed for stream %s (%s)",
                            stream.id,
                            type(coordinator_error).__name__,
                        )
                        return False

                rotation_succeeded = True
                logger.info(
                    f"Successfully rotated to segment {segment_info['segment_count']} "
                    f"for stream {stream.id}"
                )
                return True
            except asyncio.CancelledError:
                failure_reason = "rotation_cancelled"
                raise
            except Exception as process_error:
                logger.error(
                    f"Segment rotation could not stop stream {stream.id} process "
                    f"({type(process_error).__name__}); retaining current ownership"
                )
                return False
            finally:
                if rotation_generation is not None and not rotation_succeeded:
                    await self._cleanup_failed_rotation(
                        process_id,
                        captured_process,
                        replacement_process,
                        segment_info,
                        failure_reason,
                        wait_timeout=ASYNC_DELAYS.RECORDING_ERROR_RECOVERY,
                    )

    async def _cleanup_failed_rotation(
        self,
        process_id,
        captured_process,
        replacement_process,
        segment_info,
        reason,
        *,
        wait_timeout,
    ) -> None:
        channel_key = segment_info["upstream_channel_key"]
        generation = segment_info["upstream_generation"]
        cleanup_complete = False
        try:
            tracked_process = self.active_processes.get(process_id)
            process = replacement_process or tracked_process
            if process is not None and process.returncode is None:
                if process is captured_process:
                    process_group_id = segment_info["upstream_process_group_id"]
                    fingerprint = segment_info["upstream_process_start_fingerprint"]
                else:
                    identity = await self.upstream_coordinator.inspect_process_identity(
                        process.pid
                    )
                    process_group_id = identity.process_group_id
                    fingerprint = identity.fingerprint
                try:
                    await self.upstream_coordinator.assert_stop_authorized(
                        channel_key=channel_key,
                        generation=generation,
                        process_pid=process.pid,
                        process_group_id=process_group_id,
                        process_start_fingerprint=fingerprint,
                    )
                except PermissionError:
                    if process is captured_process:
                        return
                    await self.upstream_coordinator.assert_rotation_replacement_cleanup_authorized(
                        channel_key=channel_key,
                        generation=generation,
                        process_pid=process.pid,
                        process_group_id=process_group_id,
                        process_start_fingerprint=fingerprint,
                    )
                cleanup_complete = await self._terminate_process_group(
                    process,
                    process_group_id,
                    wait_timeout,
                    fingerprint,
                )
                if cleanup_complete:
                    async with self.lock:
                        if self.active_processes.get(process_id) is process:
                            del self.active_processes[process_id]
            elif process is not None:
                await process.wait()
                async with self.lock:
                    if self.active_processes.get(process_id) is process:
                        del self.active_processes[process_id]
                cleanup_complete = True
            else:
                cleanup_complete = True
        except (OSError, ProcessLookupError, KeyError, AttributeError, PermissionError):
            return
        if cleanup_complete:
            if process is not None:
                self._release_streamlink_output_secrets(process)
            await self.upstream_coordinator.release(
                channel_key=channel_key,
                generation=generation,
                reason=reason,
            )
            segment_info.pop("upstream_rotation_generation", None)

    async def monitor_process(self, process: asyncio.subprocess.Process) -> int:
        """Monitor a recording process until completion with failure detection

        Args:
            process: The process to monitor

        Returns:
            Exit code of the process (0 if segmented recording completed successfully)
        """
        known_secrets = self._get_streamlink_output_secrets(process)
        try:
            # Find if this is a segmented recording
            process_id = None
            segment_info = None

            async with self.lock:
                for pid, proc in self.active_processes.items():
                    if proc == process:
                        process_id = pid
                        break

            if process_id and process_id in self.long_stream_processes:
                segment_info = self.long_stream_processes[process_id]

            stdout, stderr = await process.communicate()
            logging_service = getattr(self, "logging_service", None)
            if logging_service:
                logging_service.log_streamlink_output(
                    process_id or "unknown",
                    stdout,
                    stderr,
                    process.returncode,
                    known_secrets=known_secrets,
                )

            startup_handoff = (
                segment_info.get("auth_startup_handoff") if segment_info else None
            )
            should_retry_anonymously = (
                startup_handoff is not None
                and startup_handoff["process"] is process
                and process.returncode is not None
                and not self._segment_has_recording_data(
                    segment_info["current_segment_path"]
                )
                and is_twitch_auth_rejection(
                    "\n".join(
                        (
                            sanitize_streamlink_output(stdout, known_secrets),
                            sanitize_streamlink_output(stderr, known_secrets),
                        )
                    )
                )
            )

            if should_retry_anonymously:
                rotation_locks = getattr(self, "rotation_locks", None)
                if rotation_locks is None:
                    rotation_locks = self.rotation_locks = {}
                rotation_lock = rotation_locks.setdefault(process_id, asyncio.Lock())

                async with rotation_lock:
                    async with self.lock:
                        if (
                            self.active_processes.get(process_id) is not process
                            or self.long_stream_processes.get(process_id)
                            is not segment_info
                        ):
                            return process.returncode or 0
                    segment_info.pop("auth_startup_handoff", None)
                    segment_info["auth_fallback_to_anonymous"] = True
                    stored_version = startup_handoff["stored_version"]
                    if stored_version is not None:
                        try:
                            from app.database import SessionLocal
                            from app.services.system.twitch_token_service import (
                                TwitchTokenService,
                            )

                            with SessionLocal() as db:
                                TwitchTokenService(db).invalidate_recording_token(
                                    stored_version
                                )
                        except Exception:
                            logger.warning(
                                "Could not invalidate runtime-rejected Twitch token"
                            )
                    if segment_info.get("upstream_channel_key"):
                        await self._replace_startup_reservation_with_anonymous(
                            segment_info
                        )
                    self._release_streamlink_output_secrets(process)
                    logger.warning(
                        "TWITCH_AUTH_FALLBACK_TO_H264 "
                        "reason=streamlink_auth_rejection source=%s attempt=2",
                        startup_handoff["source"] or "unknown",
                    )
                    replacement = await self._start_segment(
                        startup_handoff["stream"],
                        segment_info["current_segment_path"],
                        startup_handoff["quality"],
                        segment_info,
                        token_resolution=None,
                        _monitor_handoff=True,
                        _handoff_owner=process,
                        _proxy_settings=startup_handoff["proxy_settings"],
                        _supported_codecs=startup_handoff["supported_codecs"],
                    )
                if replacement is None:
                    raise ProcessError("Anonymous recording replacement did not start")
                # The replacement's completion needs the same rotation lock.
                return await self.monitor_process(replacement)

            # Handle segmented vs normal recording completion
            if segment_info:
                rotation_locks = getattr(self, "rotation_locks", None)
                if rotation_locks is None:
                    rotation_locks = self.rotation_locks = {}
                rotation_lock = rotation_locks.setdefault(process_id, asyncio.Lock())

                async with rotation_lock:
                    async with self.lock:
                        if self.active_processes.get(process_id) is not process:
                            return process.returncode or 0
                        del self.active_processes[process_id]

                    try:
                        # Claim the exact owner before finalization so rotation
                        # cannot replace it.
                        logger.info(
                            "Segmented recording completed for stream "
                            f"{segment_info['stream_id']}"
                        )
                        await self._finalize_segmented_recording(segment_info)
                        if segment_info.get("upstream_channel_key"):
                            await self.upstream_coordinator.release(
                                channel_key=segment_info["upstream_channel_key"],
                                generation=segment_info["upstream_generation"],
                                reason="recording_process_exited",
                            )
                        return 0  # Success for segmented recording
                    finally:
                        async with self.lock:
                            if (
                                self.long_stream_processes.get(process_id)
                                is segment_info
                            ):
                                del self.long_stream_processes[process_id]
            else:
                # Normal single-file recording
                # CRITICAL: Detect recording failure and update database
                if process.returncode != 0:
                    logger.error(
                        f"🚨 Recording process failed with exit code {process.returncode} (PID: {process.pid})"
                    )

                    # Extract error message from stderr
                    error_message = "Unknown error"
                    failure_reason = "streamlink_crash"

                    if stderr:
                        stderr_text = stderr.decode("utf-8", errors="replace")
                        # Parse common failure reasons
                        if (
                            "ProxyError" in stderr_text
                            or "Tunnel connection failed" in stderr_text
                        ):
                            failure_reason = "proxy_error"
                            error_message = (
                                "Proxy connection failed (500 Internal Server Error)"
                            )
                        elif "Unable to open URL" in stderr_text:
                            failure_reason = "stream_unavailable"
                            error_message = "Stream unavailable or ended"
                        elif "No playable streams found" in stderr_text:
                            failure_reason = "no_streams"
                            error_message = "No playable streams found"
                        else:
                            error_message = (
                                f"Streamlink exited with code {process.returncode}"
                            )

                    # Update database with error information
                    await self._record_failure_in_database(
                        process_id, error_message, failure_reason
                    )

                    # Broadcast WebSocket error notification
                    await self._notify_recording_failed(process_id, error_message)

                else:
                    logger.info(
                        f"✅ Recording process completed successfully (PID: {process.pid})"
                    )

                    # Send Apprise notification for recording_completed (NEW - non-segmented)
                    try:
                        from app.database import SessionLocal
                        from app.models import Stream
                        from app.services.notifications.external_notification_service import (
                            ExternalNotificationService,
                        )
                        import os

                        notification_service = ExternalNotificationService()

                        with SessionLocal() as db:
                            stream_id = (
                                process_id.split("_")[1] if "_" in process_id else None
                            )
                            if stream_id:
                                stream = (
                                    db.query(Stream)
                                    .options(joinedload(Stream.streamer))
                                    .filter(Stream.id == int(stream_id))
                                    .first()
                                )

                                if stream and stream.streamer and stream.recording_path:
                                    # Get quality from streamer recording settings
                                    from app.models import StreamerRecordingSettings

                                    recording_settings = (
                                        db.query(StreamerRecordingSettings)
                                        .filter(
                                            StreamerRecordingSettings.streamer_id
                                            == stream.streamer_id
                                        )
                                        .first()
                                    )
                                    quality = (
                                        recording_settings.quality
                                        if recording_settings
                                        and recording_settings.quality
                                        else "best"
                                    )

                                    # Calculate recording duration from stream timestamps
                                    duration_seconds = 0
                                    if stream.started_at and stream.ended_at:
                                        duration_seconds = int(
                                            (
                                                stream.ended_at - stream.started_at
                                            ).total_seconds()
                                        )

                                    hours = duration_seconds // 3600
                                    minutes = (duration_seconds % 3600) // 60

                                    # Get file size
                                    file_size_bytes = 0
                                    if os.path.exists(stream.recording_path):
                                        file_size_bytes = os.path.getsize(
                                            stream.recording_path
                                        )
                                    file_size_mb = file_size_bytes / (1024 * 1024)

                                    await notification_service.send_recording_notification(
                                        streamer_name=stream.streamer.username,
                                        event_type="recording_completed",
                                        details={
                                            "hours": hours,
                                            "minutes": minutes,
                                            "file_size_mb": f"{file_size_mb:.2f}",
                                            "quality": quality,
                                        },
                                    )

                                    logger.info(
                                        f"📧 Apprise notification sent: recording_completed for {stream.streamer.username}"
                                    )

                    except Exception as apprise_error:
                        logger.error(
                            f"Failed to send Apprise notification for recording_completed: {apprise_error}"
                        )

                return process.returncode or 0

        except Exception as e:
            logger.error(f"Error monitoring process {process.pid}: {e}", exc_info=True)
            return -1
        finally:
            # Cleanup
            await self._cleanup_process(process)

    async def _finalize_segmented_recording(self, segment_info: Dict):
        """Concatenate all segments into final TS file"""
        try:
            logger.info(
                f"Finalizing segmented recording for stream {segment_info['stream_id']}"
            )

            # Cancel monitoring task
            if segment_info["monitor_task"]:
                segment_info["monitor_task"].cancel()

            # Get all segment files from the directory (not just from total_segments)
            # This ensures we capture ALL segments including those from previous sessions
            # (e.g., after app restart during an ongoing stream)
            segment_dir = Path(segment_info["segment_dir"])
            segment_files = []

            if segment_dir.exists():
                # Find all .ts files matching the segment pattern, sorted by name
                # Pattern: *_partNNN.ts
                all_ts_files = sorted(
                    segment_dir.glob("*_part*.ts"), key=lambda x: x.name
                )

                for ts_file in all_ts_files:
                    if (
                        await async_file.exists(str(ts_file))
                        and await async_file.getsize(str(ts_file)) > 0
                    ):
                        segment_files.append(str(ts_file))

                logger.info(
                    f"📦 Found {len(segment_files)} segment files in directory "
                    f"(tracked: {len(segment_info['total_segments'])}, from disk: {len(all_ts_files)})"
                )
            else:
                # Fallback to tracked segments if directory doesn't exist
                logger.warning(
                    "Segment directory not found, using tracked segments only"
                )
                for segment in segment_info["total_segments"]:
                    if (
                        await async_file.exists(segment["path"])
                        and await async_file.getsize(segment["path"]) > 0
                    ):
                        segment_files.append(segment["path"])

            if not segment_files:
                logger.error(
                    f"No valid segment files found for stream {segment_info['stream_id']}"
                )
                return

            # Create concatenation list file for FFmpeg
            concat_list_path = Path(segment_info["segment_dir"]) / "concat_list.txt"
            with open(concat_list_path, "w") as f:
                for segment_file in segment_files:
                    f.write(f"file '{segment_file}'\n")

            # Use FFmpeg to concatenate segments
            output_path = segment_info["base_output_path"]
            cmd = [
                "ffmpeg",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list_path),
                "-c",
                "copy",
                "-y",
                output_path,
            ]

            logger.info(
                f"Concatenating {len(segment_files)} segments for stream {segment_info['stream_id']}"
            )
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                logger.info(f"Successfully concatenated segments into {output_path}")

                # Send Apprise notification for recording_completed (NEW)
                try:
                    from app.database import SessionLocal
                    from app.models import Stream
                    from app.services.notifications.external_notification_service import (
                        ExternalNotificationService,
                    )
                    from datetime import datetime
                    import os

                    notification_service = ExternalNotificationService()

                    with SessionLocal() as db:
                        stream = (
                            db.query(Stream)
                            .options(joinedload(Stream.streamer))
                            .filter(Stream.id == segment_info["stream_id"])
                            .first()
                        )

                        if stream and stream.streamer:
                            # Get quality from streamer recording settings
                            from app.models import StreamerRecordingSettings

                            recording_settings = (
                                db.query(StreamerRecordingSettings)
                                .filter(
                                    StreamerRecordingSettings.streamer_id
                                    == stream.streamer_id
                                )
                                .first()
                            )
                            quality = (
                                recording_settings.quality
                                if recording_settings and recording_settings.quality
                                else "best"
                            )

                            # Calculate recording duration
                            if segment_info.get("segment_start_time"):
                                duration_seconds = int(
                                    (
                                        datetime.now()
                                        - segment_info["segment_start_time"]
                                    ).total_seconds()
                                )
                                hours = duration_seconds // 3600
                                minutes = (duration_seconds % 3600) // 60
                            else:
                                hours = 0
                                minutes = 0

                            # Get file size
                            file_size_bytes = (
                                os.path.getsize(output_path)
                                if os.path.exists(output_path)
                                else 0
                            )
                            file_size_mb = file_size_bytes / (1024 * 1024)

                            await notification_service.send_recording_notification(
                                streamer_name=stream.streamer.username,
                                event_type="recording_completed",
                                details={
                                    "hours": hours,
                                    "minutes": minutes,
                                    "file_size_mb": f"{file_size_mb:.2f}",
                                    "quality": quality,
                                },
                            )

                            logger.info(
                                f"📧 Apprise notification sent: recording_completed for {stream.streamer.username}"
                            )

                except Exception as apprise_error:
                    logger.error(
                        f"Failed to send Apprise notification for recording_completed: {apprise_error}"
                    )

                # Move concatenated file from segments directory to parent directory
                await self._move_concatenated_file_to_parent(segment_info)

                # Trigger post-processing for the moved file
                await self._trigger_post_processing_for_segmented_recording(
                    segment_info
                )

                # Clean up segment files and directory only after post-processing starts
                await self._cleanup_segments(segment_info)
            else:
                logger.error(
                    f"Failed to concatenate segments: {stderr.decode('utf-8', errors='replace')[:500]}"
                )

        except Exception as e:
            logger.error(f"Error finalizing segmented recording: {e}", exc_info=True)

    async def _move_concatenated_file_to_parent(self, segment_info: Dict):
        """Move concatenated TS file from segments directory to parent directory"""
        try:
            segment_dir = Path(segment_info["segment_dir"])
            parent_dir = segment_dir.parent
            concatenated_file = Path(segment_info["base_output_path"])

            # Create new filename in parent directory
            final_path = parent_dir / concatenated_file.name

            # Check if target file already exists and create unique name if needed
            if final_path.exists():
                counter = 1
                base_name = final_path.stem
                extension = final_path.suffix
                while final_path.exists():
                    final_path = parent_dir / f"{base_name}_copy{counter}{extension}"
                    counter += 1
                logger.warning(f"Target file existed, using unique name: {final_path}")

            # Move the file
            if concatenated_file.exists():
                shutil.move(str(concatenated_file), str(final_path))
                logger.info(
                    f"Moved concatenated file from {concatenated_file} to {final_path}"
                )

                # Update the segment_info with new path for post-processing
                segment_info["final_output_path"] = str(final_path)
            else:
                logger.error(f"Concatenated file not found: {concatenated_file}")

        except Exception as e:
            logger.error(f"Error moving concatenated file: {e}", exc_info=True)

    async def _trigger_post_processing_for_segmented_recording(
        self, segment_info: Dict
    ):
        """Trigger post-processing for the segmented recording using injected callback"""
        try:
            # Use the final output path (moved file) for post-processing
            output_path = segment_info.get(
                "final_output_path", segment_info["base_output_path"]
            )
            stream_id = segment_info["stream_id"]
            # Get recording_id from segment_info if available
            recording_id = segment_info.get("recording_id")

            if self.post_processing_callback and recording_id:
                # Use the injected callback to trigger post-processing with correct recording_id
                await self.post_processing_callback(recording_id, output_path)
                logger.info(
                    f"Triggered post-processing for segmented recording {recording_id} (stream {stream_id}) via callback"
                )
            else:
                # Fallback to direct service access (with circular import handling)
                logger.warning(
                    "No post-processing callback available or recording_id missing, using fallback method"
                )
                await self._fallback_trigger_post_processing(
                    stream_id, output_path, recording_id
                )

        except Exception as e:
            logger.error(
                f"Error triggering post-processing for segmented recording: {e}",
                exc_info=True,
            )

    async def _fallback_trigger_post_processing(
        self, stream_id: int, output_path: str, recording_id: Optional[int] = None
    ):
        """Fallback method for post-processing when no callback is injected"""
        try:
            # Import here to avoid circular imports (only used as fallback)
            from app.routes.recording import get_recording_service

            # Get recording service and orchestrator
            recording_service = get_recording_service()
            if recording_service and recording_service.orchestrator:
                # If recording_id is not provided, try to find it by stream_id
                if not recording_id:
                    # Look for active recording for this specific stream with path validation
                    recordings = await recording_service.orchestrator.database_service.get_recordings_by_status(
                        "recording"
                    )
                    candidate_recording_id: Optional[int] = None

                    # First pass: try to find recording that matches both stream_id and path proximity
                    for recording in recordings:
                        if recording.stream_id == stream_id:
                            # Validate this recording actually belongs to the correct stream by checking the output path
                            try:
                                # Get stream data to verify streamer
                                stream_data = await recording_service.orchestrator.database_service.get_stream_by_id(
                                    recording.stream_id
                                )
                                if stream_data:
                                    streamer_data = await recording_service.orchestrator.database_service.get_streamer_by_id(
                                        stream_data.streamer_id
                                    )
                                    if streamer_data:
                                        # Check if output_path contains the correct streamer name
                                        output_path_obj = Path(output_path)
                                        if (
                                            streamer_data.username.lower()
                                            in str(output_path_obj).lower()
                                        ):
                                            candidate_recording_id = recording.id
                                            logger.info(
                                                f"Found matching recording {recording.id} for stream {stream_id} with path validation"
                                            )
                                            break
                                        else:
                                            logger.warning(
                                                f"Recording {recording.id} belongs to stream {stream_id} but path {output_path} doesn't match streamer {streamer_data.username}"
                                            )
                            except Exception as e:
                                logger.warning(
                                    f"Error validating recording {recording.id}: {e}"
                                )

                    # If no validated candidate found, fall back to first match (with warning)
                    if not candidate_recording_id:
                        logger.warning(
                            f"No path-validated recording found for stream {stream_id}, falling back to first match"
                        )
                        for recording in recordings:
                            if recording.stream_id == stream_id:
                                candidate_recording_id = recording.id
                                break

                    recording_id = candidate_recording_id

                if recording_id:
                    # Get recording data using correct recording_id
                    recording_data = recording_service.orchestrator.database_service.get_recording_by_id(
                        recording_id
                    )
                    if recording_data:
                        # Update recording status using correct recording_id
                        await recording_service.orchestrator.database_service.update_recording_status(
                            recording_id=recording_id,
                            status="completed",
                            path=output_path,  # Use actual recording_id
                        )

                        # Get additional data needed for post-processing
                        stream_data = await recording_service.orchestrator.database_service.get_stream_by_id(
                            recording_data.stream_id
                        )
                        if stream_data:
                            streamer_data = await recording_service.orchestrator.database_service.get_streamer_by_id(
                                stream_data.streamer_id
                            )
                            if streamer_data:
                                # Create recording data dict for post-processing
                                recording_data_dict = {
                                    "streamer_name": streamer_data.username,
                                    "started_at": (
                                        recording_data.start_time.isoformat()
                                        if recording_data.start_time
                                        else None
                                    ),
                                    "stream_id": stream_data.id,
                                    "recording_id": recording_id,  # Use correct recording_id
                                }

                                # Use the public enqueue_post_processing method with correct recording_id
                                await recording_service.orchestrator.enqueue_post_processing(
                                    recording_id=recording_id,  # Use actual recording_id
                                    ts_file_path=output_path,
                                    recording_data=recording_data_dict,
                                )
                                logger.info(
                                    f"Enqueued post-processing for segmented recording {recording_id} (stream {stream_id})"
                                )
                            else:
                                logger.error(
                                    f"Streamer data not found for recording {recording_id}"
                                )
                        else:
                            logger.error(
                                f"Stream data not found for recording {recording_id}"
                            )
                    else:
                        logger.error(
                            f"Recording data not found for recording {recording_id}"
                        )
                else:
                    logger.error(f"Could not find recording_id for stream {stream_id}")
            else:
                logger.warning(
                    "Could not trigger post-processing - recording service not available"
                )

        except Exception as e:
            logger.error(
                f"Error in fallback post-processing trigger: {e}", exc_info=True
            )

    async def _cleanup_segments(self, segment_info: Dict):
        """Clean up segment files after successful concatenation"""
        try:
            segment_dir = Path(segment_info["segment_dir"])

            # Remove segment files
            for segment in segment_info["total_segments"]:
                try:
                    if await async_file.exists(segment["path"]):
                        await async_file.remove(segment["path"])
                except Exception as e:
                    logger.warning(
                        f"Could not remove segment file {segment['path']}: {e}"
                    )

            # Remove concat list file
            concat_list_path = segment_dir / "concat_list.txt"
            if concat_list_path.exists():
                concat_list_path.unlink()

            # Remove segment directory if empty
            try:
                segment_dir.rmdir()
                logger.info(f"Cleaned up segment directory: {segment_dir}")
            except OSError:
                logger.debug(f"Segment directory not empty, keeping: {segment_dir}")

        except Exception as e:
            logger.error(f"Error cleaning up segments: {e}", exc_info=True)

    async def _cleanup_process(self, process: asyncio.subprocess.Process):
        """Clean up process from tracking"""
        self._release_streamlink_output_secrets(process)
        async with self.lock:
            # Remove from active processes
            for process_id, active_process in list(self.active_processes.items()):
                if active_process == process:
                    del self.active_processes[process_id]

                    # Clean up long stream tracking
                    if process_id in self.long_stream_processes:
                        segment_info = self.long_stream_processes[process_id]
                        if segment_info["monitor_task"]:
                            segment_info["monitor_task"].cancel()
                        del self.long_stream_processes[process_id]
                    break

    async def terminate_process(self, process_id: str, timeout: int = 10) -> bool:
        """
        Gracefully terminate a process (handles segmented recordings)

        Returns:
            bool: True if process was terminated or already terminated, False if termination failed

        Note: Process not found is considered SUCCESS because:
        - Process may have already terminated naturally (common with Streamlink)
        - Recording data is intact and should not be marked as failed
        - This prevents false negatives in recording status
        """
        async with self.lock:
            if process_id not in self.active_processes:
                # PRODUCTION FIX: Process not found should be considered success
                # This is NOT a breaking change - it fixes a logic bug where
                # recordings were incorrectly marked as "failed" when the process
                # had already terminated naturally (which is normal behavior)
                logger.info(
                    f"Process {process_id} not found in active processes - assuming already terminated"
                )
                return True  # Process already terminated = success, not failure

            process = self.active_processes[process_id]
            segment_info = self.long_stream_processes.get(process_id)
            if segment_info and segment_info.get("upstream_channel_key"):
                if segment_info.get("upstream_activated", True):
                    try:
                        await self.upstream_coordinator.assert_stop_authorized(
                            channel_key=segment_info["upstream_channel_key"],
                            generation=segment_info["upstream_generation"],
                            process_pid=process.pid,
                            process_group_id=segment_info["upstream_process_group_id"],
                            process_start_fingerprint=segment_info[
                                "upstream_process_start_fingerprint"
                            ],
                        )
                    except PermissionError:
                        logger.warning("Refusing stale stop for process %s", process_id)
                        return False
                elif not (
                    segment_info.get("upstream_process_group_id")
                    and segment_info.get("upstream_process_start_fingerprint")
                ):
                    logger.warning(
                        "Refusing stop before process identity is available for %s",
                        process_id,
                    )
                    return False

            self.active_processes.pop(process_id)
            if segment_info:
                if segment_info["monitor_task"]:
                    segment_info["monitor_task"].cancel()

            try:
                if segment_info and segment_info.get("upstream_channel_key"):
                    stopped = await self._terminate_process_group(
                        process,
                        segment_info["upstream_process_group_id"],
                        timeout,
                        segment_info["upstream_process_start_fingerprint"],
                    )
                    if not stopped:
                        self.active_processes[process_id] = process
                        return False
                else:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=timeout)
                logger.info(f"Process {process_id} terminated gracefully")
                self._release_streamlink_output_secrets(process)

                # Finalize segmented recording if needed
                if segment_info:
                    self.long_stream_processes.pop(process_id, None)
                    await self._finalize_segmented_recording(segment_info)

                if segment_info and segment_info.get("upstream_channel_key"):
                    await self.upstream_coordinator.release(
                        channel_key=segment_info["upstream_channel_key"],
                        generation=segment_info["upstream_generation"],
                        reason="recording_stopped",
                    )

                return True
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                logger.warning(f"Process {process_id} killed after timeout")
                self._release_streamlink_output_secrets(process)

                # Still try to finalize if it was segmented
                if process_id in self.long_stream_processes:
                    segment_info = self.long_stream_processes.pop(process_id)
                    await self._finalize_segmented_recording(segment_info)

                return True
            except Exception as e:
                if process.returncode is None:
                    self.active_processes[process_id] = process
                else:
                    self._release_streamlink_output_secrets(process)
                logger.error(f"Failed to terminate process {process_id}: {e}")
                return False

    async def _terminate_process_group(
        self,
        process,
        process_group_id,
        timeout,
        process_start_fingerprint=None,
    ) -> bool:
        if process.returncode is not None:
            await process.wait()
            return True
        if process_start_fingerprint is None:
            try:
                identity = await self.upstream_coordinator.inspect_process_identity(
                    process.pid
                )
            except Exception:
                return False
            if identity.process_group_id != process_group_id:
                return False
            process_start_fingerprint = identity.fingerprint
        elif not await self._process_identity_matches(
            process.pid,
            process_group_id,
            process_start_fingerprint,
        ):
            return False
        try:
            try:
                os.killpg(process_group_id, signal.SIGTERM)
            except ProcessLookupError:
                return process.returncode is not None
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except TimeoutError:
                if not await self._process_identity_matches(
                    process.pid,
                    process_group_id,
                    process_start_fingerprint,
                ):
                    return False
                try:
                    os.killpg(process_group_id, signal.SIGKILL)
                except ProcessLookupError:
                    return process.returncode is not None
                await asyncio.wait_for(process.wait(), timeout=timeout)
            return process.returncode is not None
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def _process_identity_matches(
        self,
        process_pid,
        process_group_id,
        process_start_fingerprint,
    ) -> bool:
        try:
            identity = await self.upstream_coordinator.inspect_process_identity(
                process_pid
            )
        except Exception:
            return False
        return (
            identity.pid,
            identity.process_group_id,
            identity.fingerprint,
        ) == (process_pid, process_group_id, process_start_fingerprint)

    async def cleanup_all(self):
        """Terminate all active processes"""
        process_ids = list(self.active_processes.keys())
        for process_id in process_ids:
            await self.terminate_process(process_id)

    def get_active_process_count(self) -> int:
        """Get the number of active recording processes"""
        return len(self.active_processes)

    async def graceful_shutdown(self, timeout: int = 15):
        """Gracefully shutdown all recording processes

        Args:
            timeout: Maximum time to wait for processes to terminate (seconds)
        """
        logger.info("🛑 Starting graceful shutdown of Process Manager...")
        self._is_shutting_down = True

        try:
            # Get list of active processes
            active_process_count = len(self.active_processes)
            segmented_process_count = len(self.long_stream_processes)

            if active_process_count == 0 and segmented_process_count == 0:
                logger.info("No active processes to shutdown")
                return

            logger.info(
                f"⏳ Terminating {active_process_count} active processes and {segmented_process_count} segmented processes..."
            )

            # Terminate all active processes gracefully
            termination_tasks = []

            for process_id in list(self.active_processes):
                termination_tasks.append(
                    self.terminate_process(process_id, timeout=timeout)
                )

            # Wait for all terminations to complete
            if termination_tasks:
                await asyncio.gather(*termination_tasks, return_exceptions=True)

            logger.info(
                "Process Manager shutdown completed with %s fenced owners retained",
                len(self.active_processes),
            )

        except Exception as e:
            logger.error(
                f"❌ Error during Process Manager shutdown: {e}", exc_info=True
            )

    async def _terminate_process_gracefully(
        self, stream_id: int, process: asyncio.subprocess.Process, timeout: int
    ):
        """Gracefully terminate a single process"""
        try:
            if process.returncode is None:  # Process is still running
                logger.info(
                    f"🔄 Terminating recording process for stream {stream_id} (PID: {process.pid})"
                )

                # Send SIGTERM for graceful termination
                process.terminate()

                try:
                    # Wait for process to terminate gracefully
                    await asyncio.wait_for(process.wait(), timeout=timeout)
                    logger.info(
                        f"✅ Process for stream {stream_id} terminated gracefully"
                    )

                except asyncio.TimeoutError:
                    # Force kill if timeout
                    logger.warning(
                        f"⚠️ Process for stream {stream_id} didn't terminate gracefully, force killing..."
                    )
                    process.kill()
                    await process.wait()
                    logger.info(f"💀 Process for stream {stream_id} force killed")

            else:
                logger.info(f"Process for stream {stream_id} already terminated")

        except Exception as e:
            logger.error(f"Error terminating process for stream {stream_id}: {e}")

    def is_shutting_down(self) -> bool:
        """Check if process manager is shutting down"""
        return self._is_shutting_down

    async def _record_failure_in_database(
        self, process_id: str, error_message: str, failure_reason: str
    ):
        """Record recording failure in database for visibility

        Args:
            process_id: Process ID (format: stream_{stream_id})
            error_message: Detailed error message
            failure_reason: Short failure category
        """
        try:
            from app.database import SessionLocal
            from app.models import Recording
            from datetime import datetime, timezone

            # Extract stream_id from process_id
            stream_id = process_id.split("_")[1] if "_" in process_id else None
            if not stream_id:
                logger.warning(
                    f"Could not extract stream_id from process_id: {process_id}"
                )
                return

            with SessionLocal() as db:
                # Find the active recording for this stream
                recording = (
                    db.query(Recording)
                    .filter(
                        Recording.stream_id == int(stream_id),
                        Recording.status == "recording",
                    )
                    .first()
                )

                if recording:
                    # Update recording with failure information
                    recording.status = "failed"
                    recording.error_message = error_message
                    recording.failure_reason = failure_reason
                    recording.failure_timestamp = datetime.now(timezone.utc)
                    recording.end_time = datetime.now(timezone.utc)

                    db.commit()
                    logger.info(
                        f"✅ Recorded failure in database for recording {recording.id}: {failure_reason}"
                    )
                else:
                    logger.warning(f"No active recording found for stream {stream_id}")

        except Exception as e:
            logger.error(f"Failed to record failure in database: {e}", exc_info=True)

    async def _notify_recording_failed(self, process_id: str, error_message: str):
        """Broadcast WebSocket notification for recording failure

        Args:
            process_id: Process ID (format: stream_{stream_id})
            error_message: Error message to broadcast
        """
        try:
            from app.database import SessionLocal
            from app.models import Stream
            from datetime import datetime, timezone

            # Extract stream_id
            stream_id = process_id.split("_")[1] if "_" in process_id else None
            if not stream_id:
                return

            with SessionLocal() as db:
                # Get stream and streamer info
                stream = (
                    db.query(Stream)
                    .options(joinedload(Stream.streamer))
                    .filter(Stream.id == int(stream_id))
                    .first()
                )

                if stream and stream.streamer:
                    # Broadcast WebSocket event
                    try:
                        from app.services.websocket.websocket_manager import (
                            websocket_manager,
                        )

                        await websocket_manager.broadcast(
                            {
                                "type": "recording_failed",
                                "data": {
                                    "stream_id": stream.id,
                                    "streamer_id": stream.streamer.id,
                                    "streamer_name": stream.streamer.username,
                                    "error_message": error_message,
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                    "stream_title": stream.title or "N/A",
                                    "category": stream.category_name or "N/A",
                                },
                            }
                        )

                        logger.info(
                            f"📡 WebSocket notification sent: recording_failed for {stream.streamer.username}"
                        )

                    except Exception as ws_error:
                        logger.error(
                            f"Failed to send WebSocket notification: {ws_error}"
                        )

                    # Send Apprise notification (NEW)
                    try:
                        from app.services.notifications.external_notification_service import (
                            ExternalNotificationService,
                        )

                        notification_service = ExternalNotificationService()

                        await notification_service.send_recording_notification(
                            streamer_name=stream.streamer.username,
                            event_type="recording_failed",
                            details={
                                "error_message": error_message,
                                "timestamp": datetime.now(timezone.utc).strftime(
                                    "%Y-%m-%d %H:%M:%S UTC"
                                ),
                                "stream_title": stream.title or "N/A",
                                "category": stream.category_name or "N/A",
                            },
                        )

                        logger.info(
                            f"📧 Apprise notification sent: recording_failed for {stream.streamer.username}"
                        )

                    except Exception as apprise_error:
                        logger.error(
                            f"Failed to send Apprise notification: {apprise_error}"
                        )

        except Exception as e:
            logger.error(f"Error in _notify_recording_failed: {e}", exc_info=True)

    async def get_recording_progress(self, recording_id: int) -> Optional[Dict]:
        """Get progress information for a recording

        Args:
            recording_id: ID of the recording to check progress for

        Returns:
            Dictionary with progress information or None if not found
        """
        try:
            process_id = f"stream_{recording_id}"

            async with self.lock:
                if process_id not in self.active_processes:
                    return None

                process = self.active_processes[process_id]

                # Check if process is still running
                if process.returncode is not None:
                    return {"status": "completed", "exit_code": process.returncode}

                # Get basic process info
                progress = {
                    "status": "running",
                    "pid": process.pid,
                    "duration": None,
                    "file_size": None,
                    "segment_count": 1,
                }

                # Add segment info if available
                if process_id in self.long_stream_processes:
                    segment_info = self.long_stream_processes[process_id]
                    progress["segment_count"] = segment_info.get("segment_count", 1)

                    # Calculate duration if we have start time
                    if "segment_start_time" in segment_info:
                        duration = datetime.now() - segment_info["segment_start_time"]
                        progress["duration"] = int(duration.total_seconds())

                    # Get file size if available
                    current_path = segment_info.get("current_segment_path")
                    if current_path and await async_file.exists(current_path):
                        file_size = await async_file.getsize(current_path)
                        progress["file_size"] = file_size

                return progress

        except Exception as e:
            logger.error(
                f"Error getting recording progress for {recording_id}: {e}",
                exc_info=True,
            )
            return None

    def set_post_processing_callback(
        self, callback: Optional[Callable[[int, str], Awaitable[None]]]
    ):
        """Set the post-processing callback for dependency injection

        Args:
            callback: Async function that takes (recording_id: int, file_path: str) parameters
        """
        self.post_processing_callback = callback
        logger.info("Post-processing callback set for ProcessManager")


# Global singleton instance - use this for all process management operations
# This ensures all RecordingOrchestrator/RecordingService instances share the same process state
process_manager = ProcessManager()
