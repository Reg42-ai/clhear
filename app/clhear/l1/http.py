# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Polite HTTP fetching for adapters (HLD §7.2, §8.7).

- Identifying UA string; backoff-and-retry on 429/5xx; never hammer endpoints.
- Fixture modes so adapters run green offline:
    CLHEAR_HTTP_MODE=replay   read recorded fixtures only (tests; the default)
    CLHEAR_HTTP_MODE=record   fetch live once, save fixture, then replay
    CLHEAR_HTTP_MODE=live     fetch live with an on-disk cache (real ingestion)
  Fixture/cache dir: CLHEAR_HTTP_FIXTURES (default tests/fixtures/http).

Fixture file = sha256(url)[:24].json.gz: {"url", "status", "content_b64"}.
"""
import base64
import gzip
import hashlib
import json
import logging
import os
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

log = logging.getLogger("clhear.l1.http")

USER_AGENT = "CLHEAR/0.1 (regulatory corpus builder; contact clhear@reg42.ai)"
DEFAULT_FIXTURES_DIR = "tests/fixtures/http"
# Minimum seconds between live requests to one publisher host. Publishers that
# rate-limit bursts get an entry here; pacing, not disguise, is the remedy.
HOST_PACING_S: dict[str, float] = {}
RETRY_AFTER_CAP_S = 300.0
_observations: ContextVar[tuple] = ContextVar("l1_http_observations", default=())
_response_meta: ContextVar[dict | None] = ContextVar("l1_http_response", default=None)
_pacing_lock = threading.Lock()
_last_request_at: dict[str, float] = {}


def _pace(url: str) -> None:
    host = (urlsplit(url).hostname or "").lower()
    interval = HOST_PACING_S.get(host)
    if not interval:
        return
    with _pacing_lock:
        wait = _last_request_at.get(host, 0.0) + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at[host] = time.monotonic()


def _retry_after_seconds(response) -> float | None:
    """A publisher's own throttle instruction beats the default backoff curve."""
    value = (getattr(response, "headers", None) or {}).get("retry-after")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            return None
    return min(max(seconds, 0.0), RETRY_AFTER_CAP_S)


def begin_fetch() -> None:
    """Start evidence for one adapter acquisition, isolated from other tasks."""
    _observations.set(())


def fetch_evidence() -> list[dict]:
    return list(_observations.get())


def publisher_checked_at() -> str | None:
    observations = fetch_evidence()
    if not observations or any(o["origin"] not in {"live", "revalidated"} for o in observations):
        return None
    return min(o["checked_at"] for o in observations)


def _observe(url: str, origin: str, content: bytes, **detail) -> None:
    _observations.set((*_observations.get(), {
        "url": url, "origin": origin,
        "checked_at": datetime.now(timezone.utc).isoformat() if origin in {"live", "revalidated"} else None,
        "sha256": hashlib.sha256(content).hexdigest(), **detail,
    }))


def _cache_path(url: str) -> Path:
    # Never share live cache files with committed replay fixtures.
    from app.clhear.settings import get_settings
    root = Path(os.environ.get("CLHEAR_HTTP_CACHE_DIR", str(Path(get_settings().clhear_artifacts_dir) / "restricted" / "http-cache")))
    return root / f"{_cache_digest(url)}.json.gz"


class FixtureMissing(RuntimeError):
    pass


class PublisherBoundaryError(ValueError):
    """A redirect or cached response is outside the reviewed publisher hosts."""


def _check_publisher_url(url, hosts):
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname in hosts
                 and parsed.port in {None, 443} and not parsed.username and not parsed.password)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise PublisherBoundaryError("Acquisition URL is outside the authorized publisher host")


def _reviewed_hosts(url, allowed_redirect_hosts):
    hosts = None if allowed_redirect_hosts is None else frozenset(allowed_redirect_hosts)
    if hosts is not None and (not hosts or any(not isinstance(h, str) or not h or h != h.lower()
                                              or any(c in h for c in "/:@?#") for h in hosts)):
        raise PublisherBoundaryError("An exact, nonempty publisher hostname allowlist is required")
    if hosts is not None:
        _check_publisher_url(url, hosts)
    return hosts


def _verified_cache_origin(record, hosts):
    """Raw cache bytes alone never prove where a guarded request terminated."""
    final_url, redirects = record.get("final_url"), record.get("redirect_chain")
    if not isinstance(final_url, str) or not isinstance(redirects, list) or len(redirects) > 5:
        return False
    try:
        for url in [final_url, *redirects]:
            _check_publisher_url(url, hosts)
    except PublisherBoundaryError:
        return False
    return True


