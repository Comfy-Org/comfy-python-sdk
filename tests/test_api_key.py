"""Credential resolution: explicit argument, then ``COMFY_API_KEY``, then a clear error.

Locks in the order the clients document, the *locality* of the failure (no
network call is attempted when nothing resolves), and the one property that is
easiest to lose by accident: the key never appears in a ``repr``, a ``str``, or
an exception message. That last one is the most common way a credential ends up
in someone's CI log, so it is asserted rather than assumed.

The self-hosted case stays keyless on purpose — a deployment named by
``COMFY_BASE_URL`` may have no auth at all, so an unresolved key is only an
error against Comfy Cloud (see ``test_auth_headers.py`` for the wire proof that
nothing is sent). ``conftest`` strips an ambient ``COMFY_API_KEY``, so every
case below sets exactly the environment it names.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest

from comfy_sdk import (
    API_KEY_ENV_VAR,
    BASE_URL_ENV_VAR,
    COMFY_CLOUD_BASE_URL,
    ROUTER_BASE_URL_ENV_VAR,
    AsyncComfy,
    Comfy,
    ComfyError,
    MissingApiKey,
)

EXPLICIT = "comfyui-explicit"
FROM_ENV = "comfyui-from-env"
LOCAL = "http://127.0.0.1:8189"

# Both clients resolve credentials identically, so every case runs against both.
CLIENTS = pytest.mark.parametrize("client_cls", [Comfy, AsyncComfy], ids=["sync", "async"])


@contextmanager
def constructed(client_cls: type, **kwargs: Any) -> Iterator[Any]:
    """Construct a client and always release its transport.

    Each client owns an httpx transport, so every one built here has to be
    closed. Only ``AsyncComfy`` needs a loop to close, and hiding that in one
    helper is what lets a case below stay a single parametrized test running
    against both clients instead of a sync copy and an async copy.
    """
    client = client_cls(**kwargs)
    try:
        yield client
    finally:
        if isinstance(client, AsyncComfy):
            asyncio.run(client.aclose())
        else:
            client.close()


def auth_header(client: Comfy | AsyncComfy) -> str | None:
    """The ``Authorization`` header this client would put on a same-origin request."""
    prepared = client._low._p
    return prepared.headers(prepared.url("/jobs")).get("Authorization")


def test_env_var_name() -> None:
    assert API_KEY_ENV_VAR == "COMFY_API_KEY"


# --- the table: explicit / env / neither / both, against the Cloud default ---


@CLIENTS
@pytest.mark.parametrize(
    "explicit, env, expected",
    [
        pytest.param(EXPLICIT, None, f"Bearer {EXPLICIT}", id="explicit-only"),
        pytest.param(None, FROM_ENV, f"Bearer {FROM_ENV}", id="env-only"),
        pytest.param(None, None, None, id="neither-raises"),
        pytest.param(EXPLICIT, FROM_ENV, f"Bearer {EXPLICIT}", id="both-explicit-wins"),
    ],
)
def test_key_resolution_order(monkeypatch, client_cls, explicit, env, expected) -> None:
    if env is not None:
        monkeypatch.setenv(API_KEY_ENV_VAR, env)
    if expected is None:
        with pytest.raises(MissingApiKey):
            client_cls(api_key=explicit)
        return
    with constructed(client_cls, api_key=explicit) as client:
        assert auth_header(client) == expected


# --- the failure is local, clear, and named -------------------------------


@CLIENTS
def test_missing_key_names_the_environment_variable(client_cls) -> None:
    """The message alone has to be enough to fix it."""
    with pytest.raises(MissingApiKey) as exc:
        client_cls()
    assert API_KEY_ENV_VAR in str(exc.value)
    assert "api_key" in str(exc.value)
    # The escape hatch for a deployment that needs no key is named too.
    assert BASE_URL_ENV_VAR in str(exc.value)


@CLIENTS
def test_missing_key_raises_before_any_network_call(monkeypatch, client_cls) -> None:
    """Construction must fail locally — not as a 401 the server has to tell us about.

    Both httpx send paths are booby-trapped, so any request the client tried to
    make would surface as the ``AssertionError`` below instead of the expected
    ``MissingApiKey``. Failing early is the whole point of the check: a missing
    credential reported as a server 401 sends the caller looking at their key's
    validity rather than its absence, and costs a round trip to say so.
    """

    def _no_network(*args: object, **kw: object) -> None:
        raise AssertionError("the client attempted a request before resolving credentials")

    monkeypatch.setattr(httpx.Client, "send", _no_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", _no_network)
    with pytest.raises(MissingApiKey):
        client_cls()


@CLIENTS
def test_missing_key_is_a_comfy_error(client_cls) -> None:
    """Catchable by an integrator who catches the SDK base error, with a code."""
    with pytest.raises(ComfyError) as exc:
        client_cls()
    assert isinstance(exc.value, MissingApiKey)
    assert exc.value.code == "missing_api_key"
    # It never reached a server, so there is no HTTP status to report.
    assert exc.value.http_status is None


# --- the key never leaks ---------------------------------------------------


@CLIENTS
def test_key_is_never_in_repr_or_str(client_cls) -> None:
    """The most common way a credential lands in a CI log."""
    with constructed(client_cls, api_key=EXPLICIT) as client:
        for rendered in (repr(client), str(client), repr(client._low), repr(client._low._p)):
            assert EXPLICIT not in rendered
        # ...and the repr still says something useful about the credential.
        assert "authenticated=True" in repr(client)
        assert client._low.authenticated is True


@CLIENTS
def test_key_from_the_environment_is_never_in_repr_or_str(monkeypatch, client_cls) -> None:
    """Same guarantee whichever source the key came from."""
    monkeypatch.setenv(API_KEY_ENV_VAR, FROM_ENV)
    with constructed(client_cls) as client:
        assert FROM_ENV not in repr(client)
        assert FROM_ENV not in str(client)
        assert FROM_ENV not in repr(client._low._p)


@CLIENTS
def test_keyless_client_reports_unauthenticated(monkeypatch, client_cls) -> None:
    monkeypatch.setenv(BASE_URL_ENV_VAR, LOCAL)
    with constructed(client_cls) as client:
        assert "authenticated=False" in repr(client)
        assert client._low.authenticated is False


def test_key_is_never_in_a_rejected_request_error(server) -> None:
    """The path where an exception is raised *while the client holds* a key.

    ``MissingApiKey`` cannot leak one (it is raised because there is none), so
    the guard that matters is a server rejection — the moment an SDK error is
    most tempting to make "helpful" by quoting the credential that failed. The
    stub answers 401 to a request that did carry the bearer token.
    """
    server.state.job_error = (401, "unauthorized")
    with Comfy(api_key=EXPLICIT) as client:
        with pytest.raises(ComfyError) as exc:
            client.submit(client.workflows.from_json({"3": {"class_type": "K", "inputs": {}}}))
    assert server.state.last_auth_header == f"Bearer {EXPLICIT}"
    assert EXPLICIT not in str(exc.value)
    assert EXPLICIT not in repr(exc.value)


# --- normalization + the self-hosted carve-out ------------------------------


@CLIENTS
@pytest.mark.parametrize("raw", [f"  {FROM_ENV}  ", f"{FROM_ENV}\n"], ids=["spaces", "newline"])
def test_surrounding_whitespace_is_stripped(monkeypatch, client_cls, raw) -> None:
    """A key read out of a file keeps its trailing newline; it must not reach the header."""
    monkeypatch.setenv(API_KEY_ENV_VAR, raw)
    with constructed(client_cls) as client:
        assert auth_header(client) == f"Bearer {FROM_ENV}"


@CLIENTS
@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_counts_as_unset_at_either_source(monkeypatch, client_cls, blank) -> None:
    """``COMFY_API_KEY=`` in a shell profile is 'no key', not a key of no characters."""
    monkeypatch.setenv(API_KEY_ENV_VAR, blank)
    with pytest.raises(MissingApiKey):
        client_cls()
    # A blank explicit argument falls through to a usable environment key.
    monkeypatch.setenv(API_KEY_ENV_VAR, FROM_ENV)
    with constructed(client_cls, api_key=blank) as client:
        assert auth_header(client) == f"Bearer {FROM_ENV}"


@CLIENTS
def test_self_hosted_deployment_still_needs_no_key(monkeypatch, client_cls) -> None:
    """The documented keyless surface is untouched: no error, and no credential."""
    monkeypatch.setenv(BASE_URL_ENV_VAR, LOCAL)
    with constructed(client_cls) as client:
        assert auth_header(client) is None


@CLIENTS
def test_environment_key_is_used_against_a_named_deployment(monkeypatch, client_cls) -> None:
    """The fallback is not Cloud-only — a serverless target picks it up too."""
    monkeypatch.setenv(BASE_URL_ENV_VAR, LOCAL)
    monkeypatch.setenv(API_KEY_ENV_VAR, FROM_ENV)
    with constructed(client_cls) as client:
        assert auth_header(client) == f"Bearer {FROM_ENV}"


@CLIENTS
def test_cloud_named_explicitly_still_requires_a_key(monkeypatch, client_cls) -> None:
    """Setting ``COMFY_BASE_URL`` to Cloud is the same surface, so the same rule."""
    monkeypatch.setenv(BASE_URL_ENV_VAR, COMFY_CLOUD_BASE_URL + "/")
    with pytest.raises(MissingApiKey):
        client_cls()


@CLIENTS
@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param(COMFY_CLOUD_BASE_URL + ":443", id="explicit-default-port"),
        pytest.param(COMFY_CLOUD_BASE_URL + ":443/", id="explicit-default-port-slash"),
        pytest.param("https://CLOUD.Comfy.ORG", id="mixed-case-host"),
    ],
)
def test_cloud_by_another_spelling_still_requires_a_key(monkeypatch, client_cls, spelling) -> None:
    """The rule follows the deployment, not the string that happens to name it.

    ``https://cloud.comfy.org:443`` is Comfy Cloud with its default port
    written out. Matching Cloud by string alone would miss it, hand back a
    keyless client, and turn this module's local error into a server 401 on the
    first request — exactly the failure the local check exists to prevent.
    """
    monkeypatch.setenv(BASE_URL_ENV_VAR, spelling)
    with pytest.raises(MissingApiKey):
        client_cls()


@CLIENTS
@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param("https://cloud.comfy.org/self-hosted", id="path-mounted"),
        pytest.param("https://cloud.comfy.org:8443", id="other-port"),
    ],
)
def test_a_different_deployment_on_the_same_host_stays_keyless(
    monkeypatch, client_cls, spelling
) -> None:
    """Recognizing more spellings of Cloud must not swallow the neighbours.

    A deployment mounted under a path on the same host — or reached on another
    port — is a different target, and the documented keyless carve-out still
    applies to it. This is the other half of the test above: the comparison has
    to be wide enough to catch Cloud and narrow enough to leave these alone.
    """
    monkeypatch.setenv(BASE_URL_ENV_VAR, spelling)
    with constructed(client_cls) as client:
        assert auth_header(client) is None


# --- a credential embedded in the base URL never leaks either --------------

PROXY_SECRET = "comfyui-proxy-secret"
PROXY_BASE_URL = f"https://proxy-user:{PROXY_SECRET}@proxy.example"


@CLIENTS
def test_base_url_userinfo_is_redacted_in_every_repr(monkeypatch, client_cls) -> None:
    """The other credential a client can hold: one embedded in its base URL.

    ``COMFY_BASE_URL=https://user:token@proxy.example`` is a valid way to reach
    a deployment behind an authenticating proxy, and a repr that prints it
    verbatim leaks it to the same CI logs and tracebacks the API key is kept
    out of. Only what is *rendered* is redacted — the transport still resolves
    requests against the URL it was given, so the proxy credential keeps
    working.

    Both targets carry the URL, because both can carry userinfo: a fronting
    proxy is as legitimate in front of Comfy Router as in front of the v2
    deployment, and ``repr(client.models)`` renders the *router* URL.
    """
    monkeypatch.setenv(BASE_URL_ENV_VAR, PROXY_BASE_URL)
    monkeypatch.setenv(ROUTER_BASE_URL_ENV_VAR, PROXY_BASE_URL)
    with constructed(client_cls) as client:
        for rendered in (
            repr(client),
            str(client),
            repr(client._low),
            repr(client._low._p),
            repr(client.models),
        ):
            assert PROXY_SECRET not in rendered
            assert "proxy-user" not in rendered
            assert "***@proxy.example" in rendered
        assert client._low.base_url == PROXY_BASE_URL
        assert client._low.router_base_url == PROXY_BASE_URL


@CLIENTS
def test_a_base_url_without_userinfo_is_rendered_unchanged(monkeypatch, client_cls) -> None:
    """Redaction stays invisible in the ordinary case — the target reads plainly."""
    monkeypatch.setenv(BASE_URL_ENV_VAR, LOCAL)
    monkeypatch.setenv(ROUTER_BASE_URL_ENV_VAR, LOCAL)
    with constructed(client_cls) as client:
        assert f"base_url={LOCAL!r}" in repr(client)
        assert f"base_url={LOCAL!r}" in repr(client._low)
        assert f"base_url={LOCAL!r}" in repr(client.models)


def test_environment_key_reaches_the_server(server, monkeypatch) -> None:
    """End-to-end: a key resolved from the environment is really sent on the wire."""
    server.state.require_auth = True
    monkeypatch.setenv(API_KEY_ENV_VAR, FROM_ENV)
    with Comfy() as client:
        job = client.submit(client.workflows.from_json({"3": {"class_type": "K", "inputs": {}}}))
        job.refresh()
    assert server.state.last_auth_header == f"Bearer {FROM_ENV}"
