"""Async Dataverse Web API client authenticated through the Azure CLI's sign-in.

Tokens are requested for the environment's tenant (``[l2] tenant_id``) explicitly; a token from
the Azure CLI's default tenant is rejected when the environment lives in another tenant.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

API = "api/data/v9.2/"
FORMATTED = 'odata.include-annotations="OData.Community.Display.V1.FormattedValue"'
BATCH_LIMIT = 1000


class DataverseError(RuntimeError):
    def __init__(self, status: int, message: str, code: str = "") -> None:
        super().__init__(f"{status} {code}: {message}" if code else f"{status}: {message}")
        self.status = status
        self.message = message
        self.code = code


class TokenProvider(Protocol):
    async def token(self) -> str: ...


@dataclass(slots=True)
class AzureCliTokenProvider:
    """Caches an Azure CLI token for the environment until five minutes before it expires."""

    environment: str
    # Empty: the Azure CLI's default tenant.
    tenant_id: str = ""
    _token: str = ""
    _expires_on: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def token(self) -> str:
        async with self._lock:
            if not self._token or time.time() > self._expires_on - 300:
                from azure.identity import AzureCliCredential

                credential = AzureCliCredential(tenant_id=self.tenant_id or None)
                scope = self.environment.rstrip("/") + "/.default"
                access = await asyncio.to_thread(credential.get_token, scope)
                self._token, self._expires_on = access.token, float(access.expires_on)
            return self._token


@dataclass(frozen=True, slots=True)
class BatchRequest:
    method: str
    path: str
    body: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class BatchResponse:
    status: int
    body: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def error(self) -> str:
        if self.body and isinstance(self.body.get("error"), dict):
            return str(self.body["error"].get("message", ""))
        return "" if self.ok else f"HTTP {self.status}"


class WebApi:
    def __init__(
        self,
        environment: str,
        *,
        tokens: TokenProvider | None = None,
        tenant_id: str = "",
        timeout_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.environment = environment.rstrip("/") + "/"
        self.tokens = tokens or AzureCliTokenProvider(self.environment, tenant_id)
        self._client = httpx.AsyncClient(
            base_url=self.environment + API, timeout=timeout_s, transport=transport
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await self.tokens.token()}",
            "Accept": "application/json",
            "OData-Version": "4.0",
            "OData-MaxVersion": "4.0",
            **(extra or {}),
        }

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        response = await self._client.request(
            method, path, params=params, json=body, headers=await self._headers(headers)
        )
        if response.status_code >= 400:
            raise _error(response.status_code, response.text)
        return response

    async def get(
        self, path: str, params: dict[str, str] | None = None, *, page_size: int = 5000
    ) -> dict[str, Any]:
        prefer = f"{FORMATTED},odata.maxpagesize={page_size}"
        response = await self._send("GET", path, params=params, headers={"Prefer": prefer})
        return response.json()

    async def get_all(
        self, path: str, params: dict[str, str] | None = None, *, limit: int | None = None
    ) -> dict[str, Any]:
        """GET a collection, following ``@odata.nextLink`` until ``limit`` rows are read."""

        body = await self.get(path, params)
        rows: list[dict[str, Any]] = body.get("value", [])
        next_link = body.pop("@odata.nextLink", None)
        while next_link and (limit is None or len(rows) < limit):
            page = await self.get(str(next_link).split(API, 1)[1])
            rows.extend(page.get("value", []))
            next_link = page.get("@odata.nextLink")
        if limit is not None and len(rows) > limit:
            del rows[limit:]
            body["truncated"] = True
        elif next_link:
            body["truncated"] = True
        body["value"] = rows
        return body

    async def create(self, entity_set: str, values: dict[str, Any]) -> str:
        response = await self._send("POST", entity_set, body=values)
        entity_id = response.headers.get("OData-EntityId", "")
        match = re.search(r"\(([0-9a-fA-F-]{36})\)\s*$", entity_id)
        if match is None:
            raise DataverseError(response.status_code, f"no record id in {entity_id!r}")
        return match.group(1).lower()

    async def update(self, entity_set: str, record_id: str, values: dict[str, Any]) -> None:
        path = f"{entity_set}({record_id})"
        await self._send("PATCH", path, body=values, headers={"If-Match": "*"})

    async def delete(self, entity_set: str, record_id: str) -> None:
        await self._send("DELETE", f"{entity_set}({record_id})")

    async def batch(self, requests: Sequence[BatchRequest], *, atomic: bool) -> list[BatchResponse]:
        """Send requests through ``$batch``, ``BATCH_LIMIT`` at a time.

        An atomic batch is one change set: if any request fails, Dataverse rolls back that change
        set and answers with the failing response only. Otherwise every request runs and reports
        its own status.
        """

        responses: list[BatchResponse] = []
        for start in range(0, len(requests), BATCH_LIMIT):
            chunk = requests[start : start + BATCH_LIMIT]
            boundary = f"batch_{uuid.uuid4().hex}"
            payload = _batch_body(self.environment + API, chunk, boundary, atomic=atomic)
            headers = {
                "Content-Type": f"multipart/mixed; boundary={boundary}",
                **({} if atomic else {"Prefer": "odata.continue-on-error"}),
            }
            response = await self._client.post(
                "$batch", content=payload.encode("utf-8"), headers=await self._headers(headers)
            )
            if response.status_code >= 400 and "multipart" not in response.headers.get(
                "Content-Type", ""
            ):
                raise _error(response.status_code, response.text)
            parsed = _parse_batch(response.text)
            if atomic and len(parsed) < len(chunk):
                failure = next((p for p in parsed if not p.ok), None)
                raise DataverseError(
                    failure.status if failure else response.status_code,
                    failure.error if failure else "batch rolled back",
                )
            responses.extend(parsed)
        return responses


def _error(status: int, text: str) -> DataverseError:
    try:
        error = json.loads(text).get("error", {})
    except (json.JSONDecodeError, AttributeError):
        return DataverseError(status, text[:500])
    return DataverseError(status, str(error.get("message", ""))[:1000], str(error.get("code", "")))


def _batch_body(base: str, requests: Sequence[BatchRequest], boundary: str, *, atomic: bool) -> str:
    def part(index: int, request: BatchRequest) -> list[str]:
        lines = [
            "Content-Type: application/http",
            "Content-Transfer-Encoding: binary",
            f"Content-ID: {index + 1}",
            "",
            f"{request.method} {base}{request.path} HTTP/1.1",
        ]
        if request.method in ("PATCH", "DELETE"):
            lines.append("If-Match: *")
        if request.body is not None:
            lines += ["Content-Type: application/json", "", json.dumps(request.body)]
        else:
            lines.append("")
        return lines

    lines: list[str] = []
    if atomic:
        changeset = f"changeset_{uuid.uuid4().hex}"
        lines += [f"--{boundary}", f"Content-Type: multipart/mixed; boundary={changeset}", ""]
        for index, request in enumerate(requests):
            lines += [f"--{changeset}", *part(index, request)]
        lines.append(f"--{changeset}--")
    else:
        for index, request in enumerate(requests):
            lines += [f"--{boundary}", *part(index, request)]
    lines += [f"--{boundary}--", ""]
    return "\r\n".join(lines)


_STATUS = re.compile(r"^HTTP/1\.1 (\d{3})")


def _parse_batch(text: str) -> list[BatchResponse]:
    """Read the status and JSON body of each response in a ``$batch`` reply, in order."""

    responses: list[BatchResponse] = []
    status: int | None = None
    body: list[str] = []
    in_body = False

    def flush() -> None:
        if status is None:
            return
        raw = "\n".join(body).strip()
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = {"error": {"message": raw[:500]}}
        responses.append(BatchResponse(status, parsed if isinstance(parsed, dict) else None))

    for line in text.splitlines():
        if line.startswith("--"):
            flush()
            status, body, in_body = None, [], False
            continue
        match = _STATUS.match(line)
        if match:
            status, body, in_body = int(match.group(1)), [], False
        elif status is not None:
            if in_body:
                body.append(line)
            elif not line.strip():
                in_body = True
    flush()
    return responses