def _mode() -> str:
    return os.environ.get("CLHEAR_HTTP_MODE", "replay")


def _fixtures_dir() -> Path:
    return Path(os.environ.get("CLHEAR_HTTP_FIXTURES", DEFAULT_FIXTURES_DIR))


def _fixture_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()[:24]
    return _fixtures_dir() / f"{digest}.json.gz"


def _read_fixture(path: Path) -> bytes:
    record = json.loads(gzip.decompress(path.read_bytes()))
    return base64.b64decode(record["content_b64"])


def _write_fixture(path: Path, url: str, status: int, content: bytes, **metadata) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"url": url, "status": status, "content_b64": base64.b64encode(content).decode(),
              "sha256": hashlib.sha256(content).hexdigest(), **metadata}
    temporary = path.with_name(path.name + f".{os.getpid()}.{time.time_ns()}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(gzip.compress(json.dumps(record).encode()))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fetch_live(url: str, timeout: float, headers: dict | None = None, attempts: int = 8, *, allowed_redirect_hosts=None) -> bytes:
    _response_meta.set(None)
    hosts = _reviewed_hosts(url, allowed_redirect_hosts)
    delay = 5.0
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request_url, redirect_chain = url, []
            for hop in range(6):
                _pace(request_url)
                resp = httpx.get(
                    request_url,
                    headers={"User-Agent": USER_AGENT, **(headers or {})},
                    timeout=timeout,
                    follow_redirects=hosts is None,
                )
                if hosts is None or resp.status_code not in {301, 302, 303, 307, 308}:
                    break
                destination = urljoin(request_url, resp.headers.get("location", ""))
                _check_publisher_url(destination, hosts)
                if destination in [*redirect_chain, request_url] or hop == 5:
                    raise PublisherBoundaryError("Publisher acquisition exceeded its bounded redirect chain")
                redirect_chain.append(request_url)
                request_url = destination
            final_url = str(resp.url)
            if hosts is not None:
                _check_publisher_url(final_url, hosts)
            provenance = {"final_url": final_url, "redirect_chain": redirect_chain if hosts is not None else [str(r.url) for r in resp.history]}
            if resp.status_code == 304:
                _response_meta.set({"status": 304, "etag": resp.headers.get("etag"),
                                    "last_modified": resp.headers.get("last-modified"), **provenance})
                return b""
            if resp.status_code == 429 or resp.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"{resp.status_code} from {url}", request=resp.request, response=resp
                )
            # legislation.gov.uk often answers 202 Accepted with an empty body
            # while it materializes data.xml — treat as retryable, not success.
            if resp.status_code == 202 or not resp.content:
                raise httpx.HTTPStatusError(
                    f"{resp.status_code} empty/accepted from {url}",
                    request=resp.request,
                    response=resp,
                )
            resp.raise_for_status()
            _response_meta.set({"status": resp.status_code, "etag": resp.headers.get("etag"),
                                "last_modified": resp.headers.get("last-modified"), **provenance})
            return resp.content
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            # 202 / empty body (TNA materializing XML) is retryable, like 429.
            if status is not None and status not in (202, 429) and status < 500:
                raise  # other 4xx: retrying will not help
            last_error = exc
            if attempt < attempts - 1:
                pause = delay
                if status == 429:
                    # Throttled: obey Retry-After when given, otherwise back off
                    # at least half a minute so the next attempt is not a burst.
                    pause = max(_retry_after_seconds(exc.response) or 0.0, delay, 30.0)
                log.warning("fetch %s failed (%s); backing off %.0fs", url, exc, pause)
                time.sleep(pause)
                delay *= 2
    raise RuntimeError(f"fetch failed after {attempts} attempts: {url}") from last_error


def last_good_used() -> bool:
    """True if ANY request in this acquisition fell back to cached bytes."""
    return any(o["origin"] == "stale_cache" for o in _observations.get())


def _cache_digest(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:24]


def _datalake_cache_key(url: str) -> str:
    # HTTP acquisition never implies permission to redistribute source bytes.
    return f"restricted/_http_cache/{_cache_digest(url)}.bin"


def _datalake_put(url: str, content: bytes) -> None:
    """Best-effort write of a successful live fetch so AWS workers can fall back."""
    if not content:
        return
    try:
        from app.clhear.settings import get_settings

        settings = get_settings()
        bucket = settings.clhear_datalake_bucket
        if not bucket:
            return
        import boto3

        boto3.client("s3", region_name=settings.aws_region).put_object(
            Bucket=bucket,
            Key=_datalake_cache_key(url),
            Body=content,
            ContentType="application/octet-stream",
        )
    except Exception as exc:
        log.warning("datalake cache write failed for %s: %s", url, exc)


