"""
Streamlink command utility functions for StreamVault.

This module provides utility functions for constructing and executing Streamlink commands,
following the robust approach from the lsdvr project. It handles proxy configuration,
quality settings, and other parameters to ensure reliable stream capture.
"""

import os
import logging
import subprocess
import json
from typing import List, Optional, Dict, Any, Tuple
from urllib.parse import urlparse

from app.models import GlobalSettings
from app.utils.security import (
    sanitize_command_for_logging,
    sanitize_proxy_url_for_logging,
)

# Get the logger
logger = logging.getLogger(__name__)


def _select_proxy_url(proxy_settings: Dict[str, str]) -> str:
    return (
        proxy_settings.get("http", "").strip()
        or proxy_settings.get("https", "").strip()
    )


def get_streamlink_version() -> str:
    """
    Get the installed version of Streamlink.

    Returns:
        String containing the version of Streamlink
    """
    try:
        result = subprocess.run(
            ["streamlink", "--version"], capture_output=True, text=True, check=True
        )
        # Extract version number from the output
        version_line = result.stdout.strip()
        # Usually outputs something like "streamlink 5.5.1"
        if version_line:
            parts = version_line.split()
            if len(parts) >= 2:
                return parts[1]  # Return just the version number
        return version_line
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to get Streamlink version: {e}")
        return "Unknown"
    except Exception as e:
        logger.error(f"Error getting Streamlink version: {e}")
        return "Error"


def check_proxy_connectivity(
    proxy_settings: Optional[Dict[str, str]] = None,
) -> Tuple[bool, str]:
    """
    Check if proxy is reachable before attempting to record.

    Args:
        proxy_settings: Optional dictionary containing "http" and/or "https" proxy URLs

    Returns:
        Tuple of (is_reachable: bool, error_message: str)
    """
    if not proxy_settings or not any(proxy_settings.values()):
        # No proxy configured, connectivity is assumed OK
        return True, ""

    # Test proxy connectivity with a simple Streamlink command
    test_cmd = ["streamlink", "--json", "twitch.tv/test"]

    proxy_url = _select_proxy_url(proxy_settings)
    if proxy_url:
        test_cmd.append(f"--http-proxy={proxy_url}")

    try:
        # Use a short timeout to fail fast if proxy is down
        result = subprocess.run(
            test_cmd,
            capture_output=True,
            text=True,
            timeout=10,  # 10 second timeout
            check=False,  # Don't raise exception on non-zero exit
        )

        # Check for proxy connection errors in stderr
        stderr_lower = result.stderr.lower() if result.stderr else ""

        # Common proxy error patterns
        proxy_error_patterns = [
            "unable to connect to proxy",
            "proxy connection failed",
            "connection refused",
            "proxy error",
            "failed to connect",
            "network is unreachable",
            "connection timed out",
            "name or service not known",  # DNS resolution failure
        ]

        for pattern in proxy_error_patterns:
            if pattern in stderr_lower:
                error_msg = f"Proxy connectivity check failed: {pattern}"
                logger.error(f"🔴 {error_msg}")
                return False, error_msg

        # If we got here without errors, proxy is reachable
        logger.debug("✅ Proxy connectivity check passed")
        return True, ""

    except subprocess.TimeoutExpired:
        error_msg = "Proxy connectivity check timed out after 10 seconds"
        logger.error(f"🔴 {error_msg}")
        return False, error_msg
    except Exception:
        error_msg = "Proxy connectivity check failed with an unexpected error"
        logger.error(f"🔴 {error_msg}")
        return False, error_msg


