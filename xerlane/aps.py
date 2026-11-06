"""Autodesk Platform Services (APS) connector: pull model object data and load it into the same Postgres schema as the XER loader.

Written from the APS documentation and the vendor OpenAPI files (see docs/aps.md) and tested against a stand-in server; it has never been run against a live Autodesk
account. A live run needs APS_CLIENT_ID and APS_CLIENT_SECRET in the environment.

Three parts, each usable on its own:
  ApsClient      two-legged OAuth, Model Derivative "List Model Views" and "Fetch All Properties", with the documented 202 polling, 429 Retry-After and 5xx handling
  map_collection one properties collection -> rows to store and items to set aside (the quarantine), each with a reason
  load_view      upsert the rows of one model view for one tenant in one transaction, with a change log, a quarantine and a no-op second run
The XER side is untouched: an object joins a task through the nullable task_code column (view aps_object_task) and reconcile() reports what does not match.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import logging
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Callable

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from . import db
from .db import tenant_tx

log = logging.getLogger("xerlane.aps")

DEFAULT_BASE_URL = "https://developer.api.autodesk.com"
TOKEN_PATH = "/authentication/v2/token"
MD_PATH = "/modelderivative/v2/designdata"
DEFAULT_SCOPES = ("data:read", "viewables:read")
REGIONS = ("US", "EMEA", "AUS")
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")

ENV_ID, ENV_SECRET, ENV_BASE, ENV_REGION = "APS_CLIENT_ID", "APS_CLIENT_SECRET", "APS_BASE_URL", "APS_REGION"

MAX_PROPERTIES_BYTES = 64 * 1024        # one object's whole properties tree; a real Revit element is a few KB
MAX_TEXT_CHARS = 2048                   # name and externalId
MAX_RAW_BYTES = 4 * 1024                # a set-aside item bigger than this is stored as a short stub, not whole
MAX_BODY_BYTES = 256 * 1024 * 1024      # one HTTP response; APS itself refuses resources over 20 MB unless forceget is sent
MAX_ID = 2**63 - 1
TASK_CODE_KEYS = ("task_code", "Task Code")
URN_RE = re.compile(r"[A-Za-z0-9_\-]+=*")
GUID_RE = re.compile(r"[A-Za-z0-9_\-]{1,64}")


# ---- errors --------------------------------------------------------------------------------------------------------------------------------------

class ApsError(RuntimeError):
    """Base of every error this module raises on purpose."""


class ApsConfigError(ApsError):
    """Missing credentials, an unsafe base URL or a bad argument."""


class ApsHttpError(ApsError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class ApsAuthError(ApsHttpError):
    """The credentials or the token were refused (401, 403)."""


class ApsRateLimited(ApsError):
    def __init__(self, message: str, retry_after: float | None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ApsNotReady(ApsError):
    """The service kept answering 202 for as long as we were willing to wait."""


class ApsPayloadError(ApsError):
    """A 200 answer that is not the documented shape."""


class _Transient(Exception):
    """A network level failure worth a retry."""


# ---- client --------------------------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelView:
    name: str | None
    role: str | None
    guid: str
    is_master: bool = False


@dataclass(frozen=True)
class _Resp:
    status: int
    headers: Any
    body: bytes


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the bearer token must only ever go to the host we were told to use."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _retry_after(headers: Any) -> float | None:
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def urn_from_object_id(object_id: str) -> str:
    """The URL-safe Base64 (no padding) form of an OSS object id such as urn:adsk.objects:os.object:bucket/file.rvt, which is what Model Derivative expects as {urn}."""
    return base64.urlsafe_b64encode(object_id.encode("utf-8")).decode("ascii").rstrip("=")


class ApsClient:
    """Two-legged OAuth plus the two Model Derivative reads. Standard library HTTP only. The secret and the token never reach a log line or an error message."""

    def __init__(self, client_id: str, client_secret: str, *, base_url: str = DEFAULT_BASE_URL, scopes: tuple[str, ...] = DEFAULT_SCOPES, region: str | None = None,
                 timeout: float = 30.0, max_retries: int = 5, backoff_base: float = 0.5, backoff_cap: float = 10.0, max_retry_after: float = 120.0,
                 max_polls: int = 12, poll_base: float = 1.0, poll_cap: float = 15.0, refresh_margin: float = 120.0, max_body_bytes: int = MAX_BODY_BYTES,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        if not client_id or not client_secret:
            raise ApsConfigError(f"{ENV_ID} and {ENV_SECRET} are both required")
        if region is not None and region not in REGIONS:
            raise ApsConfigError(f"region must be one of {', '.join(REGIONS)}")
        self._id, self._secret = client_id, client_secret
        self._basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
        self._base = self._check_base(base_url)
        self._scopes, self._region = tuple(scopes), region
        self._timeout, self._max_retries = timeout, max_retries
        self._backoff_base, self._backoff_cap, self._max_retry_after = backoff_base, backoff_cap, max_retry_after
        self._max_polls, self._poll_base, self._poll_cap = max_polls, poll_base, poll_cap
        self._refresh_margin, self._max_body = refresh_margin, max_body_bytes
        self._clock, self._sleep = clock, sleep
        self._token: str | None = None
        self._refresh_at = 0.0
        host = urllib.parse.urlsplit(self._base).hostname
        handlers: list[Any] = [_NoRedirect()]
        if host in LOOPBACK_HOSTS:
            handlers.append(urllib.request.ProxyHandler({}))         # a system proxy must never be asked to reach the loopback stand-in
        self._opener = urllib.request.build_opener(*handlers)

    @classmethod
    def from_env(cls, **kwargs: Any) -> "ApsClient":
        return cls(os.environ.get(ENV_ID, ""), os.environ.get(ENV_SECRET, ""), base_url=os.environ.get(ENV_BASE) or DEFAULT_BASE_URL,
                   region=os.environ.get(ENV_REGION) or None, **kwargs)

    @staticmethod
    def _check_base(base_url: str) -> str:
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme == "https" and parts.hostname:
            return base_url.rstrip("/")
        if parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS:
            return base_url.rstrip("/")
        raise ApsConfigError("base URL must be https (plain http is accepted for localhost only, because the credentials travel in the request)")

    # -- plumbing

    def _scrub(self, text: str) -> str:
        for secret in (self._secret, self._basic, self._token):
            if secret:
                text = text.replace(secret, "***")
        return text

    def _once(self, method: str, url: str, headers: dict[str, str], data: bytes | None) -> _Resp:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                return _Resp(resp.status, resp.headers, self._read(resp))
        except urllib.error.HTTPError as exc:
            try:
                return _Resp(exc.code, exc.headers, exc.read(2000))
            except OSError:
                return _Resp(exc.code, exc.headers, b"")
            finally:
                exc.close()
        except (OSError, http.client.HTTPException) as exc:             # URLError, timeouts and resets are all OSError
            raise _Transient(type(exc).__name__) from None

    def _read(self, resp: Any) -> bytes:
        body = resp.read(self._max_body + 1)
        if len(body) > self._max_body:
            raise ApsPayloadError(f"response is larger than {self._max_body} bytes")
        return body

    def _send(self, method: str, url: str, headers: dict[str, str], data: bytes | None = None, *, what: str) -> _Resp:
        """One request with the documented retry rules: 429 waits Retry-After, 5xx and network errors back off, every other 4xx is final."""
        attempt = 0
        while True:
            status: int | None = None
            wait: float | None = None
            try:
                resp = self._once(method, url, headers, data)
            except _Transient as exc:
                delay, why = self._backoff(attempt), f"network error {exc}"
            else:
                status = resp.status
                if status < 300:
                    log.debug("%s -> HTTP %d", what, status)
                    return resp
                if status == 429:
                    wait = _retry_after(resp.headers)
                    if wait is not None and wait > self._max_retry_after:
                        raise ApsRateLimited(f"{what}: rate limited, Retry-After {wait:g} s is over the {self._max_retry_after:g} s limit", wait)
                    delay, why = (self._backoff(attempt) if wait is None else wait), "HTTP 429"
                elif status >= 500:
                    delay, why = self._backoff(attempt), f"HTTP {status}"
                else:
                    raise self._http_error(resp, what)
            attempt += 1
            if attempt > self._max_retries:
                raise self._gave_up(what, status, wait)
            log.info("%s: %s, retry %d of %d in %.1f s", what, why, attempt, self._max_retries, delay)
            self._sleep(delay)

    def _gave_up(self, what: str, status: int | None, wait: float | None) -> ApsError:
        end = f"gave up after {self._max_retries} retries"
        if status is None:
            return ApsError(f"{what}: network error, {end}")
        if status == 429:
            return ApsRateLimited(f"{what}: still rate limited, {end}", wait)
        return ApsHttpError(status, f"{what}: HTTP {status}, {end}")

    def _backoff(self, attempt: int) -> float:
        return min(self._backoff_cap, self._backoff_base * 2**attempt)

    def _http_error(self, resp: _Resp, what: str) -> ApsHttpError:
        detail = self._scrub(resp.body.decode("utf-8", "replace")[:300]).replace("\n", " ")
        cls = ApsAuthError if resp.status in (401, 403) else ApsHttpError
        return cls(resp.status, f"{what}: HTTP {resp.status} {detail}".rstrip())

    @staticmethod
    def _json(body: bytes, what: str) -> Any:
        try:
            return json.loads(body.decode("utf-8"), parse_constant=_no_constant)
        except (ValueError, UnicodeDecodeError):
            raise ApsPayloadError(f"{what}: the answer is not valid JSON") from None

    # -- token

    def token(self) -> str:
        """The current bearer token; a new one is fetched when there is none or it is within the refresh margin of its expiry."""
        if self._token is None or self._clock() >= self._refresh_at:
            self._fetch_token()
        assert self._token is not None
        return self._token

    def _fetch_token(self) -> None:
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": " ".join(self._scopes)}).encode("ascii")
        headers = {"Authorization": f"Basic {self._basic}", "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        self._token = None
        try:
            resp = self._send("POST", self._base + TOKEN_PATH, headers, body, what="token request")
        except ApsHttpError as exc:
            if exc.status in (400, 401, 403):
                raise ApsAuthError(exc.status, f"token request refused with HTTP {exc.status}: check the client id, the secret and the scopes") from None
            raise
        doc = self._json(resp.body, "token request")
        token, lifetime = (doc.get("access_token"), doc.get("expires_in")) if isinstance(doc, dict) else (None, None)
        if not isinstance(token, str) or not token or isinstance(lifetime, bool) or not isinstance(lifetime, (int, float)) or not lifetime > 0:
            raise ApsPayloadError("token request: the answer has no access_token and a positive expires_in")
        margin = min(self._refresh_margin, lifetime / 2)               # a short lived token is refreshed halfway, never on every call
        self._token, self._refresh_at = token, self._clock() + lifetime - margin
        log.info("access token acquired, valid for %g s", lifetime)

    # -- Model Derivative

    def _get(self, path: str) -> _Resp:
        refreshed = False
        while True:
            headers = {"Authorization": f"Bearer {self.token()}", "Accept": "application/json"}
            if self._region:
                headers["region"] = self._region
            try:
                return self._send("GET", self._base + path, headers, what=f"GET {path}")
            except ApsHttpError as exc:
                if exc.status == 401 and not refreshed:                  # expired or revoked since we cached it: one new token, one more try
                    refreshed, self._token = True, None
                    log.info("token refused with HTTP 401, fetching a new one")
                    continue
                raise

    @staticmethod
    def _check_urn(urn: str) -> str:
        if not URN_RE.fullmatch(urn or ""):
            raise ApsConfigError("urn must be the URL-safe Base64 form of the object id (letters, digits, - and _)")
        return urn

    def list_views(self, urn: str) -> list[ModelView]:
        """GET /modelderivative/v2/designdata/{urn}/metadata: the model views (viewables) of a translated design and their guids."""
        resp = self._get(f"{MD_PATH}/{self._check_urn(urn)}/metadata")
        doc = self._json(resp.body, "list model views")
        data = doc.get("data") if isinstance(doc, dict) else None
        items = data.get("metadata") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ApsPayloadError("list model views: no data.metadata list in the answer")
        views = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("guid"), str) or not GUID_RE.fullmatch(item["guid"]):
                raise ApsPayloadError("list model views: an entry has no usable guid")
            views.append(ModelView(item.get("name"), item.get("role"), item["guid"], item.get("isMasterView") is True))
        return views

    def get_properties(self, urn: str, guid: str) -> list[Any]:
        """GET .../metadata/{guid}/properties: data.collection, one entry per object. The service answers 202 while it still extracts them, so the request is repeated
        with a growing, capped wait; after max_polls waits we give up with ApsNotReady instead of hanging."""
        if not GUID_RE.fullmatch(guid or ""):
            raise ApsConfigError("guid must be letters, digits, - and _")
        path = f"{MD_PATH}/{self._check_urn(urn)}/metadata/{guid}/properties"
        for poll in range(self._max_polls + 1):
            resp = self._get(path)
            if resp.status == 202:
                if poll < self._max_polls:
                    delay = min(self._poll_cap, self._poll_base * 2**poll)
                    log.info("properties of view %s are still being extracted (HTTP 202), asking again in %.1f s", guid, delay)
                    self._sleep(delay)
                continue
            doc = self._json(resp.body, "fetch properties")
            data = doc.get("data") if isinstance(doc, dict) else None
            collection = data.get("collection") if isinstance(data, dict) else None
            if not isinstance(collection, list):
                raise ApsPayloadError("fetch properties: no data.collection list in the answer")
            return collection
        raise ApsNotReady(f"properties of view {guid} were still not ready after {self._max_polls} waits")


# ---- mapping -------------------------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ObjectRow:
    model_urn: str
    view_guid: str
    object_id: int
    external_id: str | None
    name: str | None
    category: str | None
    task_code: str | None
    properties: dict[str, Any]
    content_hash: str


@dataclass(frozen=True)
class Reject:
    item_index: int
    code: str
    reason: str
    raw: Any


@dataclass
class Mapped:
    rows: list[ObjectRow] = field(default_factory=list)
    rejects: list[Reject] = field(default_factory=list)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(external_id: str | None, name: str | None, category: str | None, task_code: str | None, properties: dict[str, Any]) -> str:
    """SHA-256 of everything stored about an object except its key, so a changed row is found by one string comparison."""
    return hashlib.sha256(_canonical([external_id, name, category, task_code, properties]).encode("utf-8")).hexdigest()


def payload_digest(collection: list[Any]) -> str:
    return hashlib.sha256(_canonical(collection).encode("utf-8")).hexdigest()


def _unstorable(value: Any) -> bool:
    """True when some string in `value` holds a NUL or an unpaired surrogate: Postgres text and jsonb cannot store either, and one such value would abort the whole load."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if "\x00" in item:
                return True
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                return True
        elif isinstance(item, dict):
            for key, val in item.items():
                stack.extend((key, val))
        elif isinstance(item, list):
            stack.extend(item)
    return False


