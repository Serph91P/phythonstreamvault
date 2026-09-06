"""
Twitch OAuth Token Management Service

Handles automatic refresh of Twitch OAuth tokens to eliminate manual token copying.

Features:
- Automatic token refresh before expiration
- Secure storage of refresh tokens (encrypted)
- Fallback to environment variable (TWITCH_OAUTH_TOKEN)
- Compatible with Twitch Turbo (ad-free streams)

Flow:
1. User completes OAuth flow (one-time) → Store refresh_token
2. Before each recording → Check if access_token expired
3. If expired → Use refresh_token to get new access_token
4. Store new access_token and expiration timestamp
5. Recording uses fresh, valid token

Security:
- Refresh tokens encrypted like proxy passwords
- Access tokens never logged
- Tokens stored in database (persist across restarts)
"""

import logging
import asyncio
import aiohttp
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy.orm import Session
from app.models import GlobalSettings
from app.config.settings import get_settings
from app.utils.proxy_encryption import ProxyEncryption

logger = logging.getLogger("streamvault")


@dataclass(frozen=True)
class RecordingStoredTokenVersion:
    """Database token identity used for compare-and-set invalidation."""

    settings_id: Optional[int]
    encrypted_token: str = field(repr=False)
    encrypted_refresh_token: Optional[str] = field(repr=False)
    expires_at: Optional[datetime]


@dataclass(frozen=True)
class RecordingTokenResolution:
    """Result of resolving and live-validating recording authentication."""

    token: Optional[str] = field(repr=False)
    source: Optional[str]
    live_valid: bool
    definitive_invalid: bool
    reason: str
    stored_version: Optional[RecordingStoredTokenVersion] = field(
        default=None, repr=False
    )


