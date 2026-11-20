# Copyright (C) 2026 Percona LLC
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
"""Cover the side-car's Grafana service-account mint."""

import asyncio
import logging
import os
import ssl
import stat
import sys
import time
from collections.abc import AsyncGenerator, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app import BASE_DIR
from app.core.auth.providers.grafana.provider import GrafanaAuthProvider
from app.core.requests.connectivity import PROBE_TIMEOUT_SECONDS
from app.core.utils.date_time import utc_now
from app.core.utils.strings import b64encode_str
from sidecar import grafana_service_account as helper
from tests.sidecar.conftest import EMBEDDED_PROFILE, SIDECAR_DIR

HELPER_SCRIPT = SIDECAR_DIR / "grafana_service_account.py"
ROOT_PROFILE = BASE_DIR / "settings.yaml"

MINTED_TOKEN = "glsa_minted_by_the_stub"
PERSISTED_TOKEN = "glsa_persisted_by_an_earlier_start"
ACCOUNT_ID = 7

DEFAULT_BASIC = f"Basic {b64encode_str('admin:admin')}"

SHORT_BOUND_SECONDS = 0.5
"""An attempt bound short enough that outrunning it is visible in the wall clock."""

WAITING_FOR_GRAFANA = "Waiting for Grafana at"
"""What the helper says while Grafana answers like a server still starting."""

HELPER_SPEAKS_SECONDS = 60
"""Ample for a helper process to resolve its settings and report its first wait."""

HANDLER_STALL_SECONDS = PROBE_TIMEOUT_SECONDS + 2.0
"""Longer than every bound under test, so a stall is never simply outwaited."""

PATIENT_BOUND_SECONDS = 30
"""Long enough that a branch declining to wait for Grafana is unambiguous."""

TOKEN_FILE_MODE = 0o600
STATE_DIR_MODE = 0o700

EXPECTED_SEARCH_ATTEMPTS = 4
"""Three transient answers, then the one that succeeds."""


class StubRoute(StrEnum):
    """Name the four Grafana routes the helper drives."""

    VALIDATE = "validate"
    SEARCH = "search"
    CREATE_ACCOUNT = "create_account"
    CREATE_TOKEN = "create_token"


@dataclass(frozen=True, slots=True)
class StubResponse:
    """Define one answer the stub returns for a route.

    :param payload: The JSON body to answer with.
    :param status: The HTTP status to answer with.
    :param delay: Seconds to stall before answering, for the bound cases.
    """

    payload: Any = None
    status: int = 200
    delay: float = 0.0


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    """Record one request the stub received.

    :param route: The route that served it.
    :param method: The HTTP method.
    :param path: The full request path, including the ``/graph`` prefix.
    :param query: The parsed query string.
    :param headers: The request headers.
    :param body: The parsed JSON body, or ``None`` for a bodyless request.
    """

    route: StubRoute
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: Any


DEFAULT_RESPONSES = {
    StubRoute.VALIDATE: StubResponse([{"login": "admin", "role": "Admin"}]),
    StubRoute.SEARCH: StubResponse({"totalCount": 0, "serviceAccounts": []}),
    StubRoute.CREATE_ACCOUNT: StubResponse(
        {"id": ACCOUNT_ID, "name": "pmm-extensions", "login": "sa-pmm-extensions"},
        status=201,
    ),
    StubRoute.CREATE_TOKEN: StubResponse(
        {"id": 1, "name": "pmm-extensions-token", "key": MINTED_TOKEN}
    ),
}


class GrafanaStub:
    """Serve Grafana's mint surface on loopback and record every request.

    A real server rather than a patched verb method, because
    :meth:`app.core.requests.remote_api.RemoteAPI.auth` attaches nothing to the
    call: it sets a context variable that only ``_request`` reads when it merges
    headers, so a patch installed above ``_request`` observes no
    ``Authorization`` at all and a credential assertion written against it holds
    whether or not the credential was sent. The server is also what proves the
    endpoint's own ``/graph`` path is re-prefixed onto every request.
    """

    def __init__(self, ssl_context: ssl.SSLContext | None = None) -> None:
        """Prepare a stub that serves plain HTTP, or TLS when given a context.

        :param ssl_context: The server-side TLS context to serve with, if any.
        """
        self.requests: list[RecordedRequest] = []
        self._ssl_context = ssl_context
        self._queued: dict[StubRoute, list[StubResponse]] = {}
        self._site: web.TCPSite | None = None
        self._runner: web.AppRunner | None = None
        self._port = 0

    def queue(self, route: StubRoute, *responses: StubResponse) -> None:
        """Answer ``route`` with ``responses`` in order, repeating the last one.

        :param route: The route to program.
        :param responses: The answers, oldest first.
        """
        self._queued[route] = list(responses)

    async def start(self) -> None:
        """Bind the stub to an ephemeral loopback port."""
        application = web.Application()
        application.router.add_get(
            "/graph/api/org/users", self._handler(StubRoute.VALIDATE)
        )
        application.router.add_get(
            "/graph/api/serviceaccounts/search", self._handler(StubRoute.SEARCH)
        )
        application.router.add_post(
            "/graph/api/serviceaccounts", self._handler(StubRoute.CREATE_ACCOUNT)
        )
        application.router.add_post(
            "/graph/api/serviceaccounts/{account_id}/tokens",
            self._handler(StubRoute.CREATE_TOKEN),
        )
        self._runner = web.AppRunner(application, shutdown_timeout=SHORT_BOUND_SECONDS)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner, "127.0.0.1", 0, ssl_context=self._ssl_context
        )
        await self._site.start()
        self._port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        """Release the port, abandoning any handler still stalling."""
        if self._runner is not None:
            await self._runner.cleanup()

    @property
    def endpoint(self) -> str:
        """Return the endpoint to configure the provider with.

        :return: The loopback URL, carrying PMM's ``/graph`` base path.
        """
        scheme = "https" if self._ssl_context else "http"
        return f"{scheme}://127.0.0.1:{self._port}/graph"

    def calls(self, route: StubRoute) -> list[RecordedRequest]:
        """Return the requests ``route`` served, in order.

        :param route: The route to filter on.
        :return: The matching requests.
        """
        return [request for request in self.requests if request.route is route]

    def _handler(
        self, route: StubRoute
    ) -> Callable[[web.Request], Coroutine[Any, Any, web.Response]]:
        """Build the aiohttp handler recording and answering for ``route``.

        :param route: The route the handler serves.
        :return: The handler.
        """

        async def handle(request: web.Request) -> web.Response:
            body = await request.json() if request.can_read_body else None
            self.requests.append(
                RecordedRequest(
                    route=route,
                    method=request.method,
                    path=request.path,
                    query=dict(request.query),
                    headers=dict(request.headers),
                    body=body,
                )
            )
            queued = self._queued.get(route)
            if queued:
                response = queued.pop(0) if len(queued) > 1 else queued[0]
            else:
                response = DEFAULT_RESPONSES[route]
            if response.delay:
                await asyncio.sleep(response.delay)
            return web.json_response(response.payload, status=response.status)

        return handle


