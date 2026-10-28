"""Exercise a synthetic kernel boundary and the real isolated ingestion path."""

import json
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from time import monotonic, sleep
from typing import Any, cast

from scripts import smoke_publication as publication
from scripts.isolated_retrieval_fixture import (
    ALIASES,
    ARTICLE_URL,
    CONTROL_ORIGIN,
    DENY_IPS,
    DNS_IP,
    EXPECTED_NORMALIZED_HASH,
    EXPECTED_TITLE,
    FEED_URL,
    MIXED_HOST,
    NETWORKS,
    POLICY_TABLE,
    PRIVATE_HOST,
    PUBLIC_HOST,
    PUBLIC_IP,
    REDIRECT_URL,
    RESOLVER,
    VOLUME,
    IsolatedRetrievalFixture,
    create_fixture,
    validate_fixture_models,
    validate_policy_readback,
)
from scripts.publication_smoke_fixture import FixtureBoundaryError, PublicationSmokeFixture

SmokeFailure = publication.SmokeFailure
DENIED_PORTS = (5432, 8001, 11434, 8080)
STATE_KEYS = {"dns_queries", "source_requests", "forbidden_requests", "source_443_connections"}
SOURCE_ID = "10000000-0000-4000-8000-000000000001"
FEED_ID = "10000000-0000-4000-8000-000000000002"
JOB_ID = "10000000-0000-4000-8000-000000000003"

# Private stdin carries synthetic parameters. Only bounded booleans/counts return.
DATABASE_PROBE = r"""
import json, sys, uuid
from sqlalchemy import select, insert, func, text
from alembic.config import Config
from alembic.script import ScriptDirectory
from primary_signal.db import DatabaseSettings, create_database_engine
from primary_signal.entrypoints.migrate import migration_config_path
from primary_signal.identity.urls import identify_url
from primary_signal.sources.models import Source, Feed
from primary_signal.jobs.models import Job, JobAttempt
from primary_signal.ingestion.models import Article, ArticleUrl, FeedPollRun, FeedEntry, FetchAttempt, ContentVersion
from primary_signal.ingestion.inventory import ArticleInventoryRepository, ArticleListQuery
p=json.load(sys.stdin)
engine=create_database_engine(DatabaseSettings(statement_timeout_ms=3000,lock_timeout_ms=1000))
try:
    with engine.begin() as c:
        assert c.execute(text('SELECT current_database()')).scalar_one()==p['database'] and p['database'].endswith('_test')
        assert c.execute(text('SELECT current_user')).scalar_one()==p['migration_role']
        assert c.execute(text('SELECT rolsuper FROM pg_roles WHERE rolname=current_user')).scalar_one() is False
        assert c.execute(text('SELECT version_num FROM primary_signal.alembic_version')).scalar_one()==ScriptDirectory.from_config(Config(migration_config_path())).get_current_head()
        sid,fid,jid=(uuid.UUID(p[k]) for k in ('source_id','feed_id','job_id'))
        if p['mode']=='seed':
            assert c.execute(select(func.count()).select_from(Job)).scalar_one()==0
            now=c.execute(select(func.clock_timestamp())).scalar_one()
            identity=identify_url(p['feed_url'])
            c.execute(insert(Source).values(id=sid,source_key='synthetic-boundary',name='Synthetic boundary source',homepage_url=p['article_url']))
            c.execute(insert(Feed).values(id=fid,source_id=sid,name='Synthetic boundary feed',configured_url=p['feed_url'],normalized_url=identity.normalized_url,url_hash=identity.url_hash,url_normalization_version=identity.normalization_version,next_poll_at=now))
            c.execute(insert(Job).values(id=jid,job_type='feeds.poll',payload_version=1,payload={'feed_id':str(fid)},queue='ingestion',priority=100,run_after=now,max_attempts=3))
            result={'seeded':True}
        else:
            jobs=c.execute(select(Job.id,Job.job_type,Job.status,Job.attempt_count,Job.payload).order_by(Job.job_type)).all()
            attempts=c.execute(select(JobAttempt.status)).scalars().all()
            articles=c.execute(select(Article.id,Article.current_canonical_url_id,Article.current_content_version_id)).all()
            urls=c.execute(select(ArticleUrl.id,ArticleUrl.article_id,ArticleUrl.normalized_url)).all()
            polls=c.execute(select(FeedPollRun.status)).scalars().all()
            entries=c.execute(select(FeedEntry.article_id)).scalars().all()
            versions=c.execute(select(ContentVersion.id,ContentVersion.article_id,ContentVersion.normalized_content_hash)).all()
            fetches=c.execute(select(FetchAttempt.status)).scalars().all()
            page=ArticleInventoryRepository(c).list_articles(ArticleListQuery(search='cobalt recovery',source_key='synthetic-boundary'))
            lineage=len(articles)==len(urls)==len(entries)==1 and urls[0].article_id==articles[0].id==entries[0] and articles[0].current_canonical_url_id==urls[0].id and urls[0].normalized_url==p['article_url']
            successor=next((j for j in jobs if j.job_type=='articles.retrieve'),None)
            linked=bool(lineage and successor and successor.payload=={'article_id':str(articles[0].id),'article_url_id':str(urls[0].id)})
            complete=len(versions)==1 and versions[0].article_id==articles[0].id and articles[0].current_content_version_id==versions[0].id and versions[0].normalized_content_hash==p['content_hash'] if len(articles)==1 else False
            search=len(page.items)==1 and len(articles)==1 and page.items[0].article_id==articles[0].id and page.items[0].title==p['title']
            result={'jobs':[[j.job_type,j.status,j.attempt_count] for j in jobs],'attempts':sorted(attempts),'polls':polls,'lineage':lineage,'successor_linked':linked,'versions':len(versions),'fetches':fetches,'current_matches':complete,'search_matches':search,'search_count':len(page.items)}
    print(json.dumps(result))
finally:
    engine.dispose()
"""