def get_stream_info(
    streamer_name: str, proxy_settings: Optional[Dict[str, str]] = None
) -> Tuple[bool, Dict[str, Any]]:
    """
    Get information about a stream using Streamlink.

    Args:
        streamer_name: The streamer's username
        proxy_settings: Optional dictionary containing "http" and/or "https" proxy URLs

    Returns:
        Tuple of (success: bool, info: dict)
        where info contains stream details if successful
    """
    # Check proxy connectivity first if proxy is configured
    if proxy_settings and any(proxy_settings.values()):
        is_reachable, proxy_error = check_proxy_connectivity(proxy_settings)
        if not is_reachable:
            logger.error(
                f"🔴 PROXY_DOWN: Cannot get stream info for {streamer_name} - {proxy_error}"
            )
            return False, {
                "error": "Proxy connection failed",
                "details": proxy_error,
                "proxy_settings": {
                    k: sanitize_proxy_url_for_logging(v)
                    for k, v in proxy_settings.items()
                    if v
                },
            }

    cmd = ["streamlink", "--json", f"twitch.tv/{streamer_name}"]

    # Add proxy settings if provided
    if proxy_settings:
        proxy_url = _select_proxy_url(proxy_settings)
        if proxy_url:
            cmd.append(f"--http-proxy={proxy_url}")

    try:
        logger.debug(
            f"Running stream info command: {sanitize_command_for_logging(cmd)}"
        )
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=30
        )

        # Parse the JSON output
        stream_info = json.loads(result.stdout)
        return True, stream_info
    except subprocess.TimeoutExpired:
        error_msg = "Streamlink command timed out after 30 seconds"
        logger.error(f"🔴 {error_msg} for {streamer_name}")
        return False, {"error": error_msg}
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to get stream info for {streamer_name}")

        # Check if this is a proxy-related error
        stderr_lower = (e.stderr or "").lower()
        if any(
            pattern in stderr_lower
            for pattern in ["proxy", "connection refused", "network unreachable"]
        ):
            return False, {
                "error": "Proxy or network connection failed",
                "details": "Check proxy settings or network connectivity",
            }

        return False, {
            "error": "Streamlink command failed",
        }
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON output from Streamlink: {e}")
        return False, {
            "error": f"JSON parse error: {e}",
        }
    except Exception:
        logger.error("Unexpected error getting stream info")
        return False, {"error": "Unexpected error getting stream info"}


def get_streamlink_command(
    streamer_name: str,
    quality: str,
    output_path: str,
    proxy_settings: Optional[Dict[str, str]] = None,
    force_mode: bool = False,
    log_path: Optional[str] = None,
    supported_codecs: Optional[str] = None,
    oauth_token: Optional[str] = None,
    anonymous: bool = False,
) -> List[str]:
    """
    Generate a Streamlink command for recording a stream.

    This creates a robust Streamlink command following the approach used in lsdvr (TypeScript),
    with all parameters tuned for maximum stability and quality.

    Authentication stays process-local. Explicit anonymous mode uses a separate
    credential-free config and forces H.264.

    Args:
        streamer_name: The streamer's username
        quality: Quality setting for the stream (e.g. "best", "720p")
        output_path: Full path where the recording should be saved
        proxy_settings: Optional dictionary containing "http" and/or "https" proxy URLs
        force_mode: Use more aggressive settings for difficult connections
        log_path: Custom path for streamlink logs (if None, will use default location)
        supported_codecs: Comma-separated list of codecs (e.g. "h264,h265") - Streamlink 8.0.0+
        oauth_token: Twitch OAuth token (auto-refreshed by TwitchTokenService).
                     Enables: H.265/AV1 codecs, 1440p quality, ad-free (Turbo)
        anonymous: Omit authentication and force H.264.

    Returns:
        List of command arguments for streamlink
    """
    # Make sure we use .ts as intermediate format for better recovery
    if output_path.endswith(".mp4"):
        ts_output_path = output_path.replace(".mp4", ".ts")
    else:
        ts_output_path = output_path

    # Core streamlink command
    # Note: Most options are in /app/config/streamlink/config.twitch (auto-generated)
    # We MUST specify --config to load our custom config location
    cmd = [
        "streamlink",
        "--config",
        "/app/config/streamlink/config.twitch-anonymous"
        if anonymous
        else "/app/config/streamlink/config.twitch",
        f"twitch.tv/{streamer_name}",
        quality,
        "-o",
        ts_output_path,
    ]

    # Note: Static settings are in config.twitch; process-specific values stay on CLI:
    # - --twitch-supported-codecs (codec preferences from database)
    # - --twitch-api-header (OAuth token - auto-refreshed before recording)
    # - --http-proxy / --https-proxy (proxy settings from database)

    effective_codecs = "h264" if anonymous else supported_codecs
    if effective_codecs and effective_codecs.strip():
        # Use single argument with = for consistency (though codecs have no spaces)
        cmd.append(f"--twitch-supported-codecs={effective_codecs.strip()}")
        logger.debug(f"🎨 Overriding codec preference: {effective_codecs}")

    # CRITICAL: Always use per-recording OAuth token if provided
    # This ensures the token is fresh
    # (TwitchTokenService auto-refreshes before each recording)
    # Per-recording tokens override config.twitch to prevent stale tokens
    if not anonymous and oauth_token and oauth_token.strip():
        # Use single argument with = to prevent Streamlink from
        # parsing it as multiple args
        # CORRECT:   --twitch-api-header=Authorization=OAuth token
        # INCORRECT: --twitch-api-header Authorization=OAuth token
        token_header = f"Authorization=OAuth {oauth_token.strip()}"
        cmd.append(f"--twitch-api-header={token_header}")
        logger.debug("🔑 Using auto-refreshed OAuth token")
        logger.debug("   Enables: H.265/AV1, 1440p, ad-free (Turbo)")
    elif not anonymous:
        logger.warning("⚠️ No OAuth token - limited to 1080p H.264, ads may appear")

    # Add process-specific proxy settings if provided.
    if proxy_settings:
        cmd = _add_proxy_settings(cmd, proxy_settings, force_mode)

    return cmd