@dataclass(frozen=True, slots=True)
class HelperRun:
    """Hold the outcome of running the helper as its own process.

    :param returncode: The helper's exit status.
    :param stdout: What the helper printed for ``entrypoint.sh`` to capture.
    :param stderr: The helper's diagnostics.
    :param elapsed: Wall-clock seconds the run took.
    """

    returncode: int
    stdout: str
    stderr: str
    elapsed: float

    @property
    def token(self) -> str:
        """Return the resolved token the entrypoint would capture.

        :return: The stripped stdout, empty when the helper resolved nothing.
        """
        return self.stdout.strip()


@pytest_asyncio.fixture
async def grafana_stub() -> AsyncGenerator[GrafanaStub, None]:
    """Stand a Grafana stub up on loopback for the duration of the test.

    :return: The running stub.
    """
    stub = GrafanaStub()
    await stub.start()
    try:
        yield stub
    finally:
        await stub.stop()


def self_signed_server_context(directory: Path) -> ssl.SSLContext:
    """Build a TLS server context around a certificate no client trusts.

    :param directory: Where to write the key and the certificate.
    :return: The server-side context.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "grafana-stub")])
    now = utc_now()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .sign(key, hashes.SHA256())
    )
    key_file = directory / "stub-key.pem"
    certificate_file = directory / "stub-certificate.pem"
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    certificate_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_file, key_file)
    return context


@pytest.fixture
def provider(grafana_stub: GrafanaStub) -> GrafanaAuthProvider:
    """Build a provider pointed at the stub, as settings would resolve one.

    :param grafana_stub: The running stub.
    :return: The provider, not yet opened.
    """
    return GrafanaAuthProvider(
        endpoint=grafana_stub.endpoint, service_account_token="", verify_ssl=False
    )


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """Return an existing, owner-only state directory.

    :param tmp_path: The per-test temporary directory.
    :return: The directory the helper persists into.
    """
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    return directory


def profile_cwd(tmp_path: Path, profile: Path = EMBEDDED_PROFILE) -> Path:
    """Build the working directory the helper reads its profile from.

    Settings classes open the relative ``Path("settings.yaml")`` per
    instantiation, so the copy in the process CWD is what the helper reads.

    :param tmp_path: The per-test temporary directory.
    :param profile: The profile to copy.
    :return: The directory to run the helper from.
    """
    directory = tmp_path / "cwd"
    directory.mkdir(exist_ok=True)
    (directory / "settings.yaml").write_text(
        profile.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return directory


async def start_helper(cwd: Path, **environment: str) -> asyncio.subprocess.Process:
    """Start the helper as its own process, the way ``entrypoint.sh`` does.

    The subprocess runs on this test's event loop, so the stub keeps serving
    while the helper talks to it.

    The base environment carries the secrets a settings build needs but the
    baked profile does not supply, since the helper degrades to a skipped
    pre-flight when settings fail to resolve, which would make every token
    assertion below pass vacuously.

    :param cwd: The directory holding the profile the helper should read.
    :param environment: The environment to run with, over a minimal base.
    :return: The running helper.
    """
    base = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(BASE_DIR),
        "FASTAPI_ENV": "production_docker",
        "SECRET_KEY": "0" * 32,
        "ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
    }
    return await asyncio.create_subprocess_exec(
        sys.executable,
        str(HELPER_SCRIPT),
        cwd=str(cwd),
        env={**base, **environment},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def run_helper(cwd: Path, **environment: str) -> HelperRun:
    """Run the helper to its end and collect what it printed.

    :param cwd: The directory holding the profile the helper should read.
    :param environment: The environment to run with, over a minimal base.
    :return: The completed run.
    """
    started = time.monotonic()
    process = await start_helper(cwd, **environment)
    stdout, stderr = await process.communicate()
    return HelperRun(
        returncode=process.returncode,
        stdout=stdout.decode(),
        stderr=stderr.decode(),
        elapsed=time.monotonic() - started,
    )


@pytest.mark.asyncio
async def test_a_mint_creates_the_account_then_a_token_on_it(
    grafana_stub: GrafanaStub, provider: GrafanaAuthProvider
):
    """Find nothing, create the account, then create a token on the new account."""
    async with provider:
        token, reused = await helper.mint(provider, helper.admin_credentials())

    assert token == MINTED_TOKEN
    assert not reused
    assert [request.route for request in grafana_stub.requests] == [
        StubRoute.SEARCH,
        StubRoute.CREATE_ACCOUNT,
        StubRoute.CREATE_TOKEN,
    ]
    assert grafana_stub.calls(StubRoute.CREATE_ACCOUNT)[0].body == {
        "name": helper.SERVICE_ACCOUNT_NAME,
        "role": helper.SERVICE_ACCOUNT_ROLE,
        "isDisabled": False,
    }


@pytest.mark.asyncio
async def test_every_mint_request_carries_the_endpoints_own_base_path(
    grafana_stub: GrafanaStub, provider: GrafanaAuthProvider
):
    """Prefix every request path with the endpoint's own ``/graph`` path."""
    async with provider:
        await helper.mint(provider, helper.admin_credentials())

    assert grafana_stub.requests
    assert all(
        request.path.startswith("/graph/api/") for request in grafana_stub.requests
    )


