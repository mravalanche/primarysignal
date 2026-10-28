"""Prove feed and article recovery in separate disposable Docker stacks."""

import argparse
import json
import re
import subprocess
import tempfile
import uuid
from collections.abc import Sequence
from pathlib import Path
from time import monotonic, sleep
from typing import Any, cast

from scripts import smoke_publication as publication
from scripts.ingestion_recovery_fixture import (
    ARTICLE_ID,
    ARTICLE_URL,
    EXPECTED_NORMALIZED_HASH,
    EXPECTED_TITLE,
    FEED_ENTRY_TITLE,
    FEED_ID,
    FEED_URL,
    JOB_ID,
    NETWORKS,
    SOURCE_ID,
    SOURCE_KEY,
    URL_ID,
    IngestionRecoveryFixture,
    RecoveryMode,
    create_fixture,
    validate_fixture_models,
)
from scripts.publication_smoke_fixture import FixtureBoundaryError, PublicationSmokeFixture

SmokeFailure = publication.SmokeFailure
SERVICES = ("database", "retriever", "worker")
VOLUME = "recovery-data"
INSPECT = (
    '{"id":{{json .Id}},"image":{{json .Image}},"labels":{{json .Config.Labels}},'
    '"status":{{json .State.Status}},"exit_code":{{json .State.ExitCode}},'
    '"started_at":{{json .State.StartedAt}},"health":{{with (index .State "Health")}}'
    "{{json .Status}}{{else}}null{{end}},"
    '"restart":{{json .HostConfig.RestartPolicy.Name}},'
    '"network_mode":{{json .HostConfig.NetworkMode}},'
    '"ports":{{json .NetworkSettings.Ports}}}'
)

