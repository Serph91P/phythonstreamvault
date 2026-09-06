import importlib.util
from pathlib import Path
import asyncio

import pytest
from datetime import datetime, timedelta
from types import SimpleNamespace
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import GlobalSettings
from app.routes.twitch_auth import _build_connection_status

_module_path = (
    Path(__file__).resolve().parents[1] / "app/services/system/twitch_token_service.py"
)
_spec = importlib.util.spec_from_file_location(
    "twitch_token_service_under_test", _module_path
)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
TwitchTokenService = _module.TwitchTokenService
RecordingStoredTokenVersion = _module.RecordingStoredTokenVersion


class _Query:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class _Db:
    def __init__(self, settings):
        self.settings = settings
        self.committed = False
        self.rolled_back = False

    def query(self, _model):
        return _Query(self.settings)

    def refresh(self, _obj):
        return None

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class _Encryption:
    def encrypt(self, value):
        return f"enc:{value}"

    def decrypt(self, value):
        return value.removeprefix("enc:")


@pytest.mark.asyncio
async def test_manual_database_token_preferred_over_environment_token(monkeypatch):
    stored = SimpleNamespace(
        twitch_access_token="enc:db-token",
        twitch_refresh_token=None,
        twitch_token_expires_at=datetime.utcnow() + timedelta(hours=2),
    )
    service = TwitchTokenService.__new__(TwitchTokenService)
    service.db = _Db(stored)
    service.settings = SimpleNamespace(TWITCH_OAUTH_TOKEN="env-token")
    service.encryption = _Encryption()

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service, "_send_expiry_notification_if_needed", _noop)

    assert await service.get_valid_access_token() == "db-token"


@pytest.mark.asyncio
async def test_environment_token_used_as_fallback_when_database_token_expired(
    monkeypatch,
):
    stored = SimpleNamespace(
        twitch_access_token="enc:expired-token",
        twitch_refresh_token=None,
        twitch_token_expires_at=datetime.utcnow() - timedelta(minutes=1),
    )
    service = TwitchTokenService.__new__(TwitchTokenService)
    service.db = _Db(stored)
    service.settings = SimpleNamespace(TWITCH_OAUTH_TOKEN="env-token")
    service.encryption = _Encryption()

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service, "_send_expiry_notification_if_needed", _noop)

    assert await service.get_valid_access_token() == "env-token"


@pytest.mark.asyncio
async def test_environment_token_preferred_over_refreshable_oauth_token(monkeypatch):
    stored = SimpleNamespace(
        twitch_access_token="enc:app-oauth-token",
        twitch_refresh_token="enc:refresh-token",
        twitch_token_expires_at=datetime.utcnow() + timedelta(hours=2),
    )
    service = TwitchTokenService.__new__(TwitchTokenService)
    service.db = _Db(stored)
    service.settings = SimpleNamespace(TWITCH_OAUTH_TOKEN="env-browser-token")
    service.encryption = _Encryption()

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service, "_send_expiry_notification_if_needed", _noop)

    assert await service.get_valid_access_token() == "env-browser-token"


@pytest.mark.asyncio
async def test_store_manual_access_token_encrypts_and_clears_refresh_token(monkeypatch):
    stored = SimpleNamespace(
        twitch_access_token=None,
        twitch_refresh_token="enc:old-refresh",
        twitch_token_expires_at=None,
    )
    service = TwitchTokenService.__new__(TwitchTokenService)
    service.db = _Db(stored)
    service.settings = SimpleNamespace(TWITCH_OAUTH_TOKEN=None)
    service.encryption = _Encryption()

    async def _validate(token):
        assert token == "manual-token"
        return {"expires_in": 3600}

    monkeypatch.setattr(service, "validate_token", _validate)

    success, validation = await service.store_manual_access_token("OAuth manual-token")

    assert success is True
    assert validation == {"expires_in": 3600}
    assert stored.twitch_access_token == "enc:manual-token"
    assert stored.twitch_refresh_token is None
    assert stored.twitch_token_expires_at is not None
    assert service.settings.TWITCH_OAUTH_TOKEN is None
    assert service.db.committed is True