@pytest.mark.asyncio
async def test_the_mint_authenticates_as_the_grafana_admin(
    grafana_stub: GrafanaStub, provider: GrafanaAuthProvider
):
    """Send the admin pair as Basic on every mint request, observed at the wire."""
    async with provider:
        await helper.mint(provider, helper.admin_credentials())

    assert grafana_stub.requests
    assert all(
        request.headers["Authorization"] == DEFAULT_BASIC
        for request in grafana_stub.requests
    )


@pytest.mark.asyncio
async def test_validation_authenticates_as_the_service_account(
    grafana_stub: GrafanaStub, provider: GrafanaAuthProvider
):
    """Send the token itself as Bearer when revalidating it, observed at the wire."""
    async with provider:
        await helper.validate_token(provider, PERSISTED_TOKEN)

    assert (
        grafana_stub.calls(StubRoute.VALIDATE)[0].headers["Authorization"]
        == f"Bearer {PERSISTED_TOKEN}"
    )


@pytest.mark.asyncio
async def test_an_existing_account_is_reused_rather_than_duplicated(
    grafana_stub: GrafanaStub, provider: GrafanaAuthProvider
):
    """Mint onto the account a previous start created, leaving one row."""
    grafana_stub.queue(
        StubRoute.SEARCH,
        StubResponse(
            {
                "totalCount": 1,
                "serviceAccounts": [{"id": ACCOUNT_ID, "name": "pmm-extensions"}],
            }
        ),
    )

    async with provider:
        token, reused = await helper.mint(provider, helper.admin_credentials())

    assert token == MINTED_TOKEN
    assert reused
    assert not grafana_stub.calls(StubRoute.CREATE_ACCOUNT)
    assert grafana_stub.calls(StubRoute.CREATE_TOKEN)[0].path.endswith(
        f"/serviceaccounts/{ACCOUNT_ID}/tokens"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "found",
    [
        {
            "totalCount": 1,
            "serviceAccounts": [{"id": 9, "name": "pmm-extensions-legacy"}],
        },
        {"totalCount": 0},
        {"unexpected": 1},
        [],
    ],
    ids=["substring-match", "no-list", "unexpected-mapping", "list"],
)
@pytest.mark.usefixtures("provider")
async def test_only_an_exact_name_counts_as_the_account(
    grafana_stub: GrafanaStub, provider: GrafanaAuthProvider, found: Any
):
    """Create the account unless the search answered one named exactly ``pmm-extensions``.

    Grafana's ``query`` filters by substring, so a ``pmm-extensions-legacy`` account comes
    back for a ``pmm-extensions`` query and taking the first result would mint onto it.
    """
    grafana_stub.queue(StubRoute.SEARCH, StubResponse(found))

    async with provider:
        await helper.mint(provider, helper.admin_credentials())

    assert grafana_stub.calls(StubRoute.CREATE_ACCOUNT)
    assert grafana_stub.calls(StubRoute.CREATE_TOKEN)[0].path.endswith(
        f"/serviceaccounts/{ACCOUNT_ID}/tokens"
    )


@pytest.mark.asyncio
async def test_a_lost_account_creation_race_mints_on_the_winner(
    grafana_stub: GrafanaStub, provider: GrafanaAuthProvider
):
    """Mint on the account a concurrent side-car created rather than giving up.

    Two fresh side-cars can both search before either creates, so the loser sees
    Grafana refuse its own creation. That refusal is a race, not a fault, and its
    status varies by Grafana release — a second lookup is what settles it.
    """
    grafana_stub.queue(
        StubRoute.SEARCH,
        StubResponse({"totalCount": 0, "serviceAccounts": []}),
        StubResponse(
            {
                "totalCount": 1,
                "serviceAccounts": [{"id": ACCOUNT_ID, "name": "pmm-extensions"}],
            }
        ),
    )
    grafana_stub.queue(
        StubRoute.CREATE_ACCOUNT,
        StubResponse({"message": "service account already exists"}, status=400),
    )

    async with provider:
        token, reused = await helper.mint(provider, helper.admin_credentials())

    assert token == MINTED_TOKEN
    assert reused
    assert grafana_stub.calls(StubRoute.CREATE_TOKEN)[0].path.endswith(
        f"/serviceaccounts/{ACCOUNT_ID}/tokens"
    )


@pytest.mark.asyncio
async def test_a_refused_creation_with_no_winner_is_reported(
    grafana_stub: GrafanaStub, provider: GrafanaAuthProvider
):
    """Surface a genuine refusal, which the race recovery must not swallow."""
    grafana_stub.queue(StubRoute.CREATE_ACCOUNT, StubResponse(status=400))

    async with provider:
        with pytest.raises(helper.MintError):
            await helper.mint_with_retry(provider, helper.admin_credentials())

    assert not grafana_stub.calls(StubRoute.CREATE_TOKEN)


@pytest.mark.asyncio
async def test_each_mint_names_its_token_distinctly(
    grafana_stub: GrafanaStub,
    provider: GrafanaAuthProvider,
    monkeypatch: pytest.MonkeyPatch,
):
    """Separate two mints in the same second, which the timestamp alone cannot."""
    monkeypatch.setattr(
        helper, "utc_now", lambda: datetime.fromisoformat("2026-08-20T10:00:00+00:00")
    )

    async with provider:
        await helper.mint(provider, helper.admin_credentials())
        await helper.mint(provider, helper.admin_credentials())

    first, second = grafana_stub.calls(StubRoute.CREATE_TOKEN)
    assert first.body["name"] != second.body["name"]


@pytest.mark.asyncio
async def test_the_minted_token_is_asked_to_never_expire(
    grafana_stub: GrafanaStub, provider: GrafanaAuthProvider
):
    """Omit ``secondsToLive``, which is what makes Grafana mint a lasting token."""
    async with provider:
        await helper.mint(provider, helper.admin_credentials())

    assert set(grafana_stub.calls(StubRoute.CREATE_TOKEN)[0].body) == {"name"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "created",
    [
        {"id": 1, "name": "pmm-extensions"},
        {"id": 1, "key": ""},
        {"id": 1, "key": "   "},
    ],
    ids=["absent", "blank", "whitespace"],
)
async def test_a_token_response_without_a_key_is_reported_not_retried(
    grafana_stub: GrafanaStub, provider: GrafanaAuthProvider, created: dict[str, Any]
):
    """Fail fast on a contract mismatch, which a retry would only repeat."""
    grafana_stub.queue(StubRoute.CREATE_TOKEN, StubResponse(created))

    async with provider:
        with pytest.raises(helper.MintError):
            await helper.mint_with_retry(provider, helper.admin_credentials())

    assert len(grafana_stub.calls(StubRoute.CREATE_TOKEN)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403], ids=["unauthorized", "forbidden"])