RAW_PROBE = r"""
import json, socket, struct, sys
p=json.load(sys.stdin)
result={'connected':False,'response':False}
s=None
try:
    family=socket.AF_INET6 if ':' in p['address'] else socket.AF_INET
    s=socket.socket(family,socket.SOCK_DGRAM if p['kind']=='dns_udp' else socket.SOCK_STREAM)
    s.settimeout(.7)
    s.connect((p['address'],p['port']))
    result['connected']=True
    if p['kind'].startswith('dns_'):
        host=b''.join(bytes([len(x)])+x.encode() for x in p['hostname'].split('.'))+b'\0'
        query=struct.pack('!6H',1234,0x100,1,0,0,0)+host+struct.pack('!HH',1,1)
        s.sendall(query if p['kind']=='dns_udp' else struct.pack('!H',len(query))+query)
        data=s.recv(2048)
        if p['kind']=='dns_tcp':
            length=struct.unpack('!H',data[:2])[0];data=data[2:]
            while len(data)<length:
                piece=s.recv(length-len(data))
                if not piece:break
                data+=piece
        result['response']=len(data)>=12 and struct.unpack('!6H',data[:12])[:4]==(1234,0x8180,1,1) and socket.inet_aton(p['expected']) in data
    elif p['kind']=='http':
        s.sendall(('GET /article HTTP/1.1\r\nHost: '+p['hostname']+'\r\nConnection: close\r\n\r\n').encode())
        result['response']=s.recv(128).startswith(b'HTTP/1.0 200')
except OSError:
    pass
finally:
    if s is not None:s.close()
print(json.dumps(result))
"""

RPC_PROBE = r"""
import json, sys, urllib.request, urllib.error
p=json.load(sys.stdin)
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
request=urllib.request.Request('http://192.0.2.10:8765/v1/articles/fetch',data=json.dumps({'url':p['url']}).encode(),headers={'Content-Type':'application/json'})
try:
    with opener.open(request,timeout=10) as response:
        result={'status':response.status,'error':None}
except urllib.error.HTTPError as error:
    result={'status':error.code,'error':json.loads(error.read(16384)).get('error')}
print(json.dumps(result))
"""


