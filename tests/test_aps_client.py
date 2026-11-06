"""APS client against the localhost stand-in: token fetch and refresh, 202 polling, 429 and 5xx handling, 4xx that must not be retried, no secret in any log line.

No network and no database. Waits are injected (FakeTime), so every retry delay can be read back and no test sleeps.
"""
from __future__ import annotations

import base64
import http.client
import json
import logging
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from aps_stand_in import CLIENT_ID, CLIENT_SECRET, GUID_2D, GUID_3D, URN, VIEWS, FakeTime, aps_server, make_client, sample_collection  # noqa: F401  (aps_server is a fixture)
from xerlane.aps import (ApsAuthError, ApsClient, ApsConfigError, ApsError, ApsHttpError, ApsNotReady, ApsPayloadError, ApsRateLimited, urn_from_object_id)


@pytest.fixture()
def model(aps_server):
    aps_server.add_model(URN, VIEWS, {GUID_3D: sample_collection(), GUID_2D: []})
    return aps_server


def j(status: int, doc, **headers):
    return (status, headers, json.dumps(doc).encode())


# ---- token ---------------------------------------------------------------------------------------------------------------------------------------

def test_token_request_follows_the_documented_shape(model):
    client = make_client(model)
    assert client.token() == model.issued[0]
    assert model.requests == [("POST", "/authentication/v2/token")]
    assert model.auth_headers[0] == "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    form = model.token_forms[0]
    assert form["grant_type"] == ["client_credentials"]
    assert form["scope"] == ["data:read viewables:read"]


def test_scopes_are_configurable(model):
    make_client(model, scopes=("data:read",)).token()
    assert model.token_forms[0]["scope"] == ["data:read"]


def test_token_is_cached_and_sent_as_bearer(model):
    client = make_client(model)
    client.list_views(URN)
    client.list_views(URN)
    assert model.count("POST", "/token") == 1
    assert model.auth_headers[1] == f"Bearer {model.issued[0]}"


def test_token_is_refreshed_a_little_before_it_expires(model):
    ft = FakeTime()
    client = make_client(model, ft)                      # expires_in 3599, margin 120: refresh from 3479 s on
    client.token()
    ft.now += 3478
    client.token()
    assert len(model.issued) == 1
    ft.now += 2
    assert client.token() == model.issued[1]
    assert len(model.issued) == 2


def test_a_short_lived_token_is_refreshed_halfway_not_on_every_call(model):
    model.expires_in = 60
    ft = FakeTime()
    client = make_client(model, ft)
    client.token()
    ft.now += 29
    client.token()
    assert len(model.issued) == 1
    ft.now += 2
    client.token()
    assert len(model.issued) == 2


def test_wrong_credentials_fail_once_without_retry_or_leak(model):
    ft = FakeTime()
    client = ApsClient(CLIENT_ID, "wrong-secret-ZZZ", base_url=model.url, clock=ft.clock, sleep=ft.sleep)
    with pytest.raises(ApsAuthError) as exc:
        client.token()
    assert model.count("POST", "/token") == 1 and ft.sleeps == []
    assert exc.value.status == 401
    assert "wrong-secret-ZZZ" not in str(exc.value)


@pytest.mark.parametrize("doc", [{}, {"access_token": ""}, {"access_token": "t"}, {"access_token": "t", "expires_in": 0}, {"access_token": "t", "expires_in": -5},
                                 {"access_token": "t", "expires_in": True}, {"access_token": "t", "expires_in": "3600"}, {"access_token": 7, "expires_in": 60}, []])
def test_token_answer_without_a_usable_token_is_a_payload_error(model, doc):
    model.script("token", j(200, doc))
    with pytest.raises(ApsPayloadError):
        make_client(model).token()


def test_token_answer_that_is_not_json_is_a_payload_error(model):
    model.script("token", (200, {}, b"<html>proxy</html>"))
    with pytest.raises(ApsPayloadError):
        make_client(model).token()


