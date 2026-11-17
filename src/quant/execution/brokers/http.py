"""Requests to a broker that keep the keys from leaking through a redirect.

urllib's redirect handler copies a request's ordinary headers onto the redirected request, wherever it points. Headers added with
`add_unredirected_header` are not copied, so the keys go only to the host the request names. Reads build their request with `secret_get`; the one
writing request (quant.execution.engine, tests/contracts/test_broker_boundary.py) builds its own and attaches the keys with `with_secrets`.
"""
from __future__ import annotations

import urllib.request
from collections.abc import Mapping


def with_secrets(req: urllib.request.Request, secrets: Mapping[str, str]) -> urllib.request.Request:
    """`req` with `secrets` as headers sent to its own host only, never to a host a redirect names."""
    for k, v in secrets.items():
        req.add_unredirected_header(k, v)
    return req


def secret_get(url: str, secrets: Mapping[str, str], headers: Mapping[str, str] | None = None) -> urllib.request.Request:
    """A GET request whose `secrets` headers never follow a redirect; `headers` are ordinary."""
    return with_secrets(urllib.request.Request(url, headers=dict(headers or {})), secrets)