def _whole(value: Any) -> int | None:
    """An object id as an int, or None when it is not a whole number in 0..2**63-1 (JSON has one number type, so 7.0 is accepted as 7)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and 0 <= value <= MAX_ID:
        return value
    return None


def _category(props: dict[str, Any]) -> str | None:
    """The category when the model carries one. NOT CONFIRMED against a live translation (docs/aps.md): we look in a __category__ group, then at the top level."""
    group = props.get("__category__")
    for value in ((group.get("Category") if isinstance(group, dict) else None), props.get("Category")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def find_task_code(props: dict[str, Any], keys: tuple[str, ...] = TASK_CODE_KEYS) -> str | None:
    """The XER task code the model carries for an object: a property named like one of `keys` at the top level or one group down (APS groups properties by category)."""
    scopes = [props] + [v for v in props.values() if isinstance(v, dict)]
    for scope in scopes:
        for key in keys:
            value = scope.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
    return None


def _stub(item: Any, why: str) -> dict[str, Any]:
    out: dict[str, Any] = {why: True}
    if isinstance(item, dict) and _whole(item.get("objectid")) is not None:
        out["objectid"] = _whole(item["objectid"])
    return out


def _raw_for(item: Any) -> Any:
    """What the quarantine keeps of a bad item: the item itself, or a short stub when it is huge or holds text Postgres cannot store."""
    if _unstorable(item):
        return _stub(item, "unstorable")
    size = len(_canonical(item).encode("utf-8"))
    if size > MAX_RAW_BYTES:
        return {**_stub(item, "truncated"), "bytes": size}
    return item


def _check_item(index: int, item: Any, urn: str, guid: str, max_props: int, task_keys: tuple[str, ...]) -> ObjectRow | Reject:
    def bad(code: str, reason: str) -> Reject:
        return Reject(index, code, reason, _raw_for(item))

    if not isinstance(item, dict):
        return bad("MALFORMED_OBJECT", "the entry is not a JSON object")
    raw_id = item.get("objectid")
    if raw_id is None:
        return bad("NO_OBJECT_ID", "the entry has no objectid")
    object_id = _whole(raw_id)
    if object_id is None:
        return bad("BAD_OBJECT_ID", "objectid is not a whole number from 0 to 2**63-1")
    external_id, name, props = item.get("externalId"), item.get("name"), item.get("properties")
    if not all(v is None or isinstance(v, str) for v in (external_id, name)):
        return bad("BAD_FIELD_TYPE", "name and externalId must be text")
    if props is None:
        props = {}
    if not isinstance(props, dict):
        return bad("BAD_PROPERTIES", "properties is not a JSON object")
    if _unstorable(item):
        return bad("BAD_CHARACTER", "text holds a NUL or an unpaired surrogate, which Postgres cannot store")
    if any(v is not None and len(v) > MAX_TEXT_CHARS for v in (external_id, name)):
        return bad("VALUE_TOO_LARGE", f"name or externalId is longer than {MAX_TEXT_CHARS} characters")
    size = len(_canonical(props).encode("utf-8"))
    if size > max_props:
        return bad("VALUE_TOO_LARGE", f"properties are {size} bytes, over the {max_props} byte limit")
    category, task_code = _category(props), find_task_code(props, task_keys)
    return ObjectRow(urn, guid, object_id, external_id, name, category, task_code, props, content_hash(external_id, name, category, task_code, props))


def map_collection(urn: str, guid: str, collection: list[Any], *, max_properties_bytes: int = MAX_PROPERTIES_BYTES, task_code_keys: tuple[str, ...] = TASK_CODE_KEYS) -> Mapped:
    """Turn one view's properties collection into rows to store and items to set aside. An objectid used by more than one valid entry is ambiguous, so every entry
    that uses it is set aside (DUP_OBJECT_ID) and none is stored."""
    checked = [_check_item(i, item, urn, guid, max_properties_bytes, task_code_keys) for i, item in enumerate(collection)]
    seen: dict[int, int] = {}
    for c in checked:
        if isinstance(c, ObjectRow):
            seen[c.object_id] = seen.get(c.object_id, 0) + 1
    out = Mapped()
    for i, c in enumerate(checked):
        if isinstance(c, Reject):
            out.rejects.append(c)
        elif seen[c.object_id] > 1:
            out.rejects.append(Reject(i, "DUP_OBJECT_ID", f"objectid {c.object_id} appears {seen[c.object_id]} times in this view", _raw_for(collection[i])))
        else:
            out.rows.append(c)
    return out


# ---- schema --------------------------------------------------------------------------------------------------------------------------------------

def init_schema(admin_dsn: str, *, schema: str = db.SCHEMA, app_role: str = db.APP_ROLE, owner_role: str = db.OWNER_ROLE) -> None:
    """Create the APS tables and view and grant the app role what it needs. Run db.init_db first (it makes the roles and the schema). Safe to run again."""
    ddl = resources.files("xerlane").joinpath("aps_schema.sql").read_text(encoding="utf-8")
    sch, owner, app = sql.Identifier(schema), sql.Identifier(owner_role), sql.Identifier(app_role)
    with psycopg.connect(admin_dsn) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (db.INIT_LOCK,))
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(owner))
        conn.execute(sql.SQL("SET LOCAL search_path = {}").format(sch))
        conn.execute(ddl)
        conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(sch, app))
        conn.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(sch, app))


# ---- load ----------------------------------------------------------------------------------------------------------------------------------------

@dataclass
class ApsLoadResult:
    model_urn: str
    view_guid: str
    status: str                        # changed | unchanged
    added: int = 0
    changed: int = 0
    removed: int = 0
    unchanged: int = 0
    quarantined: int = 0               # set aside in this payload
    quarantined_new: int = 0           # of those, rows written now (the same payload again writes none)
    run_id: int | None = None


def view_lock_key(tenant: str, urn: str, guid: str) -> int:
    """The advisory lock number of one view of one tenant: two loads of the same view take turns, so the second sees the first one's rows instead of racing it."""
    digest = hashlib.sha256(f"{tenant}\0{urn}\0{guid}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _changed_fields(have: tuple, want: ObjectRow) -> str:
    """Which stored columns differ, for the change log. The hash says THAT a row changed; this says what a reader should look at."""
    names = ("external_id", "name", "category", "task_code", "properties")
    fields = [n for n, old, new in zip(names, have[1:6], (want.external_id, want.name, want.category, want.task_code, want.properties)) if old != new]
    return ",".join(fields)


def load_view(conn: psycopg.Connection, tenant: str, model_urn: str, view_guid: str, collection: list[Any], *, allow_empty: bool = False,
              max_properties_bytes: int = MAX_PROPERTIES_BYTES, task_code_keys: tuple[str, ...] = TASK_CODE_KEYS) -> ApsLoadResult:
    """Bring the stored objects of one model view in line with a properties collection, in one transaction under the tenant's row level security.

    New objects are inserted, objects whose content hash differs are updated, stored objects missing from the payload are deleted, and each change is written to
    aps_change_event under one aps_load_run row. Items that fail validation go to aps_quarantine. Loading the same payload again writes nothing. A payload that holds no
    valid object while objects are stored would delete them all; that is refused unless allow_empty is set."""
    if not isinstance(collection, list):
        raise ApsPayloadError("the properties collection must be a list")
    mapped = map_collection(model_urn, view_guid, collection, max_properties_bytes=max_properties_bytes, task_code_keys=task_code_keys)
    sha = payload_digest(collection)
    key = (tenant, model_urn, view_guid)
    with tenant_tx(conn, tenant) as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (view_lock_key(*key),))
        cur.execute("SELECT object_id, external_id, name, category, task_code, properties, content_hash FROM aps_object WHERE tenant_id = %s AND model_urn = %s AND view_guid = %s", key)
        have = {r[0]: r for r in cur.fetchall()}
        want = {r.object_id: r for r in mapped.rows}
        if have and not want and not allow_empty:
            raise ApsPayloadError(f"the payload holds no valid object but {len(have)} are stored for this view; loading it would delete them all (allow_empty overrides)")
        added = sorted(want.keys() - have.keys())
        removed = sorted(have.keys() - want.keys())
        changed = sorted(i for i in want.keys() & have.keys() if want[i].content_hash != have[i][6])
        cur.execute("SELECT item_index FROM aps_quarantine WHERE tenant_id = %s AND model_urn = %s AND view_guid = %s AND payload_sha256 = %s", (*key, sha))
        known = {r[0] for r in cur.fetchall()}
        fresh = [r for r in mapped.rejects if r.item_index not in known]
        result = ApsLoadResult(model_urn, view_guid, "unchanged", len(added), len(changed), len(removed), len(want) - len(added) - len(changed),
                               len(mapped.rejects), len(fresh))
        if not (added or changed or removed or fresh):
            return result
        counts = {"added": result.added, "changed": result.changed, "removed": result.removed, "unchanged": result.unchanged, "quarantined": result.quarantined_new}
        cur.execute("INSERT INTO aps_load_run (tenant_id, model_urn, view_guid, payload_sha256, counts) VALUES (%s, %s, %s, %s, %s) RETURNING run_id",
                    (*key, sha, Jsonb(counts)))
        run_id = cur.fetchone()[0]
        events: list[tuple] = []
        for i in added:
            w = want[i]
            events.append((tenant, run_id, "OBJECT_ADDED", model_urn, view_guid, i, w.external_id, w.name, None, None, w.content_hash))
        for i in changed:
            w = want[i]
            events.append((tenant, run_id, "OBJECT_CHANGED", model_urn, view_guid, i, w.external_id, w.name, _changed_fields(have[i], w), have[i][6], w.content_hash))
        for i in removed:
            h = have[i]
            events.append((tenant, run_id, "OBJECT_REMOVED", model_urn, view_guid, i, h[1], h[2], None, h[6], None))
        cur.executemany("INSERT INTO aps_object (tenant_id, model_urn, view_guid, object_id, external_id, name, category, task_code, properties, content_hash) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        [(tenant, model_urn, view_guid, w.object_id, w.external_id, w.name, w.category, w.task_code, Jsonb(w.properties), w.content_hash)
                         for w in (want[i] for i in added)])
        cur.executemany("UPDATE aps_object SET external_id = %s, name = %s, category = %s, task_code = %s, properties = %s, content_hash = %s "
                        "WHERE tenant_id = %s AND model_urn = %s AND view_guid = %s AND object_id = %s",
                        [(w.external_id, w.name, w.category, w.task_code, Jsonb(w.properties), w.content_hash, tenant, model_urn, view_guid, w.object_id)
                         for w in (want[i] for i in changed)])
        if removed:
            cur.execute("DELETE FROM aps_object WHERE tenant_id = %s AND model_urn = %s AND view_guid = %s AND object_id = ANY(%s)", (*key, removed))
        cur.executemany("INSERT INTO aps_change_event (tenant_id, run_id, kind, model_urn, view_guid, object_id, external_id, name, fields, old_hash, new_hash) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", events)
        cur.executemany("INSERT INTO aps_quarantine (tenant_id, model_urn, view_guid, payload_sha256, item_index, reason_code, reason, raw) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        [(*key, sha, r.item_index, r.code, r.reason, Jsonb(r.raw)) for r in fresh])
        result.status, result.run_id = "changed", run_id
        return result


@dataclass
class ViewOutcome:
    view_guid: str
    name: str | None
    role: str | None
    status: str                        # changed | unchanged | failed
    load: ApsLoadResult | None = None
    error: str | None = None


def sync_model(client: ApsClient, conn: psycopg.Connection, tenant: str, urn: str, *, roles: tuple[str, ...] | None = None, allow_empty: bool = False) -> list[ViewOutcome]:
    """Pull every model view of a translated design (or only those whose role is in `roles`, e.g. ("3d",)) and load each one. A view the service cannot deliver is
    reported as failed and the other views still load; a database error is not caught."""
    out: list[ViewOutcome] = []
    for view in client.list_views(urn):
        if roles is not None and view.role not in roles:
            continue
        try:
            collection = client.get_properties(urn, view.guid)
            res = load_view(conn, tenant, urn, view.guid, collection, allow_empty=allow_empty)
        except ApsError as exc:
            log.warning("view %s failed: %s", view.guid, exc)
            out.append(ViewOutcome(view.guid, view.name, view.role, "failed", error=str(exc)))
            continue
        out.append(ViewOutcome(view.guid, view.name, view.role, res.status, res))
    return out


# ---- reconcile -----------------------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ObjectRef:
    model_urn: str
    view_guid: str
    object_id: int
    external_id: str | None
    name: str | None
    task_code: str | None


@dataclass(frozen=True)
class TaskRef:
    proj_id: int
    task_id: int
    task_code: str
    task_name: str | None


@dataclass
class ApsReconcileReport:
    objects: int                                     # objects in scope
    matched_objects: int                             # objects whose task_code matches a task
    no_task_code: int                                # objects the model gives no task code
    no_task: int                                     # objects with a task code that no task carries
    tasks: int                                       # tasks in scope
    tasks_without_object: int
    objects_without_task_code: list[ObjectRef] = field(default_factory=list)       # the lists below are cut at `limit`; the counts above are exact
    objects_without_task: list[ObjectRef] = field(default_factory=list)
    tasks_missing_object: list[TaskRef] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.no_task_code or self.no_task or self.tasks_without_object)

    def render(self) -> str:
        lines = [f"objects {self.objects}: {self.matched_objects} matched a task, {self.no_task} carry a task code no task has, {self.no_task_code} carry no task code",
                 f"tasks {self.tasks}: {self.tasks_without_object} have no object"]
        lines += [f"  object without task code: {o.model_urn[:12]}.. view {o.view_guid} object {o.object_id} {o.name or ''}".rstrip() for o in self.objects_without_task_code]
        lines += [f"  object without task: code {o.task_code} view {o.view_guid} object {o.object_id} {o.name or ''}".rstrip() for o in self.objects_without_task]
        lines += [f"  task without object: {t.task_code} (project {t.proj_id}, task {t.task_id}) {t.task_name or ''}".rstrip() for t in self.tasks_missing_object]
        return "\n".join(lines)