@pytest.mark.parametrize("status", [400, 401, 403])
def test_a_refused_token_request_is_an_auth_error_with_a_hint_and_no_retry(model, status):
    model.script("token", j(status, {"developerMessage": "no"}))
    ft = FakeTime()
    with pytest.raises(ApsAuthError) as exc:
        make_client(model, ft).token()
    assert exc.value.status == status and f"HTTP {status}" in str(exc.value) and "check the client id" in str(exc.value)
    assert ft.sleeps == [] and model.count("POST", "/token") == 1


@pytest.mark.parametrize("status", [402, 404, 415])
def test_other_token_endpoint_4xx_is_final_and_keeps_its_status(model, status):
    model.script("token", j(status, {"developerMessage": "gone"}))
    ft = FakeTime()
    with pytest.raises(ApsHttpError) as exc:
        make_client(model, ft).token()
    assert exc.value.status == status and not isinstance(exc.value, ApsAuthError) and ft.sleeps == []


def test_a_one_second_token_is_accepted_and_refreshed_after_half_a_second(model):
    model.expires_in = 1
    ft = FakeTime()
    client = make_client(model, ft)
    client.token()
    ft.now += 0.4
    client.token()
    assert len(model.issued) == 1
    ft.now += 0.2
    client.token()
    assert len(model.issued) == 2


def test_token_endpoint_honours_429_and_5xx_too(model):
    model.script("token", j(429, {"developerMessage": "slow down"}, **{"Retry-After": "3"}), j(503, {}))
    ft = FakeTime()
    assert make_client(model, ft).token() == model.issued[0]
    assert ft.sleeps == [3.0, 1.0]


# ---- expired token mid session ---------------------------------------------------------------------------------------------------------------------

def test_a_token_the_server_expired_is_replaced_once_and_the_call_succeeds(model):
    client = make_client(model)
    client.list_views(URN)
    model.expire_all_tokens()
    views = client.list_views(URN)
    assert [v.guid for v in views] == [GUID_3D, GUID_2D]
    assert len(model.issued) == 2
    assert model.auth_headers[-1] == f"Bearer {model.issued[1]}"


def test_a_second_401_in_a_row_is_an_auth_error_not_a_loop(model):
    client = make_client(model)
    client.list_views(URN)
    model.script("views", j(401, {"developerMessage": "expired"}), j(401, {"developerMessage": "expired"}))
    with pytest.raises(ApsAuthError) as exc:
        client.list_views(URN)
    assert exc.value.status == 401
    assert model.count("GET", "/metadata") == 3                      # the first good call, then the two refused ones
    assert len(model.issued) == 2


def test_403_is_an_auth_error_and_is_not_retried(model):
    model.script("views", j(403, {"developerMessage": "no access"}))
    ft = FakeTime()
    with pytest.raises(ApsAuthError) as exc:
        make_client(model, ft).list_views(URN)
    assert exc.value.status == 403 and ft.sleeps == [] and len(model.issued) == 1


# ---- list views ----------------------------------------------------------------------------------------------------------------------------------

def test_list_views_parses_the_documented_shape(model):
    views = make_client(model).list_views(URN)
    assert [(v.name, v.role, v.guid, v.is_master) for v in views] == [("New Construction", "3d", GUID_3D, True), ("Level 1 plan", "2d", GUID_2D, False)]
    assert model.requests[-1] == ("GET", f"/modelderivative/v2/designdata/{URN}/metadata")


@pytest.mark.parametrize("doc", [{}, {"data": {}}, {"data": {"metadata": "x"}}, {"data": {"metadata": [{"name": "no guid"}]}}, {"data": {"metadata": [5]}},
                                 {"data": {"metadata": [{"guid": "../etc"}]}}, {"data": {"metadata": [{"guid": 7}]}}, []])
def test_list_views_rejects_a_wrong_shape(model, doc):
    model.script("views", j(200, doc))
    with pytest.raises(ApsPayloadError):
        make_client(model).list_views(URN)


def test_unknown_model_is_a_404_that_is_not_retried(model):
    ft = FakeTime()
    with pytest.raises(ApsHttpError) as exc:
        make_client(model, ft).list_views("bm9wZQ")
    assert exc.value.status == 404 and ft.sleeps == [] and model.count("GET", "/metadata") == 1


