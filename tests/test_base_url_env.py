"""Comfy Cloud by default; ``COMFY_BASE_URL`` is the only way to change target."""

from __future__ import annotations

import pytest

from comfy_sdk import BASE_URL_ENV_VAR, COMFY_CLOUD_BASE_URL, AsyncComfy, Comfy

CLOUD_JOB_URL = COMFY_CLOUD_BASE_URL + "/api/v2/jobs/j1"
LOCAL = "http://127.0.0.1:8189"


def job_url(client: Comfy | AsyncComfy) -> str:
    return client._low._p.url("/jobs/j1")


def test_constant_points_at_comfy_cloud() -> None:
    assert COMFY_CLOUD_BASE_URL == "https://cloud.comfy.org"


def test_env_var_name() -> None:
    assert BASE_URL_ENV_VAR == "COMFY_BASE_URL"


def test_defaults_to_comfy_cloud() -> None:
    with Comfy(api_key="comfyui-test") as client:
        assert job_url(client) == CLOUD_JOB_URL


def test_env_var_selects_the_deployment(monkeypatch) -> None:
    monkeypatch.setenv(BASE_URL_ENV_VAR, LOCAL)
    with Comfy() as client:
        assert job_url(client) == LOCAL + "/api/v2/jobs/j1"


def test_env_var_is_read_per_construction(monkeypatch) -> None:
    """A later client picks up a changed target — the value is not import-time."""
    with Comfy() as first:
        assert job_url(first) == CLOUD_JOB_URL
    monkeypatch.setenv(BASE_URL_ENV_VAR, LOCAL)
    with Comfy() as second:
        assert job_url(second) == LOCAL + "/api/v2/jobs/j1"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_env_var_means_comfy_cloud(monkeypatch, blank: str) -> None:
    monkeypatch.setenv(BASE_URL_ENV_VAR, blank)
    with Comfy() as client:
        assert job_url(client) == CLOUD_JOB_URL


def test_surrounding_whitespace_is_ignored(monkeypatch) -> None:
    monkeypatch.setenv(BASE_URL_ENV_VAR, f"  {LOCAL}  ")
    with Comfy() as client:
        assert job_url(client) == LOCAL + "/api/v2/jobs/j1"


@pytest.mark.parametrize(
    "bad",
    [
        "cloud.comfy.org",  # no scheme
        "ftp://cloud.comfy.org",  # not http(s)
        "file:///etc/passwd",
        "http://",  # no host
        "not a url",
    ],
)
def test_malformed_env_var_is_rejected(monkeypatch, bad: str) -> None:
    monkeypatch.setenv(BASE_URL_ENV_VAR, bad)
    with pytest.raises(ValueError, match=BASE_URL_ENV_VAR):
        Comfy()


def test_base_url_is_not_a_constructor_parameter() -> None:
    """The pre-env-var positional form must fail loudly, not read a URL as a key."""
    with pytest.raises(TypeError):
        Comfy(LOCAL)  # type: ignore[misc]


async def test_async_defaults_to_comfy_cloud() -> None:
    async with AsyncComfy(api_key="comfyui-test") as client:
        assert job_url(client) == CLOUD_JOB_URL


async def test_async_env_var_selects_the_deployment(monkeypatch) -> None:
    monkeypatch.setenv(BASE_URL_ENV_VAR, LOCAL)
    async with AsyncComfy() as client:
        assert job_url(client) == LOCAL + "/api/v2/jobs/j1"


async def test_async_rejects_positional_base_url() -> None:
    with pytest.raises(TypeError):
        AsyncComfy(LOCAL)  # type: ignore[misc]
