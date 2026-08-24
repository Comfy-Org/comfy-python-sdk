"""Sync/async public-surface parity, derived by introspection.

The README promises "swap the import and add ``await``" as the only difference
between :class:`~comfy_sdk.client.Comfy` and
:class:`~comfy_sdk.client.AsyncComfy`, and the awaitable form of an operation is
deliberately the *async client* rather than a suffixed method. Both only hold
while the two surfaces stay symmetric, and the failure mode is quiet: a method
lands on one client, the other silently lacks it, and nobody finds out until a
user does.

So this module walks the surface rather than listing it. A hand-maintained list
of pairs is the thing that goes stale first — it cannot fail for the class
somebody forgot to add to it. Pairs are discovered two ways:

* every public ``AsyncX`` class defined in ``comfy_sdk`` / ``comfy_low`` is
  paired with its ``X`` counterpart (a missing counterpart is itself a
  failure); and
* both clients are constructed and their public namespace attributes
  (``assets`` / ``workflows`` / ``jobs`` / ``models`` / whatever lands next) are
  paired by attribute name — which is what covers the namespaces rather than
  only the client root.

For each pair this compares public member *names*, whether each is reached as
a method or as a property, and the parameter names, order and kinds of the
methods they share. It also bans a spelling that encodes sync-vs-async anywhere
on the surface, and refuses to pass on an empty introspection result: a parity
test that discovered nothing would be green for the wrong reason.

Dunders (``__enter__``/``__aenter__``, ...) are excluded — they are a mechanical
consequence of sync vs async, not part of the call surface a swap has to match.
Deliberate asymmetries live in ``_RENAMES`` / ``_ALLOWED_ASYMMETRY`` below, each
with the reason it is deliberate; anything not listed there fails.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import pkgutil
import re
from typing import Any

import pytest

import comfy_low
import comfy_sdk
from comfy_sdk import AsyncComfy, Comfy

#: The packages whose public classes make up the surface under test.
_PACKAGES = (comfy_sdk, comfy_low)
_PACKAGE_NAMES = {p.__name__ for p in _PACKAGES}

#: Constructing a Cloud-targeted client requires a key. Nothing here makes a
#: request, so any non-empty string does. Instantiating is what exposes the
#: namespaces at all: they are assigned in ``__init__``, so no amount of
#: class-level introspection would find them.
_DUMMY_KEY = "comfyui-parity-test-key"

# --- deliberate asymmetries ---------------------------------------------
#
# These two tables are the only escape hatch. An asymmetry not written here
# fails, which is the point: an intentional exception stays visible, and an
# accidental one still breaks the build.

#: Async spellings of the same operation under a different name. asyncio's
#: convention is ``aclose()`` for an awaitable closer; ``close()`` cannot be
#: reused for it, because a name callers expect to be synchronous returning an
#: un-awaited coroutine fails silently. httpx and asyncio's own streams spell
#: it the same way.
_RENAMES = {"aclose": "close"}

#: ``(pair label, member name) -> why it legitimately exists on one side only.``
#: Empty today: once ``aclose`` is normalised, the surfaces are symmetric.
_ALLOWED_ASYMMETRY: dict[tuple[str, str], str] = {}

#: A name that encodes sync-vs-async instead of letting the client encode it.
#: ``run_async`` is the specific decision this guards — there is one ``run``,
#: and its awaitable form is ``AsyncComfy().models.run`` — and the pattern
#: generalises it, so a second name for one operation cannot land on review
#: oversight alone. A published method name cannot be withdrawn.
_SUFFIXED = re.compile(r"(^async_|_async$|_sync$|^a_)")


def _public_modules(package: Any) -> list[Any]:
    """``package`` plus every importable submodule of it whose path is public."""
    found = [package]
    for info in pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}."):
        if any(part.startswith("_") for part in info.name.split(".")):
            continue
        found.append(importlib.import_module(info.name))
    return found


def _public_classes() -> list[type]:
    """Public classes defined anywhere in the two packages, each listed once.

    Keyed on the defining module rather than the bare name: the wire models in
    ``comfy_low.models`` share names (``Asset``, ``Job``, ``Output``) with the
    hand-written SDK handles, and collapsing those together would pair a client
    handle against a pydantic model.
    """
    classes: dict[tuple[str, str], type] = {}
    for package in _PACKAGES:
        for module in _public_modules(package):
            for name, obj in vars(module).items():
                if name.startswith("_") or not inspect.isclass(obj):
                    continue
                if obj.__module__.split(".")[0] not in _PACKAGE_NAMES:
                    continue  # re-exported from elsewhere (httpx, pydantic, ...)
                classes[(obj.__module__, obj.__qualname__)] = obj
    return list(classes.values())


def _counterpart(async_cls: type, name: str, classes: list[type]) -> type:
    """The sync class ``name`` that ``async_cls`` mirrors, nearest definition first."""
    candidates = [cls for cls in classes if cls.__name__ == name]
    assert candidates, (
        f"{async_cls.__module__}.{async_cls.__name__} has no sync counterpart {name} — "
        "an async-only class means the two surfaces have diverged"
    )
    scopes = (
        lambda cls: cls.__module__ == async_cls.__module__,  # same module
        lambda cls: cls.__module__.split(".")[0] == async_cls.__module__.split(".")[0],
    )
    for in_scope in scopes:
        nearest = [cls for cls in candidates if in_scope(cls)]
        if len(nearest) == 1:
            return nearest[0]
        assert len(nearest) < 2, (
            f"{name} is defined more than once near {async_cls.__module__} "
            f"({[cls.__module__ for cls in nearest]}) — cannot tell which one "
            f"{async_cls.__name__} mirrors"
        )
    assert len(candidates) == 1, (
        f"{name} is ambiguous across the packages ({[cls.__module__ for cls in candidates]})"
    )
    return candidates[0]


def _name_convention_pairs() -> list[tuple[str, type, type]]:
    """``AsyncX`` paired with ``X``, for every public class in the two packages."""
    classes = _public_classes()
    pairs = []
    for cls in sorted(classes, key=lambda c: (c.__module__, c.__name__)):
        if not cls.__name__.startswith("Async"):
            continue
        sync = _counterpart(cls, cls.__name__[len("Async") :], classes)
        pairs.append((sync.__name__, sync, cls))
    return pairs


def _client_namespaces() -> tuple[dict[str, type], dict[str, type]]:
    """The public namespace attributes of a sync and an async client, by name.

    Constructing both is the only way to see them, since they are assigned in
    ``__init__``. Neither client makes a request here, and both are closed
    before the types are handed back.
    """
    sync_client = Comfy(api_key=_DUMMY_KEY)
    async_client = AsyncComfy(api_key=_DUMMY_KEY)
    try:
        sync_ns = {n: type(v) for n, v in vars(sync_client).items() if not n.startswith("_")}
        async_ns = {n: type(v) for n, v in vars(async_client).items() if not n.startswith("_")}
        return sync_ns, async_ns
    finally:
        sync_client.close()
        # No loop is running at import time, which is when this is called.
        asyncio.run(async_client.aclose())


_SYNC_NAMESPACES, _ASYNC_NAMESPACES = _client_namespaces()


def _namespace_pairs() -> list[tuple[str, type, type]]:
    """A pair per namespace the two clients agree on, labelled ``client.<name>``."""
    return [
        (f"client.{name}", _SYNC_NAMESPACES[name], _ASYNC_NAMESPACES[name])
        for name in sorted(_SYNC_NAMESPACES.keys() & _ASYNC_NAMESPACES.keys())
    ]


def _discover_pairs() -> list[tuple[str, type, type]]:
    """Every sync/async class pair reachable from the clients, deduplicated.

    The client root is the one pair named outright — it is the entry point, not
    a discovery. Everything else falls out of the two mechanisms above, so a
    class or namespace added later is covered without editing this file.
    """
    pairs = [("Comfy", Comfy, AsyncComfy), *_name_convention_pairs(), *_namespace_pairs()]
    seen: dict[tuple[str, str], tuple[str, type, type]] = {}
    for label, sync_cls, async_cls in pairs:
        key = (f"{sync_cls.__module__}.{sync_cls.__qualname__}", async_cls.__qualname__)
        seen.setdefault(key, (label, sync_cls, async_cls))
    return sorted(seen.values(), key=lambda pair: pair[0])


_PAIRS = _discover_pairs()
_IDS = [pair[0] for pair in _PAIRS]


def _public_members(cls: type) -> dict[str, Any]:
    """Public class-level members of ``cls``, with descriptors left uninvoked."""
    members = {}
    for name in dir(cls):
        if name.startswith("_"):
            continue
        try:
            members[name] = inspect.getattr_static(cls, name)
        except AttributeError:  # pragma: no cover - defensive
            continue
    return members


def _canonical(name: str) -> str:
    return _RENAMES.get(name, name)


def _unwrap(member: Any) -> Any:
    """The underlying function of a property, staticmethod or classmethod."""
    if isinstance(member, property):
        return member.fget
    if isinstance(member, (staticmethod, classmethod)):
        return member.__func__
    return member


def _kind(member: Any) -> str:
    """How a member is reached: as a call, as an attribute, or as a value.

    Compared alongside the names because a property on one side and a method on
    the other is the same silent break — ``job.id`` on one client and
    ``job.id()`` on the other read identically in a diff.
    """
    if isinstance(member, property):
        return "property"
    if inspect.isroutine(_unwrap(member)):
        return "method"
    return "attribute"


def _params(func: Any) -> list[tuple[str, str]]:
    """Parameter names, order and kinds — the part of a signature a caller sees."""
    return [(p.name, p.kind.name) for p in inspect.signature(func).parameters.values()]


def _methods(cls: type) -> dict[str, Any]:
    """Public callables on ``cls``, keyed by canonical (rename-normalised) name.

    Property getters are included: their parameter lists are part of the surface
    too, and ``_kind`` is what keeps a property from silently matching a method.
    """
    out = {}
    for name, member in _public_members(cls).items():
        func = _unwrap(member)
        if inspect.isroutine(func):
            out[_canonical(name)] = func
    return out


def test_the_two_clients_expose_the_same_namespaces() -> None:
    """``assets`` / ``workflows`` / ``jobs`` / ``models`` — one client cannot grow one alone."""
    sync_only = _SYNC_NAMESPACES.keys() - _ASYNC_NAMESPACES.keys()
    async_only = _ASYNC_NAMESPACES.keys() - _SYNC_NAMESPACES.keys()
    assert not sync_only and not async_only, (
        f"client namespaces diverge: sync-only={sorted(sync_only)} async-only={sorted(async_only)}"
    )


@pytest.mark.parametrize("label,sync_cls,async_cls", _PAIRS, ids=_IDS)
def test_public_surface_names_match(label: str, sync_cls: type, async_cls: type) -> None:
    """A public member on one side of a pair must exist on the other, reached the same way."""
    sync_members = {_canonical(n): m for n, m in _public_members(sync_cls).items()}
    async_members = {_canonical(n): m for n, m in _public_members(async_cls).items()}
    sync_only = {
        n
        for n in sync_members.keys() - async_members.keys()
        if (label, n) not in _ALLOWED_ASYMMETRY
    }
    async_only = {
        n
        for n in async_members.keys() - sync_members.keys()
        if (label, n) not in _ALLOWED_ASYMMETRY
    }
    assert not sync_only and not async_only, (
        f"{sync_cls.__name__}/{async_cls.__name__} public surface diverges: "
        f"sync-only={sorted(sync_only)} async-only={sorted(async_only)} — add the member to "
        "the other class, or declare the asymmetry in _ALLOWED_ASYMMETRY with a reason"
    )
    kind_clashes = [
        f"{n}: sync is a {_kind(sync_members[n])}, async is a {_kind(async_members[n])}"
        for n in sorted(sync_members.keys() & async_members.keys())
        if _kind(sync_members[n]) != _kind(async_members[n])
    ]
    assert not kind_clashes, (
        f"{sync_cls.__name__}/{async_cls.__name__} members differ in kind: "
        + "; ".join(kind_clashes)
    )


@pytest.mark.parametrize("label,sync_cls,async_cls", _PAIRS, ids=_IDS)
def test_paired_methods_take_the_same_parameters(
    label: str, sync_cls: type, async_cls: type
) -> None:
    """Same parameter names, in the same order, with the same kinds.

    Only ``await`` is supposed to differ between the two calls, so a parameter
    renamed, reordered, or moved between positional and keyword-only on one side
    breaks anyone porting code across the swap — silently, at their call site.
    """
    sync_methods = _methods(sync_cls)
    async_methods = _methods(async_cls)
    mismatches = []
    for name in sorted(set(sync_methods) & set(async_methods)):
        sync_params = _params(sync_methods[name])
        async_params = _params(async_methods[name])
        if sync_params != async_params:
            mismatches.append(f"{name}: sync{sync_params} != async{async_params}")
    assert not mismatches, (
        f"{sync_cls.__name__}/{async_cls.__name__} signatures diverge: " + "; ".join(mismatches)
    )


@pytest.mark.parametrize("label,sync_cls,async_cls", _PAIRS, ids=_IDS)
def test_no_suffixed_async_spellings(label: str, sync_cls: type, async_cls: type) -> None:
    """No public name encodes sync-vs-async; the client it hangs off does that."""
    offenders = sorted(
        f"{cls.__name__}.{name}"
        for cls in (sync_cls, async_cls)
        for name in _public_members(cls)
        if _SUFFIXED.search(name)
    )
    assert offenders == [], (
        f"suffixed async/sync variants on the public surface: {offenders} — the awaitable "
        "form of an operation is the async client, not a second method name"
    )


def test_the_models_namespace_is_covered() -> None:
    """The walk has to reach the namespaces, not just the client root.

    ``models`` is the one this is written for: ``run`` lives there, on a
    namespace object rather than on the client, so a root-only walk would miss
    it entirely and still look green.
    """
    assert "models" in _SYNC_NAMESPACES and "models" in _ASYNC_NAMESPACES, (
        f"namespaces reached: sync={sorted(_SYNC_NAMESPACES)} async={sorted(_ASYNC_NAMESPACES)} "
        "— `models` was not among them, so this file is not testing what it claims to"
    )
    models_pairs = [pair for pair in _PAIRS if pair[1] is _SYNC_NAMESPACES["models"]]
    assert models_pairs, "the models namespace produced no pair to compare"
    _label, sync_models, async_models = models_pairs[0]
    assert "run" in _methods(sync_models), f"{sync_models.__name__}.run is not being compared"
    assert "run" in _methods(async_models), f"{async_models.__name__}.run is not being compared"


def test_introspection_is_not_vacuous() -> None:
    """Fail loudly on an empty walk instead of passing on an empty set.

    Every assertion above compares two sets, so a discovery bug that returned
    nothing would make all of them trivially true — and a parametrized test with
    no cases does not run at all. This is the one test that asserts the walk
    found something.
    """
    assert _PAIRS, "introspection discovered no sync/async class pairs at all"
    empty = [label for label, sync_cls, _ in _PAIRS if not _methods(sync_cls)]
    assert not empty, f"pairs discovered with zero public methods (introspection broke?): {empty}"
    compared = sum(len(set(_methods(s)) & set(_methods(a))) for _, s, a in _PAIRS)
    assert compared > 0, "no paired public methods were compared at all"