@pytest.mark.parametrize("urn", ["", "../secret", "a/b", "a b", "x?y=1", "a#b"])
def test_a_urn_that_could_change_the_path_is_refused_before_any_request(model, urn):
    with pytest.raises(ApsConfigError):
        make_client(model).list_views(urn)
    assert model.requests == []


@pytest.mark.parametrize("guid", ["", "../x", "a/b", "a b", "g" * 65])
def test_a_guid_that_could_change_the_path_is_refused_before_any_request(model, guid):
    with pytest.raises(ApsConfigError):
        make_client(model).get_properties(URN, guid)
    assert model.requests == []


def test_urn_from_object_id_is_urlsafe_base64_without_padding():
    assert urn_from_object_id("urn:adsk.objects:os.object:test/model.rvt") == URN
    assert "=" not in urn_from_object_id("urn:adsk.objects:os.object:bucket/a.rvt")
    tricky = "urn:adsk.objects:os.object:bucket/???>>>"                # standard Base64 of this holds + and / and padding; the URL-safe form must not
    standard = base64.b64encode(tricky.encode()).decode()
    assert "/" in standard and "+" in standard and standard.endswith("=")
    assert urn_from_object_id(tricky) == standard.replace("+", "-").replace("/", "_").rstrip("=")


def test_region_header_is_sent_only_when_set(model):
    make_client(model).list_views(URN)
    make_client(model, region="EMEA").list_views(URN)
    assert model.regions == [None, "EMEA"]
    with pytest.raises(ApsConfigError):
        make_client(model, region="MARS")


# ---- properties and the 202 polling --------------------------------------------------------------------------------------------------------------

def test_properties_come_back_as_the_collection(model):
    assert make_client(model).get_properties(URN, GUID_3D) == sample_collection()
    assert model.requests[-1] == ("GET", f"/modelderivative/v2/designdata/{URN}/metadata/{GUID_3D}/properties")


def test_202_is_polled_with_growing_waits_until_200(model):
    model.models[URN]["pending"][GUID_3D] = 3
    ft = FakeTime()
    out = make_client(model, ft).get_properties(URN, GUID_3D)
    assert out == sample_collection()
    assert ft.sleeps == [1.0, 2.0, 4.0]
    assert model.count("GET", "/properties") == 4


def test_poll_wait_is_capped(model):
    model.models[URN]["pending"][GUID_3D] = 5
    ft = FakeTime()
    make_client(model, ft, poll_cap=3.0).get_properties(URN, GUID_3D)
    assert ft.sleeps == [1.0, 2.0, 3.0, 3.0, 3.0]


def test_polling_stops_after_max_polls_and_does_not_sleep_after_the_last_try(model):
    model.models[URN]["pending"][GUID_3D] = 99
    ft = FakeTime()
    with pytest.raises(ApsNotReady):
        make_client(model, ft, max_polls=3).get_properties(URN, GUID_3D)
    assert model.count("GET", "/properties") == 4
    assert ft.sleeps == [1.0, 2.0, 4.0]


def test_ready_on_the_very_last_allowed_poll_succeeds(model):
    model.models[URN]["pending"][GUID_3D] = 3
    assert make_client(model, max_polls=3).get_properties(URN, GUID_3D) == sample_collection()


def test_polling_does_not_count_against_the_error_retries(model):
    model.models[URN]["pending"][GUID_3D] = 4
    model.script("props", j(503, {}), j(503, {}))
    ft = FakeTime()
    assert make_client(model, ft, max_retries=2).get_properties(URN, GUID_3D) == sample_collection()
    assert ft.sleeps == [0.5, 1.0, 1.0, 2.0, 4.0, 8.0]


@pytest.mark.parametrize("body", [b"not json", b'{"data": {"collection": NaN}}', b'{"data": {"collection": [Infinity]}}', b'{"data": {"type": "properties"}}',
                                  b'{"data": {"collection": {"a": 1}}}', b"[]", b"", b"\xff\xfe"])
def test_malformed_properties_answers_are_payload_errors(model, body):
    model.models[URN]["raw"][GUID_3D] = body
    with pytest.raises(ApsPayloadError):
        make_client(model).get_properties(URN, GUID_3D)