def reconcile(conn: psycopg.Connection, tenant: str, *, model_urn: str | None = None, proj_id: int | None = None, limit: int = 1000) -> ApsReconcileReport:
    """Compare the loaded APS objects with the loaded XER tasks of one tenant, without touching either. `model_urn` limits the objects, `proj_id` limits the tasks.
    A task code is unique only inside one project, so with more than one project loaded pass proj_id."""
    obj_where, obj_args = "o.tenant_id = %s AND (%s::text IS NULL OR o.model_urn = %s)", (tenant, model_urn, model_urn)
    task_where, task_args = "t.tenant_id = %s AND (%s::bigint IS NULL OR t.proj_id = %s)", (tenant, proj_id, proj_id)
    has_task = "EXISTS (SELECT 1 FROM task t WHERE t.tenant_id = o.tenant_id AND t.task_code = o.task_code AND (%s::bigint IS NULL OR t.proj_id = %s))"
    has_obj = "EXISTS (SELECT 1 FROM aps_object o WHERE o.tenant_id = t.tenant_id AND o.task_code = t.task_code AND (%s::text IS NULL OR o.model_urn = %s))"
    obj_cols = "o.model_urn, o.view_guid, o.object_id, o.external_id, o.name, o.task_code"
    with tenant_tx(conn, tenant) as cur:
        def one(query: str, args: tuple) -> int:
            cur.execute(query, args)
            return cur.fetchone()[0]

        objects = one(f"SELECT count(*) FROM aps_object o WHERE {obj_where}", obj_args)
        matched = one(f"SELECT count(*) FROM aps_object o WHERE {obj_where} AND o.task_code IS NOT NULL AND {has_task}", (*obj_args, proj_id, proj_id))
        no_code = one(f"SELECT count(*) FROM aps_object o WHERE {obj_where} AND o.task_code IS NULL", obj_args)
        tasks = one(f"SELECT count(*) FROM task t WHERE {task_where}", task_args)
        orphans = one(f"SELECT count(*) FROM task t WHERE {task_where} AND NOT {has_obj}", (*task_args, model_urn, model_urn))
        cur.execute(f"SELECT {obj_cols} FROM aps_object o WHERE {obj_where} AND o.task_code IS NULL ORDER BY o.model_urn, o.view_guid, o.object_id LIMIT %s", (*obj_args, limit))
        no_code_rows = [ObjectRef(*r) for r in cur.fetchall()]
        cur.execute(f"SELECT {obj_cols} FROM aps_object o WHERE {obj_where} AND o.task_code IS NOT NULL AND NOT {has_task} ORDER BY o.model_urn, o.view_guid, o.object_id LIMIT %s",
                    (*obj_args, proj_id, proj_id, limit))
        no_task_rows = [ObjectRef(*r) for r in cur.fetchall()]
        cur.execute(f"SELECT t.proj_id, t.task_id, t.task_code, t.task_name FROM task t WHERE {task_where} AND NOT {has_obj} ORDER BY t.proj_id, t.task_id LIMIT %s",
                    (*task_args, model_urn, model_urn, limit))
        orphan_rows = [TaskRef(*r) for r in cur.fetchall()]
    return ApsReconcileReport(objects, matched, no_code, objects - matched - no_code, tasks, orphans, no_code_rows, no_task_rows, orphan_rows)