def _shared(fixture: IsolatedRetrievalFixture) -> PublicationSmokeFixture:
    return cast(PublicationSmokeFixture, cast(object, fixture))


def _run(
    fixture: IsolatedRetrievalFixture, args: tuple[str, ...], **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    return publication._command(_shared(fixture), args, **kwargs)  # pyright: ignore[reportPrivateUsage]


def _docker(fixture: IsolatedRetrievalFixture, *args: str, **kwargs: Any) -> str:
    return _run(fixture, (*fixture.docker_args, *args), **kwargs).stdout.strip()


def _compose(fixture: IsolatedRetrievalFixture, *args: str, **kwargs: Any) -> str:
    return _run(
        fixture, (*fixture.docker_args, *fixture.compose_args, *args), **kwargs
    ).stdout.strip()


def _json(value: str, stage: str) -> dict[str, Any]:
    try:
        result = json.loads(value)
    except ValueError, TypeError:
        raise SmokeFailure(stage) from None
    if not isinstance(result, dict):
        raise SmokeFailure(stage)
    return cast(dict[str, Any], result)


def _inspect(
    fixture: IsolatedRetrievalFixture, service: str, identifier: str | None = None
) -> dict[str, Any]:
    identifier = identifier or _compose(
        fixture, "ps", "--all", "--quiet", service, stage="owned container"
    )
    if re.fullmatch(r"[0-9a-f]{64}", identifier) is None:
        raise SmokeFailure("owned container")
    state = _json(
        _docker(fixture, "inspect", "--format", "{{json .}}", identifier, stage="owned container"),
        "owned container",
    )
    labels = state.get("Config", {}).get("Labels", {})
    host = state.get("HostConfig", {})
    ports = cast(dict[str, Any], state.get("NetworkSettings", {}).get("Ports") or {})
    if (
        state.get("Id") != identifier
        or labels.get("com.docker.compose.project") != fixture.project
        or labels.get("com.docker.compose.service") != service
        or str(labels.get("com.docker.compose.oneoff", "")).lower() != "false"
        or host.get("Privileged") is not False
        or host.get("PidMode") not in ("", None)
        or host.get("NetworkMode") in ("host", "none")
        or host.get("Devices")
        or host.get("RestartPolicy", {}).get("Name") != "no"
        or any(ports.values())
    ):
        raise SmokeFailure("owned container boundary")
    if service != "database":
        adds = {name.removeprefix("CAP_") for name in cast(list[str], host.get("CapAdd") or [])}
        if set(host.get("CapDrop") or []) != {"ALL"} or adds != (
            {"NET_ADMIN"} if service == "policy" else set()
        ):
            raise SmokeFailure("container capability boundary")
    return state


def _wait(
    fixture: IsolatedRetrievalFixture, service: str, *, exited: bool = False, healthy: bool = False
) -> dict[str, Any]:
    deadline = monotonic() + 45
    while True:
        state = _inspect(fixture, service)
        current = state["State"]
        if exited and current["Status"] == "exited":
            if current["ExitCode"] != 0:
                raise SmokeFailure("packaged processor exit")
            return state
        if (
            not exited
            and current["Status"] == "running"
            and (not healthy or current.get("Health", {}).get("Status") == "healthy")
        ):
            return state
        if current["Status"] in ("dead", "exited") or monotonic() >= deadline:
            raise SmokeFailure("bounded container wait")
        sleep(0.2)


def _python(
    fixture: IsolatedRetrievalFixture,
    service: str,
    code: str,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = _inspect(fixture, service)
    return _json(
        _docker(
            fixture,
            "exec",
            "-i",
            state["Id"],
            "python",
            "-c",
            code,
            input_text=json.dumps(parameters or {}),
            stage="scoped namespace probe",
            timeout=20,
        ),
        "scoped namespace probe",
    )


def _policy(fixture: IsolatedRetrievalFixture) -> dict[str, int]:
    owner = _inspect(fixture, "policy")
    return validate_policy_readback(
        _docker(
            fixture,
            "exec",
            owner["Id"],
            "nft",
            "-j",
            "list",
            "table",
            "inet",
            POLICY_TABLE,
            stage="policy readback",
        )
    )


def _local_route(fixture: IsolatedRetrievalFixture, address: str) -> None:
    if address not in (*ALIASES, "::1"):
        raise SmokeFailure("probe destination allowlist")
    if _namespace(fixture, "policy") != _namespace(fixture, "probe"):
        raise SmokeFailure("probe namespace identity")
    owner = _inspect(fixture, "policy")
    ipv6 = address == "::1"
    addresses = json.loads(
        _docker(
            fixture,
            "exec",
            owner["Id"],
            "ip",
            "-j",
            "-6" if ipv6 else "-4",
            "address",
            "show",
            "dev",
            "lo",
            stage="owned alias",
        )
    )
    if not any(
        a.get("local") == address and a.get("prefixlen") == (128 if ipv6 else 32)
        for link in addresses
        for a in link.get("addr_info", [])
    ):
        raise SmokeFailure("owned alias")
    routes = json.loads(
        _docker(
            fixture,
            "exec",
            owner["Id"],
            "ip",
            "-j",
            "-6" if ipv6 else "-4",
            "route",
            "get",
            address,
            stage="owned local route",
        )
    )
    if len(routes) != 1 or routes[0].get("type") != "local" or routes[0].get("dev") != "lo":
        raise SmokeFailure("owned local route")


def _state(fixture: IsolatedRetrievalFixture) -> dict[str, int]:
    value = _python(
        fixture,
        "probe",
        "import json,urllib.request;op=urllib.request.build_opener(urllib.request.ProxyHandler({}));r=op.open('"
        + CONTROL_ORIGIN
        + "/state',timeout=2);print(r.read(4096).decode())",
    )
    if set(value) != STATE_KEYS or any(
        type(n) is not int or not 0 <= n <= 1000000 for n in value.values()
    ):
        raise SmokeFailure("synthetic receipt shape")
    return value


def _evidence(
    before: dict[str, int],
    after: dict[str, int],
    receipts_before: dict[str, int],
    receipts_after: dict[str, int],
    *,
    counter: str,
    allowed: bool,
    receipt: str | None = None,
) -> None:
    if (
        after[counter] <= before[counter]
        or receipts_after["forbidden_requests"] != receipts_before["forbidden_requests"]
    ):
        raise SmokeFailure("kernel counter and sink evidence")
    if allowed and (receipt is None or receipts_after[receipt] <= receipts_before[receipt]):
        raise SmokeFailure("allowed sink evidence")


def _raw(
    fixture: IsolatedRetrievalFixture,
    address: str,
    port: int,
    *,
    kind: str,
    counter: str,
    allowed: bool,
    receipt: str | None = None,
) -> None:
    # Independently prove locality before every connection; never send real DNS to Docker's stub.
    _local_route(fixture, address)
    before, receipts_before = _policy(fixture), _state(fixture)
    result = _python(
        fixture,
        "probe",
        RAW_PROBE,
        {
            "address": address,
            "port": port,
            "kind": kind,
            "hostname": PUBLIC_HOST,
            "expected": PUBLIC_IP,
        },
    )
    if result.get("connected") is not allowed or (
        allowed and kind != "connect" and result.get("response") is not True
    ):
        raise SmokeFailure("raw namespace connectivity")
    # Socket accept handlers may run after connect returns.
    sleep(0.1)
    _evidence(
        before,
        _policy(fixture),
        receipts_before,
        _state(fixture),
        counter=counter,
        allowed=allowed,
        receipt=receipt,
    )


def _namespace(fixture: IsolatedRetrievalFixture, service: str) -> str:
    result = _python(
        fixture,
        service,
        "import json,os;print(json.dumps({'namespace':os.readlink('/proc/self/ns/net')}))",
    )
    value = result.get("namespace")
    if not isinstance(value, str) or re.fullmatch(r"net:\[[0-9]+\]", value) is None:
        raise SmokeFailure("network namespace identity")
    return value


def _ready(fixture: IsolatedRetrievalFixture) -> None:
    # Probe from the independent processor namespace without claiming work.
    code = "from primary_signal.retrieval.client import RetrieverClient;from time import monotonic,sleep;deadline=monotonic()+15\nwhile True:\n try:\n  with RetrieverClient('http://192.0.2.10:8765') as client:client.require_ready('feeds.fetch');client.require_ready('articles.fetch')\n  break\n except Exception:\n  if monotonic()>=deadline:raise\n  sleep(.2)"
    _compose(
        fixture,
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "feed_processor",
        "python",
        "-c",
        code,
        stage="real retriever readiness",
        timeout=25,
    )


def _managed_restart(fixture: IsolatedRetrievalFixture, owner: dict[str, Any]) -> None:
    services = ("retriever", "synthetic", "probe", "feed_processor", "article_processor")
    identities = {service: _inspect(fixture, service)["Id"] for service in services}
    _owner_unchanged(fixture, owner)
    # Every namespace user stops before its owner; this is a managed lifecycle proof.
    _compose(
        fixture, "stop", "--timeout", "10", *services, stage="managed workload stop", timeout=70
    )
    for service, identifier in identities.items():
        if _inspect(fixture, service, identifier)["State"]["Status"] != "exited":
            raise SmokeFailure("managed workload stop")
    _docker(fixture, "restart", owner["Id"], stage="owned policy restart", timeout=30)
    new_owner = _wait(fixture, "policy", healthy=True)
    if (
        new_owner["Id"] != owner["Id"]
        or new_owner["State"]["StartedAt"] == owner["State"]["StartedAt"]
    ):
        raise SmokeFailure("managed owner restart")
    deadline = monotonic() + 15
    while True:
        try:
            _policy(fixture)
            break
        except SmokeFailure, FixtureBoundaryError:
            if monotonic() >= deadline:
                raise
            sleep(0.2)
    for service in ("synthetic", "probe", "retriever"):
        _docker(fixture, "start", identities[service], stage="verified workload restart")
    _topology(fixture)
    _ready(fixture)
    result = _json(
        _compose(
            fixture,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "feed_processor",
            "python",
            "-c",
            RPC_PROBE,
            input_text=json.dumps({"url": ARTICLE_URL}),
            stage="post-restart real retrieval",
            timeout=30,
        ),
        "post-restart real retrieval",
    )
    if result != {"status": 200, "error": None} or _state(fixture)["forbidden_requests"] != 0:
        raise SmokeFailure("managed restart retrieval")
    _pipeline_snapshot(_database(fixture, "snapshot"), complete=True)
    _owner_unchanged(fixture, new_owner)
    _progress("managed policy restart reapplied verified rules before real retrieval resumed")


def _topology(fixture: IsolatedRetrievalFixture) -> dict[str, Any]:
    owner = _inspect(fixture, "policy")
    namespace = _namespace(fixture, "policy")
    for service in ("synthetic", "retriever", "probe"):
        if _namespace(fixture, service) != namespace:
            raise SmokeFailure("shared retrieval namespace")
        state = _inspect(fixture, service)
        if state["HostConfig"]["NetworkMode"] != "container:" + owner["Id"]:
            raise SmokeFailure("shared retrieval namespace")
        if any("DATABASE" in item.upper() for item in state["Config"].get("Env", [])):
            raise SmokeFailure("retrieval database isolation")
        if any(
            "postgres" in m.get("Destination", "")
            or "database_password" in m.get("Destination", "")
            for m in state.get("Mounts", [])
        ):
            raise SmokeFailure("retrieval credential mounts")
    resolver = _python(
        fixture,
        "retriever",
        "import json,pathlib,sys;p=json.load(sys.stdin);print(json.dumps({'exact':pathlib.Path('/etc/resolv.conf').read_text()==p['resolver']}))",
        {"resolver": RESOLVER},
    )
    if resolver != {"exact": True}:
        raise SmokeFailure("fixed resolver binding")
    if set(owner["NetworkSettings"]["Networks"]) != {fixture.project + "_" + NETWORKS[0]}:
        raise SmokeFailure("retrieval network isolation")
    return owner


def _owner_unchanged(fixture: IsolatedRetrievalFixture, owner: dict[str, Any]) -> None:
    current = _inspect(fixture, "policy", owner["Id"])
    if (
        current["State"]["Status"] != "running"
        or current["State"]["StartedAt"] != owner["State"]["StartedAt"]
    ):
        raise SmokeFailure("policy owner lifetime")
    _policy(fixture)


def _database(fixture: IsolatedRetrievalFixture, mode: str) -> dict[str, Any]:
    parameters = {
        "mode": mode,
        "database": fixture.database_name,
        "migration_role": fixture.migration_role,
        "source_id": SOURCE_ID,
        "feed_id": FEED_ID,
        "job_id": JOB_ID,
        "feed_url": FEED_URL,
        "article_url": ARTICLE_URL,
        "content_hash": EXPECTED_NORMALIZED_HASH,
        "title": EXPECTED_TITLE,
    }
    return _json(
        _compose(
            fixture,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "setup",
            "python",
            "-c",
            DATABASE_PROBE,
            input_text=json.dumps(parameters),
            stage="non-superuser database proof",
            timeout=45,
        ),
        "database proof",
    )


def _pipeline_snapshot(value: dict[str, Any], *, complete: bool) -> None:
    expected_jobs = [
        ["articles.retrieve", "succeeded" if complete else "queued", 1 if complete else 0],
        ["feeds.poll", "succeeded", 1],
    ]
    if (
        value.get("jobs") != expected_jobs
        or value.get("polls") != ["succeeded"]
        or value.get("lineage") is not True
        or value.get("successor_linked") is not True
    ):
        raise SmokeFailure("atomic feed lineage")
    if value.get("attempts") != ["succeeded"] * (2 if complete else 1):
        raise SmokeFailure("single job attempt history")
    if complete:
        if (
            value.get("versions") != 1
            or value.get("fetches") != ["fetched"]
            or value.get("current_matches") is not True
            or value.get("search_matches") is not True
            or value.get("search_count") != 1
        ):
            raise SmokeFailure("single searchable article")
    elif value.get("versions") != 0 or value.get("fetches") != [] or value.get("search_count") != 0:
        raise SmokeFailure("feed content separation")


def _worker(fixture: IsolatedRetrievalFixture, service: str, owner: dict[str, Any]) -> None:
    _owner_unchanged(fixture, owner)
    _compose(
        fixture,
        "up",
        "--detach",
        "--no-deps",
        "--no-build",
        service,
        stage="packaged worker startup",
    )
    state = _wait(fixture, service, exited=True)
    if (
        set(state["NetworkSettings"]["Networks"]) != {fixture.project + "_" + n for n in NETWORKS}
        or state["HostConfig"]["NetworkMode"] == "container:" + owner["Id"]
    ):
        raise SmokeFailure("processor namespace isolation")
    _owner_unchanged(fixture, owner)


def _rejected_targets(fixture: IsolatedRetrievalFixture) -> None:
    targets = (
        ("private", "http://" + PRIVATE_HOST + "/article"),
        ("mixed DNS", "http://" + MIXED_HOST + "/article"),
        ("redirect", REDIRECT_URL),
    )
    for label, url in targets:
        result = _json(
            _compose(
                fixture,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "feed_processor",
                "python",
                "-c",
                RPC_PROBE,
                input_text=json.dumps({"url": url}),
                stage="real API denied target",
                timeout=30,
            ),
            "real API denied target",
        )
        if result != {"status": 502, "error": "forbidden_address"}:
            raise SmokeFailure("real retriever " + label + " rejection")
        if _state(fixture)["forbidden_requests"] != 0:
            raise SmokeFailure("real retriever forbidden sink")


def _exists(fixture: IsolatedRetrievalFixture, kind: str, name: str) -> bool:
    return publication._exists(_shared(fixture), kind, name)  # pyright: ignore[reportPrivateUsage]


def _fresh(fixture: IsolatedRetrievalFixture) -> None:
    if not _docker(fixture, "version", "--format", "{{.Server.APIVersion}}", stage="local daemon"):
        raise SmokeFailure("local daemon")
    if (
        _docker(
            fixture,
            "ps",
            "--all",
            "--quiet",
            "--filter",
            "label=com.docker.compose.project=" + fixture.project,
            stage="fresh project",
        )
        or any(_exists(fixture, "image", i) for i in (fixture.image, fixture.policy_image))
        or _exists(fixture, "volume", fixture.project + "_" + VOLUME)
        or any(_exists(fixture, "network", fixture.project + "_" + n) for n in NETWORKS)
    ):
        raise SmokeFailure("fresh project")


def _cleanup(fixture: IsolatedRetrievalFixture) -> None:
    failed = False
    try:
        _compose(
            fixture,
            "down",
            "--volumes",
            "--remove-orphans",
            "--timeout",
            "10",
            stage="cleanup",
            timeout=90,
        )
    except SmokeFailure, OSError, subprocess.SubprocessError:
        failed = True
    for image in (fixture.image, fixture.policy_image):
        try:
            if _exists(fixture, "image", image):
                _docker(fixture, "image", "rm", image, stage="cleanup")
        except SmokeFailure, OSError, subprocess.SubprocessError:
            failed = True
    if (
        any(_exists(fixture, "image", i) for i in (fixture.image, fixture.policy_image))
        or _exists(fixture, "volume", fixture.project + "_" + VOLUME)
        or any(_exists(fixture, "network", fixture.project + "_" + n) for n in NETWORKS)
        or _docker(
            fixture,
            "ps",
            "--all",
            "--quiet",
            "--filter",
            "label=com.docker.compose.project=" + fixture.project,
            stage="cleanup",
        )
    ):
        failed = True
    if failed:
        raise SmokeFailure("cleanup")


def _progress(message: str) -> None:
    print("Synthetic isolated retrieval: " + message + ".", flush=True)


def _exercise(fixture: IsolatedRetrievalFixture) -> None:
    _progress("building tracked runtime and narrow policy tooling")
    context = publication._build_context(_shared(fixture))  # pyright: ignore[reportPrivateUsage]
    _docker(
        fixture,
        "build",
        "--target",
        "runtime",
        "--tag",
        fixture.image,
        str(context),
        stage="runtime build",
        timeout=600,
    )
    policy_context = fixture.directory / "policy-build"
    policy_context.mkdir(mode=0o700)
    (policy_context / "Dockerfile").write_text(fixture.expected_files["Dockerfile.policy"])
    _docker(
        fixture,
        "build",
        "--tag",
        fixture.policy_image,
        str(policy_context),
        stage="policy tooling build",
        timeout=600,
    )
    for image in (fixture.image, fixture.policy_image):
        identity = _docker(
            fixture, "image", "inspect", "--format", "{{.Id}}", image, stage="image identity"
        )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
            raise SmokeFailure("image identity")
        _progress("built image " + identity)
    _compose(
        fixture,
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        "90",
        "--no-build",
        "database",
        "policy",
        stage="guarded namespace startup",
        timeout=120,
    )
    _policy(fixture)  # Must succeed before any real retriever starts.
    _progress("verified installed default-deny policy before retriever startup")
    _compose(
        fixture,
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "setup",
        stage="non-superuser migration",
        timeout=120,
    )
    if _database(fixture, "seed") != {"seeded": True}:
        raise SmokeFailure("synthetic seed")
    _compose(
        fixture,
        "up",
        "--detach",
        "--no-deps",
        "--no-build",
        "synthetic",
        "probe",
        stage="synthetic services",
    )
    deadline = monotonic() + 15
    while True:
        try:
            _state(fixture)
            break
        except SmokeFailure:
            if monotonic() >= deadline:
                raise
            sleep(0.2)
    _raw(
        fixture,
        DNS_IP,
        53,
        kind="dns_udp",
        counter="output_dns_udp",
        allowed=True,
        receipt="dns_queries",
    )
    _raw(
        fixture,
        DNS_IP,
        53,
        kind="dns_tcp",
        counter="output_dns_tcp",
        allowed=True,
        receipt="dns_queries",
    )
    _raw(
        fixture,
        PUBLIC_IP,
        80,
        kind="http",
        counter="output_public",
        allowed=True,
        receipt="source_requests",
    )
    _raw(
        fixture,
        PUBLIC_IP,
        443,
        kind="connect",
        counter="output_public",
        allowed=True,
        receipt="source_443_connections",
    )
    for address in DENY_IPS:
        _raw(fixture, address, 80, kind="connect", counter="output_denied", allowed=False)
    _raw(fixture, "127.0.0.11", 53, kind="connect", counter="output_denied", allowed=False)
    for port in DENIED_PORTS:
        _raw(fixture, PUBLIC_IP, port, kind="connect", counter="output_drop", allowed=False)
    _raw(fixture, "::1", 80, kind="connect", counter="output_ipv6", allowed=False)
    _progress("verified DNS and HTTP ports plus denied address classes, other ports and IPv6")
    _compose(
        fixture,
        "up",
        "--detach",
        "--no-deps",
        "--no-build",
        "retriever",
        stage="real retriever startup",
    )
    _wait(fixture, "retriever")
    _ready(fixture)
    owner = _topology(fixture)
    before = _state(fixture)
    _worker(fixture, "feed_processor", owner)
    _pipeline_snapshot(_database(fixture, "snapshot"), complete=False)
    _worker(fixture, "article_processor", owner)
    _pipeline_snapshot(_database(fixture, "snapshot"), complete=True)
    after = _state(fixture)
    if (
        after["dns_queries"] <= before["dns_queries"]
        or after["source_requests"] < before["source_requests"] + 2
        or after["forbidden_requests"] != 0
    ):
        raise SmokeFailure("actual retriever sink evidence")
    _progress(
        "real feed and article processors persisted one searchable version through isolated retrieval"
    )
    _rejected_targets(fixture)
    _owner_unchanged(fixture, owner)
    _progress(
        "real retriever rejected private, mixed DNS and redirect targets without forbidden sink receipts"
    )
    _managed_restart(fixture, owner)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    status = 1
    try:
        with tempfile.TemporaryDirectory(prefix="ps-ib-", dir="/tmp") as directory:
            project = "ps-isolated-" + uuid.uuid4().hex
            fixture = create_fixture(
                root,
                Path(directory),
                project,
                image=project + ":local",
                policy_image=project + ":policy",
            )
            owned = False
            try:
                validate_fixture_models(fixture)
                _fresh(fixture)
                owned = True
                _exercise(fixture)
                status = 0
            except SmokeFailure as error:
                _progress(f"failed during {error}; command output and fixture data withheld")
            except FixtureBoundaryError, OSError, ValueError, subprocess.SubprocessError:
                _progress("failed; command output and fixture data withheld")
            finally:
                if owned:
                    try:
                        _cleanup(fixture)
                        _progress("removed only this run containers, networks, volume and images")
                    except SmokeFailure, OSError, subprocess.SubprocessError:
                        status = 1
                        _progress("cleanup failed")
    except FixtureBoundaryError, OSError, ValueError, subprocess.SubprocessError:
        _progress("fixture preparation failed")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