# This code runs only in a short-lived setup container with the separate
# schema-owner fixture login. All identifiers and predicates arrive privately.
DATABASE_PROBE = r"""
import json, sys, uuid
from sqlalchemy import select, insert, update, func, text
from primary_signal.db import DatabaseSettings, create_database_engine
from alembic.config import Config
from alembic.script import ScriptDirectory
from primary_signal.entrypoints.migrate import migration_config_path
from primary_signal.identity.urls import identify_url
from primary_signal.ingestion.models import Article, ArticleUrl, ContentVersion, FetchAttempt, FeedPollRun, FeedEntry
from primary_signal.ingestion.inventory import ArticleInventoryRepository, ArticleListQuery
from primary_signal.jobs.models import Job, JobAttempt
from primary_signal.sources.models import Source, Feed
p = json.load(sys.stdin)
assert p['scenario'] in ('article','feed')
settings = DatabaseSettings(statement_timeout_ms=3000, lock_timeout_ms=1000)
engine = create_database_engine(settings)
sid, aid, uid, jid, fid = (uuid.UUID(p[k]) for k in ('source_id','article_id','url_id','job_id','feed_id'))
try:
    with engine.begin() as c:
        assert c.execute(text('SELECT current_database()')).scalar_one() == p['database']
        assert c.execute(text('SELECT current_user')).scalar_one() == p['migration_role']
        assert p['database'].endswith('_test')
        if p['mode'] == 'seed':
            assert c.execute(text('SELECT version_num FROM primary_signal.alembic_version')).scalar_one()==ScriptDirectory.from_config(Config(migration_config_path())).get_current_head()
            assert c.execute(select(func.count()).select_from(Job)).scalar_one() == 0
            now = c.execute(select(func.clock_timestamp())).scalar_one()
            c.execute(insert(Source).values(id=sid, source_key=p['source_key'], name='Synthetic recovery source', homepage_url='https://recovery.public.example/'))
            if p['scenario']=='article':
                identity = identify_url(p['article_url'])
                c.execute(insert(Article).values(id=aid, source_id=sid, first_seen_at=now, last_seen_at=now))
                c.execute(insert(ArticleUrl).values(id=uid, article_id=aid, original_url=p['article_url'], normalized_url=identity.normalized_url, normalized_url_hash=identity.url_hash, normalization_version=identity.normalization_version, kind='submitted', first_seen_at=now, last_seen_at=now))
                c.execute(update(Article).where(Article.id==aid).values(current_canonical_url_id=uid))
                job_type, queue, payload = 'articles.retrieve','retrieval',{'article_id':str(aid),'article_url_id':str(uid)}
            else:
                identity = identify_url(p['feed_url'])
                c.execute(insert(Feed).values(id=fid, source_id=sid, name='Synthetic recovery feed', configured_url=p['feed_url'], normalized_url=identity.normalized_url, url_hash=identity.url_hash, url_normalization_version=identity.normalization_version, next_poll_at=now))
                job_type, queue, payload = 'feeds.poll','ingestion',{'feed_id':str(fid)}
            c.execute(insert(Job).values(id=jid, job_type=job_type, payload_version=1, payload=payload, queue=queue, priority=100, run_after=now, max_attempts=3))
            result = {'seeded':True}
        elif p['mode'] == 'capture':
            row = c.execute(select(Job.status, Job.worker_id, Job.lease_token, Job.lease_expires_at, Job.attempt_count, JobAttempt.id).join(JobAttempt, JobAttempt.job_id==Job.id).where(Job.id==jid, JobAttempt.attempt_number==1)).one()
            assert row.status=='running' and row.attempt_count==1
            assert row.worker_id and row.lease_token and row.lease_expires_at
            result = {'job_id':str(jid),'attempt_id':str(row.id),'attempt_number':1,'worker_id':row.worker_id,'lease_token':str(row.lease_token),'lease_expires_at':row.lease_expires_at.isoformat()}
        elif p['mode'] == 'expire':
            old = p['lease']
            count = c.execute(update(Job).where(Job.id==jid, Job.status=='running', Job.attempt_count==1, Job.worker_id==old['worker_id'], Job.lease_token==uuid.UUID(old['lease_token'])).values(lease_expires_at=func.clock_timestamp()-text("interval '1 second'"))).rowcount
            assert count==1
            result = {'expired':True}
        elif p['mode'] == 'due':
            old = p['lease']
            assert c.execute(select(JobAttempt.status).where(JobAttempt.id==uuid.UUID(old['attempt_id']), JobAttempt.job_id==jid)).scalar_one()=='lease_expired'
            count = c.execute(update(Job).where(Job.id==jid, Job.status=='queued', Job.attempt_count==1, Job.worker_id.is_(None), Job.lease_token.is_(None)).values(run_after=func.clock_timestamp()-text("interval '1 second'"))).rowcount
            assert count==1
            result = {'due':True}
        elif p['mode'] == 'snapshot':
            job = c.execute(select(Job.status,Job.attempt_count).where(Job.id==jid)).one()
            attempts = c.execute(select(JobAttempt.status).where(JobAttempt.job_id==jid).order_by(JobAttempt.attempt_number)).scalars().all()
            articles = c.execute(select(Article.id,Article.current_canonical_url_id,Article.current_content_version_id).where(Article.source_id==sid)).all()
            article_ids = [article.id for article in articles]
            versions = c.execute(select(ContentVersion.id,ContentVersion.normalized_content_hash).where(ContentVersion.article_id.in_(article_ids))).all()
            fetches = c.execute(select(FetchAttempt.status).where(FetchAttempt.article_id.in_(article_ids))).scalars().all()
            current = articles[0].current_content_version_id if len(articles)==1 else None
            page = ArticleInventoryRepository(c).list_articles(ArticleListQuery(search='cobalt recovery',source_key=p['source_key']))
            result = {'status':job.status,'attempt_count':job.attempt_count,'attempts':attempts,'fetches':fetches,'version_count':len(versions),'current':str(current) if current else None,'single_current':len(versions)==1 and versions[0].id==current,'hash_matches':len(versions)==1 and versions[0].normalized_content_hash==p['content_hash'],'search_count':len(page.items),'search_matches':len(page.items)==1 and page.items[0].article_id==aid and page.items[0].title==p['title']}
            if p['scenario']=='feed':
                urls = c.execute(select(ArticleUrl.id,ArticleUrl.article_id,ArticleUrl.normalized_url).where(ArticleUrl.article_id.in_(article_ids))).all()
                entries = c.execute(select(FeedEntry.article_id,FeedEntry.reported_title).where(FeedEntry.feed_id==fid)).all()
                polls = c.execute(select(FeedPollRun.status).where(FeedPollRun.feed_id==fid)).scalars().all()
                successors = c.execute(select(Job.id,Job.job_type,Job.payload_version,Job.queue,Job.status,Job.attempt_count,Job.payload,Job.worker_id,Job.lease_token,Job.lease_expires_at,Job.heartbeat_at).where(Job.id!=jid)).all()
                successor_ids = [successor.id for successor in successors]
                successor_attempts = c.execute(select(func.count()).select_from(JobAttempt).where(JobAttempt.job_id.in_(successor_ids))).scalar_one()
                linked = len(articles)==1 and len(urls)==1 and urls[0].article_id==articles[0].id and articles[0].current_canonical_url_id==urls[0].id and urls[0].normalized_url==p['article_url']
                successor_matches = len(successors)==1 and linked and successors[0].job_type=='articles.retrieve' and successors[0].payload_version==1 and successors[0].queue=='retrieval' and successors[0].status=='queued' and successors[0].attempt_count==0 and successors[0].payload=={'article_id':str(articles[0].id),'article_url_id':str(urls[0].id)} and all(value is None for value in (successors[0].worker_id,successors[0].lease_token,successors[0].lease_expires_at,successors[0].heartbeat_at))
                result.update({'polls':polls,'entry_count':len(entries),'entry_matches':len(entries)==1 and len(articles)==1 and entries[0].article_id==articles[0].id and entries[0].reported_title==p['feed_entry_title'],'article_count':len(articles),'canonical_url_count':len(urls),'canonical_matches':linked,'successor_count':len(successors),'successor_matches':successor_matches,'successor_attempt_count':successor_attempts,'total_jobs':c.execute(select(func.count()).select_from(Job)).scalar_one()})
        else:
            raise ValueError('unknown fixture probe')
    print(json.dumps(result))
finally:
    engine.dispose()
"""

