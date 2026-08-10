"""Server-rendered web UI (Jinja2 + htmx, no SPA build step). Every write goes through
app/services/entries.py -- exactly the same functions the MCP tools call -- so there is only
ever one write path, not two that could drift apart.
"""

from pathlib import Path
from uuid import UUID

import markdown as md
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_session
from app.db.models import Project, Source, Subtopic
from app.db.repository import (
    NotFoundError,
    get_all_subtopics_for_project,
    get_project_by_slug,
    get_subtopic_path_parts,
)
from app.services import entries as entries_service
from app.services import search as search_service
from app.services.git_store import get_git_store

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def web_actor(request: Request) -> str:
    """Authelia forward-auth (production) sets Remote-User on the way in; local dev has no
    such header, so writes are attributed to a generic "web" actor instead."""
    remote_user = request.headers.get("remote-user")
    return f"user:{remote_user}" if remote_user else "user:web"


def _tag_list(raw: str) -> list[str]:
    return [t.strip() for t in raw.split(",") if t.strip()]


def _build_tree(subtopics: list[Subtopic]) -> list[dict]:
    by_parent: dict = {}
    for s in subtopics:
        by_parent.setdefault(s.parent_subtopic_id, []).append(s)
    for children in by_parent.values():
        children.sort(key=lambda s: s.name)

    def node(s: Subtopic, parent_path: str) -> dict:
        path = f"{parent_path}/{s.slug}" if parent_path else s.slug
        return {"subtopic": s, "path": path, "children": [node(c, path) for c in by_parent.get(s.id, [])]}

    return [node(s, "") for s in by_parent.get(None, [])]


async def _project_and_path_for_entry(session: AsyncSession, entry) -> tuple[Project, str]:
    subtopic = await session.get(Subtopic, entry.subtopic_id)
    project = await session.get(Project, subtopic.project_id)
    path_parts = await get_subtopic_path_parts(session, subtopic)
    return project, "/".join(path_parts[1:])


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    projects = list((await session.execute(select(Project).order_by(Project.name))).scalars())
    open_entries = await entries_service.list_open(session)
    return templates.TemplateResponse(
        request, "dashboard.html", {"projects": projects, "open_entries": open_entries}
    )


@router.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, q: str = "", session: AsyncSession = Depends(get_session)):
    results = await search_service.search(session, query=q, limit=25) if q.strip() else []
    return templates.TemplateResponse(request, "search.html", {"query": q, "results": results})


@router.get("/projects/{project_slug}", response_class=HTMLResponse)
async def project_view(request: Request, project_slug: str, session: AsyncSession = Depends(get_session)):
    return await _render_project_page(request, session, project_slug, subtopic_path=None)


@router.get("/projects/{project_slug}/{subtopic_path:path}", response_class=HTMLResponse)
async def subtopic_view(
    request: Request, project_slug: str, subtopic_path: str, session: AsyncSession = Depends(get_session)
):
    return await _render_project_page(request, session, project_slug, subtopic_path=subtopic_path)


async def _render_project_page(request: Request, session: AsyncSession, project_slug: str, *, subtopic_path):
    try:
        project = await get_project_by_slug(session, project_slug)
        entries = await entries_service.get_entries(session, project_slug=project_slug, subtopic_path=subtopic_path)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    all_subtopics = await get_all_subtopics_for_project(session, project)
    tree = _build_tree(all_subtopics)
    return templates.TemplateResponse(
        request,
        "project.html",
        {"project": project, "tree": tree, "entries": entries, "subtopic_path": subtopic_path},
    )


@router.get("/entries/new", response_class=HTMLResponse)
async def new_entry_form(
    request: Request, project: str, subtopic: str = "", session: AsyncSession = Depends(get_session)
):
    try:
        proj = await get_project_by_slug(session, project)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "entry_edit.html",
        {"mode": "new", "project": proj, "subtopic_path": subtopic, "entry": None, "tags_csv": "", "error": None},
    )


