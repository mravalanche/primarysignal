"""Exercise a synthetic kernel boundary and the real isolated ingestion path."""

import argparse
import ipaddress
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
from scripts.isolated_retrieval_fixture import (
    ALIASES,
    ARTICLE_URL,
    CONTROL_ORIGIN,
    DENY_IPS,
    DNS_IP,
    EXPECTED_NORMALIZED_HASH,
    EXPECTED_TITLE,
    FEED_URL,
    LIVE_SOURCES,
    MIXED_HOST,
    NETWORKS,
    POLICY_TABLE,
    PRIVATE_HOST,
    PUBLIC_HOST,
    PUBLIC_IP,
    REDIRECT_URL,
    RESOLVER,
    TRANSIT_HOST,
    TRANSIT_URL,
    UPSTREAM_DNS_IP,
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
UPSTREAM_KEYS = {"upstream_udp_queries", "upstream_tcp_queries", "upstream_failures"}
SOURCE_ID = "10000000-0000-4000-8000-000000000001"
FEED_ID = "10000000-0000-4000-8000-000000000002"
JOB_ID = "10000000-0000-4000-8000-000000000003"
_live_output = False

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


DNS_WIRE_PROBE = r"""
import json, secrets, socket, struct, sys, time
p=json.load(sys.stdin)
ident=secrets.randbits(16)
host=b''.join(bytes([len(part)])+part.encode('ascii') for part in p['host'].split('.'))+b'\0'
question=host+struct.pack('!HH',p['type'],1)
query=struct.pack('!6H',ident,0x100,1,0,0,0)+question
deadline=time.monotonic()+4
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM if p['protocol']=='udp' else socket.SOCK_STREAM)
def remaining():
    value=deadline-time.monotonic()
    if value<=0:raise TimeoutError('bounded DNS deadline')
    s.settimeout(value)
def read_exact(size):
    result=b''
    while len(result)<size:
        remaining();piece=s.recv(size-len(result))
        if not piece:raise OSError('short DNS response')
        result+=piece
    return result
try:
    remaining();s.connect((p['resolver'],53))
    remaining();s.sendall(query if p['protocol']=='udp' else struct.pack('!H',len(query))+query)
    if p['protocol']=='tcp':
        size=struct.unpack('!H',read_exact(2))[0]
        assert 12<=size<=4096
        response=read_exact(size)
    else:
        remaining();response=s.recv(4097)
    assert 12<=len(response)<=4096
    rid,flags,qd,an,ns,ar=struct.unpack('!6H',response[:12])
    assert rid==ident and flags&0x8000 and not flags&0x7800 and flags&15==0 and not flags&0x200 and qd==1 and an<=64 and ns<=64 and ar<=64
    assert response[12:12+len(question)]==question
    print(json.dumps({'response':True,'type':p['type'],'answers':an}))
finally:
    s.close()
"""

TRANSIT_ANSWERS = r"""
import json, sys
from primary_signal.retrieval.dns import DEFAULT_RESOLVER
from primary_signal.retrieval.policy import validate_target
p=json.load(sys.stdin)
target=validate_target(p['url'],DEFAULT_RESOLVER)
print(json.dumps({'addresses':[str(address) for address in target.addresses]}))
"""

TRANSIT_PROBE = r"""
import json, re, sys, urllib.request, urllib.error
p=json.load(sys.stdin)
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
request=urllib.request.Request('http://192.0.2.10:8765/v1/articles/fetch',data=json.dumps({'url':p['url']}).encode(),headers={'Content-Type':'application/json'})
try:
    with opener.open(request,timeout=30) as response:
        body=response.read(65537)
        assert len(body)<=65536
        data=json.loads(body)
        title,content=data.get('extracted_title'),data.get('extracted_text')
        size,words=data.get('decoded_byte_count'),data.get('word_count')
        hashes=all(isinstance(data.get(key),str) and re.fullmatch('[0-9a-f]{64}',data[key]) for key in ('raw_response_hash','normalized_content_hash'))
        result={'api_status':response.status,'source_status':data.get('status'),'exact_url':data.get('final_url')==p['url'],'empty_redirect_chain':data.get('redirect_chain')==[],'bounded':type(size) is int and 0<size<=65536 and isinstance(title,str) and len(title)<=256 and isinstance(content,str) and 0<len(content)<=32768,'html':isinstance(data.get('content_type'),str) and data['content_type'].split(';',1)[0].strip().lower() in ('text/html','application/xhtml+xml'),'title_matches':title=='Example Domain','hashes':bool(hashes),'word_count':words}
except urllib.error.HTTPError as error:
    body=error.read(16385)
    assert len(body)<=16384
    code=json.loads(body).get('error')
    result={'api_status':error.code,'error':code if isinstance(code,str) and re.fullmatch('[a-z_]{1,50}',code) else 'withheld'}
print(json.dumps(result))
"""


# Live URLs and extracted content stay inside the disposable database/process.
LIVE_DATABASE_PROBE = r"""
import ipaddress, json, re, sys, uuid
from urllib.parse import urlsplit
from sqlalchemy import select, insert, update, func, text, cast, Text
from primary_signal.db import DatabaseSettings, create_database_engine
from primary_signal.identity.urls import identify_url
from primary_signal.sources.models import Source, Feed
from primary_signal.jobs.models import Job, JobAttempt
from primary_signal.ingestion.models import Article, ArticleUrl, FeedEntry, FeedPollRun, FetchAttempt, ContentVersion
from primary_signal.ingestion.inventory import ArticleInventoryRepository, ArticleListQuery
p=json.load(sys.stdin)
assert p['mode'] in ('seed','activate_feed','poll','select','activate_article','verify_article','counts','complete')
assert len(p['sources'])==3 and {s['class_key'] for s in p['sources']}=={'authority','vendor','research'}
contexts=p['sources']
for s in contexts:
    for key in ('source_id','feed_id','feed_job_id'):s[key]=uuid.UUID(s[key])
sids=[s['source_id'] for s in contexts]
fids=[s['feed_id'] for s in contexts]
fjobs=[s['feed_job_id'] for s in contexts]
selected=p.get('selected',[])
engine=create_database_engine(DatabaseSettings(statement_timeout_ms=5000,lock_timeout_ms=1000))
def eligible(url,host,*,allow_root=False):
    try:
        parsed=urlsplit(url)
        return parsed.scheme=='https' and parsed.hostname==host and parsed.username is None and parsed.password is None and parsed.port in (None,443) and parsed.netloc in (host,host+':443') and bool(parsed.path) and (allow_root or parsed.path!='/')
    except ValueError:
        return False
def unclaimed(job_id,job_type,queue):
    return (Job.id==job_id,Job.job_type==job_type,Job.payload_version==1,Job.queue==queue,Job.status=='queued',Job.attempt_count==0,Job.worker_id.is_(None),Job.lease_token.is_(None),Job.lease_expires_at.is_(None),Job.heartbeat_at.is_(None))
def source_for(key):
    return next(s for s in contexts if s['class_key']==key)
def selected_for(key):
    return next(s for s in selected if s['class_key']==key)
def content_allowed(title,body):
    if not isinstance(body,str) or not body.strip():
        return False
    heading=' '.join((title or '').casefold().split())
    sample=' '.join(body[:8192].casefold().split())
    headings=('access denied','just a moment','attention required','verify you are human','checking your browser','403 forbidden','forbidden','request blocked','service unavailable','consent required','sign in to continue','login required')
    prompts=('please enable javascript','enable javascript and cookies','verify you are human','checking your browser','this site requires javascript','access to this page has been denied','you have been blocked','please complete the security check','before you continue','sign in to continue','please sign in to continue','please accept cookies to continue')
    return not heading.startswith(headings) and not sample.startswith(prompts) and sample not in headings
def verify_article(c,item):
    s=source_for(item['class_key'])
    aid,uid,jid=(uuid.UUID(item[k]) for k in ('article_id','url_id','job_id'))
    job=c.execute(select(Job.status,Job.attempt_count,Job.payload).where(Job.id==jid)).one()
    assert job.status=='succeeded' and job.attempt_count==1 and job.payload=={'article_id':str(aid),'article_url_id':str(uid)}
    attempts=c.execute(select(JobAttempt.status).where(JobAttempt.job_id==jid)).scalars().all()
    assert attempts==['succeeded']
    article=c.execute(select(Article.source_id,Article.current_canonical_url_id,Article.current_content_version_id).where(Article.id==aid)).one()
    assert article.source_id==s['source_id']
    assert c.execute(select(func.count()).select_from(FeedEntry).where(FeedEntry.feed_id==s['feed_id'],FeedEntry.article_id==aid)).scalar_one()>=1
    requested=c.execute(select(ArticleUrl.normalized_url).where(ArticleUrl.id==uid,ArticleUrl.article_id==aid)).scalar_one()
    assert eligible(requested,s['article_host'])
    canonical=c.execute(select(ArticleUrl.normalized_url).where(ArticleUrl.id==article.current_canonical_url_id,ArticleUrl.article_id==aid)).scalar_one()
    assert eligible(canonical,s['article_host'])
    fetches=c.execute(select(FetchAttempt.id,FetchAttempt.status,FetchAttempt.job_id,FetchAttempt.requested_url,FetchAttempt.final_url,FetchAttempt.redirect_chain,FetchAttempt.resulting_content_version_id).where(FetchAttempt.article_id==aid)).all()
    assert len(fetches)==1
    fetch=fetches[0]
    assert fetch.status=='fetched' and fetch.job_id==jid and identify_url(fetch.requested_url).normalized_url==requested
    assert eligible(fetch.final_url,s['article_host']) and all(eligible(hop['url'],s['article_host'],allow_root=True) for hop in fetch.redirect_chain)
    versions=c.execute(select(ContentVersion.id,ContentVersion.origin_fetch_attempt_id,ContentVersion.raw_response_hash,ContentVersion.normalized_content_hash,ContentVersion.extracted_title,ContentVersion.extracted_text,ContentVersion.word_count,ContentVersion.fetched_at).where(ContentVersion.article_id==aid)).all()
    assert len(versions)==1
    version=versions[0]
    assert version.id==article.current_content_version_id==fetch.resulting_content_version_id and version.origin_fetch_attempt_id==fetch.id and len(version.raw_response_hash)==64
    assert content_allowed(version.extracted_title,version.extracted_text) and version.word_count>0 and len(version.normalized_content_hash)==64
    # Terms come strictly from persisted extracted text, never title or feed listing.
    lexemes=c.execute(text("SELECT t FROM unnest(tsvector_to_array(to_tsvector('english'::regconfig,:body))) AS t WHERE t ~ '^[a-z][a-z0-9]{2,31}$' AND NOT (t=ANY(tsvector_to_array(to_tsvector('english'::regconfig,:title)))) ORDER BY t LIMIT 32"),{'body':version.extracted_text,'title':version.extracted_title or ''}).scalars().all()
    assert lexemes
    repository=ArticleInventoryRepository(c)
    found=False
    for term in lexemes:
        page=repository.list_articles(ArticleListQuery(search=term,source_key=s['source_key'],limit=50))
        if any(row.article_id==aid and row.source_key==s['source_key'] and row.canonical_url==canonical and row.fetched_at==version.fetched_at for row in page.items):
            found=True;break
    assert found
try:
    with engine.begin() as c:
        assert p['database'].endswith('_test') and c.execute(text('SELECT current_database()')).scalar_one()==p['database']
        assert c.execute(text('SELECT current_user')).scalar_one()==p['migration_role']
        assert c.execute(text('SELECT rolsuper FROM pg_roles WHERE rolname=current_user')).scalar_one() is False
        now=c.execute(select(func.clock_timestamp())).scalar_one()
        if p['mode']=='seed':
            assert c.execute(select(func.count()).select_from(Source).where(Source.id.in_(sids))).scalar_one()==0
            for s in contexts:
                identity=identify_url(s['feed_url'])
                assert eligible(s['feed_url'],s['article_host'])
                c.execute(insert(Source).values(id=s['source_id'],source_key=s['source_key'],name=s['name'],homepage_url='https://'+s['article_host']+'/'))
                c.execute(insert(Feed).values(id=s['feed_id'],source_id=s['source_id'],name=s['name'],configured_url=s['feed_url'],normalized_url=identity.normalized_url,url_hash=identity.url_hash,url_normalization_version=identity.normalization_version,next_poll_at=now))
                c.execute(insert(Job).values(id=s['feed_job_id'],job_type='feeds.poll',payload_version=1,payload={'feed_id':str(s['feed_id'])},queue='ingestion',priority=100,run_after=func.clock_timestamp()+text("interval '1 day'"),max_attempts=1))
            result={'seeded':3}
        elif p['mode']=='activate_feed':
            s=source_for(p['class_key'])
            assert c.execute(select(Feed.source_id).where(Feed.id==s['feed_id'])).scalar_one()==s['source_id']
            assert c.execute(select(Job.payload).where(Job.id==s['feed_job_id'])).scalar_one()=={'feed_id':str(s['feed_id'])}
            assert c.execute(select(func.count()).select_from(JobAttempt).where(JobAttempt.job_id==s['feed_job_id'])).scalar_one()==0
            assert c.execute(update(Job).where(*unclaimed(s['feed_job_id'],'feeds.poll','ingestion')).values(run_after=func.clock_timestamp()-text("interval '1 second'"))).rowcount==1
            assert c.execute(select(Job.id).where(Job.queue=='ingestion',Job.status=='queued',Job.run_after<=func.clock_timestamp())).scalars().all()==[s['feed_job_id']]
            result={'activated':1}
        elif p['mode']=='poll':
            s=source_for(p['class_key'])
            job=c.execute(select(Job.status,Job.attempt_count).where(Job.id==s['feed_job_id'])).one()
            assert job.status=='succeeded' and job.attempt_count==1
            assert c.execute(select(JobAttempt.status).where(JobAttempt.job_id==s['feed_job_id'])).scalars().all()==['succeeded']
            polls=c.execute(select(FeedPollRun.status,FeedPollRun.requested_url,FeedPollRun.job_id).where(FeedPollRun.feed_id==s['feed_id'])).all()
            assert len(polls)==1 and polls[0].status=='succeeded' and polls[0].requested_url==s['feed_url'] and polls[0].job_id==s['feed_job_id']
            assert c.execute(select(func.count()).select_from(FeedEntry).join(Article,Article.id==FeedEntry.article_id).where(FeedEntry.feed_id==s['feed_id'],Article.source_id==s['source_id'])).scalar_one()>0
            result={'poll_passed':1}
        elif p['mode']=='select':
            items=[]
            for s in contexts:
                rows=c.execute(select(FeedEntry.reported_url,Article.id.label('article_id'),ArticleUrl.id.label('url_id'),ArticleUrl.normalized_url,Job.id.label('job_id'),Job.payload,Job.status,Job.attempt_count,Job.worker_id,Job.lease_token,Job.lease_expires_at,Job.heartbeat_at).select_from(FeedEntry).join(Article,Article.id==FeedEntry.article_id).join(ArticleUrl,(ArticleUrl.id==Article.current_canonical_url_id)&(ArticleUrl.article_id==Article.id)).join(Job,(Job.payload['article_id'].astext==cast(Article.id,Text))&(Job.payload['article_url_id'].astext==cast(ArticleUrl.id,Text))).where(FeedEntry.feed_id==s['feed_id'],Article.source_id==s['source_id'],Job.job_type=='articles.retrieve',Job.payload_version==1,Job.queue=='retrieval').order_by(FeedEntry.reported_published_at.desc().nulls_last(),ArticleUrl.normalized_url,Job.id)).all()
                candidates=[r for r in rows if eligible(r.normalized_url,s['article_host']) and r.reported_url and identify_url(r.reported_url).normalized_url==r.normalized_url and r.status=='queued' and r.attempt_count==0 and all(value is None for value in (r.worker_id,r.lease_token,r.lease_expires_at,r.heartbeat_at))]
                assert candidates
                row=candidates[0]
                assert row.payload=={'article_id':str(row.article_id),'article_url_id':str(row.url_id)}
                assert c.execute(select(func.count()).select_from(FetchAttempt).where(FetchAttempt.article_id==row.article_id)).scalar_one()==0
                assert c.execute(select(func.count()).select_from(ContentVersion).where(ContentVersion.article_id==row.article_id)).scalar_one()==0
                items.append({'class_key':s['class_key'],'article_id':str(row.article_id),'url_id':str(row.url_id),'job_id':str(row.job_id)})
            chosen={uuid.UUID(item['job_id']) for item in items}
            assert len(chosen)==3
            articles=c.execute(select(Article.id).where(Article.source_id.in_(sids))).scalars().all()
            owned=c.execute(select(Job.id,Job.status,Job.attempt_count,Job.worker_id,Job.lease_token,Job.lease_expires_at,Job.heartbeat_at).where(Job.job_type=='articles.retrieve',Job.queue=='retrieval',Job.payload['article_id'].astext.in_([str(aid) for aid in articles]))).all()
            assert len(owned)>=3 and all(row.status=='queued' and row.attempt_count==0 and all(value is None for value in (row.worker_id,row.lease_token,row.lease_expires_at,row.heartbeat_at)) for row in owned)
            owned_ids=[row.id for row in owned]
            assert c.execute(select(func.count()).select_from(JobAttempt).where(JobAttempt.job_id.in_(owned_ids))).scalar_one()==0
            # Hold the fixed selections until individually activated; defer only this fixture's other successors.
            assert c.execute(update(Job).where(Job.id.in_(owned_ids),Job.job_type=='articles.retrieve',Job.payload_version==1,Job.queue=='retrieval',Job.status=='queued',Job.attempt_count==0,Job.worker_id.is_(None),Job.lease_token.is_(None),Job.lease_expires_at.is_(None),Job.heartbeat_at.is_(None)).values(run_after=func.clock_timestamp()+text("interval '1 day'"))).rowcount==len(owned_ids)
            assert c.execute(update(Job).where(Job.id.in_(chosen),Job.status=='queued',Job.attempt_count==0).values(max_attempts=1)).rowcount==3
            result={'selected':items}
        elif p['mode']=='activate_article':
            item=selected_for(p['class_key']);s=source_for(p['class_key'])
            aid,uid,jid=(uuid.UUID(item[k]) for k in ('article_id','url_id','job_id'))
            assert c.execute(select(Article.source_id).where(Article.id==aid,Article.current_canonical_url_id==uid)).scalar_one()==s['source_id']
            assert c.execute(select(Job.payload).where(Job.id==jid)).scalar_one()=={'article_id':str(aid),'article_url_id':str(uid)}
            assert c.execute(select(func.count()).select_from(JobAttempt).where(JobAttempt.job_id==jid)).scalar_one()==0
            assert c.execute(update(Job).where(*unclaimed(jid,'articles.retrieve','retrieval')).values(run_after=func.clock_timestamp()-text("interval '1 second'"))).rowcount==1
            assert c.execute(select(Job.id).where(Job.queue=='retrieval',Job.status=='queued',Job.run_after<=func.clock_timestamp())).scalars().all()==[jid]
            result={'activated':1}
        elif p['mode']=='verify_article':
            verify_article(c,selected_for(p['class_key']))
            result={'article_passed':1,'search_passed':1}
        else:
            articles=c.execute(select(Article.id).where(Article.source_id.in_(sids))).scalars().all()
            jobs=c.execute(select(Job.id).where(Job.job_type=='articles.retrieve',Job.queue=='retrieval',Job.payload['article_id'].astext.in_([str(aid) for aid in articles]))).scalars().all()
            feed_attempts=c.execute(select(func.count()).select_from(JobAttempt).where(JobAttempt.job_id.in_(fjobs))).scalar_one()
            article_attempts=c.execute(select(func.count()).select_from(JobAttempt).where(JobAttempt.job_id.in_(jobs))).scalar_one()
            result={'feed_attempts':feed_attempts,'article_attempts':article_attempts}
            if p['mode']=='complete':
                assert feed_attempts==article_attempts==3 and len(selected)==3
                for item in selected:verify_article(c,item)
                chosen=[uuid.UUID(item['job_id']) for item in selected]
                unselected=[jid for jid in jobs if jid not in chosen]
                pending=c.execute(select(Job.status,Job.attempt_count,Job.run_after,Job.worker_id,Job.lease_token,Job.lease_expires_at,Job.heartbeat_at).where(Job.id.in_(unselected))).all()
                assert all(row.status=='queued' and row.attempt_count==0 and row.run_after>now and all(value is None for value in (row.worker_id,row.lease_token,row.lease_expires_at,row.heartbeat_at)) for row in pending)
                assert c.execute(select(func.count()).select_from(JobAttempt).where(JobAttempt.job_id.in_(unselected))).scalar_one()==0
                assert c.execute(select(func.count()).select_from(FetchAttempt).where(FetchAttempt.article_id.in_(articles))).scalar_one()==3
                assert c.execute(select(func.count()).select_from(ContentVersion).where(ContentVersion.article_id.in_(articles))).scalar_one()==3
                result.update(classes_passed=3,search_checks=3)
    print(json.dumps(result))
finally:
    engine.dispose()
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
    interfaces = _interfaces(fixture) if fixture.transit_example else {}
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
        ),
        transit_example=fixture.transit_example,
        rpc_interface=interfaces.get("rpc"),
        egress_interface=interfaces.get("egress"),
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
    expected_keys = STATE_KEYS | UPSTREAM_KEYS if fixture.transit_example else STATE_KEYS
    if set(value) != expected_keys or any(
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
    owner_networks = {fixture.project + "_" + NETWORKS[0]}
    if fixture.transit_example:
        owner_networks.add(fixture.project + "_" + fixture.network_names[-1])
    if set(owner["NetworkSettings"]["Networks"]) != owner_networks:
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


def _interfaces(fixture: IsolatedRetrievalFixture) -> dict[str, str]:
    owner = _inspect(fixture, "policy")
    raw = _python(
        fixture,
        "policy",
        "import json,pathlib;print(pathlib.Path('/tmp/interfaces.json').read_text())",
    )
    if (
        set(raw) != {"rpc", "egress"}
        or any(
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", name) is None
            or name == "lo"
            for name in raw.values()
        )
        or raw["rpc"] == raw["egress"]
    ):
        raise SmokeFailure("owned transit interface names")
    interfaces = cast(dict[str, str], raw)
    addresses = json.loads(
        _docker(
            fixture,
            "exec",
            owner["Id"],
            "ip",
            "-j",
            "-4",
            "address",
            "show",
            stage="owned interface addresses",
        )
    )
    expected = {
        interfaces["rpc"]: "192.0.2.10",
        interfaces["egress"]: owner["NetworkSettings"]["Networks"][
            fixture.project + "_" + fixture.network_names[-1]
        ]["IPAddress"],
    }
    for name, address in expected.items():
        matching = [link for link in addresses if link.get("ifname") == name]
        if len(matching) != 1 or not any(
            item.get("local") == address for item in matching[0].get("addr_info", [])
        ):
            raise SmokeFailure("owned transit interface addresses")
    return interfaces


def _external_route(
    fixture: IsolatedRetrievalFixture, address: str, interfaces: dict[str, str]
) -> None:
    candidate = ipaddress.ip_address(address)
    if not isinstance(candidate, ipaddress.IPv4Address) or address in ALIASES:
        raise SmokeFailure("external IPv4 candidate")
    # Reuse the application's complete special-purpose policy, never is_private alone.
    from primary_signal.retrieval.policy import validate_target

    validate_target("https://" + address + "/", lambda _host, _port: (address,))
    if (
        _namespace(fixture, "policy") != _namespace(fixture, "probe")
        or _interfaces(fixture) != interfaces
    ):
        raise SmokeFailure("transit namespace identity")
    owner = _inspect(fixture, "policy")
    network = owner["NetworkSettings"]["Networks"][
        fixture.project + "_" + fixture.network_names[-1]
    ]
    routes = json.loads(
        _docker(
            fixture,
            "exec",
            owner["Id"],
            "ip",
            "-j",
            "-4",
            "route",
            "get",
            address,
            stage="owned external route",
        )
    )
    if (
        len(routes) != 1
        or routes[0].get("type", "unicast") != "unicast"
        or routes[0].get("dev") != interfaces["egress"]
        or routes[0].get("prefsrc") != network["IPAddress"]
        or routes[0].get("gateway") != network["Gateway"]
        or routes[0].get("dst") != address
    ):
        raise SmokeFailure("owned external route")


def _no_upstream(fixture: IsolatedRetrievalFixture) -> None:
    if fixture.transit_example:
        state = _state(fixture)
        if any(state[key] != 0 for key in UPSTREAM_KEYS):
            raise SmokeFailure("unexpected upstream DNS before transit phase")
        counters = _policy(fixture)
        if any(
            counters["output_upstream_dns_" + protocol] != 0
            or counters["input_upstream_dns_reply_" + protocol] != 0
            for protocol in ("udp", "tcp")
        ):
            raise SmokeFailure("unexpected upstream counters before transit phase")


def _transit_answers(value: dict[str, Any]) -> tuple[str, ...]:
    from primary_signal.retrieval.policy import validate_target

    addresses = value.get("addresses")
    if (
        set(value) != {"addresses"}
        or not isinstance(addresses, list)
        or not addresses
        or any(
            not isinstance(address, str) or address in ALIASES
            for address in cast(list[object], addresses)
        )
    ):
        raise SmokeFailure("transit DNS answer shape")
    # Validate the whole A+AAAA set before selecting any usable IPv4 route.
    target = validate_target(TRANSIT_URL, lambda _host, _port: cast(list[str], addresses))
    ipv4 = tuple(
        str(address) for address in target.addresses if isinstance(address, ipaddress.IPv4Address)
    )
    if not ipv4:
        raise SmokeFailure("no usable IPv4 transit answer")
    return ipv4


def _transit_result(value: dict[str, Any]) -> None:
    if value.get("api_status") != 200:
        status = value.get("api_status")
        code = value.get("error")
        if (
            type(status) is int
            and 100 <= status <= 599
            and isinstance(code, str)
            and re.fullmatch(r"[a-z_]{1,50}", code)
        ):
            raise SmokeFailure(f"verified example HTTPS response (API {status}, {code})")
    expected = {
        "api_status",
        "source_status",
        "exact_url",
        "empty_redirect_chain",
        "bounded",
        "html",
        "title_matches",
        "hashes",
        "word_count",
    }
    if (
        set(value) != expected
        or value.get("api_status") != 200
        or value.get("source_status") != 200
        or any(
            value.get(key) is not True
            for key in (
                "exact_url",
                "empty_redirect_chain",
                "bounded",
                "html",
                "title_matches",
                "hashes",
            )
        )
        or type(value.get("word_count")) is not int
        or not 0 < value["word_count"] <= 4096
    ):
        raise SmokeFailure("verified example HTTPS response")


def _transit(fixture: IsolatedRetrievalFixture) -> None:
    owner = _topology(fixture)
    _no_upstream(fixture)
    interfaces = _interfaces(fixture)
    _external_route(fixture, UPSTREAM_DNS_IP, interfaces)
    _progress("verified owned RPC and egress interfaces before public DNS")
    for protocol in ("udp", "tcp"):
        for kind in (1, 28):
            _local_route(fixture, DNS_IP)
            _external_route(fixture, UPSTREAM_DNS_IP, interfaces)
            before, receipts_before = _policy(fixture), _state(fixture)
            try:
                result = _python(
                    fixture,
                    "probe",
                    DNS_WIRE_PROBE,
                    {"host": TRANSIT_HOST, "resolver": DNS_IP, "protocol": protocol, "type": kind},
                )
            except SmokeFailure:
                observed = _policy(fixture)
                if (
                    observed["output_upstream_dns_" + protocol]
                    > before["output_upstream_dns_" + protocol]
                    and observed["input_upstream_dns_reply_" + protocol]
                    == before["input_upstream_dns_reply_" + protocol]
                ):
                    raise SmokeFailure(
                        "approved public DNS " + protocol + " sent without an established reply"
                    ) from None
                raise SmokeFailure(
                    "approved public DNS " + protocol + " invalid or unavailable response"
                ) from None
            after, receipts_after = _policy(fixture), _state(fixture)
            if (
                result.get("response") is not True
                or result.get("type") != kind
                or type(result.get("answers")) is not int
                or (kind == 1 and result["answers"] == 0)
                or any(
                    after[rule] <= before[rule]
                    for rule in (
                        "output_upstream_dns_" + protocol,
                        "input_upstream_dns_reply_" + protocol,
                    )
                )
                or receipts_after["upstream_" + protocol + "_queries"]
                <= receipts_before["upstream_" + protocol + "_queries"]
                or receipts_after["upstream_failures"] != 0
                or receipts_after["forbidden_requests"] != 0
            ):
                raise SmokeFailure("approved public DNS " + protocol)
    answers = _python(fixture, "probe", TRANSIT_ANSWERS, {"url": TRANSIT_URL})
    for address in _transit_answers(answers):
        _external_route(fixture, address, interfaces)
    _owner_unchanged(fixture, owner)
    _progress("verified public UDP and TCP DNS with complete A and AAAA validation")
    before, receipts_before = _policy(fixture), _state(fixture)
    # Exactly one unchanged packaged API invocation. No public body leaves the child.
    value = _json(
        _compose(
            fixture,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "feed_processor",
            "python",
            "-c",
            TRANSIT_PROBE,
            input_text=json.dumps({"url": TRANSIT_URL}),
            stage="fixed example HTTPS invocation",
            timeout=45,
        ),
        "fixed example HTTPS metadata",
    )
    _transit_result(value)
    after, receipts_after = _policy(fixture), _state(fixture)
    if (
        after["output_public"] <= before["output_public"]
        or after["input_public_reply"] <= before["input_public_reply"]
        or receipts_after["upstream_udp_queries"] + receipts_after["upstream_tcp_queries"]
        <= receipts_before["upstream_udp_queries"] + receipts_before["upstream_tcp_queries"]
        or receipts_after["upstream_failures"] != 0
        or receipts_after["forbidden_requests"] != 0
        or receipts_after["source_requests"] != receipts_before["source_requests"]
    ):
        raise SmokeFailure("real HTTPS transit counter evidence")
    _pipeline_snapshot(_database(fixture, "snapshot"), complete=True)
    _owner_unchanged(fixture, owner)
    _progress(
        "verified one real example HTTPS response through the unchanged retriever without database persistence"
    )


def _live_probe(
    fixture: IsolatedRetrievalFixture,
    mode: str,
    sources: list[dict[str, str]],
    selected: list[dict[str, str]],
    *,
    class_key: str | None = None,
) -> dict[str, Any]:
    parameters = {
        "mode": mode,
        "database": fixture.database_name,
        "migration_role": fixture.migration_role,
        "sources": sources,
        "selected": selected,
        "class_key": class_key,
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
            LIVE_DATABASE_PROBE,
            input_text=json.dumps(parameters),
            stage="private live database proof",
            timeout=60,
        ),
        "private live database proof",
    )


def _live_counts(value: dict[str, Any], counts: dict[str, int]) -> None:
    if set(value) != {"feed_attempts", "article_attempts"} or any(
        type(value[key]) is not int or not 0 <= value[key] <= 3
        for key in ("feed_attempts", "article_attempts")
    ):
        raise SmokeFailure("live attempt bound")
    counts.update(value)


def _live_sources(fixture: IsolatedRetrievalFixture, counts: dict[str, int]) -> None:
    sources = [
        {
            "class_key": s.class_key,
            "source_key": s.source_key,
            "name": s.name,
            "feed_url": s.feed_url,
            "article_host": s.article_host,
            "source_id": str(uuid.uuid4()),
            "feed_id": str(uuid.uuid4()),
            "feed_job_id": str(uuid.uuid4()),
        }
        for s in LIVE_SOURCES
    ]
    selected: list[dict[str, str]] = []
    owner = _topology(fixture)
    try:
        if _live_probe(fixture, "seed", sources, selected) != {"seeded": 3}:
            raise SmokeFailure("live source seed")
        for source in sources:
            _owner_unchanged(fixture, owner)
            if _live_probe(
                fixture, "activate_feed", sources, selected, class_key=source["class_key"]
            ) != {"activated": 1}:
                raise SmokeFailure("live feed activation")
            _worker(fixture, "feed_processor", owner)
            if _live_probe(fixture, "poll", sources, selected, class_key=source["class_key"]) != {
                "poll_passed": 1
            }:
                raise SmokeFailure("live feed proof")
        result = _live_probe(fixture, "select", sources, selected)
        items = result.get("selected")
        if (
            set(result) != {"selected"}
            or not isinstance(items, list)
            or len(cast(list[object], items)) != 3
        ):
            raise SmokeFailure("live fixed selection")
        selected = cast(list[dict[str, str]], items)
        if {item.get("class_key") for item in selected} != {
            source["class_key"] for source in sources
        } or len({item.get("job_id") for item in selected}) != 3:
            raise SmokeFailure("live selection ownership")
        for source in sources:
            if _live_probe(
                fixture, "activate_article", sources, selected, class_key=source["class_key"]
            ) != {"activated": 1}:
                raise SmokeFailure("live article activation")
            _worker(fixture, "article_processor", owner)
            if _live_probe(
                fixture, "verify_article", sources, selected, class_key=source["class_key"]
            ) != {"article_passed": 1, "search_passed": 1}:
                raise SmokeFailure("live article provenance and search")
            counts["source_classes_passed"] += 1
            counts["fulltext_search_checks"] += 1
        result = _live_probe(fixture, "complete", sources, selected)
        if result != {
            "feed_attempts": 3,
            "article_attempts": 3,
            "classes_passed": 3,
            "search_checks": 3,
        }:
            raise SmokeFailure("live complete proof")
        _owner_unchanged(fixture, owner)
    finally:
        _live_counts(_live_probe(fixture, "counts", sources, selected), counts)


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
        or any(
            _exists(fixture, "network", fixture.project + "_" + n) for n in fixture.network_names
        )
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
        or any(
            _exists(fixture, "network", fixture.project + "_" + n) for n in fixture.network_names
        )
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
    if _live_output:
        return
    print("Synthetic isolated retrieval: " + message + ".", flush=True)


def _exercise(fixture: IsolatedRetrievalFixture, live_counts: dict[str, int] | None = None) -> None:
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
    _no_upstream(fixture)
    _managed_restart(fixture, owner)
    _no_upstream(fixture)
    if fixture.transit_example:
        _transit(fixture)
    if fixture.live_three_sources:
        _live_sources(fixture, live_counts if live_counts is not None else {})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transit-example",
        action="store_true",
        help="Also verify fixed public DNS and one example.com HTTPS response",
    )
    parser.add_argument(
        "--live-three-sources",
        action="store_true",
        help="Also verify the three fixed public source classes; print aggregate counts only",
    )
    args = parser.parse_args(argv)
    global _live_output
    _live_output = args.live_three_sources
    counts = {
        "feed_attempts": 0,
        "article_attempts": 0,
        "source_classes_passed": 0,
        "fulltext_search_checks": 0,
        "cleanup_passes": 0,
    }
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
                transit_example=args.transit_example or args.live_three_sources,
                live_three_sources=args.live_three_sources,
            )
            owned = False
            try:
                validate_fixture_models(fixture)
                _fresh(fixture)
                owned = True
                _exercise(fixture, counts)
                status = 0
            except SmokeFailure as error:
                _progress(f"failed during {error}; command output and fixture data withheld")
            except FixtureBoundaryError, OSError, ValueError, subprocess.SubprocessError:
                _progress("failed; command output and fixture data withheld")
            except Exception:
                status = 1
                if not args.live_three_sources:
                    raise
            finally:
                if owned:
                    try:
                        _cleanup(fixture)
                        counts["cleanup_passes"] = 1
                        _progress("removed only this run containers, networks, volume and images")
                    except SmokeFailure, OSError, subprocess.SubprocessError:
                        status = 1
                        _progress("cleanup failed")
    except FixtureBoundaryError, OSError, ValueError, subprocess.SubprocessError:
        status = 1
        counts["cleanup_passes"] = 0
        _progress("fixture preparation failed")
    except Exception:
        status = 1
        counts["cleanup_passes"] = 0
        if not args.live_three_sources:
            raise
    if args.live_three_sources:
        print(json.dumps(counts, sort_keys=True), flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