FENCE_PROBE = r"""
import json, sys, uuid
from datetime import datetime
from sqlalchemy import text
from primary_signal.db import DatabaseSettings, create_database_engine
from primary_signal.jobs.catalogue import build_default_catalogue
from primary_signal.jobs.contracts import RetrieveArticleV1, PollFeedV1
from primary_signal.jobs.repository import JobLease, LostLease
from primary_signal.jobs.transactions import TransactionalJobQueue
p = json.load(sys.stdin)
engine = create_database_engine(DatabaseSettings(statement_timeout_ms=3000, lock_timeout_ms=1000))
try:
    with engine.connect() as c:
        assert c.execute(text('SELECT current_database()')).scalar_one()==p['database']
        assert c.execute(text('SELECT current_user')).scalar_one()==p['processor_role']
        assert c.execute(text('SELECT NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls) FROM pg_catalog.pg_roles WHERE rolname=current_user')).scalar_one()
        roles = set(c.execute(text('SELECT parent.rolname FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles parent ON parent.oid=m.roleid JOIN pg_catalog.pg_roles child ON child.oid=m.member WHERE child.rolname=current_user')).scalars())
        expected_roles = {'primary_signal_cap_queue_consume','primary_signal_cap_article_persist'} if p['scenario']=='article' else {'primary_signal_cap_queue_submit','primary_signal_cap_queue_consume','primary_signal_cap_feed_poll'}
        assert roles==expected_roles
    old=p['lease']
    payload = RetrieveArticleV1(article_id=uuid.UUID(p['article_id']),article_url_id=uuid.UUID(p['url_id'])) if p['scenario']=='article' else PollFeedV1(feed_id=uuid.UUID(p['feed_id']))
    job_type = 'articles.retrieve' if p['scenario']=='article' else 'feeds.poll'
    lease=JobLease(job_id=uuid.UUID(old['job_id']),attempt_id=uuid.UUID(old['attempt_id']),attempt_number=old['attempt_number'],worker_id=old['worker_id'],lease_token=uuid.UUID(old['lease_token']),lease_expires_at=datetime.fromisoformat(old['lease_expires_at']),job_type=job_type,payload_version=1,payload=payload)
    def forbidden_callback(*args):
        raise RuntimeError('stale completion callback ran')
    try:
        TransactionalJobQueue(engine,build_default_catalogue()).succeed(lease,on_success=forbidden_callback)
    except LostLease:
        print(json.dumps({'fenced':True}))
    else:
        raise RuntimeError('stale completion succeeded')
finally:
    engine.dispose()
"""

