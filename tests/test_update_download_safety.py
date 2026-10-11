"""Closed-set download-safety negatives for the public release distribution.

Every test is pure: HTTP hops are served by a fake connection and DNS is an
injected resolver, so nothing here touches the network.
"""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from src.update.service import (
    REQUIRED_PRODUCTION_GATES,
    TrustPolicy,
    UpdateError,
    UpdateService,
    _request_headers,
    _stable_manifest_url,
    _validate_hop_url,
    _validate_package_url,
)


REPOSITORY = "gualtier-xu/Fudan-CourseLens"
MANIFEST_URL = _stable_manifest_url()
PACKAGE_URL = (
    f"https://github.com/{REPOSITORY}/releases"
    "/download/client-v1.1.0/courselens-windows-x86_64.zip"
)


def target_of(url: str) -> str:
    split = urlsplit(url)
    return split.path + (f"?{split.query}" if split.query else "")
ALLOWED_HOSTS = {
    "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
}
GLOBAL_IP = "93.184.216.34"
DISTRIBUTION = {
    "repository": REPOSITORY,
    "visibility": "public",
    "auth_model": "none",
    "tag_namespace": "client-v",
    "manifest_asset": "courselens-windows-manifest.json",
    "source_repository_access": False,
}
MANIFEST_TARGET = target_of(MANIFEST_URL)
PACKAGE_TARGET = target_of(PACKAGE_URL)


def policy(**overrides):
    return TrustPolicy(
        channel="stable",
        platform="windows",
        architecture="x86_64",
        minimum_version="1.0.0",
        minimum_key_epoch=1,
        keys={},
        allowed_hosts=set(overrides.pop("allowed_hosts", ALLOWED_HOSTS)),
        manifest_url=overrides.pop("manifest_url", MANIFEST_URL),
        enabled=True,
        distribution=overrides.pop("distribution", dict(DISTRIBUTION)),
        production_gates={name: True for name in REQUIRED_PRODUCTION_GATES},
    )


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b""):
        self.status = status
        self._headers = headers or {}
        self._body = body
        self._offset = 0

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self._body) - self._offset
        chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk


def redirect(location: str) -> FakeResponse:
    return FakeResponse(status=302, headers={"Location": location})


def ok(body: bytes) -> FakeResponse:
    return FakeResponse(status=200, headers={"Content-Length": str(len(body))}, body=body)


def make_fake_connection(routes: dict, requests: list):
    class FakeConnection:
        def __init__(self, host, port, timeout=None, context=None):
            self.host = host

        def request(self, method, target, headers=None):
            requests.append((target, {str(k).casefold(): str(v) for k, v in (headers or {}).items()}))

        def getresponse(self):
            return routes[requests[-1][0]].pop(0)

        def close(self):
            pass

    return FakeConnection