def _add_proxy_settings(
    cmd: List[str], proxy_settings: Dict[str, str], force_mode: bool
) -> List[str]:
    """
    Add proxy settings to the Streamlink command.

    Args:
        cmd: Existing command list to extend
        proxy_settings: Dictionary with "http" and/or "https" keys for proxy URLs
        force_mode: Whether to use more aggressive settings

    Returns:
        Updated command list with proxy settings
    """
    filtered_cmd = []
    skip_value = False
    for arg in cmd:
        if skip_value:
            skip_value = False
            continue
        if arg in ("--http-proxy", "--https-proxy"):
            skip_value = True
            continue
        if arg.startswith(("--http-proxy=", "--https-proxy=")):
            continue
        filtered_cmd.append(arg)
    cmd = filtered_cmd

    http_proxy = proxy_settings.get("http", "").strip()
    proxy_url = _select_proxy_url(proxy_settings)
    if not proxy_url:
        return cmd

    proxy_label = "HTTP" if http_proxy else "HTTPS"
    if not proxy_url.startswith(("http://", "https://")):
        error_msg = (
            f"{proxy_label} proxy URL must start with 'http://' or 'https://'. "
            f"Current value: {sanitize_proxy_url_for_logging(proxy_url)}"
        )
        logger.error(f"PROXY_VALIDATION_FAILED: {error_msg}")
        raise ValueError(error_msg)

    cmd.append(f"--http-proxy={proxy_url}")
    logger.debug(
        "Using %s proxy via --http-proxy: %s",
        proxy_label,
        sanitize_proxy_url_for_logging(proxy_url),
    )

    seg_timeout = "60" if not force_mode else "90"
    stream_timeout = "300" if not force_mode else "360"
    helper_options = (
        ("--stream-segment-timeout", seg_timeout),
        ("--stream-timeout", stream_timeout),
        ("--stream-segmented-queue-deadline", "8"),
        ("--stream-segment-attempts", "5"),
        ("--ringbuffer-size", "512M"),
        ("--hls-segment-stream-data", None),
        ("--hls-playlist-reload-time", "segment"),
    )
    for option, value in helper_options:
        if any(arg == option or arg.startswith(f"{option}=") for arg in cmd):
            continue
        cmd.append(option)
        if value is not None:
            cmd.append(value)

    return cmd