def _datalake_get(url: str) -> bytes | None:
    try:
        from app.clhear.settings import get_settings

        settings = get_settings()
        bucket = settings.clhear_datalake_bucket
        if not bucket:
            return None
        import boto3

        resp = boto3.client("s3", region_name=settings.aws_region).get_object(
            Bucket=bucket, Key=_datalake_cache_key(url)
        )
        body = resp["Body"].read()
        return body or None
    except Exception:
        return None


def get(url: str, timeout: float = 60.0, headers: dict | None = None, *, allowed_redirect_hosts=None) -> bytes:
    """Fetch url as bytes honoring CLHEAR_HTTP_MODE (replay/record/live).

    Live mode always contacts the publisher. A valid 304 checks cached bytes;
    an outage can return private last-good bytes but never advances freshness.
    """
    # A reviewed host contract also protects local caches.
    hosts = _reviewed_hosts(url, allowed_redirect_hosts)
    guarded_cache = hosts is not None
    mode = _mode()
    path = _fixture_path(url)
    if mode not in {"replay", "record", "live"}:
        raise ValueError(f"Unknown CLHEAR_HTTP_MODE: {mode}")
    if mode in {"replay", "record"} and path.exists():
        if guarded_cache:
            record = json.loads(gzip.decompress(path.read_bytes()))
            if record.get("url") != url or (record.get("final_url") and not _verified_cache_origin(record, hosts)):
                raise PublisherBoundaryError("Recorded response is outside the authorized publisher host")
            # Old authored fixtures have no response provenance; their origin
            # remains fixture and can never establish live publisher freshness.
        body = _read_fixture(path)
        _observe(url, "fixture", body)
        return body
    if mode == "replay":
        _observe(url, "failed", b"")
        raise FixtureMissing(f"no recorded fixture for {url} (CLHEAR_HTTP_MODE=replay)")
    cache_path = _cache_path(url)
    cached, cache_meta = None, {}
    if mode == "live" and cache_path.exists():
        try:
            cache_meta = json.loads(gzip.decompress(cache_path.read_bytes()))
            candidate = base64.b64decode(cache_meta["content_b64"], validate=True)
            if (cache_meta["url"] == url and cache_meta["sha256"] == hashlib.sha256(candidate).hexdigest()
                    and (not guarded_cache or _verified_cache_origin(cache_meta, hosts))):
                cached = candidate
        except (ValueError, KeyError, OSError):
            cache_meta = {}
    request_headers = dict(headers or {})
    if cached:
        if cache_meta.get("etag"):
            request_headers["If-None-Match"] = cache_meta["etag"]
        elif cache_meta.get("last_modified"):
            request_headers["If-Modified-Since"] = cache_meta["last_modified"]
    try:
        _response_meta.set(None)
        content = (_fetch_live(url, timeout, request_headers, allowed_redirect_hosts=hosts) if guarded_cache else
                   _fetch_live(url, timeout, request_headers))
        response = _response_meta.get() or {"status": 200}
        origin = "live"
        if response.get("status") == 304:
            if not cached:
                raise RuntimeError("Publisher returned 304 without verified cached bytes")
            content, origin = cached, "revalidated"
        if not content:
            raise RuntimeError("Publisher returned no source bytes")
    except PublisherBoundaryError:
        _observe(url, "failed", b"")
        raise  # a disallowed redirect cannot be hidden by last-good bytes
    except Exception:
        # The legacy datalake cache stores raw bytes without final-URL proof.
        cached = cached or (_datalake_get(url) if mode == "live" and not guarded_cache else None)
        if cached:
            log.warning("live fetch failed for %s; using datalake last-good", url)
            _observe(url, "stale_cache", cached, final_url=cache_meta.get("final_url"),
                     redirect_chain=cache_meta.get("redirect_chain", []))
            return cached
        _observe(url, "failed", b"")
        raise
    _observe(url, origin, content, status=response.get("status"), final_url=response.get("final_url"),
             redirect_chain=response.get("redirect_chain", []))
    destination = cache_path if mode == "live" else path
    validators = {k: response.get(k) or (cache_meta.get(k) if origin == "revalidated" else None)
                  for k in ("etag", "last_modified")}
    _write_fixture(destination, url, 200, content,
                   **validators, final_url=response.get("final_url"), redirect_chain=response.get("redirect_chain", []))
    if mode == "live":
        _datalake_put(url, content)
    return content
