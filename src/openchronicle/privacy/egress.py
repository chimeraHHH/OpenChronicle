"""Cross-store fence for privacy-sensitive model and public-read egress."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import ParamSpec, TypeVar

from ..capture import store_lock as capture_store
from ..store import files as files_store

P = ParamSpec("P")
R = TypeVar("R")


@contextmanager
def privacy_egress_lock() -> Iterator[None]:
    """Serialize an authorized response with every explicit local cleanup.

    Memory/timeline cleanup acquires ``review_operation_lock``; capture
    cleanup acquires ``capture_store_lock``. Taking both in the canonical
    review→capture order lets a caller rebuild and authorize an authoritative
    snapshot, then keep it stable through a short publication or public
    response assembly section.
    """
    with files_store.review_operation_lock(), capture_store.capture_store_lock():
        yield


@contextmanager
def model_egress_lock() -> Iterator[None]:
    """Fence provider egress against explicit cleanup without pausing capture.

    Provider callers take the capture-store lock only for short authoritative
    snapshot/revalidation sections. Holding it across network I/O would block
    normal capture persistence and overflow the bounded event queue. Explicit
    cleanup also takes this review fence, so it still cannot cross an in-flight
    provider call.
    """
    with files_store.review_operation_lock():
        yield


def privacy_egress_fenced(func: Callable[P, R]) -> Callable[P, R]:
    """Linearize authoritative reads, authorization, and response assembly."""

    @wraps(func)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with privacy_egress_lock():
            return func(*args, **kwargs)

    return wrapped