@router.post("/entries/new")
async def create_entry(
    request: Request,
    project: str = Form(...),
    subtopic: str = Form(...),
    title: str = Form(...),
    body_markdown: str = Form(...),
    tags: str = Form(""),
    follow_up_status: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    entry = await entries_service.upsert_entry(
        session,
        project_slug=project,
        subtopic_path=subtopic,
        title=title,
        body_markdown=body_markdown,
        actor=web_actor(request),
        tags=_tag_list(tags),
        follow_up_status=follow_up_status or "none",
    )
    return RedirectResponse(f"/entries/{entry.id}", status_code=303)


@router.get("/entries/{entry_id}", response_class=HTMLResponse)
async def entry_view(request: Request, entry_id: UUID, session: AsyncSession = Depends(get_session)):
    try:
        entry = await entries_service.get_entry_by_id(session, entry_id)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    project, subtopic_path = await _project_and_path_for_entry(session, entry)
    rendered_html = md.markdown(entry.body_markdown, extensions=["extra", "sane_lists", "nl2br"])
    sources = (await session.execute(select(Source).where(Source.entry_id == entry.id))).scalars()
    return templates.TemplateResponse(
        request,
        "entry_view.html",
        {
            "entry": entry,
            "project": project,
            "subtopic_path": subtopic_path,
            "rendered_html": rendered_html,
            "sources": list(sources),
        },
    )


@router.get("/entries/{entry_id}/edit", response_class=HTMLResponse)
async def edit_entry_form(request: Request, entry_id: UUID, session: AsyncSession = Depends(get_session)):
    try:
        entry = await entries_service.get_entry_by_id(session, entry_id)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    project, subtopic_path = await _project_and_path_for_entry(session, entry)
    return templates.TemplateResponse(
        request,
        "entry_edit.html",
        {
            "mode": "edit",
            "project": project,
            "subtopic_path": subtopic_path,
            "entry": entry,
            "tags_csv": ", ".join(t.name for t in entry.tags),
            "error": None,
        },
    )


@router.post("/entries/{entry_id}/edit")
async def update_entry_route(
    request: Request,
    entry_id: UUID,
    title: str = Form(...),
    body_markdown: str = Form(...),
    tags: str = Form(""),
    follow_up_status: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    try:
        entry = await entries_service.update_entry(
            session,
            entry_id=entry_id,
            title=title,
            body_markdown=body_markdown,
            actor=web_actor(request),
            tags=_tag_list(tags),
            follow_up_status=follow_up_status or "none",
        )
    except ValueError as exc:
        existing = await entries_service.get_entry_by_id(session, entry_id)
        project, subtopic_path = await _project_and_path_for_entry(session, existing)
        return templates.TemplateResponse(
            request,
            "entry_edit.html",
            {
                "mode": "edit",
                "project": project,
                "subtopic_path": subtopic_path,
                "entry": existing,
                "tags_csv": tags,
                "error": str(exc),
            },
            status_code=409,
        )
    return RedirectResponse(f"/entries/{entry.id}", status_code=303)


@router.get("/entries/{entry_id}/history", response_class=HTMLResponse)
async def entry_history(request: Request, entry_id: UUID, session: AsyncSession = Depends(get_session)):
    try:
        entry = await entries_service.get_entry_by_id(session, entry_id)
        versions = await entries_service.get_history(session, entry_id, limit=50)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    project, subtopic_path = await _project_and_path_for_entry(session, entry)
    return templates.TemplateResponse(
        request,
        "history.html",
        {"entry": entry, "project": project, "subtopic_path": subtopic_path, "versions": versions},
    )


@router.get("/entries/{entry_id}/history/{version_id}/diff", response_class=HTMLResponse)
async def entry_diff(
    request: Request, entry_id: UUID, version_id: UUID, session: AsyncSession = Depends(get_session)
):
    import difflib

    try:
        entry = await entries_service.get_entry_by_id(session, entry_id)
        versions = await entries_service.get_history(session, entry_id, limit=50)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc

    versions_desc = sorted(versions, key=lambda v: v.created_at, reverse=True)
    idx = next((i for i, v in enumerate(versions_desc) if v.id == version_id), None)
    if idx is None:
        raise HTTPException(404, "unknown version")
    current = versions_desc[idx]
    previous = versions_desc[idx + 1] if idx + 1 < len(versions_desc) else None

    diff_html = difflib.HtmlDiff(wrapcolumn=90).make_table(
        (previous.body_markdown if previous else "").splitlines(),
        current.body_markdown.splitlines(),
        fromdesc=previous.created_at.isoformat() if previous else "(leer)",
        todesc=current.created_at.isoformat(),
        context=True,
        numlines=3,
    )
    project, subtopic_path = await _project_and_path_for_entry(session, entry)
    return templates.TemplateResponse(
        request,
        "diff.html",
        {"entry": entry, "project": project, "subtopic_path": subtopic_path, "version": current, "diff_html": diff_html},
    )