async def test_a_rejected_admin_credential_is_reported_not_retried(
    grafana_stub: GrafanaStub, provider: GrafanaAuthProvider, status_code: int
):
    """Stop on a wrong admin password rather than resending it until the bound."""
    grafana_stub.queue(StubRoute.SEARCH, StubResponse(status=status_code))

    async with provider:
        with pytest.raises(helper.MintError) as raised:
            await helper.mint_with_retry(provider, helper.admin_credentials())

    assert "GF_SECURITY_ADMIN_PASSWORD" in str(raised.value)
    assert len(grafana_stub.calls(StubRoute.SEARCH)) == 1


@pytest.mark.asyncio
async def test_a_misconfigured_endpoint_is_reported_not_retried(
    grafana_stub: GrafanaStub, provider: GrafanaAuthProvider
):
    """Name the URL that answered 404 rather than retrying a wrong address."""
    grafana_stub.queue(StubRoute.SEARCH, StubResponse(status=404))

    async with provider:
        with pytest.raises(helper.MintError) as raised:
            await helper.mint_with_retry(provider, helper.admin_credentials())

    assert str(provider.endpoint) in str(raised.value)
    assert len(grafana_stub.calls(StubRoute.SEARCH)) == 1


@pytest.mark.asyncio
async def test_an_untrusted_certificate_is_reported_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Report a certificate the client does not trust, which no wait changes.

    The Grafana client reports every connection failure as a 502, the status a
    starting Grafana also gives, so without its own branch this fault would be
    waited on forever. The bound is what turns that wait into a failure here.
    """
    monkeypatch.setattr(helper, "RETRY_INTERVAL_SECONDS", 0.05)
    stub = GrafanaStub(ssl_context=self_signed_server_context(tmp_path))
    await stub.start()
    verifying = GrafanaAuthProvider(
        endpoint=stub.endpoint, service_account_token="", verify_ssl=True
    )
    try:
        async with verifying, asyncio.timeout(PATIENT_BOUND_SECONDS):
            with pytest.raises(helper.MintError) as raised:
                await helper.mint_with_retry(verifying, helper.admin_credentials())
    finally:
        await stub.stop()

    assert "certificate" in str(raised.value)
    assert stub.endpoint in str(raised.value)
    assert not stub.requests


@pytest.mark.asyncio
async def test_a_starting_grafana_is_retried_until_it_answers(
    grafana_stub: GrafanaStub,
    provider: GrafanaAuthProvider,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """Retry every transient answer a starting Grafana gives through PMM's proxy.

    The wait is reported once here rather than once per attempt: the three
    retries all fall inside one reporting interval.
    """
    monkeypatch.setattr(helper, "RETRY_INTERVAL_SECONDS", 0.05)
    grafana_stub.queue(
        StubRoute.SEARCH,
        StubResponse(status=503),
        StubResponse(status=502),
        StubResponse(status=504),
        DEFAULT_RESPONSES[StubRoute.SEARCH],
    )

    async with provider:
        token, reused = await helper.mint_with_retry(
            provider, helper.admin_credentials()
        )

    assert token == MINTED_TOKEN
    assert not reused
    assert len(grafana_stub.calls(StubRoute.SEARCH)) == EXPECTED_SEARCH_ATTEMPTS
    reported = capsys.readouterr().err
    assert reported.count(WAITING_FOR_GRAFANA) == 1
    assert grafana_stub.endpoint in reported
    assert "HTTP 503" in reported


@pytest.mark.asyncio
async def test_the_wait_is_reported_again_after_each_interval(
    grafana_stub: GrafanaStub,
    provider: GrafanaAuthProvider,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """Keep saying what the start waits for, with the answer Grafana last gave.

    The wait has no bound, so a log that fell silent after one line would read
    as a hung container.
    """
    monkeypatch.setattr(helper, "RETRY_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(helper, "WAIT_REPORT_INTERVAL_SECONDS", 0.0)
    still_starting = (StubResponse(status=503), StubResponse(status=502))
    grafana_stub.queue(
        StubRoute.SEARCH, *still_starting, DEFAULT_RESPONSES[StubRoute.SEARCH]
    )

    async with provider:
        await helper.mint_with_retry(provider, helper.admin_credentials())

    reported = capsys.readouterr().err
    assert reported.count(WAITING_FOR_GRAFANA) == len(still_starting)
    assert "HTTP 502" in reported.splitlines()[-1]


@pytest.mark.asyncio
async def test_a_hung_attempt_is_abandoned_and_retried(
    grafana_stub: GrafanaStub,
    provider: GrafanaAuthProvider,
    monkeypatch: pytest.MonkeyPatch,
):
    """Stop a stalled call the client's own 300 s total would let run on.

    ``BaseRemoteAPI`` opens its session with ``ClientTimeout(total=300, ...)``
    and offers no field to narrow it, so the per-attempt wrapper is the only
    thing that turns a Grafana which accepts a connection and never answers
    into another attempt.
    """
    monkeypatch.setattr(helper, "RETRY_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(helper, "MINT_ATTEMPT_TIMEOUT_SECONDS", SHORT_BOUND_SECONDS)
    grafana_stub.queue(
        StubRoute.SEARCH,
        StubResponse(delay=HANDLER_STALL_SECONDS, payload={}),
        DEFAULT_RESPONSES[StubRoute.SEARCH],
    )
    started = time.monotonic()

    async with provider:
        token, _ = await helper.mint_with_retry(provider, helper.admin_credentials())

    assert token == MINTED_TOKEN
    assert time.monotonic() - started < HANDLER_STALL_SECONDS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (200, "ACCEPTED"),
        (401, "REJECTED"),
        (403, "FORBIDDEN"),
        (503, "UNREACHABLE"),
    ],
)
async def test_validation_classifies_what_grafana_answers(
    grafana_stub: GrafanaStub,
    provider: GrafanaAuthProvider,
    status_code: int,
    expected: str,
):
    """Separate a rejected token from an under-privileged one and from an outage."""
    grafana_stub.queue(StubRoute.VALIDATE, StubResponse([], status=status_code))

    async with provider:
        state = await helper.validate_token(provider, PERSISTED_TOKEN)

    assert state is helper.TokenStateEnum[expected]


@pytest.mark.asyncio
async def test_a_hung_validation_is_unreachable_within_the_probe_bound(
    grafana_stub: GrafanaStub, provider: GrafanaAuthProvider
):
    """Stop waiting on a stalled Grafana instead of holding the whole start path."""
    grafana_stub.queue(
        StubRoute.VALIDATE, StubResponse(delay=HANDLER_STALL_SECONDS, payload=[])
    )
    started = time.monotonic()

    async with provider:
        state = await helper.validate_token(provider, PERSISTED_TOKEN)

    assert state is helper.TokenStateEnum.UNREACHABLE
    assert time.monotonic() - started < PROBE_TIMEOUT_SECONDS + 1


@pytest.mark.asyncio
async def test_the_minted_token_reaches_no_log_record(
    grafana_stub: GrafanaStub,
    provider: GrafanaAuthProvider,
    caplog: pytest.LogCaptureFixture,
):
    """Keep the credential out of ``docker logs`` on a DEBUG-level deployment.

    ``RemoteAPI.request`` logs the parsed response body at DEBUG, and the
    create-token response carries the token in its ``key`` field.
    """
    logging.getLogger(provider.logger_name).setLevel(logging.DEBUG)

    with caplog.at_level(logging.DEBUG):
        async with provider:
            token, reused = await helper.mint(provider, helper.admin_credentials())

    assert token == MINTED_TOKEN
    assert not reused
    assert not [
        record for record in caplog.records if MINTED_TOKEN in record.getMessage()
    ]


@pytest.mark.asyncio
async def test_quieting_the_client_log_is_scoped_to_the_mint(
    grafana_stub: GrafanaStub, provider: GrafanaAuthProvider
):
    """Restore the caller's own level, so the mute cannot silence the rest of a run."""
    logger = logging.getLogger(provider.logger_name)
    logger.setLevel(logging.DEBUG)

    async with provider:
        await helper.mint(provider, helper.admin_credentials())

    assert logger.level == logging.DEBUG


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({}, ("admin", "admin")),
        (
            {"GF_SECURITY_ADMIN_USER": "root", "GF_SECURITY_ADMIN_PASSWORD": "s3cret"},
            ("root", "s3cret"),
        ),
        ({"GF_SECURITY_ADMIN_PASSWORD": "  "}, ("admin", "admin")),
        ({"GF_SECURITY_ADMIN_PASSWORD": " pad ded "}, ("admin", " pad ded ")),
    ],
    ids=["unset", "supplied", "blank", "padded"],
)
def test_the_admin_credential_falls_back_to_grafanas_default(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    expected: tuple[str, str],
):
    """Use the operator's values, treating a blank one as unset."""
    for name in ("GF_SECURITY_ADMIN_USER", "GF_SECURITY_ADMIN_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    assert helper.admin_credentials() == b64encode_str("{}:{}".format(*expected))


@pytest.mark.parametrize(
    "content", ["", "   \n\t ", "\n"], ids=["empty", "whitespace", "newline"]
)
def test_a_blank_persisted_file_is_read_as_absent(state_dir: Path, content: str):
    """Fall through to a mint rather than resolving the empty string."""
    (state_dir / helper.PERSISTED_FILENAME).write_text(content, encoding="utf-8")

    assert helper.read_persisted_token(state_dir) is None


def test_an_unreadable_persisted_file_is_read_as_absent(state_dir: Path):
    """Treat a file the container cannot read as no token at all."""
    token_file = state_dir / helper.PERSISTED_FILENAME
    token_file.write_text(PERSISTED_TOKEN, encoding="utf-8")
    token_file.chmod(0o000)

    assert helper.read_persisted_token(state_dir) is None


def test_a_persisted_token_is_read_back_stripped(state_dir: Path):
    """Strip the trailing newline a shell redirection would leave."""
    (state_dir / helper.PERSISTED_FILENAME).write_text(
        f"  {PERSISTED_TOKEN}\n", encoding="utf-8"
    )

    assert helper.read_persisted_token(state_dir) == PERSISTED_TOKEN


def test_the_persisted_token_is_written_owner_only(state_dir: Path):
    """Keep the credential unreadable to anything but the side-car's own user."""
    assert helper.write_persisted_token(state_dir, MINTED_TOKEN)

    token_file = state_dir / helper.PERSISTED_FILENAME
    assert token_file.read_text(encoding="utf-8").strip() == MINTED_TOKEN
    assert stat.S_IMODE(token_file.stat().st_mode) == TOKEN_FILE_MODE


def test_a_missing_state_directory_is_created_owner_only(tmp_path: Path):
    """Create the mount point's directory rather than losing the token."""
    directory = tmp_path / "absent" / "state"

    assert helper.write_persisted_token(directory, MINTED_TOKEN)
    assert stat.S_IMODE(directory.stat().st_mode) == STATE_DIR_MODE


def test_a_read_only_state_directory_does_not_fail_the_run(tmp_path: Path):
    """Warn that the token will not survive a restart, but keep it for this run."""
    directory = tmp_path / "state"
    directory.mkdir(mode=0o500)

    assert helper.write_persisted_token(directory, MINTED_TOKEN) is False


@pytest.mark.parametrize("raw", ["90", "nonsense"], ids=["a-number", "text"])
def test_a_set_mint_timeout_is_reported_as_no_longer_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], raw: str
):
    """Tell a deployment that sets the retired bound that nothing reads it.

    The value is not validated, because it is not used. A deployment that sets
    ``90`` waits past it, and this line is what keeps that from being silent.
    """
    monkeypatch.setenv("EXTENSIONS_GRAFANA_MINT_TIMEOUT", raw)

    helper.report_retired_timeout()

    reported = capsys.readouterr().err
    assert "EXTENSIONS_GRAFANA_MINT_TIMEOUT" in reported
    assert "no longer read" in reported


