"""One Honcho client per OAuth *grant*: refresh-token rotation must not churn the client cache.

Refresh tokens rotate on every exchange, so keying the client cache on them made managers bound an
hour apart evict each other's client, stranding cached SDK sessions on a dead bearer (every forced
refresh "succeeded" while retries still 401ed until restart).
"""

import json
from types import SimpleNamespace

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from plugins.memory.honcho import oauth
from plugins.memory.honcho import session as session_mod
from plugins.memory.honcho.client import HonchoClientConfig, get_honcho_client, reset_honcho_client
from plugins.memory.honcho.session import HonchoSessionManager

honcho = pytest.importorskip("honcho", reason="honcho SDK not installed")

TOKEN_ENDPOINT = "http://localhost:8000/oauth/token"


@pytest.fixture(autouse=True)
def _clean_client_cache():
    reset_honcho_client()
    yield
    reset_honcho_client()


@pytest.fixture
def fake_honcho(monkeypatch):
    class _FakeHoncho:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(honcho, "Honcho", _FakeHoncho)
    return _FakeHoncho


def _oauth_host(path, *, access="hch-at-old", refresh="hch-rt-old", grant_id=None, workspace=None):
    block = {"refreshToken": refresh, "expiresAt": 9_999_999_999, "clientId": "hermes-desktop",
             "tokenEndpoint": TOKEN_ENDPOINT, "scope": "write", "tokenType": "Bearer"}
    if grant_id:
        block["grantId"] = grant_id
    host = {"apiKey": access, "oauth": block}
    if workspace:
        host["workspace"] = workspace
    path.write_text(json.dumps({"hosts": {"hermes": host}}), encoding="utf-8")


def _oauth_block(path):
    return json.loads(path.read_text())["hosts"]["hermes"]["oauth"]


def _rotate(path, monkeypatch, n):
    monkeypatch.setattr(oauth, "_http_post_form_status", lambda url, data, timeout: (200, {
        "access_token": f"hch-at-r{n}", "refresh_token": f"hch-rt-r{n}", "expires_in": 3600}))
    assert oauth.force_refresh_token(path, "hermes") == f"hch-at-r{n}"
    return _oauth_block(path)


def _install(path, n):
    oauth.install_grant(
        path, "hermes",
        {"access_token": f"hch-at-login{n}", "refresh_token": f"hch-rt-login{n}", "expires_in": 3600},
        client_id="hermes-desktop", token_endpoint=TOKEN_ENDPOINT)
    return _oauth_block(path)


def test_rotation_keeps_grant_id_and_relogin_replaces_it(tmp_path, monkeypatch):
    path = tmp_path / "honcho.json"
    path.write_text("{}", encoding="utf-8")
    first_login = _install(path, 1)
    assert first_login["grantId"]

    rotated = [_rotate(path, monkeypatch, n) for n in (1, 2)]

    assert {r["grantId"] for r in rotated} == {first_login["grantId"]}
    assert rotated[0]["refreshToken"] != rotated[1]["refreshToken"]
    assert _install(path, 2)["grantId"] != first_login["grantId"]


def test_legacy_grant_adopts_one_id_on_first_rotation(tmp_path, monkeypatch):
    path = tmp_path / "honcho.json"
    _oauth_host(path)

    rotated = [_rotate(path, monkeypatch, n) for n in (1, 2)]

    assert rotated[0]["grantId"] and rotated[0]["grantId"] == rotated[1]["grantId"]


def test_refresh_token_rotation_keeps_one_client(tmp_path, fake_honcho):
    """A manager bound after an hourly rotation lands on the SAME client; a re-login gets a new one."""
    home = tmp_path / "p"
    home.mkdir()
    path = home / "honcho.json"

    token = set_hermes_home_override(home)
    try:
        _oauth_host(path, access="hch-at-1", refresh="hch-rt-1", grant_id="grant-a", workspace="w")
        client_early = get_honcho_client(HonchoClientConfig.from_global_config())
        _oauth_host(path, access="hch-at-2", refresh="hch-rt-2", grant_id="grant-a", workspace="w")
        client_late = get_honcho_client(HonchoClientConfig.from_global_config())
        _oauth_host(path, access="hch-at-3", refresh="hch-rt-3", grant_id="grant-b", workspace="w")
        client_relogin = get_honcho_client(HonchoClientConfig.from_global_config())
    finally:
        reset_hermes_home_override(token)

    assert client_early is client_late
    assert client_relogin is not client_early


def test_replaced_client_drops_sdk_objects_bound_to_the_old_one(monkeypatch):
    """A forced refresh rotates the bearer on the CURRENT client only; sessions cached on a rebuilt
    slot's old client would 401 forever, so they must be re-resolved on the live client."""
    class FakeClient:
        def __init__(self, name):
            self.name = name

        def session(self, session_id):
            return SimpleNamespace(id=session_id, client=self)

    live = {"client": FakeClient("old")}
    monkeypatch.setattr(session_mod, "get_honcho_client", lambda *a, **k: live["client"])

    mgr = HonchoSessionManager(config=HonchoClientConfig(host="hermes"))
    assert mgr._sdk_session("s1").client.name == "old"
    live["client"] = FakeClient("rebuilt")

    assert mgr.honcho.name == "rebuilt"
    assert mgr._sdk_session("s1").client.name == "rebuilt"