def resolve_to(ip: str):
    def resolve(hostname, port, type=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    return resolve


def sequence_resolver(*ips):
    state = {"call": 0}

    def resolve(hostname, port, type=0):
        ip = ips[min(state["call"], len(ips) - 1)]
        state["call"] += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    return resolve


def build(tmp_path, monkeypatch, routes, transport=None, resolver=None):
    requests: list = []
    monkeypatch.setattr(http.client, "HTTPSConnection", make_fake_connection(routes, requests))
    if resolver is not None:
        monkeypatch.setattr(socket, "getaddrinfo", resolver)
    service = UpdateService(
        current_version="1.0.0",
        trust_path=tmp_path / "trust.json",
        state_root=tmp_path / "state",
        install_root=tmp_path / "install",
        transport=transport,
    )
    return service, requests


def test_manifest_download_succeeds_with_zero_hops(tmp_path, monkeypatch):
    body = b'{"schema":"courselens.client-update.v1"}'
    service, requests = build(
        tmp_path, monkeypatch, {MANIFEST_TARGET: [ok(body)]}, resolver=resolve_to(GLOBAL_IP)
    )
    assert service._fetch(MANIFEST_URL, None, 256 * 1024, policy()) == body
    assert len(requests) == 1


def test_one_and_two_hop_redirect_chains_succeed(tmp_path, monkeypatch):
    body = b"package-bytes"
    service, requests = build(
        tmp_path, monkeypatch,
        {
            MANIFEST_TARGET: [redirect(PACKAGE_URL)],
            PACKAGE_TARGET: [ok(body)],
        },
        resolver=resolve_to(GLOBAL_IP),
    )
    assert service._fetch(MANIFEST_URL, None, 256 * 1024, policy()) == body
    assert [target for target, _ in requests] == [MANIFEST_TARGET, PACKAGE_TARGET]

    # Observed production shape: latest/download -> download/<tag> -> asset host.
    asset_url = f"https://release-assets.githubusercontent.com/{REPOSITORY}/asset?sig=1"
    service, requests = build(
        tmp_path, monkeypatch,
        {
            MANIFEST_TARGET: [redirect(PACKAGE_URL)],
            PACKAGE_TARGET: [redirect(asset_url)],
            target_of(asset_url): [ok(body)],
        },
        resolver=resolve_to(GLOBAL_IP),
    )
    assert service._fetch(MANIFEST_URL, None, 256 * 1024, policy()) == body
    assert len(requests) == 3


def test_three_hops_are_allowed_and_four_are_rejected(tmp_path, monkeypatch):
    body = b"package-bytes"
    third = f"https://release-assets.githubusercontent.com/{REPOSITORY}/third"
    fourth = f"https://release-assets.githubusercontent.com/{REPOSITORY}/fourth"
    service, _ = build(
        tmp_path, monkeypatch,
        {
            MANIFEST_TARGET: [redirect(PACKAGE_URL)],
            PACKAGE_TARGET: [redirect(third)],
            target_of(third): [redirect(fourth)],
            target_of(fourth): [ok(body)],
        },
        resolver=resolve_to(GLOBAL_IP),
    )
    assert service._fetch(MANIFEST_URL, None, 256 * 1024, policy()) == body

    service, requests = build(
        tmp_path, monkeypatch,
        {
            MANIFEST_TARGET: [redirect(PACKAGE_URL)],
            PACKAGE_TARGET: [redirect(third)],
            target_of(third): [redirect(fourth)],
            target_of(fourth): [redirect(f"{fourth}-5")],
        },
        resolver=resolve_to(GLOBAL_IP),
    )
    with pytest.raises(UpdateError, match="download_redirect_untrusted"):
        service._fetch(MANIFEST_URL, None, 256 * 1024, policy())
    assert len(requests) == 4


@pytest.mark.parametrize("location", [
    "http://github.com/gualtier-xu/Fudan-CourseLens/releases/download/client-v1.1.0/x.zip",
    "/gualtier-xu/Fudan-CourseLens/releases/download/client-v1.1.0/x.zip",
])
def test_non_absolute_https_redirects_are_rejected(tmp_path, monkeypatch, location):
    service, requests = build(
        tmp_path, monkeypatch,
        {MANIFEST_TARGET: [redirect(location)]},
        resolver=resolve_to(GLOBAL_IP),
    )
    with pytest.raises(UpdateError, match="download_redirect_untrusted"):
        service._fetch(MANIFEST_URL, None, 256 * 1024, policy())
    assert len(requests) == 1


@pytest.mark.parametrize("location", [
    "https://publisher:token@github.com/gualtier-xu/Fudan-CourseLens/releases/download/client-v1.1.0/x.zip",
    "https://github.com/gualtier-xu/Fudan-CourseLens/releases/download/client-v1.1.0/x.zip#fragment",
])
def test_redirect_credentials_or_fragments_are_rejected(tmp_path, monkeypatch, location):
    service, _ = build(
        tmp_path, monkeypatch,
        {MANIFEST_TARGET: [redirect(location)]},
        resolver=resolve_to(GLOBAL_IP),
    )
    with pytest.raises(UpdateError, match="source_url_blocked"):
        service._fetch(MANIFEST_URL, None, 256 * 1024, policy())


def test_disallowed_host_is_rejected_on_the_start_and_later_hop(tmp_path, monkeypatch):
    with pytest.raises(UpdateError, match="source_url_blocked"):
        _validate_hop_url(
            "https://evil.example.com/manifest.json",
            allowed_hosts=ALLOWED_HOSTS, resolver=resolve_to(GLOBAL_IP),
        )
    evil = "https://evil.example.com/gualtier-xu/Fudan-CourseLens/releases/download/client-v1.1.0/x.zip"
    service, requests = build(
        tmp_path, monkeypatch,
        {MANIFEST_TARGET: [redirect(evil)]},
        resolver=resolve_to(GLOBAL_IP),
    )
    with pytest.raises(UpdateError, match="source_url_blocked"):
        service._fetch(MANIFEST_URL, None, 256 * 1024, policy())
    assert len(requests) == 1


def test_non_global_address_on_a_later_hop_is_rejected(tmp_path, monkeypatch):
    # The first hop resolves globally; the second hop re-resolves and now
    # returns a loopback address, proving per-hop re-resolution.
    service, requests = build(
        tmp_path, monkeypatch,
        {MANIFEST_TARGET: [redirect(PACKAGE_URL)]},
        resolver=sequence_resolver(GLOBAL_IP, "127.0.0.1"),
    )
    with pytest.raises(UpdateError, match="source_address_blocked"):
        service._fetch(MANIFEST_URL, None, 256 * 1024, policy())
    assert len(requests) == 1


@pytest.mark.parametrize("resolves_to", ["127.0.0.1", "10.1.2.3", "169.254.1.2", "192.168.1.2"])
def test_non_global_addresses_are_rejected_before_contact(tmp_path, monkeypatch, resolves_to):
    service, requests = build(
        tmp_path, monkeypatch,
        {MANIFEST_TARGET: [ok(b"x")]},
        resolver=resolve_to(resolves_to),
    )
    with pytest.raises(UpdateError, match="source_address_blocked"):
        service._fetch(MANIFEST_URL, None, 256 * 1024, policy())
    assert requests == []


def test_non_redirect_intermediate_status_is_rejected(tmp_path, monkeypatch):
    service, _ = build(
        tmp_path, monkeypatch,
        {MANIFEST_TARGET: [FakeResponse(status=404)]},
        resolver=resolve_to(GLOBAL_IP),
    )
    with pytest.raises(UpdateError, match="download_http_error"):
        service._fetch(MANIFEST_URL, None, 256 * 1024, policy())

    service, _ = build(
        tmp_path, monkeypatch,
        {MANIFEST_TARGET: [redirect(PACKAGE_URL)], PACKAGE_TARGET: [FakeResponse(status=500)]},
        resolver=resolve_to(GLOBAL_IP),
    )
    with pytest.raises(UpdateError, match="download_http_error"):
        service._fetch(MANIFEST_URL, None, 256 * 1024, policy())


@pytest.mark.parametrize("value", [
    f"https://github.com/{REPOSITORY}/releases/download/v1.2.3/courselens-windows-x86_64.zip",
    f"https://github.com/gualtier-xu/Other/releases/download/client-v1.2.3/courselens-windows-x86_64.zip",
    f"https://github.com/{REPOSITORY}/releases/download/client-v1.2/courselens-windows-x86_64.zip",
    f"https://github.com/{REPOSITORY}/releases/download/client-v01.2.3/courselens-windows-x86_64.zip",
    f"https://github.com/{REPOSITORY}/releases/download/client-v1.2.3/",
    f"https://github.com/{REPOSITORY}/releases/download/client-v1.2.3/../evil.zip",
    f"https://objects.githubusercontent.com/client-v1.2.3/courselens-windows-x86_64.zip",
    f"https://github.com/{REPOSITORY}/releases/download/client-v1.2.3/x.zip?query=1",
])
def test_package_urls_outside_the_pinned_shape_are_rejected(value):
    with pytest.raises(UpdateError, match="source_url_blocked"):
        _validate_package_url(value)
    _validate_package_url(PACKAGE_URL)  # the pinned shape itself passes


def write_trust(path: Path, *, manifest_url: str) -> None:
    trust = {
        "schema": "courselens.client-update-trust.v2",
        "enabled": True,
        "channel": "stable", "platform": "windows", "architecture": "x86_64",
        "minimum_version": "1.0.0", "minimum_key_epoch": 1,
        "root_keys": {}, "release_key_authorizations": [],
        "allowed_hosts": sorted(ALLOWED_HOSTS),
        "manifest_url": manifest_url,
        "distribution": dict(DISTRIBUTION),
        "production_gates": {name: True for name in REQUIRED_PRODUCTION_GATES},
    }
    path.write_text(json.dumps(trust), encoding="utf-8")


def test_manifest_url_must_equal_the_derived_stable_url(tmp_path, monkeypatch):
    # 被测语义=distribution 门的 manifest_url 形状；宿主平台钉 windows/x86_64
    # 使 _policy 的门序与跑套件的 OS 无关（Windows 宿主上=恒等零变化）。
    monkeypatch.setattr(
        "src.update.service.host_platform", lambda: ("windows", "x86_64")
    )
    service = UpdateService(
        current_version="1.0.0",
        trust_path=tmp_path / "trust.json",
        state_root=tmp_path / "state",
        install_root=tmp_path / "install",
    )
    write_trust(service.trust_path, manifest_url="https://objects.githubusercontent.com/manifest.json")
    with pytest.raises(UpdateError, match="distribution_policy_invalid"):
        service._policy()
    # The exact derived stable URL is accepted (and only fails later on keys).
    write_trust(service.trust_path, manifest_url=_stable_manifest_url())
    with pytest.raises(UpdateError, match="update_not_configured"):
        service._policy()


def test_committed_manifest_url_is_the_derived_stable_url():
    committed = json.loads(
        Path("config/client-update-trust.json").read_text(encoding="utf-8")
    )
    assert committed["manifest_url"] == _stable_manifest_url()


def test_request_headers_never_carry_authorization(tmp_path, monkeypatch):
    for destination in (None, tmp_path / "package.zip"):
        assert not any("authorization" in name for name in _request_headers(destination))
    body = b"package-bytes"
    service, requests = build(
        tmp_path, monkeypatch,
        {
            MANIFEST_TARGET: [redirect(PACKAGE_URL)],
            PACKAGE_TARGET: [ok(body)],
        },
        resolver=resolve_to(GLOBAL_IP),
    )
    destination = tmp_path / "staging" / "package.zip"
    assert service._fetch(PACKAGE_URL, destination, len(body), policy()) is None
    assert requests
    for _, headers in requests:
        assert not any("authorization" in name for name in headers)


def test_transport_mode_still_validates_the_start_url(tmp_path, monkeypatch):
    calls: list = []

    def transport(url, destination, limit):
        calls.append(url)
        if destination is None:
            return b"{}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"package")
        return None

    service, requests = build(
        tmp_path, monkeypatch, {}, transport=transport, resolver=resolve_to(GLOBAL_IP)
    )
    wrong = (
        f"https://github.com/{REPOSITORY}/releases"
        "/download/v1.1.0/courselens-windows-x86_64.zip"
    )
    with pytest.raises(UpdateError, match="source_url_blocked"):
        service._fetch(wrong, tmp_path / "package.zip", 1024, policy())
    assert calls == [] and requests == []
    destination = tmp_path / "staging" / "package.zip"
    assert service._fetch(PACKAGE_URL, destination, 1024, policy()) is None
    assert calls == [PACKAGE_URL]