@pytest.mark.parametrize("raw", [None, "", "  "], ids=["unset", "empty", "blank"])
def test_an_unset_mint_timeout_is_not_mentioned(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raw: str | None,
):
    """Say nothing about the retired bound to a deployment that never set it."""
    monkeypatch.delenv("EXTENSIONS_GRAFANA_MINT_TIMEOUT", raising=False)
    if raw is not None:
        monkeypatch.setenv("EXTENSIONS_GRAFANA_MINT_TIMEOUT", raw)

    helper.report_retired_timeout()

    assert capsys.readouterr().err == ""


@pytest.mark.asyncio
async def test_a_first_start_mints_and_persists_a_token(
    grafana_stub: GrafanaStub, tmp_path: Path, state_dir: Path
):
    """Resolve a token on a fresh install with nothing configured anywhere.

    The create path already requests Admin, so the post-mint role probe is
    skipped — only a reused account needs that round trip.
    """
    run = await run_helper(
        profile_cwd(tmp_path),
        AUTH__PROVIDER__GRAFANA__ENDPOINT=grafana_stub.endpoint,
        EXTENSIONS_STATE_DIR=str(state_dir),
    )

    assert run.returncode == 0, run.stderr
    assert run.token == MINTED_TOKEN
    assert helper.read_persisted_token(state_dir) == MINTED_TOKEN
    assert grafana_stub.calls(StubRoute.CREATE_ACCOUNT)
    assert not grafana_stub.calls(StubRoute.VALIDATE)


