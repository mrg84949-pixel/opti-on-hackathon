from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from web.admin_auth import AdminAuth, resolve_admin_auth, resolve_org_id_from_auth


@pytest.mark.asyncio
async def test_resolve_admin_auth_returns_401_when_unauthorized():
    with patch("web.admin_auth.settings") as mock_settings:
        mock_settings.admin_api_token = "1234"
        with pytest.raises(HTTPException) as exc:
            await resolve_admin_auth(None, None)
        assert exc.value.status_code == 401
        assert exc.value.detail == "Unauthorized"


@pytest.mark.asyncio
async def test_resolve_admin_auth_returns_401_when_token_invalid():
    with patch("web.admin_auth.settings") as mock_settings:
        mock_settings.admin_api_token = "1234"
        with pytest.raises(HTTPException) as exc:
            await resolve_admin_auth("Bearer wrong", None)
        assert exc.value.status_code == 401
        assert exc.value.detail == "Unauthorized"


@pytest.mark.asyncio
async def test_resolve_admin_auth_passes_when_bearer_matches():
    with patch("web.admin_auth.settings") as mock_settings:
        mock_settings.admin_api_token = "1234"
        auth = await resolve_admin_auth("Bearer 1234", None)
    assert auth.mode == "token"
    assert auth.org_id is None
    assert auth.admin_id is None


@pytest.mark.asyncio
async def test_resolve_admin_auth_ignores_bearer_when_token_not_configured():
    with patch("web.admin_auth.settings") as mock_settings:
        mock_settings.admin_api_token = ""
        with pytest.raises(HTTPException) as exc:
            await resolve_admin_auth("Bearer 1234", None)
        assert exc.value.status_code == 401


def test_resolve_org_id_from_auth_prefers_session_org():
    auth = AdminAuth(mode="session", org_id=5, admin_id=1)
    assert resolve_org_id_from_auth(auth, x_org_id=7) == 5


def test_resolve_org_id_from_auth_uses_header_for_ops_token():
    auth = AdminAuth(mode="token", org_id=None, admin_id=None)
    assert resolve_org_id_from_auth(auth, x_org_id=7) == 7


def test_resolve_org_id_from_auth_falls_back_to_settings():
    auth = AdminAuth(mode="token", org_id=None, admin_id=None)
    with patch("web.admin_auth.settings") as mock_settings:
        mock_settings.default_org_id = 3
        assert resolve_org_id_from_auth(auth, x_org_id=None) == 3