@pytest.mark.asyncio
async def test_store_manual_access_token_uses_default_expiry_when_expires_in_missing(
    monkeypatch,
):
    stored = SimpleNamespace(
        twitch_access_token=None,
        twitch_refresh_token="enc:old-refresh",
        twitch_token_expires_at=None,
    )
    service = TwitchTokenService.__new__(TwitchTokenService)
    service.db = _Db(stored)
    service.settings = SimpleNamespace(TWITCH_OAUTH_TOKEN=None)
    service.encryption = _Encryption()

    async def _validate(token):
        assert token == "manual-token"
        return {"client_id": "client", "login": "user"}

    monkeypatch.setattr(service, "validate_token", _validate)

    success, validation = await service.store_manual_access_token("manual-token")

    assert success is True
    assert validation == {"client_id": "client", "login": "user"}
    assert stored.twitch_access_token == "enc:manual-token"
    assert stored.twitch_refresh_token is None
    assert stored.twitch_token_expires_at > datetime.utcnow() + timedelta(days=59)
    assert service.db.committed is True


@pytest.mark.asyncio
async def test_store_manual_access_token_uses_default_expiry_when_expires_in_zero(
    monkeypatch,
):
    stored = SimpleNamespace(
        twitch_access_token=None,
        twitch_refresh_token="enc:old-refresh",
        twitch_token_expires_at=None,
    )
    service = TwitchTokenService.__new__(TwitchTokenService)
    service.db = _Db(stored)
    service.settings = SimpleNamespace(TWITCH_OAUTH_TOKEN=None)
    service.encryption = _Encryption()

    async def _validate(_token):
        return {"expires_in": 0}

    monkeypatch.setattr(service, "validate_token", _validate)

    success, validation = await service.store_manual_access_token("manual-token")

    assert success is True
    assert validation == {"expires_in": 0}
    assert stored.twitch_token_expires_at > datetime.utcnow() + timedelta(days=59)
    assert service.db.committed is True


@pytest.mark.asyncio
async def test_revalidate_stored_manual_token_repairs_stale_expiry(monkeypatch):
    stored = SimpleNamespace(
        twitch_access_token="enc:manual-token",
        twitch_refresh_token=None,
        twitch_token_expires_at=datetime.utcnow() - timedelta(days=1),
    )
    service = TwitchTokenService.__new__(TwitchTokenService)
    service.db = _Db(stored)
    service.settings = SimpleNamespace(TWITCH_OAUTH_TOKEN=None)
    service.encryption = _Encryption()

    async def _validate(token):
        assert token == "manual-token"
        return {"expires_in": 7200}

    monkeypatch.setattr(service, "validate_token", _validate)

    assert await service.revalidate_stored_manual_token(stored) is True
    assert stored.twitch_token_expires_at > datetime.utcnow() + timedelta(hours=1)
    assert service.db.committed is True


@pytest.mark.asyncio
async def test_revalidate_stored_manual_token_uses_default_expiry_when_expires_in_missing(
    monkeypatch,
):
    stored = SimpleNamespace(
        twitch_access_token="enc:manual-token",
        twitch_refresh_token=None,
        twitch_token_expires_at=datetime.utcnow() - timedelta(days=1),
    )
    service = TwitchTokenService.__new__(TwitchTokenService)
    service.db = _Db(stored)
    service.settings = SimpleNamespace(TWITCH_OAUTH_TOKEN=None)
    service.encryption = _Encryption()

    async def _validate(token):
        assert token == "manual-token"
        return {"client_id": "client", "login": "user"}

    monkeypatch.setattr(service, "validate_token", _validate)

    assert await service.revalidate_stored_manual_token(stored) is True
    assert stored.twitch_token_expires_at > datetime.utcnow() + timedelta(days=59)
    assert service.db.committed is True


@pytest.mark.asyncio
async def test_revalidate_stored_manual_token_keeps_stale_expiry_when_invalid(
    monkeypatch,
):
    stale_expiry = datetime.utcnow() - timedelta(days=1)
    stored = SimpleNamespace(
        twitch_access_token="enc:manual-token",
        twitch_refresh_token=None,
        twitch_token_expires_at=stale_expiry,
    )
    service = TwitchTokenService.__new__(TwitchTokenService)
    service.db = _Db(stored)
    service.settings = SimpleNamespace(TWITCH_OAUTH_TOKEN=None)
    service.encryption = _Encryption()

    async def _validate(_token):
        return None

    monkeypatch.setattr(service, "validate_token", _validate)

    assert await service.revalidate_stored_manual_token(stored) is False
    assert stored.twitch_token_expires_at == stale_expiry
    assert service.db.committed is False


def _recording_service(stored, env_token=None):
    service = TwitchTokenService.__new__(TwitchTokenService)
    service.db = _Db(stored)
    service.settings = SimpleNamespace(TWITCH_OAUTH_TOKEN=env_token)
    service.encryption = _Encryption()
    return service