@pytest.mark.asyncio
async def test_a_reused_account_at_admin_probes_without_warning(
    grafana_stub: GrafanaStub, tmp_path: Path, state_dir: Path
):
    """Probe a reused account that already holds Admin, and stay quiet on ACCEPTED.

    Guards the FORBIDDEN-only warn: a flipped guard would emit a false role-gap
    diagnostic on every healthy reuse.
    """
    grafana_stub.queue(
        StubRoute.SEARCH,
        StubResponse(
            {
                "totalCount": 1,
                "serviceAccounts": [{"id": ACCOUNT_ID, "name": "pmm-extensions"}],
            }
        ),
    )

    run = await run_helper(
        profile_cwd(tmp_path),
        AUTH__PROVIDER__GRAFANA__ENDPOINT=grafana_stub.endpoint,
        EXTENSIONS_STATE_DIR=str(state_dir),
    )

    assert run.returncode == 0, run.stderr
    assert run.token == MINTED_TOKEN
    assert helper.read_persisted_token(state_dir) == MINTED_TOKEN
    assert (
        grafana_stub.calls(StubRoute.VALIDATE)[0].headers["Authorization"]
        == f"Bearer {MINTED_TOKEN}"
    )
    assert "ranks below" not in run.stderr


@pytest.mark.asyncio
async def test_a_reused_account_below_admin_warns_after_mint(
    grafana_stub: GrafanaStub, tmp_path: Path, state_dir: Path
):
    """Warn on FORBIDDEN after minting onto an existing under-privileged account.

    The token is still returned and persisted: a re-mint cannot raise the role.
    """
    grafana_stub.queue(
        StubRoute.SEARCH,
        StubResponse(
            {
                "totalCount": 1,
                "serviceAccounts": [{"id": ACCOUNT_ID, "name": "pmm-extensions"}],
            }
        ),
    )
    grafana_stub.queue(StubRoute.VALIDATE, StubResponse(status=403))

    run = await run_helper(
        profile_cwd(tmp_path),
        AUTH__PROVIDER__GRAFANA__ENDPOINT=grafana_stub.endpoint,
        EXTENSIONS_STATE_DIR=str(state_dir),
    )

    assert run.returncode == 0, run.stderr
    assert run.token == MINTED_TOKEN
    assert helper.read_persisted_token(state_dir) == MINTED_TOKEN
    assert (
        grafana_stub.calls(StubRoute.VALIDATE)[0].headers["Authorization"]
        == f"Bearer {MINTED_TOKEN}"
    )
    assert f"{helper.SERVICE_ACCOUNT_NAME!r}" in run.stderr
    assert helper.SERVICE_ACCOUNT_ROLE in run.stderr
    assert "ranks below" in run.stderr


@pytest.mark.asyncio
async def test_a_reused_account_with_unreachable_probe_warns_after_mint(
    grafana_stub: GrafanaStub, tmp_path: Path, state_dir: Path
):
    """Warn on UNREACHABLE after minting onto an existing account.

    Grafana may go down between the mint and the probe; the token is still
    returned and persisted, matching :func:`keep_persisted_token`.
    """
    grafana_stub.queue(
        StubRoute.SEARCH,
        StubResponse(
            {
                "totalCount": 1,
                "serviceAccounts": [{"id": ACCOUNT_ID, "name": "pmm-extensions"}],
            }
        ),
    )
    grafana_stub.queue(StubRoute.VALIDATE, StubResponse(status=503))

    run = await run_helper(
        profile_cwd(tmp_path),
        AUTH__PROVIDER__GRAFANA__ENDPOINT=grafana_stub.endpoint,
        EXTENSIONS_STATE_DIR=str(state_dir),
    )

    assert run.returncode == 0, run.stderr
    assert run.token == MINTED_TOKEN
    assert helper.read_persisted_token(state_dir) == MINTED_TOKEN
    assert (
        grafana_stub.calls(StubRoute.VALIDATE)[0].headers["Authorization"]
        == f"Bearer {MINTED_TOKEN}"
    )
    assert "Could not reach Grafana" in run.stderr
    assert "probe the freshly minted token" in run.stderr
    assert "using it unvalidated" in run.stderr