def get_proxy_settings_from_db() -> Dict[str, str]:
    """
    Get proxy settings from the database.

    Returns:
        Dictionary with http and https proxy settings
    """
    from app.database import SessionLocal

    proxy_settings = {}

    with SessionLocal() as db:
        global_settings = db.query(GlobalSettings).first()
        if global_settings:
            if global_settings.http_proxy and global_settings.http_proxy.strip():
                proxy_settings["http"] = global_settings.http_proxy.strip()
            if global_settings.https_proxy and global_settings.https_proxy.strip():
                proxy_settings["https"] = global_settings.https_proxy.strip()

    return proxy_settings


def _validate_twitch_video_id(video_id: str) -> str:
    normalized = video_id.strip()
    if not normalized.isdigit():
        raise ValueError("Twitch video ID must contain digits only")

    return normalized


def _validate_twitch_clip_url(clip_url: str) -> str:
    parsed = urlparse(clip_url.strip())
    host = parsed.netloc.lower()
    allowed_hosts = {"clips.twitch.tv", "www.twitch.tv", "twitch.tv"}

    if (
        parsed.scheme != "https"
        or host not in allowed_hosts
        or not parsed.path.strip("/")
    ):
        raise ValueError("Clip URL must be an https Twitch clip URL")

    return parsed.geturl()


def get_streamlink_vod_command(
    video_id: str,
    quality: str,
    output_path: str,
    proxy_settings: Optional[Dict[str, str]] = None,
    force_mode: bool = False,
) -> List[str]:
    """
    Generate a Streamlink command for downloading a VOD.

    Args:
        video_id: The Twitch VOD ID
        quality: Quality setting for the stream (e.g. "best", "720p")
        output_path: Full path where the VOD should be saved
        proxy_settings: Optional dictionary containing "http" and/or "https" proxy URLs
        force_mode: Use more aggressive settings

    Returns:
        List of command arguments for streamlink
    """
    # Find ffmpeg binary path (use env var or default)
    normalized_video_id = _validate_twitch_video_id(video_id)
    ffmpeg_bin: str = os.environ.get("FFMPEG_PATH") or "ffmpeg"

    # Core command for VOD download
    cmd = [
        "streamlink",
        "--ffmpeg-ffmpeg",
        ffmpeg_bin,
        "-o",
        output_path,
        "--stream-segment-threads",
        "10",
        "--url",
        f"https://www.twitch.tv/videos/{normalized_video_id}",
        "--default-stream",
        quality,
    ]

    cmd.extend(["--loglevel", "debug"])

    # Add proxy settings if provided
    if proxy_settings:
        cmd = _add_proxy_settings(cmd, proxy_settings, force_mode)

    return cmd


def get_streamlink_clip_command(
    clip_url: str,
    quality: str,
    output_path: str,
    proxy_settings: Optional[Dict[str, str]] = None,
) -> List[str]:
    """
    Generate a Streamlink command for downloading a Twitch clip.

    Args:
        clip_url: URL to the Twitch clip
        quality: Quality setting (e.g. "best", "720p")
        output_path: Path where the clip should be saved
        proxy_settings: Optional dictionary containing proxy settings

    Returns:
        List of command arguments for streamlink
    """
    # Find ffmpeg binary path (use env var or default)
    normalized_clip_url = _validate_twitch_clip_url(clip_url)
    ffmpeg_bin: str = os.environ.get("FFMPEG_PATH") or "ffmpeg"

    # Core command for clip download
    cmd = [
        "streamlink",
        "--ffmpeg-ffmpeg",
        ffmpeg_bin,
        "-o",
        output_path,
        "--stream-segment-threads",
        "10",
        "--url",
        normalized_clip_url,
        "--default-stream",
        quality,
    ]

    cmd.extend(["--loglevel", "debug"])

    # Add proxy settings if provided
    if proxy_settings:
        cmd = _add_proxy_settings(cmd, proxy_settings, False)

    return cmd