@pytest.mark.asyncio
async def test_recording_token_live_validation_accepts_well_formed_200(monkeypatch):
    stored = SimpleNamespace(
        id=1,
        twitch_access_token="enc:manual-secret",
        twitch_refresh_token=None,
        twitch_token_expires_at=datetime.utcnow() + timedelta(days=1),
    )
    service = _recording_service(stored)

    async def _valid(_token):
        return "valid"

    monkeypatch.setattr(service, "_validate_recording_token", _valid, raising=False)

    resolution = await service.resolve_recording_token()

    assert resolution.token == "manual-secret"
    assert resolution.source == "database_manual"
    assert resolution.live_valid is True
    assert resolution.definitive_invalid is False
    assert "manual-secret" not in repr(resolution)
    assert "enc:manual-secret" not in repr(resolution)


@pytest.mark.asyncio
async def test_recording_token_definitive_invalidity_expires_same_stored_version(
    monkeypatch,
):
    original_expiry = datetime.utcnow() + timedelta(days=1)
    stored = SimpleNamespace(
        id=1,
        twitch_access_token="enc:revoked-secret",
        twitch_refresh_token=None,
        twitch_token_expires_at=original_expiry,
    )
    service = _recording_service(stored)
    invalidated = []

    async def _invalid(_token):
        return "invalid"

    def _invalidate(version):
        invalidated.append(version)
        stored.twitch_token_expires_at = datetime.utcnow() - timedelta(seconds=1)
        return True

    monkeypatch.setattr(service, "_validate_recording_token", _invalid, raising=False)
    monkeypatch.setattr(
        service, "invalidate_recording_token", _invalidate, raising=False
    )

    resolution = await service.resolve_recording_token()

    assert resolution.token is None
    assert resolution.source == "database_manual"
    assert resolution.definitive_invalid is True
    assert len(invalidated) == 1
    assert invalidated[0].encrypted_token == "enc:revoked-secret"
    assert invalidated[0].expires_at == original_expiry
    assert stored.twitch_access_token == "enc:revoked-secret"
    assert stored.twitch_token_expires_at < datetime.utcnow()


@pytest.mark.asyncio
async def test_recording_token_transient_validation_uses_anonymous_without_mutation(
    monkeypatch,
):
    original_expiry = datetime.utcnow() + timedelta(days=1)
    stored = SimpleNamespace(
        id=1,
        twitch_access_token="enc:temporarily-unverifiable",
        twitch_refresh_token=None,
        twitch_token_expires_at=original_expiry,
    )
    service = _recording_service(stored)

    async def _transient(_token):
        return "transient"

    monkeypatch.setattr(service, "_validate_recording_token", _transient, raising=False)

    resolution = await service.resolve_recording_token()

    assert resolution.token is None
    assert resolution.source == "database_manual"
    assert resolution.live_valid is False
    assert resolution.definitive_invalid is False
    assert resolution.reason == "validation_transient"
    assert stored.twitch_access_token == "enc:temporarily-unverifiable"
    assert stored.twitch_token_expires_at == original_expiry
    assert service.db.committed is False


@pytest.mark.asyncio
async def test_recording_token_resolution_timeout_is_transient_and_bounded(monkeypatch):
    stored = SimpleNamespace(
        id=1,
        twitch_access_token="enc:slow-secret",
        twitch_refresh_token=None,
        twitch_token_expires_at=datetime.utcnow() + timedelta(days=1),
    )
    service = _recording_service(stored)
    service.RECORDING_VALIDATION_TIMEOUT_SECONDS = 0.01

    async def _never_returns(_token):
        await asyncio.Event().wait()

    monkeypatch.setattr(
        service, "_validate_recording_token", _never_returns, raising=False
    )

    resolution = await asyncio.wait_for(service.resolve_recording_token(), timeout=0.2)

    assert resolution.token is None
    assert resolution.reason == "validation_transient"
    assert resolution.definitive_invalid is False
    assert service.db.committed is False