@pytest.mark.asyncio
async def test_a_race_recovery_reuse_probes_the_minted_token(
    grafana_stub: GrafanaStub, tmp_path: Path, state_dir: Path
):
    """Probe after minting onto the account a concurrent side-car created.

    Losing the create race still reuses an account whose role PMM Extensions never set, so
    the same FORBIDDEN diagnostic applies.
    """
    grafana_stub.queue(
        StubRoute.SEARCH,
        StubResponse({"totalCount": 0, "serviceAccounts": []}),
        StubResponse(
            {
                "totalCount": 1,
                "serviceAccounts": [{"id": ACCOUNT_ID, "name": "pmm-extensions"}],
            }
        ),
    )
    grafana_stub.queue(
        StubRoute.CREATE_ACCOUNT,
        StubResponse({"message": "service account already exists"}, status=400),
    )
    grafana_stub.queue(StubRoute.VALIDATE, StubResponse(status=403))

    run = await run_helper(
        profile_cwd(tmp_path),
        AUTH__PROVIDER__GRAFANA__ENDPOINT=grafana_stub.endpoint,
        EXTENSIONS_STATE_DIR=str(state_dir),
    )

    assert run.returncode == 0, run.stderr
    assert run.token == MINTED_TOKEN
    assert helper.read_persisted_token(state_dir) == MINTED_TOKEN
    assert (
        grafana_stub.calls(StubRoute.VALIDATE)[0].headers["Authorization"]
        == f"Bearer {MINTED_TOKEN}"
    )
    assert "ranks below" in run.stderr
    assert helper.SERVICE_ACCOUNT_ROLE in run.stderr


@pytest.mark.asyncio
async def test_a_state_directory_it_cannot_write_still_resolves_a_token(
    grafana_stub: GrafanaStub, tmp_path: Path
):
    """Start with a token that cannot be persisted rather than not starting.

    Losing the token at the next start costs a re-mint; refusing to resolve one
    costs Grafana-backed sign-in and the PMM syncer for the whole run.
    """
    directory = tmp_path / "read-only-state"
    directory.mkdir(mode=0o500)

    run = await run_helper(
        profile_cwd(tmp_path),
        AUTH__PROVIDER__GRAFANA__ENDPOINT=grafana_stub.endpoint,
        EXTENSIONS_STATE_DIR=str(directory),
    )

    assert run.returncode == 0, run.stderr
    assert run.token == MINTED_TOKEN
    assert "Could not persist" in run.stderr


@pytest.mark.asyncio
async def test_a_persisted_token_grafana_accepts_is_reused(
    grafana_stub: GrafanaStub, tmp_path: Path, state_dir: Path
):
    """Mint nothing when the token a previous start persisted still works."""
    helper.write_persisted_token(state_dir, PERSISTED_TOKEN)

    run = await run_helper(
        profile_cwd(tmp_path),
        AUTH__PROVIDER__GRAFANA__ENDPOINT=grafana_stub.endpoint,
        EXTENSIONS_STATE_DIR=str(state_dir),
    )

    assert run.returncode == 0, run.stderr
    assert run.token == PERSISTED_TOKEN
    assert not grafana_stub.calls(StubRoute.CREATE_TOKEN)


@pytest.mark.asyncio
async def test_a_persisted_token_grafana_rejects_is_replaced(
    grafana_stub: GrafanaStub, tmp_path: Path, state_dir: Path
):
    """Mint a replacement and overwrite the file when Grafana rejects the token."""
    helper.write_persisted_token(state_dir, PERSISTED_TOKEN)
    grafana_stub.queue(StubRoute.VALIDATE, StubResponse(status=401))

    run = await run_helper(
        profile_cwd(tmp_path),
        AUTH__PROVIDER__GRAFANA__ENDPOINT=grafana_stub.endpoint,
        EXTENSIONS_STATE_DIR=str(state_dir),
    )

    assert run.returncode == 0, run.stderr
    assert run.token == MINTED_TOKEN
    assert helper.read_persisted_token(state_dir) == MINTED_TOKEN


@pytest.mark.asyncio
async def test_a_persisted_token_survives_a_restart(
    grafana_stub: GrafanaStub, tmp_path: Path, state_dir: Path
):
    """Read the token back on the next start, so a restart needs no admin credential."""
    environment = {
        "AUTH__PROVIDER__GRAFANA__ENDPOINT": grafana_stub.endpoint,
        "EXTENSIONS_STATE_DIR": str(state_dir),
    }
    cwd = profile_cwd(tmp_path)

    first = await run_helper(cwd, **environment)
    second = await run_helper(cwd, **environment)

    assert first.token == second.token == MINTED_TOKEN
    assert len(grafana_stub.calls(StubRoute.CREATE_TOKEN)) == 1


