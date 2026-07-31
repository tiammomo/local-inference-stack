#!/usr/bin/env python3
"""Direct, redirect-free HTTP helpers for loopback-only local services."""

from __future__ import annotations

import ipaddress
from http.cookiejar import CookieJar
from typing import Any
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)
from urllib.parse import urlsplit


class RejectRedirectHandler(HTTPRedirectHandler):
    """Reject redirects so credentials cannot cross the reviewed local origin."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_loopback_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not is_loopback_host(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(f"local service URL must be an uncredentialed loopback HTTP(S) URL: {url}")
    try:
        _ = parsed.port
    except ValueError as error:
        raise ValueError(f"local service URL has an invalid port: {url}") from error
    return url


def direct_opener(cookie_jar: CookieJar | None = None) -> Any:
    handlers: list[Any] = [ProxyHandler({}), RejectRedirectHandler()]
    if cookie_jar is not None:
        handlers.append(HTTPCookieProcessor(cookie_jar))
    return build_opener(*handlers)


def direct_urlopen(request_or_url: Request | str, *, timeout: float) -> Any:
    url = (
        request_or_url.full_url
        if isinstance(request_or_url, Request)
        else str(request_or_url)
    )
    validate_loopback_url(url)
    return direct_opener().open(request_or_url, timeout=timeout)