def test_a_response_over_the_size_cap_is_refused(model):
    with pytest.raises(ApsPayloadError):
        make_client(model, max_body_bytes=50).get_properties(URN, GUID_3D)
    assert make_client(model, max_body_bytes=10_000_000).get_properties(URN, GUID_3D)


# ---- 429, 5xx, other 4xx -------------------------------------------------------------------------------------------------------------------------

def test_429_waits_exactly_retry_after(model):
    model.script("views", j(429, {"developerMessage": "Quota limit exceeded."}, **{"Retry-After": "25"}))
    ft = FakeTime()
    make_client(model, ft).list_views(URN)
    assert ft.sleeps == [25.0]


def test_429_without_a_usable_retry_after_falls_back_to_backoff(model):
    for value in ("soon", "-4", "nan", "inf"):
        model.script("views", j(429, {}, **{"Retry-After": value}))
    model.script("views", j(429, {}))
    ft = FakeTime()
    make_client(model, ft, max_retries=9).list_views(URN)
    assert ft.sleeps == [0.5, 1.0, 2.0, 4.0, 8.0]


def test_retry_after_zero_means_retry_at_once(model):
    model.script("views", j(429, {}, **{"Retry-After": "0"}))
    ft = FakeTime()
    make_client(model, ft).list_views(URN)
    assert ft.sleeps == [0.0]


def test_a_retry_after_longer_than_we_will_wait_stops_at_once(model):
    model.script("views", j(429, {}, **{"Retry-After": "3600"}))
    ft = FakeTime()
    with pytest.raises(ApsRateLimited) as exc:
        make_client(model, ft).list_views(URN)
    assert exc.value.retry_after == 3600.0 and ft.sleeps == [] and model.count("GET", "/metadata") == 1
    assert str(exc.value).startswith("GET /modelderivative/v2/designdata/") and "3600" in str(exc.value)


def test_a_retry_after_exactly_at_the_limit_is_waited(model):
    model.script("views", j(429, {}, **{"Retry-After": "120"}))
    ft = FakeTime()
    make_client(model, ft).list_views(URN)
    assert ft.sleeps == [120.0]


def test_429_forever_gives_up_after_max_retries(model):
    model.script("views", *[j(429, {}, **{"Retry-After": "1"})] * 10)
    ft = FakeTime()
    with pytest.raises(ApsRateLimited) as exc:
        make_client(model, ft, max_retries=3).list_views(URN)
    assert model.count("GET", "/metadata") == 4 and ft.sleeps == [1.0, 1.0, 1.0]
    assert exc.value.retry_after == 1.0 and "3 retries" in str(exc.value) and str(exc.value).startswith("GET ")


def test_5xx_is_retried_with_growing_waits_then_succeeds(model):
    model.script("views", j(503, {}), j(502, {}), j(500, {}))
    ft = FakeTime()
    assert len(make_client(model, ft).list_views(URN)) == 2
    assert ft.sleeps == [0.5, 1.0, 2.0]


def test_5xx_backoff_is_capped_and_the_retry_count_is_bounded(model):
    model.script("views", *[j(500, {"developerMessage": "boom"})] * 20)
    ft = FakeTime()
    with pytest.raises(ApsHttpError) as exc:
        make_client(model, ft, max_retries=5, backoff_cap=2.0).list_views(URN)
    assert exc.value.status == 500
    assert model.count("GET", "/metadata") == 6
    assert ft.sleeps == [0.5, 1.0, 2.0, 2.0, 2.0]
    assert "5 retries" in str(exc.value)


@pytest.mark.parametrize("status", [300, 304, 400, 404, 406, 409, 410, 413, 422])
def test_every_other_non_2xx_answer_is_final_and_never_retried(model, status):
    model.script("views", j(status, {"diagnostics": "no"}))
    ft = FakeTime()
    with pytest.raises(ApsHttpError) as exc:
        make_client(model, ft).list_views(URN)
    assert exc.value.status == status and not isinstance(exc.value, ApsAuthError)
    assert ft.sleeps == [] and model.count("GET", "/metadata") == 1
    assert str(exc.value).startswith("GET /modelderivative/v2/designdata/")


