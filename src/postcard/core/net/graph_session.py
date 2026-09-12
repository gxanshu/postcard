"""A thin Microsoft Graph client over urllib, for the mail endpoints Postcard uses.

Like the IMAP and SMTP sessions it only speaks the protocol: no database, no
GTK, and it runs on a worker thread. Graph is plain HTTPS plus JSON, so the
stdlib is enough and the Flatpak needs no new module.
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field

from . import NET_TIMEOUT_SECONDS
from .auth import MECHANISM_XOAUTH2, Credential

logger = logging.getLogger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"

# Message ids otherwise change whenever a message moves folder, and a move is
# exactly when the app still holds the old one.
PREFER_IMMUTABLE_IDS = 'IdType="ImmutableId"'

# The most requests one $batch call may carry.
BATCH_LIMIT = 20

# Upload sessions take chunks in multiples of 320 KiB; 4 MiB keeps each PUT
# well under the timeout on a slow link.
UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024

JSON_TYPE = "application/json"

# Throttling (429) and a busy or restarting backend (503/504) are temporary by
# definition; Graph says how long to wait in Retry-After.
_RETRY_STATUSES = frozenset({429, 503, 504})
_MAX_ATTEMPTS = 4
_MAX_RETRY_SECONDS = 30


class GraphError(Exception):
    """A request Graph answered with an error, or could not be made at all."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> dict:
        return json.loads(self.body) if self.body else {}


@dataclass(frozen=True)
class BatchRequest:
    """One request inside a $batch, addressed relative to GRAPH_ROOT."""

    method: str
    path: str
    body: dict | None = None


@dataclass(frozen=True)
class BatchResponse:
    status: int
    body: dict

    @property
    def is_ok(self) -> bool:
        return 200 <= self.status < 300


Transport = Callable[[urllib.request.Request], Response]


def urlopen_transport(request: urllib.request.Request) -> Response:
    """Send a request for real. An HTTP error status is returned, not raised,
    so retrying and error parsing live in one place."""
    try:
        with urllib.request.urlopen(request, timeout=NET_TIMEOUT_SECONDS) as reply:
            return Response(reply.status, reply.read(), dict(reply.headers))
    except urllib.error.HTTPError as error:
        return Response(error.code, error.read(), dict(error.headers))
    except urllib.error.URLError as error:
        # The reason is the socket or TLS error itself, which errors.classify
        # already knows how to word; the URLError wrapper would hide it.
        if isinstance(error.reason, Exception):
            raise error.reason from error
        raise


def quote_id(value: str) -> str:
    """A Graph id made safe for a URL path segment."""
    return urllib.parse.quote(value, safe="")


def with_query(path: str, **params: str | int) -> str:
    """path plus OData query options, e.g. with_query(p, top=50) -> p?$top=50."""
    query = urllib.parse.urlencode(
        {f"${name}": value for name, value in params.items()},
        quote_via=urllib.parse.quote,
        safe="$,",
    )
    return f"{path}?{query}" if query else path


def error_from(response: Response) -> GraphError:
    try:
        detail = response.json().get("error", {})
    except ValueError:
        detail = {}
    if not isinstance(detail, dict):
        detail = {}
    return GraphError(
        response.status,
        str(detail.get("code", "")),
        str(detail.get("message", "")) or f"HTTP {response.status}",
    )


def retry_delay(headers: Mapping[str, str], attempt: int) -> float:
    """Seconds to wait before retrying: Retry-After when given, else backoff."""
    lowered = {name.lower(): value for name, value in headers.items()}
    try:
        seconds = float(lowered.get("retry-after", ""))
    except ValueError:
        seconds = 2.0**attempt
    return max(0.0, min(seconds, _MAX_RETRY_SECONDS))