CONTROL = r"""
import sys, urllib.request
operation=sys.argv[1]
assert operation in ('state','release')
request=urllib.request.Request('http://127.0.0.1:8765/control/'+operation,method='POST' if operation=='release' else 'GET')
with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=2) as response:
    print(response.read(1025).decode())
"""


def _shared(fixture: IngestionRecoveryFixture) -> PublicationSmokeFixture:
    # These helpers use only root/directory/env/docker_args; no publication state.
    return cast(PublicationSmokeFixture, cast(object, fixture))


def _run(
    fixture: IngestionRecoveryFixture,
    arguments: tuple[str, ...],
    *,
    stage: str,
    timeout: int = 60,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return publication._command(  # pyright: ignore[reportPrivateUsage]
        _shared(fixture),
        arguments,
        stage=stage,
        timeout=timeout,
        input_text=input_text,
        check=check,
    )


def _docker(fixture: IngestionRecoveryFixture, *args: str, **kwargs: Any) -> str:
    return _run(fixture, (*fixture.docker_args, *args), **kwargs).stdout.strip()


def _compose(fixture: IngestionRecoveryFixture, *args: str, **kwargs: Any) -> str:
    return _run(
        fixture, (*fixture.docker_args, *fixture.compose_args, *args), **kwargs
    ).stdout.strip()


def _json(value: str, stage: str) -> dict[str, Any]:
    try:
        parsed: object = json.loads(value) if len(value) <= 16384 else None
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        raise SmokeFailure(stage)
    return cast(dict[str, Any], parsed)


def _parameters(fixture: IngestionRecoveryFixture, mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "scenario": fixture.mode,
        "feed_id": str(FEED_ID),
        "feed_url": FEED_URL,
        "feed_entry_title": FEED_ENTRY_TITLE,
        "database": fixture.database_name,
        "migration_role": fixture.migration_role,
        "processor_role": fixture.processor_role,
        "source_id": str(SOURCE_ID),
        "article_id": str(ARTICLE_ID),
        "url_id": str(URL_ID),
        "job_id": str(JOB_ID),
        "source_key": SOURCE_KEY,
        "article_url": ARTICLE_URL,
        "title": EXPECTED_TITLE,
        "content_hash": EXPECTED_NORMALIZED_HASH,
    }


def _probe(
    fixture: IngestionRecoveryFixture,
    mode: str,
    *,
    lease: dict[str, Any] | None = None,
) -> dict[str, Any]:
    arguments = _parameters(fixture, mode)
    if lease is not None:
        arguments["lease"] = lease
    service, code = ("worker", FENCE_PROBE) if mode == "fence" else ("setup", DATABASE_PROBE)
    result = _compose(
        fixture,
        "run",
        "--rm",
        "--no-deps",
        "-T",
        service,
        "python",
        "-c",
        code,
        stage="private fixture probe",
        input_text=json.dumps(arguments),
        timeout=30,
    )
    return _json(result, "private fixture probe")


def _exists(fixture: IngestionRecoveryFixture, kind: str, name: str) -> bool:
    return publication._exists(_shared(fixture), kind, name)  # pyright: ignore[reportPrivateUsage]


def _fresh(fixture: IngestionRecoveryFixture) -> None:
    if not _docker(fixture, "version", "--format", "{{.Server.APIVersion}}", stage="local daemon"):
        raise SmokeFailure("local daemon")
    if _docker(
        fixture,
        "ps",
        "--all",
        "--quiet",
        "--filter",
        f"label=com.docker.compose.project={fixture.project}",
        stage="fresh project",
    ):
        raise SmokeFailure("fresh project")
    if _exists(fixture, "image", fixture.image) or _exists(
        fixture, "volume", f"{fixture.project}_{VOLUME}"
    ):
        raise SmokeFailure("fresh project")
    if any(_exists(fixture, "network", f"{fixture.project}_{name}") for name in NETWORKS):
        raise SmokeFailure("fresh project")


def _inspect(
    fixture: IngestionRecoveryFixture, service: str, identifier: str | None = None
) -> dict[str, Any]:
    identifier = identifier or _compose(
        fixture, "ps", "--all", "--quiet", service, stage="owned container"
    )
    if re.fullmatch(r"[0-9a-f]{64}", identifier) is None:
        raise SmokeFailure("owned container")
    state = _json(
        _docker(fixture, "inspect", "--format", INSPECT, identifier, stage="owned container"),
        "owned container",
    )
    raw_labels, raw_ports = state.get("labels"), state.get("ports")
    labels = cast(dict[str, Any], raw_labels) if isinstance(raw_labels, dict) else {}
    ports = cast(dict[str, Any], raw_ports) if isinstance(raw_ports, dict) else {}
    if (
        state.get("id") != identifier
        or not isinstance(raw_labels, dict)
        or (raw_ports is not None and not isinstance(raw_ports, dict))
        or labels.get("com.docker.compose.project") != fixture.project
        or labels.get("com.docker.compose.service") != service
        or str(labels.get("com.docker.compose.oneoff", "")).lower() != "false"
        or state.get("restart") != "no"
        or not isinstance(state.get("started_at"), str)
        or any(ports.values())
    ):
        raise SmokeFailure("owned container boundary")
    return state


def _wait(
    fixture: IngestionRecoveryFixture,
    service: str,
    identifier: str,
    *,
    status: str,
    exit_code: int | None = None,
    healthy: bool = False,
    timeout: float = 30,
) -> dict[str, Any]:
    deadline = monotonic() + timeout
    while True:
        state = _inspect(fixture, service, identifier)
        if (
            state["status"] == status
            and (exit_code is None or state.get("exit_code") == exit_code)
            and (not healthy or state.get("health") == "healthy")
        ):
            return state
        if monotonic() >= deadline:
            raise SmokeFailure("bounded container wait")
        sleep(0.25)


def _control(fixture: IngestionRecoveryFixture, operation: str = "state") -> dict[str, Any]:
    value = _json(
        _compose(
            fixture,
            "exec",
            "-T",
            "retriever",
            "python",
            "-c",
            CONTROL,
            operation,
            stage="synthetic fetch control",
            timeout=5,
        ),
        "synthetic fetch control",
    )
    if operation == "release":
        if set(value) != {"released"} or value["released"] is not True:
            raise SmokeFailure("synthetic fetch control")
        state = _control(fixture)
        if state["released"] is not True:
            raise SmokeFailure("synthetic fetch control")
        return state
    if (
        set(value) != {"first_started", "request_count", "released"}
        or type(value["first_started"]) is not bool
        or type(value["released"]) is not bool
        or type(value["request_count"]) is not int
        or value["request_count"] < 0
    ):
        raise SmokeFailure("synthetic fetch control")
    return value


def _wait_fetch(fixture: IngestionRecoveryFixture) -> None:
    deadline = monotonic() + 30
    while True:
        state = _control(fixture)
        if state == {"first_started": True, "request_count": 1, "released": False}:
            return
        if monotonic() >= deadline or state["request_count"] > 1:
            raise SmokeFailure("blocked first article fetch")
        sleep(0.25)


def _volume(fixture: IngestionRecoveryFixture) -> str:
    value = _docker(
        fixture,
        "volume",
        "inspect",
        "--format",
        '{{.Name}}|{{.CreatedAt}}|{{index .Labels "com.docker.compose.project"}}',
        f"{fixture.project}_{VOLUME}",
        stage="owned volume",
    )
    if not value.startswith(f"{fixture.project}_{VOLUME}|") or not value.endswith(
        "|" + fixture.project
    ):
        raise SmokeFailure("owned volume")
    return value


def _database_identity(fixture: IngestionRecoveryFixture) -> str:
    value = _compose(
        fixture,
        "exec",
        "-T",
        "database",
        "psql",
        "--no-psqlrc",
        "--no-password",
        "--username",
        fixture.bootstrap_role,
        "--dbname",
        fixture.database_name,
        "--no-align",
        "--tuples-only",
        "--set",
        "ON_ERROR_STOP=1",
        "--command",
        "SELECT current_database(),system_identifier::text FROM pg_control_system()",
        stage="disposable database identity",
    )
    if not re.fullmatch(re.escape(fixture.database_name) + r"\|[0-9]+", value):
        raise SmokeFailure("disposable database identity")
    return value


def _no_observations(snapshot: dict[str, Any], mode: str = "article") -> None:
    if (
        snapshot.get("fetches") != []
        or snapshot.get("version_count") != 0
        or snapshot.get("current") is not None
        or snapshot.get("search_count") != 0
    ):
        raise SmokeFailure("partial article observations")
    if mode == "feed":
        expected: dict[str, Any] = {
            "polls": [],
            "entry_count": 0,
            "article_count": 0,
            "canonical_url_count": 0,
            "successor_count": 0,
            "total_jobs": 1,
        }
        if any(snapshot.get(key) != value for key, value in expected.items()):
            raise SmokeFailure("partial feed observations")


def _complete(snapshot: dict[str, Any], mode: str = "article") -> None:
    if mode == "feed":
        _no_observations(snapshot)
        expected = {
            "status": "succeeded",
            "attempt_count": 2,
            "attempts": ["lease_expired", "succeeded"],
            "polls": ["succeeded"],
            "entry_count": 1,
            "entry_matches": True,
            "article_count": 1,
            "canonical_url_count": 1,
            "canonical_matches": True,
            "successor_count": 1,
            "successor_matches": True,
            "successor_attempt_count": 0,
            "total_jobs": 2,
        }
        if any(snapshot.get(key) != value for key, value in expected.items()):
            raise SmokeFailure("recovered feed result")
        return
    expected = {
        "status": "succeeded",
        "attempt_count": 2,
        "attempts": ["lease_expired", "succeeded"],
        "fetches": ["fetched"],
        "version_count": 1,
        "single_current": True,
        "hash_matches": True,
        "search_count": 1,
        "search_matches": True,
    }
    if any(snapshot.get(key) != value for key, value in expected.items()) or not snapshot.get(
        "current"
    ):
        raise SmokeFailure("recovered article result")


def _cleanup(fixture: IngestionRecoveryFixture) -> None:
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
    try:
        if _exists(fixture, "image", fixture.image):
            _docker(fixture, "image", "rm", fixture.image, stage="cleanup")
    except SmokeFailure, OSError, subprocess.SubprocessError:
        failed = True
    try:
        if (
            _exists(fixture, "volume", f"{fixture.project}_{VOLUME}")
            or any(_exists(fixture, "network", f"{fixture.project}_{name}") for name in NETWORKS)
            or _exists(fixture, "image", fixture.image)
            or _docker(
                fixture,
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={fixture.project}",
                stage="cleanup",
            )
        ):
            failed = True
    except SmokeFailure, OSError, subprocess.SubprocessError:
        failed = True
    if failed:
        raise SmokeFailure("cleanup")


def _kill_worker(fixture: IngestionRecoveryFixture, identifier: str) -> dict[str, Any]:
    # The namespace owner is never signalled; only this verified service is.
    if _inspect(fixture, "worker", identifier)["status"] != "running":
        raise SmokeFailure("worker stopped before kill")
    _docker(fixture, "kill", "--signal", "KILL", identifier, stage="owned worker kill")
    return _wait(fixture, "worker", identifier, status="exited", exit_code=137)


def _owner_unchanged(fixture: IngestionRecoveryFixture, owner: dict[str, Any]) -> bool:
    current = _inspect(fixture, "retriever", owner["id"])
    return current["status"] == "running" and current["started_at"] == owner["started_at"]


def _progress(message: str) -> None:
    print("Synthetic ingestion recovery: " + message + ".", flush=True)


def _exercise(fixture: IngestionRecoveryFixture) -> None:
    _progress("building the current packaged runtime")
    context = publication._build_context(_shared(fixture))  # pyright: ignore[reportPrivateUsage]
    _docker(
        fixture,
        "build",
        "--target",
        "runtime",
        "--tag",
        fixture.image,
        str(context),
        stage="image build",
        timeout=600,
    )
    image = _docker(
        fixture, "image", "inspect", "--format", "{{.Id}}", fixture.image, stage="runtime image"
    )
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
        raise SmokeFailure("runtime image")
    _progress("built runtime image " + image)
    _compose(
        fixture,
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        "90",
        "--no-build",
        "database",
        "retriever",
        stage="fixture startup",
        timeout=120,
    )
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
    if _probe(fixture, "seed") != {"seeded": True}:
        raise SmokeFailure("synthetic seed")
    database = _inspect(fixture, "database")
    owner = _inspect(fixture, "retriever")
    volume, identity = _volume(fixture), _database_identity(fixture)
    _progress("waiting for a committed lease blocked inside " + fixture.mode + " retrieval")
    _compose(
        fixture,
        "up",
        "--detach",
        "--no-deps",
        "--no-build",
        "worker",
        stage="packaged worker startup",
    )
    worker = _inspect(fixture, "worker")
    if (
        worker["image"] != image
        or owner["image"] != image
        or worker["network_mode"] != "container:" + owner["id"]
        or owner["status"] != "running"
    ):
        raise SmokeFailure("worker namespace boundary")
    _wait_fetch(fixture)
    lease = _probe(fixture, "capture")
    _no_observations(_probe(fixture, "snapshot"), fixture.mode)
    killed = _kill_worker(fixture, worker["id"])
    _no_observations(_probe(fixture, "snapshot"), fixture.mode)
    _control(fixture, "release")
    if _probe(fixture, "expire", lease=lease) != {"expired": True}:
        raise SmokeFailure("owned lease expiry")
    _progress("restarting the same database and worker containers with the same volume")
    _docker(fixture, "stop", "--time", "10", database["id"], stage="owned database restart")
    _docker(fixture, "start", database["id"], stage="owned database restart")
    restarted_db = _wait(fixture, "database", database["id"], status="running", healthy=True)
    if (
        restarted_db["started_at"] == database["started_at"]
        or _volume(fixture) != volume
        or _database_identity(fixture) != identity
        or not _owner_unchanged(fixture, owner)
    ):
        raise SmokeFailure("database or namespace identity changed")
    _no_observations(_probe(fixture, "snapshot"), fixture.mode)
    _docker(fixture, "start", worker["id"], stage="owned worker recovery")
    recovered = _wait(fixture, "worker", worker["id"], status="exited", exit_code=0)
    pending = _probe(fixture, "snapshot")
    _no_observations(pending, fixture.mode)
    if (
        pending["status"] != "queued"
        or pending["attempts"] != ["lease_expired"]
        or recovered["started_at"] == killed["started_at"]
    ):
        raise SmokeFailure("expired attempt recovery")
    if (
        _probe(fixture, "fence", lease=lease) != {"fenced": True}
        or _probe(fixture, "snapshot") != pending
    ):
        raise SmokeFailure("stale lease fencing")
    if _probe(fixture, "due", lease=lease) != {"due": True}:
        raise SmokeFailure("owned retry scheduling")
    _progress("finishing the recovered " + fixture.mode + " job and checking its durable result")
    _docker(fixture, "start", worker["id"], stage="owned worker retry")
    completed = _wait(fixture, "worker", worker["id"], status="exited", exit_code=0)
    snapshot = _probe(fixture, "snapshot")
    _complete(snapshot, fixture.mode)
    if completed["started_at"] == recovered["started_at"] or _control(fixture) != {
        "first_started": True,
        "request_count": 2,
        "released": True,
    }:
        raise SmokeFailure("duplicate retrieval or unchanged worker")
    if (
        _probe(fixture, "fence", lease=lease) != {"fenced": True}
        or _probe(fixture, "snapshot") != snapshot
    ):
        raise SmokeFailure("completed result changed by stale owner")
    if (
        _volume(fixture) != volume
        or _database_identity(fixture) != identity
        or not _owner_unchanged(fixture, owner)
    ):
        raise SmokeFailure("final persistence identity")


def _case(root: Path, mode: RecoveryMode) -> int:
    project = "ps-ingestion-recovery-" + mode + "-" + uuid.uuid4().hex
    status = 1
    try:
        with tempfile.TemporaryDirectory(prefix="ps-ir-", dir="/tmp") as directory:
            fixture = create_fixture(
                root, Path(directory), project, image=project + ":local", mode=mode
            )
            owned = False
            try:
                _progress("validating synthetic boundaries")
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
                    _progress("removing only this run containers, volume, network and image")
                    try:
                        _cleanup(fixture)
                    except SmokeFailure, OSError, subprocess.SubprocessError:
                        status = 1
                        _progress("cleanup failed")
    except FixtureBoundaryError, OSError, ValueError, subprocess.SubprocessError:
        _progress("fixture preparation failed")
    if status == 0:
        _progress(
            "passed killed-worker recovery, persistent restart and stale fencing; all resources removed"
        )
    return status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("article", "feed", "all"), default="article")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    modes: tuple[RecoveryMode, ...] = (
        ("article", "feed") if args.mode == "all" else (cast(RecoveryMode, args.mode),)
    )
    for mode in modes:
        _progress("starting a fresh " + mode + " case")
        if _case(root, mode) != 0:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