class TwitchTokenService:
    """Service for managing Twitch OAuth token refresh"""

    # Twitch OAuth endpoints
    TOKEN_URL = "https://id.twitch.tv/oauth2/token"
    VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"

    # Token expiration buffer (refresh 1 hour before expiration)
    EXPIRATION_BUFFER_SECONDS = 3600

    # Notify one day before manual browser tokens expire.
    MANUAL_TOKEN_NOTIFICATION_BUFFER_SECONDS = 24 * 3600

    # Conservative fallback when Twitch validation confirms a manual browser
    # token but does not include a usable expires_in value.
    DEFAULT_MANUAL_TOKEN_EXPIRES_IN_SECONDS = 60 * 24 * 3600

    RECORDING_VALIDATION_TIMEOUT_SECONDS = 3.0
    RECORDING_INVALIDATED_TOKEN_EXPIRY = datetime(1970, 1, 1)

    # Global lock for token refresh (prevents duplicate refreshes)
    _refresh_lock = asyncio.Lock()
    _last_expiry_notification_at: Optional[datetime] = None

    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()
        self.encryption = ProxyEncryption()

    async def resolve_recording_token(self) -> RecordingTokenResolution:
        """Resolve one candidate and live-validate it before recording."""
        source = None
        try:
            async with asyncio.timeout(self.RECORDING_VALIDATION_TIMEOUT_SECONDS):
                global_settings = self.db.query(GlobalSettings).first()
                token = None
                stored_version = None

                if (
                    global_settings
                    and global_settings.twitch_access_token
                    and not global_settings.twitch_refresh_token
                    and self._is_token_valid(global_settings)
                ):
                    source = "database_manual"
                    token = self.encryption.decrypt(global_settings.twitch_access_token)
                    stored_version = self._stored_token_version(global_settings)
                elif self.settings.TWITCH_OAUTH_TOKEN:
                    source = "environment"
                    token = self.settings.TWITCH_OAUTH_TOKEN.strip()
                elif global_settings and global_settings.twitch_refresh_token:
                    if self._is_token_valid(global_settings):
                        source = "database_oauth"
                        if global_settings.twitch_access_token:
                            token = self.encryption.decrypt(
                                global_settings.twitch_access_token
                            )
                    else:
                        async with self._refresh_lock:
                            self.db.refresh(global_settings)
                            if self._is_token_valid(global_settings):
                                source = "database_oauth"
                                if global_settings.twitch_access_token:
                                    token = self.encryption.decrypt(
                                        global_settings.twitch_access_token
                                    )
                            else:
                                source = "oauth_refresh"
                                token = await self._refresh_access_token(
                                    global_settings
                                )
                    if token and global_settings.twitch_access_token:
                        stored_version = self._stored_token_version(global_settings)

                if not token:
                    return RecordingTokenResolution(
                        token=None,
                        source=source,
                        live_valid=False,
                        definitive_invalid=False,
                        reason="credential_unavailable",
                        stored_version=stored_version,
                    )

                validation = await self._validate_recording_token(token)
                if validation == "valid":
                    return RecordingTokenResolution(
                        token=token,
                        source=source,
                        live_valid=True,
                        definitive_invalid=False,
                        reason="validated",
                        stored_version=stored_version,
                    )
                if validation == "invalid":
                    if stored_version is not None:
                        self.invalidate_recording_token(stored_version)
                    return RecordingTokenResolution(
                        token=None,
                        source=source,
                        live_valid=False,
                        definitive_invalid=True,
                        reason="validation_invalid",
                        stored_version=stored_version,
                    )
                return RecordingTokenResolution(
                    token=None,
                    source=source,
                    live_valid=False,
                    definitive_invalid=False,
                    reason="validation_transient",
                    stored_version=stored_version,
                )
        except TimeoutError:
            logger.warning("Twitch recording token validation timed out")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Twitch recording token validation failed transiently")

        return RecordingTokenResolution(
            token=None,
            source=source,
            live_valid=False,
            definitive_invalid=False,
            reason="validation_transient",
        )

    def _stored_token_version(
        self, global_settings: GlobalSettings
    ) -> RecordingStoredTokenVersion:
        return RecordingStoredTokenVersion(
            settings_id=getattr(global_settings, "id", None),
            encrypted_token=global_settings.twitch_access_token,
            encrypted_refresh_token=global_settings.twitch_refresh_token,
            expires_at=global_settings.twitch_token_expires_at,
        )

    async def _validate_recording_token(self, access_token: str) -> str:
        """Classify one Twitch validation response without retries."""
        timeout = aiohttp.ClientTimeout(total=self.RECORDING_VALIDATION_TIMEOUT_SECONDS)
        headers = {"Authorization": f"OAuth {access_token}"}
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    self.VALIDATE_URL,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    if response.status in (401, 403):
                        return "invalid"
                    if response.status != 200:
                        return "transient"
                    payload = await response.json()
                    expires_in = (
                        payload.get("expires_in") if isinstance(payload, dict) else None
                    )
                    if (
                        not isinstance(payload, dict)
                        or not isinstance(payload.get("client_id"), str)
                        or not payload["client_id"].strip()
                        or not isinstance(expires_in, int)
                        or isinstance(expires_in, bool)
                        or expires_in < 0
                    ):
                        return "transient"
                    return "valid"
        except asyncio.CancelledError:
            raise
        except (TimeoutError, aiohttp.ClientError, ValueError, TypeError):
            return "transient"
        except Exception:
            return "transient"

    def invalidate_recording_token(self, version: RecordingStoredTokenVersion) -> bool:
        """Expire a DB token only when its credential and expiry are unchanged."""
        conditions = [
            GlobalSettings.twitch_access_token == version.encrypted_token,
            GlobalSettings.twitch_token_expires_at == version.expires_at,
            GlobalSettings.twitch_refresh_token == version.encrypted_refresh_token,
        ]
        if version.settings_id is not None:
            conditions.append(GlobalSettings.id == version.settings_id)

        try:
            updated = (
                self.db.query(GlobalSettings)
                .filter(*conditions)
                .update(
                    {
                        GlobalSettings.twitch_token_expires_at: self.RECORDING_INVALIDATED_TOKEN_EXPIRY
                    },
                    synchronize_session=False,
                )
            )
            if updated == 1:
                self.db.commit()
                logger.warning("Stored Twitch token was invalidated")
                return True
            self.db.rollback()
            logger.info("Skipped invalidating a concurrently replaced Twitch token")
            return False
        except Exception:
            self.db.rollback()
            logger.warning("Could not conditionally invalidate stored Twitch token")
            return False

    async def get_valid_access_token(self) -> Optional[str]:
        """
        Get a valid Twitch OAuth access token.

        Returns either:
        1. Encrypted browser token stored in the database (preferred)
        2. Browser token from environment variable (backward compatibility)
        3. Database token from OAuth flow (auto-refreshed when possible)
        4. None (if no token available)

        Tokens entered on the Twitch Connection page replace the legacy
        TWITCH_OAUTH_TOKEN environment variable. The environment variable is
        still used as a fallback for existing installations.

        Returns:
            Optional[str]: Valid access token or None
        """
        try:
            global_settings = self.db.query(GlobalSettings).first()

            # === PRIORITY 1: Encrypted manual browser token from database ===
            # Manual tokens saved from the Twitch Connection page clear the
            # refresh token. That lets us prefer the encrypted DB token over
            # the legacy env var without accidentally letting an older app
            # OAuth token override a browser token from TWITCH_OAUTH_TOKEN.
            if (
                global_settings
                and global_settings.twitch_access_token
                and not global_settings.twitch_refresh_token
            ):
                if self._is_token_valid(global_settings):
                    logger.debug("✅ Using encrypted Twitch OAuth token from database")
                    await self._send_expiry_notification_if_needed(global_settings)
                    return self.encryption.decrypt(global_settings.twitch_access_token)

                await self._send_expiry_notification_if_needed(
                    global_settings, expired=True
                )

            # === PRIORITY 2: Environment variable fallback/backward compatibility ===
            if self.settings.TWITCH_OAUTH_TOKEN:
                logger.debug(
                    "✅ Using browser token from environment variable fallback (TWITCH_OAUTH_TOKEN)"
                )
                return self.settings.TWITCH_OAUTH_TOKEN

            # === PRIORITY 3: Database refresh token from OAuth flow ===
            if global_settings and global_settings.twitch_refresh_token:
                # Token expired → Refresh it (with lock to prevent duplicate refreshes)
                async with self._refresh_lock:
                    # Double-check: Another task may have refreshed while we waited
                    self.db.refresh(global_settings)  # Reload from database
                    if self._is_token_valid(global_settings):
                        logger.debug("Token was refreshed by another task, using it")
                        if global_settings.twitch_access_token:
                            return self.encryption.decrypt(
                                global_settings.twitch_access_token
                            )

                    logger.info("Access token expired, refreshing...")
                    new_access_token = await self._refresh_access_token(global_settings)
                    if new_access_token:
                        return new_access_token

                    logger.warning(
                        "⚠️ Token refresh failed and no valid database token available"
                    )

            # === PRIORITY 4: No token available ===
            logger.warning(
                "❌ No Twitch OAuth token available. H.265/1440p quality unavailable."
            )
            logger.warning("   → Add a token in Settings → Twitch Connection")
            return None

        except Exception as e:
            logger.error(f"Error getting valid access token: {e}")
            # Last resort fallback to environment variable
            return self.settings.TWITCH_OAUTH_TOKEN

    def _is_token_valid(self, global_settings: GlobalSettings) -> bool:
        """
        Check if the stored access token is still valid (not expired).

        Args:
            global_settings: GlobalSettings object with token info

        Returns:
            bool: True if token is valid, False if expired or missing
        """
        if not global_settings.twitch_token_expires_at:
            return False

        # Add buffer time (refresh 1 hour before expiration)
        expires_with_buffer = global_settings.twitch_token_expires_at - timedelta(
            seconds=self.EXPIRATION_BUFFER_SECONDS
        )

        is_valid = datetime.utcnow() < expires_with_buffer

        if not is_valid:
            logger.debug(f"Token expired at {global_settings.twitch_token_expires_at}")

        return is_valid

    async def _refresh_access_token(
        self, global_settings: GlobalSettings
    ) -> Optional[str]:
        """
        Refresh the Twitch OAuth access token using the stored refresh token.

        Args:
            global_settings: GlobalSettings object with refresh token

        Returns:
            Optional[str]: New access token or None on failure
        """
        try:
            # Decrypt refresh token
            refresh_token = self.encryption.decrypt(
                global_settings.twitch_refresh_token
            )

            if not refresh_token:
                logger.error("Failed to decrypt refresh token")
                return None

            # === STEP 1: Request new access token from Twitch ===
            payload = {
                "client_id": self.settings.TWITCH_APP_ID,
                "client_secret": self.settings.TWITCH_APP_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(self.TOKEN_URL, data=payload) as response:
                    if response.status != 200:
                        logger.error("Token refresh failed (%s)", response.status)
                        return None

                    token_data = await response.json()

            # === STEP 2: Extract new tokens ===
            new_access_token = token_data.get("access_token")
            new_refresh_token = token_data.get(
                "refresh_token"
            )  # Twitch may rotate refresh token
            expires_in = token_data.get("expires_in", 14400)  # Default 4 hours

            if not new_access_token:
                logger.error("No access token in refresh response")
                return None

            # === STEP 3: Calculate expiration timestamp ===
            expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

            # === STEP 4: Update database ===
            # Store access token in database (encrypted)
            encrypted_access_token = self.encryption.encrypt(new_access_token)
            global_settings.twitch_access_token = encrypted_access_token
            global_settings.twitch_token_expires_at = expires_at

            # Update refresh token if Twitch rotated it
            if new_refresh_token and new_refresh_token != refresh_token:
                global_settings.twitch_refresh_token = self.encryption.encrypt(
                    new_refresh_token
                )
                logger.info("Refresh token rotated by Twitch")

            self.db.commit()

            logger.info(
                f"✅ Access token refreshed successfully (expires at {expires_at})"
            )

            # === STEP 5: Regenerate Streamlink config with new token ===
            try:
                from app.services.system.streamlink_config_service import (
                    streamlink_config_service,
                )

                config_updated = await streamlink_config_service.regenerate_config()
                if config_updated:
                    logger.info("🔄 Streamlink config updated with refreshed token")
                else:
                    logger.warning(
                        "⚠️ Failed to update Streamlink config after token refresh"
                    )
            except Exception as config_error:
                logger.error(
                    f"❌ Error updating Streamlink config after token refresh: {config_error}"
                )
                # Don't fail token refresh if config update fails

            return new_access_token

        except Exception:
            logger.error("Error refreshing access token")
            self.db.rollback()
            return None

    async def store_oauth_tokens(
        self, access_token: str, refresh_token: str, expires_in: int = 14400
    ) -> bool:
        """
        Store OAuth tokens received from Twitch OAuth callback.

        This is called after user completes OAuth flow (one-time setup).

        Args:
            access_token: Short-lived access token (4-6 hours)
            refresh_token: Long-lived refresh token (months/years)
            expires_in: Access token lifetime in seconds

        Returns:
            bool: True if stored successfully, False otherwise
        """
        try:
            global_settings = self.db.query(GlobalSettings).first()

            if not global_settings:
                logger.error("GlobalSettings not found, cannot store tokens")
                return False

            # Calculate expiration timestamp
            expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

            # Encrypt tokens
            encrypted_refresh_token = self.encryption.encrypt(refresh_token)
            encrypted_access_token = self.encryption.encrypt(access_token)

            # Store in database
            global_settings.twitch_refresh_token = encrypted_refresh_token
            global_settings.twitch_access_token = encrypted_access_token
            global_settings.twitch_token_expires_at = expires_at

            self.db.commit()

            logger.info(
                f"✅ OAuth tokens stored successfully (expires at {expires_at})"
            )
            logger.info("🎯 Automatic token refresh enabled!")

            # === Update Streamlink config with new token ===
            try:
                from app.services.system.streamlink_config_service import (
                    streamlink_config_service,
                )

                config_updated = await streamlink_config_service.regenerate_config()
                if config_updated:
                    logger.info("🔄 Streamlink config generated with new OAuth token")
                else:
                    logger.warning(
                        "⚠️ Failed to generate Streamlink config with new token"
                    )
            except Exception as config_error:
                logger.error(
                    f"❌ Error generating Streamlink config after OAuth setup: {config_error}"
                )
                # Don't fail OAuth setup if config generation fails

            return True

        except Exception as e:
            logger.error(f"Error storing OAuth tokens: {e}")
            self.db.rollback()
            return False

    async def store_manual_access_token(
        self, access_token: str
    ) -> tuple[bool, Optional[dict]]:
        """Validate and store a manually supplied browser OAuth token encrypted in DB."""
        clean_token = access_token.strip()
        if clean_token.lower().startswith("oauth:"):
            clean_token = clean_token.split(":", 1)[1].strip()
        if clean_token.lower().startswith("oauth "):
            clean_token = clean_token.split(" ", 1)[1].strip()

        if not clean_token:
            return False, None

        validation = await self.validate_token(clean_token)
        if not validation:
            return False, None

        try:
            global_settings = self.db.query(GlobalSettings).first()
            if not global_settings:
                logger.error("GlobalSettings not found, cannot store manual token")
                return False, validation

            expires_in = int(validation.get("expires_in") or 0)
            if expires_in <= 0:
                logger.warning(
                    "Twitch token validation did not include a usable expires_in; "
                    "using default manual token lifetime"
                )
                expires_in = self.DEFAULT_MANUAL_TOKEN_EXPIRES_IN_SECONDS

            expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

            global_settings.twitch_access_token = self.encryption.encrypt(clean_token)
            global_settings.twitch_token_expires_at = expires_at
            # Manual browser tokens have no refresh token. Clear any older OAuth
            # refresh token so status accurately reports manual-token behavior.
            global_settings.twitch_refresh_token = None
            self.db.commit()

            try:
                from app.services.system.streamlink_config_service import (
                    streamlink_config_service,
                )

                await streamlink_config_service.regenerate_config()
            except Exception as config_error:
                logger.error(
                    f"❌ Error updating Streamlink config after manual token save: {config_error}"
                )

            logger.info("✅ Manual Twitch OAuth token stored in database")
            return True, validation
        except Exception as e:
            logger.error(f"Error storing manual Twitch token: {e}")
            self.db.rollback()
            return False, validation

    async def revalidate_stored_manual_token(
        self, global_settings: GlobalSettings
    ) -> bool:
        """Live-validate a stored manual browser token and repair its expiry.

        Manual browser tokens do not have refresh tokens. Older installs may
        have a valid encrypted token with a missing or stale expiry timestamp,
        which makes the UI report "Browser Token Expired" even though Twitch
        still accepts the token. This method validates the decrypted token with
        Twitch and updates the stored expiry from the validation response.

        Returns True when Twitch confirms the token is currently valid.
        """
        if (
            not global_settings
            or not global_settings.twitch_access_token
            or global_settings.twitch_refresh_token
        ):
            return False
        if (
            global_settings.twitch_token_expires_at
            == self.RECORDING_INVALIDATED_TOKEN_EXPIRY
        ):
            return False

        try:
            access_token = self.encryption.decrypt(global_settings.twitch_access_token)
            if not access_token:
                return False

            validation = await self.validate_token(access_token)
            if not validation:
                return False

            expires_in = int(validation.get("expires_in") or 0)
            if expires_in <= 0:
                logger.warning(
                    "Twitch token revalidation did not include a usable expires_in; "
                    "using default manual token lifetime"
                )
                expires_in = self.DEFAULT_MANUAL_TOKEN_EXPIRES_IN_SECONDS

            global_settings.twitch_token_expires_at = datetime.utcnow() + timedelta(
                seconds=expires_in
            )
            self.db.commit()

            return True
        except Exception as e:
            logger.error(f"Error revalidating stored manual Twitch token: {e}")
            self.db.rollback()
            return False

    async def _send_expiry_notification_if_needed(
        self, global_settings: GlobalSettings, expired: bool = False
    ) -> None:
        """Send an Apprise notification when the stored token is expired or near expiry."""
        expires_at = global_settings.twitch_token_expires_at
        if not expires_at:
            return

        now = datetime.utcnow()
        if (
            self.__class__._last_expiry_notification_at
            and now - self.__class__._last_expiry_notification_at < timedelta(hours=12)
        ):
            return

        seconds_remaining = (expires_at - now).total_seconds()
        if (
            not expired
            and seconds_remaining > self.MANUAL_TOKEN_NOTIFICATION_BUFFER_SECONDS
        ):
            return

        try:
            from app.services.notification_service import NotificationService

            title = (
                "StreamVault Twitch OAuth token expired"
                if expired
                else "StreamVault Twitch OAuth token expires soon"
            )
            body = (
                "Your stored Twitch OAuth token has expired. Open Settings > Twitch Connection "
                "and save a fresh browser token to keep H.265/1440p recordings working."
                if expired
                else "Your stored Twitch OAuth token expires soon. Open Settings > Twitch Connection "
                "and save a fresh browser token to avoid recording quality fallback."
            )
            await NotificationService().send_notification(body, title)
            self.__class__._last_expiry_notification_at = now
        except Exception as e:
            logger.debug(f"Could not send Twitch token expiry notification: {e}")

    async def validate_token(self, access_token: str) -> Optional[dict]:
        """
        Validate an access token with Twitch API.

        Useful for checking token validity and getting user info.

        Args:
            access_token: Access token to validate

        Returns:
            Optional[dict]: Token validation response or None on failure
        """
        try:
            headers = {"Authorization": f"OAuth {access_token}"}

            async with aiohttp.ClientSession() as session:
                async with session.get(self.VALIDATE_URL, headers=headers) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        logger.warning(f"Token validation failed: {response.status}")
                        return None

        except Exception as e:
            logger.error(f"Error validating token: {e}")
            return None

    def clear_tokens(self) -> bool:
        """
        Clear stored OAuth tokens (user logout/disconnect).

        Returns:
            bool: True if cleared successfully
        """
        try:
            global_settings = self.db.query(GlobalSettings).first()

            if global_settings:
                global_settings.twitch_refresh_token = None
                global_settings.twitch_access_token = None
                global_settings.twitch_token_expires_at = None
                self.db.commit()

            # Clear environment variable
            self.settings.TWITCH_OAUTH_TOKEN = None

            logger.info("OAuth tokens cleared")
            return True

        except Exception as e:
            logger.error(f"Error clearing tokens: {e}")
            self.db.rollback()
            return False