class GraphSession:
    def __init__(
        self,
        credential: Credential,
        transport: Transport = urlopen_transport,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # Only an OAuth token opens Graph; a password would just be a 401.
        if credential.mechanism != MECHANISM_XOAUTH2:
            raise GraphError(
                0, "unsupportedCredential", f"{credential.mechanism} cannot sign in"
            )
        self._token = credential.secret
        self._transport = transport
        self._sleep = sleep

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = JSON_TYPE,
        prefer: str = "",
    ) -> Response:
        """One request, retried while Graph says it is only busy. Raises
        GraphError for any other error status."""
        url = path if path.startswith("https://") else GRAPH_ROOT + path
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Prefer": ", ".join(filter(None, (PREFER_IMMUTABLE_IDS, prefer))),
        }
        if body is not None:
            headers["Content-Type"] = content_type

        for attempt in range(_MAX_ATTEMPTS):
            request = urllib.request.Request(
                url, data=body, headers=headers, method=method
            )
            response = self._transport(request)
            if response.status not in _RETRY_STATUSES or attempt == _MAX_ATTEMPTS - 1:
                break
            delay = retry_delay(response.headers, attempt)
            logger.debug(
                "Graph answered %d to %s %s; retrying in %.0fs",
                response.status,
                method,
                url.split("?")[0],
                delay,
            )
            self._sleep(delay)

        if response.status >= 400:
            raise error_from(response)
        return response

    def get(self, path: str, prefer: str = "") -> dict:
        return self.request("GET", path, prefer=prefer).json()

    def send(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload).encode() if payload is not None else b""
        return self.request(method, path, body).json()

    def pages(self, path: str, prefer: str = "") -> Iterator[dict]:
        """Every page of a collection, following @odata.nextLink."""
        next_path: str | None = path
        while next_path:
            page = self.get(next_path, prefer)
            yield page
            next_path = page.get("@odata.nextLink")

    def batch(self, requests: list[BatchRequest]) -> list[BatchResponse]:
        """Run requests through $batch, BATCH_LIMIT at a time, answers in order.

        A request throttled inside the batch is retried in a later batch rather
        than reported, since the batch itself succeeded around it.
        """
        answers: dict[int, BatchResponse] = {}
        pending = list(range(len(requests)))
        for attempt in range(_MAX_ATTEMPTS):
            throttled: list[int] = []
            delay = 0.0
            for start in range(0, len(pending), BATCH_LIMIT):
                chunk = pending[start : start + BATCH_LIMIT]
                for index, status, headers, body in self._run_batch(requests, chunk):
                    if status in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS - 1:
                        throttled.append(index)
                        delay = max(delay, retry_delay(headers, attempt))
                    else:
                        answers[index] = BatchResponse(status, body)
            if not throttled:
                break
            self._sleep(delay)
            pending = throttled
        return [answers[index] for index in range(len(requests))]

    def _run_batch(
        self, requests: list[BatchRequest], indexes: list[int]
    ) -> Iterator[tuple[int, int, dict, dict]]:
        payload = {"requests": []}
        for index in indexes:
            request = requests[index]
            entry: dict = {
                "id": str(index),
                "method": request.method,
                "url": request.path,
                "headers": {"Prefer": PREFER_IMMUTABLE_IDS},
            }
            if request.body is not None:
                entry["body"] = request.body
                entry["headers"]["Content-Type"] = JSON_TYPE
            payload["requests"].append(entry)

        reply = self.send("POST", "/$batch", payload)
        seen = set()
        for answer in reply.get("responses", []):
            index = int(answer.get("id", -1))
            if index not in indexes:
                continue
            seen.add(index)
            body = answer.get("body")
            yield (
                index,
                int(answer.get("status", 0)),
                answer.get("headers") or {},
                body if isinstance(body, dict) else {},
            )
        for index in set(indexes) - seen:
            yield index, 0, {}, {"error": {"message": "no answer in the batch"}}

    def upload(self, upload_url: str, data: bytes) -> None:
        """Upload bytes to an attachment upload session.

        The session URL carries its own authorization, and Graph rejects a PUT
        that also sends the bearer token, so this bypasses request().
        """
        total = len(data)
        for start in range(0, total, UPLOAD_CHUNK_BYTES):
            chunk = data[start : start + UPLOAD_CHUNK_BYTES]
            end = start + len(chunk) - 1
            request = urllib.request.Request(
                upload_url,
                data=chunk,
                method="PUT",
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{total}",
                },
            )
            response = self._transport(request)
            if response.status >= 400:
                raise error_from(response)
