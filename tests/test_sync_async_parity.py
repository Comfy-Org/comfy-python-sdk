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
a method or as a property, the parameter names, order, kinds *and defaults* of
the methods they share, and — the property the "add ``await``" half of the
promise actually rests on — that the async side of a shared method is the
awaitable one and the sync side is not. It also bans a spelling that encodes
sync-vs-async anywhere on the surface, and refuses to pass on an empty
introspection result: a parity test that discovered nothing would be green for
the wrong reason.

Dunders (``__enter__``/``__aenter__``, ...) are excluded — they are a mechanical
consequence of sync vs async, not part of the call surface a swap has to match.
Deliberate asymmetries live in ``_RENAMES`` / ``_ALLOWED_ASYMMETRY`` /
``_SYNC_ON_BOTH`` below, each with the reason it is deliberate; anything not
listed there fails.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import importlib
import inspect
import os
import pkgutil
import re
from collections.abc import Iterator
from typing import Any

import pytest

import comfy_low
import comfy_sdk
from comfy_sdk import AsyncComfy, Comfy
from comfy_sdk.client import BASE_URL_ENV_VAR

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
# These tables are the only escape hatch. An asymmetry not written here
# fails, which is the point: an intentional exception stays visible, and an
# accidental one still breaks the build. All three are keyed on the
# module-qualified *sync* class name (see ``_pair_key``) rather than on a
# pair's display label, because a label is whichever discovery mechanism
# reached the pair first and is therefore not a stable key to write against.

#: Async spellings of the same operation under a different name, applied to the
#: *async* side only. asyncio's convention is ``aclose()`` for an awaitable
#: closer; ``close()`` cannot be reused for it, because a name callers expect to
#: be synchronous returning an un-awaited coroutine fails silently. httpx and
#: asyncio's own streams spell it the same way. Renaming only the async side is
#: what keeps the normalisation direction-aware: a sync class that grew an
#: ``aclose``, or an async class that kept a bare ``close``, is a real
#: divergence and still has to fail.
_RENAMES = {"aclose": "close"}

#: ``(pair key, member name) -> why it legitimately exists on one side only.``
#: Empty today: once ``aclose`` is normalised, the surfaces are symmetric.
_ALLOWED_ASYMMETRY: dict[tuple[str, str], str] = {}

#: ``(pair key, member name) -> why the async side is deliberately not
#: awaitable.`` Every other shared method has to be awaitable on the async
#: client, since that is the entire "add ``await``" contract; these do no I/O,
#: so awaiting them would be ceremony with nothing behind it.
_SYNC_ON_BOTH: dict[tuple[str, str], str] = {
    ("comfy_sdk.assets.AssetFactory", "from_file"): (
        "builds a handle around a local path; the upload is the awaitable step (commit)"
    ),
    ("comfy_sdk.assets.AssetFactory", "from_bytes"): (
        "builds a handle around bytes already in memory; the upload is commit"
    ),
    ("comfy_sdk.assets.AssetFactory", "from_stream"): (
        "drains a synchronous BinaryIO to hash it; the upload is commit"
    ),
    ("comfy_sdk.jobs.Job", "get_outputs"): (
        "reads outputs already on the handle — no re-fetch, so nothing to await"
    ),
}

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


def _ours(cls: type) -> bool:
    """Whether ``cls`` is defined in one of the two packages under test."""
    return inspect.isclass(cls) and cls.__module__.split(".")[0] in _PACKAGE_NAMES


