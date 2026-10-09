"""Authenticated browser forms for editorial publication decisions."""

import uuid
from collections.abc import Mapping
from typing import Protocol, cast
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from primary_signal.publication.editorial_reader import EditorialReader
from primary_signal.publication.models import validate_public_identifier
from primary_signal.publication.writer import OperatorDecision, PublicationConflict
from primary_signal.web.admin.routes import service_for
from primary_signal.web.common.templates import TemplateSurface, create_templates

router = APIRouter(prefix="/admin/desk/stories", tags=["admin-decisions"])
templates = create_templates(TemplateSurface.ADMIN)


class DecisionWriter(Protocol):
    def publish_reviewed(
        self,
        *,
        story_id: uuid.UUID,
        revision_id: uuid.UUID,
        input_fingerprint: str,
        expected_current_revision_id: uuid.UUID | None,
        decision: OperatorDecision,
    ) -> None: ...

    def suppress(
        self,
        *,
        story_id: uuid.UUID,
        expected_current_revision_id: uuid.UUID,
        decision: OperatorDecision,
    ) -> None: ...


async def _form(request: Request, required: set[str]) -> Mapping[str, str]:
    """Read one small URL-encoded form with no duplicate or unknown fields."""

    if request.headers.get("content-type", "").split(";", 1)[0].strip() != (
        "application/x-www-form-urlencoded"
    ):
        raise HTTPException(status_code=400, detail="Invalid decision request")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 4096:
            raise HTTPException(status_code=400, detail="Invalid decision request")
        body.extend(chunk)
    try:
        parsed = parse_qs(
            body.decode("utf-8", errors="strict"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=len(required),
            errors="strict",
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Invalid decision request") from error
    if set(parsed) != required or any(len(values) != 1 for values in parsed.values()):
        raise HTTPException(status_code=400, detail="Invalid decision request")
    fields = {key: values[0] for key, values in parsed.items()}
    if not service_for(request).valid_csrf(request.state.admin_session, fields["csrf_token"]):
        raise HTTPException(status_code=403, detail="Request verification failed")
    if not 1 <= len(fields["reason"].strip()) <= 1024:
        raise HTTPException(status_code=400, detail="Invalid decision reason")
    return fields


def _uuid(value: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid decision request") from error
    if str(parsed) != value:
        raise HTTPException(status_code=400, detail="Invalid decision request")
    return parsed


def _story_id(request: Request, slug: str) -> uuid.UUID:
    try:
        validate_public_identifier(slug, name="story slug", slug=True)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="Story not found") from error
    reader = cast(EditorialReader, request.app.state.editorial_reader)
    detail = reader.get_story(slug)
    if detail is None:
        raise HTTPException(status_code=404, detail="Story not found")
    return detail.story.story_id


def _conflict(request: Request, slug: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="decision_conflict.html",
        context={"story_url": f"/admin/desk/stories/{slug}"},
        status_code=409,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/{slug}/publish")
async def publish(request: Request, slug: str) -> Response:
    fields = await _form(
        request,
        {
            "csrf_token",
            "candidate_revision_id",
            "evidence_fingerprint",
            "expected_current_revision_id",
            "reason",
        },
    )
    story_id = await run_in_threadpool(_story_id, request, slug)
    fingerprint = fields["evidence_fingerprint"]
    if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
        raise HTTPException(status_code=400, detail="Invalid decision request")
    expected = fields["expected_current_revision_id"]
    writer = cast(DecisionWriter, request.app.state.decision_writer)
    try:
        await run_in_threadpool(
            writer.publish_reviewed,
            story_id=story_id,
            revision_id=_uuid(fields["candidate_revision_id"]),
            input_fingerprint=fingerprint,
            expected_current_revision_id=_uuid(expected) if expected else None,
            decision=OperatorDecision(
                actor=request.app.state.decision_actor, reason=fields["reason"].strip()
            ),
        )
    except PublicationConflict:
        return _conflict(request, slug)
    return RedirectResponse(url=f"/admin/desk/stories/{slug}#story-detail", status_code=303)


@router.post("/{slug}/suppress")
async def suppress(request: Request, slug: str) -> Response:
    fields = await _form(request, {"csrf_token", "expected_current_revision_id", "reason"})
    story_id = await run_in_threadpool(_story_id, request, slug)
    writer = cast(DecisionWriter, request.app.state.decision_writer)
    try:
        await run_in_threadpool(
            writer.suppress,
            story_id=story_id,
            expected_current_revision_id=_uuid(fields["expected_current_revision_id"]),
            decision=OperatorDecision(
                actor=request.app.state.decision_actor, reason=fields["reason"].strip()
            ),
        )
    except PublicationConflict:
        return _conflict(request, slug)
    return RedirectResponse(url=f"/admin/desk/stories/{slug}#story-detail", status_code=303)