@pytest.mark.asyncio
async def test_an_unreachable_grafana_keeps_the_persisted_token_without_waiting(
    tmp_path: Path, state_dir: Path, unused_tcp_port: int
):
    """Keep a working credential through an outage, without waiting for Grafana.

    Only a start that has to mint waits. One that already holds a token must
    come up during an outage, or a Grafana restart would take the side-car down
    with it.
    """
    helper.write_persisted_token(state_dir, PERSISTED_TOKEN)

    run = await run_helper(
        profile_cwd(tmp_path),
        AUTH__PROVIDER__GRAFANA__ENDPOINT=f"http://127.0.0.1:{unused_tcp_port}/graph",
        EXTENSIONS_STATE_DIR=str(state_dir),
    )

    assert run.returncode == 0, run.stderr
    assert run.token == PERSISTED_TOKEN
    assert run.elapsed < PATIENT_BOUND_SECONDS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configured",
    ["AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN", "PMM__API_KEY"],
)
async def test_a_configured_token_is_never_minted_on_top_of(
    grafana_stub: GrafanaStub, tmp_path: Path, state_dir: Path, configured: str
):
    """Export either already-configured mint-gate name without minting on top."""
    run = await run_helper(
        profile_cwd(tmp_path),
        AUTH__PROVIDER__GRAFANA__ENDPOINT=grafana_stub.endpoint,
        EXTENSIONS_STATE_DIR=str(state_dir),
        **{configured: "glsa_configured_by_the_operator"},
    )

    assert run.returncode == 0, run.stderr
    assert run.token == "glsa_configured_by_the_operator"
    assert not grafana_stub.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mounted",
    ["AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN", "PMM__API_KEY"],
)
async def test_a_mounted_token_is_never_minted_on_top_of(
    grafana_stub: GrafanaStub, tmp_path: Path, state_dir: Path, mounted: str
):
    """Export a SECRETS_DIR-mounted mint-gate name without minting on top."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / mounted).write_text("glsa_mounted\n", encoding="utf-8")

    run = await run_helper(
        profile_cwd(tmp_path),
        AUTH__PROVIDER__GRAFANA__ENDPOINT=grafana_stub.endpoint,
        EXTENSIONS_STATE_DIR=str(state_dir),
        SECRETS_DIR=str(secrets_dir),
    )

    assert run.returncode == 0, run.stderr
    assert run.token == "glsa_mounted"
    assert not grafana_stub.requests


@pytest.mark.asyncio
async def test_service_account_token_wins_over_a_differing_pmm_api_key(
    grafana_stub: GrafanaStub, tmp_path: Path, state_dir: Path
):
    """Prefer AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN when both differ."""
    run = await run_helper(
        profile_cwd(tmp_path),
        AUTH__PROVIDER__GRAFANA__ENDPOINT=grafana_stub.endpoint,
        EXTENSIONS_STATE_DIR=str(state_dir),
        AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN="glsa_service_account",
        PMM__API_KEY="glsa_pmm_api_key",
    )

    assert run.returncode == 0, run.stderr
    assert run.token == "glsa_service_account"
    assert not grafana_stub.requests


@pytest.mark.asyncio
async def test_a_mounted_service_account_token_wins_over_a_differing_pmm_api_key(
    grafana_stub: GrafanaStub, tmp_path: Path, state_dir: Path
):
    """Prefer a mounted service-account token when both mint-gate files differ."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "AUTH__PROVIDER__GRAFANA__SERVICE_ACCOUNT_TOKEN").write_text(
        "glsa_service_account\n", encoding="utf-8"
    )
    (secrets_dir / "PMM__API_KEY").write_text("glsa_pmm_api_key\n", encoding="utf-8")

    run = await run_helper(
        profile_cwd(tmp_path),
        AUTH__PROVIDER__GRAFANA__ENDPOINT=grafana_stub.endpoint,
        EXTENSIONS_STATE_DIR=str(state_dir),
        SECRETS_DIR=str(secrets_dir),
    )

    assert run.returncode == 0, run.stderr
    assert run.token == "glsa_service_account"
    assert not grafana_stub.requests


@pytest.mark.asyncio
async def test_a_non_grafana_provider_owes_no_token(tmp_path: Path, state_dir: Path):
    """Leave a casdoor deployment exactly as it was, with nothing to resolve.

    Exit 0 with no token and no diagnostic is the helper's statement that no
    token is owed. The Grafana provider is left out of the environment
    altogether: an endpoint with no token beside it does not validate, and the
    run would then end on unresolved settings rather than on the provider.
    """
    run = await run_helper(
        profile_cwd(tmp_path, ROOT_PROFILE),
        FASTAPI_ENV="development",
        EXTENSIONS_STATE_DIR=str(state_dir),
        AUTH__PROVIDER__CASDOOR__CLIENT_ID="client-id",
        AUTH__PROVIDER__CASDOOR__CLIENT_SECRET="client-secret",
    )

    assert run.returncode == 0, run.stderr
    assert run.token == ""
    assert "[grafana-mint]" not in run.stderr
    assert not (state_dir / helper.PERSISTED_FILENAME).exists()


@pytest.mark.asyncio
async def test_unresolvable_settings_are_reported_as_an_unresolved_token(
    tmp_path: Path, state_dir: Path
):
    """Exit non-zero without a traceback when the settings cannot be read.

    Exit 0 with no token means the provider is provably not Grafana, and the
    entrypoint marks that start as holding all it needs. Settings that did not
    resolve prove nothing of the kind, and the five programs say why.
    """
    run = await run_helper(
        profile_cwd(tmp_path, ROOT_PROFILE),
        FASTAPI_ENV="development",
        EXTENSIONS_STATE_DIR=str(state_dir),
    )

    assert run.returncode == 1
    assert run.token == ""
    assert run.stderr.strip()
    assert "Traceback" not in run.stderr


@pytest.mark.asyncio
async def test_a_grafana_that_is_not_up_is_waited_for_by_name(
    tmp_path: Path, state_dir: Path, unused_tcp_port: int
):
    """Hold the start on a Grafana nothing listens at yet, and say which one.

    A helper that printed nothing and exited would start every program without
    the token it needs, so the helper is still running after it has reported
    the wait. The report carries the connection error under the 502 the client
    reports it as, which is what tells a refused connection from a failed
    handshake.
    """
    endpoint = f"http://127.0.0.1:{unused_tcp_port}/graph"
    process = await start_helper(
        profile_cwd(tmp_path),
        AUTH__PROVIDER__GRAFANA__ENDPOINT=endpoint,
        EXTENSIONS_STATE_DIR=str(state_dir),
    )
    assert process.stderr is not None
    reported = ""
    try:
        async with asyncio.timeout(HELPER_SPEAKS_SECONDS):
            while WAITING_FOR_GRAFANA not in reported:
                line = await process.stderr.readline()
                assert line, f"the helper exited instead of waiting: {reported}"
                reported += line.decode()
        still_waiting = process.returncode is None
    finally:
        process.kill()
        await process.communicate()

    assert still_waiting
    assert endpoint in reported
    assert "ClientConnectorError" in reported
    assert "Traceback" not in reported


@pytest.mark.asyncio
async def test_a_grafana_that_starts_late_is_minted_against_without_a_restart(
    grafana_stub: GrafanaStub, tmp_path: Path, state_dir: Path
):
    """Resolve the token from the same helper run that found Grafana not ready."""
    grafana_stub.queue(
        StubRoute.SEARCH,
        StubResponse(status=503),
        DEFAULT_RESPONSES[StubRoute.SEARCH],
    )

    run = await run_helper(
        profile_cwd(tmp_path),
        AUTH__PROVIDER__GRAFANA__ENDPOINT=grafana_stub.endpoint,
        EXTENSIONS_STATE_DIR=str(state_dir),
    )

    assert run.returncode == 0, run.stderr
    assert run.token == MINTED_TOKEN
    assert WAITING_FOR_GRAFANA in run.stderr
    assert helper.read_persisted_token(state_dir) == MINTED_TOKEN