def _qualified(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _pair_key(sync_cls: type) -> str:
    """The stable key a pair is written against in the asymmetry tables."""
    return _qualified(sync_cls)


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


@contextlib.contextmanager
def _default_deployment() -> Iterator[None]:
    """Build the probe clients against the default deployment, not the runner's.

    ``_resolve_base_url()`` *raises* on a malformed ``COMFY_BASE_URL``, and the
    construction below runs at import — i.e. at collection time, before any
    fixture could adjust the environment. Inheriting an ambient value would let
    a stray export in a developer's shell turn this whole module into a
    collection error that has nothing to do with parity.
    """
    saved = os.environ.pop(BASE_URL_ENV_VAR, None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ[BASE_URL_ENV_VAR] = saved


def _run(coro: Any) -> None:
    """Drive one coroutine to completion on a loop of our own.

    A private loop rather than :func:`asyncio.run` so this does not depend on
    (or disturb) whatever the importing process has installed as the current
    event loop.
    """
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


def _namespace_types(client: Any) -> dict[str, type]:
    """The types behind a client's public namespace attributes, by name.

    Instance ``vars`` covers the namespaces assigned in ``__init__``, which is
    all of them today; class-level ``property`` / ``cached_property``
    descriptors are walked too, so a namespace later exposed that way keeps its
    pair coverage instead of silently dropping out of the comparison.
    """
    names = {name for name in vars(client) if not name.startswith("_")}
    cls = type(client)
    for name in dir(cls):
        if name.startswith("_"):
            continue
        descriptor = inspect.getattr_static(cls, name, None)
        if isinstance(descriptor, (property, functools.cached_property)):
            names.add(name)
    return {name: type(getattr(client, name)) for name in sorted(names)}


def _client_namespaces() -> tuple[dict[str, type], dict[str, type]]:
    """The public namespace attributes of a sync and an async client, by name.

    Constructing both is the only way to see them, since they are assigned in
    ``__init__``. Neither client makes a request here, and each is closed
    independently — an :class:`~contextlib.ExitStack` rather than one
    ``finally``, so a raise from the second construction still closes the first
    client's pool, and a raise from one close still runs the other.
    """
    with _default_deployment(), contextlib.ExitStack() as stack:
        sync_client = Comfy(api_key=_DUMMY_KEY)
        stack.callback(sync_client.close)
        async_client = AsyncComfy(api_key=_DUMMY_KEY)
        stack.callback(lambda: _run(async_client.aclose()))
        return _namespace_types(sync_client), _namespace_types(async_client)


_SYNC_NAMESPACES, _ASYNC_NAMESPACES = _client_namespaces()


def _namespace_pairs() -> list[tuple[str, type, type]]:
    """A pair per namespace the two clients agree on, labelled ``client.<name>``.

    Two filters keep the parametrization honest. A namespace both clients share
    *by identity* (one ``WorkflowFactory`` assigned in both ``__init__``\\ s) is
    skipped: comparing a class against itself is green no matter what it
    contains, and a vacuous case reads like coverage. So is anything whose type
    is not one of our own classes — a future ``self.timeout = None`` would
    otherwise be paired as ``(NoneType, NoneType)`` and fail with a misleading
    "introspection broke?".
    """
    pairs = []
    for name in sorted(_SYNC_NAMESPACES.keys() & _ASYNC_NAMESPACES.keys()):
        sync_cls, async_cls = _SYNC_NAMESPACES[name], _ASYNC_NAMESPACES[name]
        if sync_cls is async_cls or not _ours(sync_cls) or not _ours(async_cls):
            continue
        pairs.append((f"client.{name}", sync_cls, async_cls))
    return pairs


def _discover_pairs() -> list[tuple[str, type, type]]:
    """Every sync/async class pair reachable from the clients, deduplicated.

    The client root is the one pair named outright — it is the entry point, not
    a discovery. Everything else falls out of the two mechanisms above, so a
    class or namespace added later is covered without editing this file.
    """
    pairs = [("Comfy", Comfy, AsyncComfy), *_name_convention_pairs(), *_namespace_pairs()]
    seen: dict[tuple[str, str], tuple[str, type, type]] = {}
    for label, sync_cls, async_cls in pairs:
        # Both halves module-qualified: two distinct ``AsyncX`` classes in
        # different modules that resolve to the same sync counterpart must not
        # collapse onto one key, or ``setdefault`` drops the second pair and
        # coverage shrinks with nothing failing.
        key = (_qualified(sync_cls), _qualified(async_cls))
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


def _members(cls: type, *, rename: bool) -> dict[str, Any]:
    """Public members of ``cls``, optionally under the async-side rename.

    ``rename=True`` is for the *async* half of a pair only. A collision under
    the rename (a class carrying both ``close`` and ``aclose``) is a failure
    rather than an overwrite: silently keeping one of the two would drop the
    other out of every name, kind and signature comparison below — exactly the
    divergence this module exists to catch.
    """
    out: dict[str, Any] = {}
    spelling: dict[str, str] = {}  # canonical name -> the member that claimed it
    for name, member in _public_members(cls).items():
        key = _RENAMES.get(name, name) if rename else name
        if key in out:
            raise AssertionError(
                f"{_qualified(cls)} exposes both {spelling[key]!r} and {name!r}, which "
                f"normalise to {key!r} under _RENAMES — one of the two would drop out of the "
                f"parity comparison unnoticed. Keep a single spelling per operation."
            )
        out[key] = member
        spelling[key] = name
    return out


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


def _call_form(func: Any) -> str:
    """What calling ``func`` hands back: a value, an awaitable, or an iterator.

    Looked through ``functools.wraps`` with :func:`inspect.unwrap` so the
    ``@contextmanager`` / ``@asynccontextmanager`` pair (``open``,
    ``get_asset_content``) is classified by the generator underneath rather
    than by the plain wrapper both decorators leave on top.
    """
    inner = inspect.unwrap(func)
    for candidate in (func, inner):
        if inspect.iscoroutinefunction(candidate):
            return "awaitable"
        if inspect.isasyncgenfunction(candidate):
            return "async iterator"
        if inspect.isgeneratorfunction(candidate):
            return "iterator"
    return "plain"


#: The async call form each sync one has to be mirrored by. Anything a caller
#: iterates stays something they iterate; anything else becomes awaitable.
_EXPECTED_ASYNC_FORM = {"plain": "awaitable", "iterator": "async iterator"}


def _methods(cls: type, *, rename: bool = False) -> dict[str, Any]:
    """Public callables on ``cls``, keyed by (optionally renamed) name.

    Property getters are included: their parameter lists are part of the surface
    too, and ``_kind`` is what keeps a property from silently matching a method.
    """
    out = {}
    for name, member in _members(cls, rename=rename).items():
        func = _unwrap(member)
        if inspect.isroutine(func):
            out[name] = func
    return out


class _Required:
    """Sentinel standing in for "no default", so it renders in a diff."""

    def __repr__(self) -> str:
        return "<required>"


_REQUIRED = _Required()

_EMPTY = inspect.Parameter.empty


def _params(func: Any) -> list[tuple[str, str, Any]]:
    """Parameter names, order, kinds and defaults — what a caller sees.

    The default is part of it: ``timeout: float | None = None`` on one client
    and a required ``timeout`` on the other (or two different default values)
    breaks a caller at a call site they never touched, which is precisely the
    silent port failure this module promises cannot happen.
    """
    return [
        (p.name, p.kind.name, _REQUIRED if p.default is _EMPTY else p.default)
        for p in inspect.signature(func).parameters.values()
    ]


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
    key = _pair_key(sync_cls)
    sync_members = _members(sync_cls, rename=False)
    async_members = _members(async_cls, rename=True)
    sync_only = {
        n for n in sync_members.keys() - async_members.keys() if (key, n) not in _ALLOWED_ASYMMETRY
    }
    async_only = {
        n for n in async_members.keys() - sync_members.keys() if (key, n) not in _ALLOWED_ASYMMETRY
    }
    assert not sync_only and not async_only, (
        f"{sync_cls.__name__}/{async_cls.__name__} public surface diverges: "
        f"sync-only={sorted(sync_only)} async-only={sorted(async_only)} — add the member to "
        f"the other class, or declare the asymmetry in _ALLOWED_ASYMMETRY under the key "
        f"{key!r} with a reason"
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
    """Same parameter names, in the same order, with the same kinds and defaults.

    Only ``await`` is supposed to differ between the two calls, so a parameter
    renamed, reordered, moved between positional and keyword-only, or made
    optional on one side breaks anyone porting code across the swap — silently,
    at their call site.
    """
    sync_methods = _methods(sync_cls)
    async_methods = _methods(async_cls, rename=True)
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
def test_the_async_side_is_the_awaitable_one(label: str, sync_cls: type, async_cls: type) -> None:
    """The half of the promise the names alone cannot check.

    "Swap the import and add ``await``" is a claim about *how a call behaves*,
    not just about what it is spelled. Comparing names and signatures leaves it
    entirely unasserted: dropping ``async`` from ``AsyncModels.run`` — or
    adding it to ``Models.run`` — changes no name, no kind and no signature.
    So the sync side must never hand back something to await, and the async
    side must, unless ``_SYNC_ON_BOTH`` says why it does no I/O.
    """
    key = _pair_key(sync_cls)
    sync_members = _members(sync_cls, rename=False)
    async_members = _members(async_cls, rename=True)
    problems = []
    for name in sorted(sync_members.keys() & async_members.keys()):
        if _kind(sync_members[name]) != "method" or _kind(async_members[name]) != "method":
            continue  # properties are values on both sides; kind parity covers them
        sync_form = _call_form(_unwrap(sync_members[name]))
        async_form = _call_form(_unwrap(async_members[name]))
        if sync_form not in _EXPECTED_ASYNC_FORM:
            problems.append(
                f"{name}: the sync member is {sync_form} — a sync client must not hand back "
                f"something the caller has to await"
            )
            continue
        allowed = (key, name) in _SYNC_ON_BOTH
        expected = "plain" if allowed else _EXPECTED_ASYNC_FORM[sync_form]
        if async_form != expected:
            why = (
                f" (_SYNC_ON_BOTH declares it plain: {_SYNC_ON_BOTH[(key, name)]} — drop the "
                f"entry if that is no longer true)"
                if allowed
                else ""
            )
            problems.append(
                f"{name}: sync is {sync_form}, so async should be {expected}, but it is "
                f"{async_form}{why}"
            )
    assert not problems, (
        f"{sync_cls.__name__}/{async_cls.__name__} breaks the add-`await` contract: "
        + "; ".join(problems)
        + f" — make the async member awaitable, or declare it under the key {key!r} in "
        "_SYNC_ON_BOTH with the reason it does no I/O"
    )


def test_no_suffixed_async_spellings() -> None:
    """No public name encodes sync-vs-async; the client it hangs off does that.

    Swept over every public class in the two packages rather than over the
    discovered pairs: the ban is on the spelling *anywhere on the surface*, and
    a pair-scoped sweep would never look at an unpaired class (``Workflow``,
    ``WorkflowFactory``, the exception types), so a ``save_async`` landing on
    one of those would pass.
    """
    offenders = sorted(
        f"{cls.__module__}.{cls.__qualname__}.{name}"
        for cls in _public_classes()
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
    assert not [label for label, s, a in _PAIRS if s is a], (
        "a pair was discovered comparing a class against itself, which cannot fail"
    )
    empty = [label for label, sync_cls, _ in _PAIRS if not _methods(sync_cls)]
    assert not empty, f"pairs discovered with zero public methods (introspection broke?): {empty}"
    compared = sum(len(set(_methods(s)) & set(_methods(a, rename=True))) for _, s, a in _PAIRS)
    assert compared > 0, "no paired public methods were compared at all"


def test_the_asymmetry_tables_stay_current() -> None:
    """An allowance for something that no longer exists is a stale exemption.

    Both tables are escape hatches keyed on ``module.Class``; an entry that
    matches nothing is worse than no entry, because it reads like a live
    exception to a rule it has stopped touching — and it would silently
    re-arm as an exemption if that name ever came back for another reason.
    """
    pairs = {_pair_key(sync_cls): (sync_cls, async_cls) for _, sync_cls, async_cls in _PAIRS}
    stale = []
    for table, must_be_shared in ((_ALLOWED_ASYMMETRY, False), (_SYNC_ON_BOTH, True)):
        for key, name in table:
            if key not in pairs:
                stale.append(f"{key}.{name} (no such pair is discovered)")
                continue
            sync_cls, async_cls = pairs[key]
            on_sync = name in _members(sync_cls, rename=False)
            on_async = name in _members(async_cls, rename=True)
            if must_be_shared and not (on_sync and on_async):
                stale.append(f"{key}.{name} (not a member of both sides)")
            elif not must_be_shared and not (on_sync or on_async):
                stale.append(f"{key}.{name} (not a member of either side)")
    assert stale == [], (
        f"asymmetry declared for something introspection never sees: {sorted(stale)} — the "
        "class or member was renamed or removed, so drop the entry (keys are "
        "module-qualified sync class names)"
    )