@pytest.mark.asyncio
async def test_recording_oauth_refresh_uses_refresh_lock(monkeypatch):
    stored = SimpleNamespace(
        id=1,
        twitch_access_token="enc:expired-oauth",
        twitch_refresh_token="enc:refresh-token",
        twitch_token_expires_at=datetime.utcnow() - timedelta(minutes=1),
    )
    service = _recording_service(stored)
    lock_events = []

    class _Lock:
        async def __aenter__(self):
            lock_events.append("entered")

        async def __aexit__(self, *_args):
            lock_events.append("exited")

    async def _refresh(settings):
        settings.twitch_access_token = "enc:fresh-oauth"
        settings.twitch_token_expires_at = datetime.utcnow() + timedelta(hours=2)
        return "fresh-oauth"

    async def _valid(_token):
        return "valid"

    service._refresh_lock = _Lock()
    monkeypatch.setattr(service, "_refresh_access_token", _refresh)
    monkeypatch.setattr(service, "_validate_recording_token", _valid)

    resolution = await service.resolve_recording_token()

    assert resolution.token == "fresh-oauth"
    assert resolution.source == "oauth_refresh"
    assert lock_events == ["entered", "exited"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (200, {"client_id": "client", "expires_in": 3600}, "valid"),
        (200, {}, "transient"),
        (401, None, "invalid"),
        (403, None, "invalid"),
        (500, None, "transient"),
    ],
)
async def test_recording_live_validation_classifies_http_response(
    monkeypatch, status, payload, expected
):
    request = {}

    class _Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def json(self):
            return payload

    response = _Response()
    response.status = status

    class _Session:
        def __init__(self, *, timeout):
            request["timeout"] = timeout.total

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def get(self, url, **kwargs):
            request.update(url=url, **kwargs)
            return response

    monkeypatch.setattr(_module.aiohttp, "ClientSession", _Session)
    service = _recording_service(None)

    result = await service._validate_recording_token("never-log-this")

    assert result == expected
    assert request["timeout"] == 3.0
    assert request["allow_redirects"] is False
    assert request["url"] == service.VALIDATE_URL


def test_recording_token_invalidation_is_guarded_against_replacement(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'tokens.db'}", future=True)
    Base.metadata.create_all(engine, tables=[GlobalSettings.__table__])
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    old_expiry = datetime.utcnow() + timedelta(days=1)
    new_expiry = datetime.utcnow() + timedelta(days=2)

    with Session() as db:
        db.add(
            GlobalSettings(
                id=1,
                twitch_access_token="enc:old-token",
                twitch_refresh_token=None,
                twitch_token_expires_at=old_expiry,
            )
        )
        db.commit()
        version = RecordingStoredTokenVersion(
            settings_id=1,
            encrypted_token="enc:old-token",
            encrypted_refresh_token=None,
            expires_at=old_expiry,
        )
        stored = db.get(GlobalSettings, 1)
        stored.twitch_access_token = "enc:new-token"
        stored.twitch_token_expires_at = new_expiry
        db.commit()

        service = TwitchTokenService.__new__(TwitchTokenService)
        service.db = db
        assert service.invalidate_recording_token(version) is False

        db.expire_all()
        current = db.get(GlobalSettings, 1)
        assert current.twitch_access_token == "enc:new-token"
        assert current.twitch_token_expires_at == new_expiry

    engine.dispose()


def test_definitive_manual_token_invalidation_makes_status_invalid(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'status.db'}", future=True)
    Base.metadata.create_all(engine, tables=[GlobalSettings.__table__])
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    expiry = datetime.utcnow() + timedelta(days=1)

    with Session() as db:
        db.add(
            GlobalSettings(
                id=1,
                twitch_access_token="enc:revoked-token",
                twitch_refresh_token=None,
                twitch_token_expires_at=expiry,
            )
        )
        db.commit()
        service = TwitchTokenService.__new__(TwitchTokenService)
        service.db = db
        version = RecordingStoredTokenVersion(
            settings_id=1,
            encrypted_token="enc:revoked-token",
            encrypted_refresh_token=None,
            expires_at=expiry,
        )

        assert service.invalidate_recording_token(version) is True
        db.expire_all()
        status = _build_connection_status(db.get(GlobalSettings, 1), None)
        assert status["connected"] is True
        assert status["valid"] is False
        assert status["source"] == "database_manual"

        async def _must_not_revalidate(_token):
            raise AssertionError("runtime-invalidated token must stay invalid")

        service.encryption = _Encryption()
        service.validate_token = _must_not_revalidate
        assert (
            service.RECORDING_INVALIDATED_TOKEN_EXPIRY
            == db.get(GlobalSettings, 1).twitch_token_expires_at
        )

        assert (
            asyncio.run(
                service.revalidate_stored_manual_token(db.get(GlobalSettings, 1))
            )
            is False
        )

    engine.dispose()
