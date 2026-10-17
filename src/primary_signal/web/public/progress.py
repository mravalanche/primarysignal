"""Local project delivery ledger, sourced from versioned exit checks."""

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from primary_signal.web.common.templates import TemplateSurface, create_templates

router = APIRouter(include_in_schema=False)
templates = create_templates(TemplateSurface.PUBLIC)
MILESTONES = Path(__file__).with_name("milestones.json")


def load_progress() -> dict[str, object]:
    """Calculate progress from explicit, reviewable acceptance checks."""

    data = json.loads(MILESTONES.read_text(encoding="utf-8"))
    completed = 0
    total = 0
    for milestone in data["milestones"]:
        checks = milestone["checks"]
        milestone["completed"] = sum(check["done"] is True for check in checks)
        milestone["total"] = len(checks)
        milestone["percent"] = round(100 * milestone["completed"] / milestone["total"])
        completed += milestone["completed"]
        total += milestone["total"]
    data["completed"] = completed
    data["total"] = total
    data["percent"] = round(100 * completed / total)
    return data


@router.get("/progress", response_class=HTMLResponse)
def progress_page(request: Request) -> HTMLResponse:
    """Show the delivery ledger in the local synthetic demonstration."""

    return templates.TemplateResponse(
        request=request,
        name="progress.html",
        context={"progress": load_progress()},
    )