# ---- command line --------------------------------------------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """python -m xerlane.aps sync|reconcile|init-schema. Credentials come from APS_CLIENT_ID and APS_CLIENT_SECRET, the database from the usual XERLANE_* settings."""
    ap = argparse.ArgumentParser(prog="python -m xerlane.aps", description=main.__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-schema", help="create the APS tables (administrator connection)")
    s = sub.add_parser("sync", help="pull a translated model's object properties and load them")
    s.add_argument("--tenant", required=True)
    s.add_argument("--urn", required=True, help="the URL-safe Base64 URN of the translated design")
    s.add_argument("--roles", help="comma separated view roles to load, e.g. 3d (default: every view)")
    s.add_argument("--allow-empty", action="store_true", help="let an empty payload delete the stored objects of a view")
    r = sub.add_parser("reconcile", help="report APS objects with no XER task and XER tasks with no APS object")
    r.add_argument("--tenant", required=True)
    r.add_argument("--urn")
    r.add_argument("--proj-id", type=int)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "init-schema":
            init_schema(db.default_admin_dsn())
            print("APS tables ready")
            return 0
        conn = db.connect()
        try:
            if a.cmd == "reconcile":
                report = reconcile(conn, a.tenant, model_urn=a.urn, proj_id=a.proj_id)
                print(report.render())
                return 0 if report.clean else 1
            roles = tuple(x.strip() for x in a.roles.split(",")) if a.roles else None
            outcomes = sync_model(ApsClient.from_env(), conn, a.tenant, a.urn, roles=roles, allow_empty=a.allow_empty)
        finally:
            conn.close()
    except ApsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for o in outcomes:
        if o.load:
            print(f"view {o.view_guid} ({o.role}): {o.status}, +{o.load.added} ~{o.load.changed} -{o.load.removed}, {o.load.unchanged} unchanged, {o.load.quarantined} set aside")
        else:
            print(f"view {o.view_guid} ({o.role}): FAILED {o.error}")
    return 1 if any(o.status == "failed" for o in outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