def test_a_dropped_connection_is_retried_then_reported():
    """A server that accepts and hangs up at once (a reset or an empty reply), the usual failure behind a flaky proxy or a restarting service."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    hits = []

    def hang_up():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            hits.append(1)
            conn.close()

    threading.Thread(target=hang_up, daemon=True).start()
    ft = FakeTime()
    client = ApsClient(CLIENT_ID, CLIENT_SECRET, base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", clock=ft.clock, sleep=ft.sleep, max_retries=2, timeout=2)
    try:
        with pytest.raises(ApsError) as exc:
            client.token()
    finally:
        listener.close()
    assert len(hits) == 3 and ft.sleeps == [0.5, 1.0]
    assert "network error" in str(exc.value) and "2 retries" in str(exc.value)


def test_a_redirect_is_not_followed_so_the_token_stays_on_our_host(model):
    seen = []

    class Other(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    other = HTTPServer(("127.0.0.1", 0), Other)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        model.script("views", (302, {"Location": f"http://127.0.0.1:{other.server_address[1]}/steal"}, b""))
        with pytest.raises(ApsHttpError) as exc:
            make_client(model).list_views(URN)
        assert exc.value.status == 302 and seen == []
    finally:
        other.shutdown()
        other.server_close()


# ---- configuration -------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["http://developer.api.autodesk.com", "http://example.com", "ftp://localhost", "developer.api.autodesk.com", "https://", ""])
def test_a_base_url_that_would_send_credentials_in_clear_is_refused(url):
    with pytest.raises(ApsConfigError):
        ApsClient("id", "secret", base_url=url)


@pytest.mark.parametrize("url", ["https://developer.api.autodesk.com", "https://developer.api.autodesk.com/", "http://localhost:8080", "http://127.0.0.1:1"])
def test_https_and_loopback_base_urls_are_accepted(url):
    ApsClient("id", "secret", base_url=url)


@pytest.mark.parametrize("cid,secret", [("", "s"), ("i", ""), ("", "")])
def test_both_credentials_are_required(cid, secret):
    with pytest.raises(ApsConfigError):
        ApsClient(cid, secret)


def test_from_env_reads_the_documented_variable_names(monkeypatch, model):
    monkeypatch.setenv("APS_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("APS_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("APS_BASE_URL", model.url)
    monkeypatch.setenv("APS_REGION", "AUS")
    client = ApsClient.from_env()
    assert len(client.list_views(URN)) == 2 and model.regions == ["AUS"]
    monkeypatch.delenv("APS_CLIENT_SECRET")
    with pytest.raises(ApsConfigError):
        ApsClient.from_env()


# ---- no secret anywhere ---------------------------------------------------------------------------------------------------------------------------

def test_no_secret_token_or_basic_header_in_any_log_line_or_error(model, caplog):
    """A busy run: success, 429, 5xx, an expired token, polling, a refused call whose error body echoes the credentials back. Nothing sensitive may be logged or raised."""
    caplog.set_level(logging.DEBUG)
    ft = FakeTime()
    client = make_client(model, ft)
    model.models[URN]["pending"][GUID_3D] = 1
    model.script("views", j(429, {}, **{"Retry-After": "2"}), j(500, {}))
    client.list_views(URN)
    client.get_properties(URN, GUID_3D)
    model.expire_all_tokens()
    client.list_views(URN)
    tok = client.token()
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    model.script("views", j(400, {"diagnostics": f"bad request, token {tok}, secret {CLIENT_SECRET}, basic {basic}"}))
    with pytest.raises(ApsHttpError) as exc:
        client.list_views(URN)
    model.script("token", j(401, {"developerMessage": f"bad secret {CLIENT_SECRET}"}))
    with pytest.raises(ApsAuthError) as exc2:
        ApsClient(CLIENT_ID, CLIENT_SECRET, base_url=model.url, clock=ft.clock, sleep=ft.sleep).token()
    texts = [r.getMessage() for r in caplog.records] + [str(exc.value), str(exc2.value), repr(exc.value), repr(client)]
    blob = "\n".join(texts)
    assert len(caplog.records) >= 6, "the run should have logged its retries and polls"
    for needle in (CLIENT_SECRET, basic, *model.issued, "Bearer ", "Basic "):
        assert needle not in blob, f"{needle[:12]}... leaked"
    assert "***" in str(exc.value)                                  # the echoed token and secret were replaced, not just dropped
    assert any("retry" in t for t in texts)


# ---- defaults, size cap, truncated answers, proxies, logging ------------------------------------------------------------------------------------------

def test_the_default_retry_and_poll_limits_are_five_retries_and_twelve_waits(model):
    model.script("views", *[j(500, {})] * 20)
    ft = FakeTime()
    with pytest.raises(ApsHttpError):
        make_client(model, ft).list_views(URN)
    assert model.count("GET", "/metadata") == 6 and ft.sleeps == [0.5, 1.0, 2.0, 4.0, 8.0]
    model.models[URN]["pending"][GUID_3D] = 99
    ft = FakeTime()
    with pytest.raises(ApsNotReady):
        make_client(model, ft).get_properties(URN, GUID_3D)
    assert model.count("GET", "/properties") == 13 and len(ft.sleeps) == 12 and ft.sleeps[-1] == 15.0


def test_the_size_cap_is_exact(model):
    body = b'{"data": {"collection": []}}'
    model.models[URN]["raw"][GUID_3D] = body
    client = make_client(model)
    client.token()                                                      # the token answer is read under the cap too, so fetch it before the cap is set tight
    client._max_body = len(body)
    assert client.get_properties(URN, GUID_3D) == []
    client._max_body = len(body) - 1
    with pytest.raises(ApsPayloadError, match="larger than"):
        client.get_properties(URN, GUID_3D)


def test_an_answer_cut_off_before_its_content_length_is_retried_not_parsed(model):
    model.script("views", (200, {"X-Truncate": "1"}, b'{"data": {"metadata": [{"gu'))
    ft = FakeTime()
    views = make_client(model, ft).list_views(URN)
    assert [v.guid for v in views] == [GUID_3D, GUID_2D]
    assert ft.sleeps == [0.5] and model.count("GET", "/metadata") == 2


def test_an_error_body_that_dies_mid_read_still_reports_the_status(model):
    class Body:
        def read(self, n=-1):
            raise http.client.IncompleteRead(b"par")

        def close(self):
            pass

    err = urllib.error.HTTPError("http://x/", 400, "Bad Request", http.client.HTTPMessage(), Body())

    class Opener:
        def open(self, req, timeout=None):
            raise err

    client = make_client(model)
    client._opener = Opener()
    resp = client._once("GET", "http://x/", {}, None)
    assert resp.status == 400 and resp.body == b""


def test_system_proxy_settings_are_ignored_for_a_loopback_stand_in(model, monkeypatch):
    for var in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")                    # a dead proxy: any request that goes through it fails
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    assert len(make_client(model, max_retries=0, timeout=3).list_views(URN)) == 2


def test_a_real_host_keeps_the_system_proxy(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://proxy.example:3128")
    client = ApsClient("id", "secret")
    proxies = [h.proxies for h in client._opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert proxies and proxies[0].get("https") == "http://proxy.example:3128"


def test_base_url_is_stored_without_a_trailing_slash():
    assert ApsClient("id", "secret", base_url="https://developer.api.autodesk.com/")._base == "https://developer.api.autodesk.com"
    assert ApsClient("id", "secret", base_url="http://localhost:9/")._base == "http://localhost:9"


def test_the_run_is_traceable_in_the_log(model, caplog):
    caplog.set_level(logging.DEBUG, logger="xerlane.aps")
    client = make_client(model)
    client.list_views(URN)
    model.expire_all_tokens()
    model.models[URN]["pending"][GUID_3D] = 1
    client.get_properties(URN, GUID_3D)
    text = "\n".join(r.getMessage() for r in caplog.records)
    for expected in ("access token acquired, valid for 3599 s", f"GET /modelderivative/v2/designdata/{URN}/metadata -> HTTP 200", "token refused with HTTP 401, fetching a new one",
                     f"properties of view {GUID_3D} are still being extracted (HTTP 202), asking again in 1.0 s"):
        assert expected in text, expected
