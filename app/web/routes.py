"""Server-rendered web UI (Jinja2 + htmx, no SPA build step). Every write goes through
app/services/entries.py -- exactly the same functions the MCP tools call -- so there is only
ever one write path, not two that could drift apart.
"""

import zlib
from pathlib import Path
from uuid import UUID

import markdown as md
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.base import get_session
from app.db.models import Project, Source, Subtopic
from app.db.repository import (
    NotFoundError,
    collect_descendant_ids,
    find_subtopic_by_path,
    get_all_subtopics_for_project,
    get_project_by_slug,
    get_subtopic_path_parts,
)
from app.services import entries as entries_service
from app.services import extraction as extraction_service
from app.services import projects as projects_service
from app.services import relations as relations_service
from app.services import search as search_service
from app.services.git_store import get_git_store

def require_web_user(request: Request) -> str:
    """Authentication gate for the web UI, checked in the app and not only in the proxy.

    Authelia's forward-auth sets Remote-User on the way in and every web route sits
    behind it. Verifying the same header here a second time means a route that loses
    its middleware turns into a 401 for everyone instead of an open door.

    Wired as a router-level dependency on purpose: attaching it only where an actor is
    needed would gate the writes and leave every read route -- dashboard, search, entry
    view -- wide open, and reads are the larger half of the surface.

    WEB_AUTH_REQUIRED=false turns it off for local development, where no proxy exists.
    """
    remote_user = request.headers.get("remote-user")
    if remote_user:
        return f"user:{remote_user}"
    if get_settings().web_auth_required:
        raise HTTPException(status_code=401, detail="unauthenticated")
    return "user:web"


router = APIRouter(dependencies=[Depends(require_web_user)])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def web_actor(request: Request) -> str:
    """Git author / entries.updated_by for writes coming from the web UI."""
    return require_web_user(request)


def _tag_list(raw: str) -> list[str]:
    return [t.strip() for t in raw.split(",") if t.strip()]


def tag_color(name: str) -> str:
    """Deterministic per-tag-name color (same tag always gets the same color, across requests
    and restarts) -- crc32 rather than Python's built-in hash(), since str hashing is
    randomized per-process by default and would give every tag a new color on every restart.
    Fixed saturation/lightness so it only needs to work as a border accent on top of the
    existing light/dark `.tag` background, not as full-contrast text-on-background."""
    hue = zlib.crc32(name.encode("utf-8")) % 360
    return f"hsl({hue}, 65%, 45%)"


templates.env.filters["tag_color"] = tag_color


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
    # 5 projects today -- a query per card is fine at this scale, no need to batch.
    project_stats = {p.id: await projects_service.get_project_stats(session, p) for p in projects}
    open_entries = await entries_service.list_open(session)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"projects": projects, "project_stats": project_stats, "open_entries": open_entries},
    )


@router.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, q: str = "", tags: str = "", session: AsyncSession = Depends(get_session)):
    tag_list = _tag_list(tags)
    results = (
        await search_service.search(session, query=q, tags=tag_list or None, limit=25) if q.strip() or tag_list else []
    )
    return templates.TemplateResponse(request, "search.html", {"query": q, "tags_csv": tags, "results": results})


# NOTE ON ORDERING: /projects/new and /projects/{project_slug}/edit + /delete MUST be
# registered before the catch-all GET /projects/{project_slug}/{subtopic_path:path} below --
# Starlette matches routes in registration order, not by specificity, so a later-registered
# "/edit" would otherwise be swallowed by the catch-all as subtopic_path="edit".


@router.get("/projects/new", response_class=HTMLResponse)
async def new_project_form(request: Request):
    return templates.TemplateResponse(request, "project_edit.html", {"mode": "new", "project": None, "error": None})


@router.post("/projects/new")
async def create_project_route(
    request: Request,
    name: str = Form(...),
    sensitivity_level: str = Form(...),
    description: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    try:
        project = await projects_service.create_project(
            session, name=name, sensitivity_level=sensitivity_level, description=description
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "project_edit.html",
            {"mode": "new", "project": None, "error": str(exc)},
            status_code=409,
        )
    return RedirectResponse(f"/projects/{project.slug}", status_code=303)


@router.get("/projects/{project_slug}", response_class=HTMLResponse)
async def project_view(request: Request, project_slug: str, session: AsyncSession = Depends(get_session)):
    return await _render_project_page(request, session, project_slug, subtopic_path=None)


@router.get("/projects/{project_slug}/edit", response_class=HTMLResponse)
async def edit_project_form(request: Request, project_slug: str, session: AsyncSession = Depends(get_session)):
    try:
        project = await get_project_by_slug(session, project_slug)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    stats = await projects_service.get_project_stats(session, project)
    return templates.TemplateResponse(
        request, "project_edit.html", {"mode": "edit", "project": project, "stats": stats, "error": None}
    )


@router.post("/projects/{project_slug}/edit")
async def update_project_route(
    request: Request,
    project_slug: str,
    name: str = Form(...),
    description: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    project = await projects_service.rename_project(
        session, project_slug=project_slug, name=name, description=description
    )
    return RedirectResponse(f"/projects/{project.slug}", status_code=303)


@router.post("/projects/{project_slug}/delete")
async def delete_project_route(
    request: Request,
    project_slug: str,
    confirm_slug: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    try:
        project = await get_project_by_slug(session, project_slug)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    if confirm_slug != project.slug:
        stats = await projects_service.get_project_stats(session, project)
        return templates.TemplateResponse(
            request,
            "project_edit.html",
            {
                "mode": "edit",
                "project": project,
                "stats": stats,
                "error": f"Bestätigung stimmt nicht überein (erwartet: {project.slug})",
            },
            status_code=409,
        )
    await projects_service.delete_project(session, project_slug=project_slug, actor=web_actor(request))
    return RedirectResponse("/", status_code=303)


# also registered before the catch-all below, same reason as /edit and /delete above.
@router.get("/projects/{project_slug}/graph", response_class=HTMLResponse)
async def project_graph(request: Request, project_slug: str, session: AsyncSession = Depends(get_session)):
    try:
        project = await get_project_by_slug(session, project_slug)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return templates.TemplateResponse(request, "project_graph.html", {"project": project})


@router.get("/projects/{project_slug}/graph/data")
async def project_graph_data(request: Request, project_slug: str, session: AsyncSession = Depends(get_session)):
    try:
        project = await get_project_by_slug(session, project_slug)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return await relations_service.get_project_relation_graph(session, project.id)


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
    stats_map = await projects_service.get_subtopic_stats_map(session, project)

    # scope stats come straight from the entries list already fetched for this exact scope
    # (project-wide or subtopic+descendants) -- no extra query needed for entry_count/char_count.
    if subtopic_path:
        scoped_subtopic = await find_subtopic_by_path(session, project, subtopic_path)
        subtopic_count = len(collect_descendant_ids(all_subtopics, scoped_subtopic.id)) - 1
    else:
        subtopic_count = len(all_subtopics)
    stats = {
        "entry_count": len(entries),
        "char_count": sum(len(e.body_markdown) for e in entries),
        "subtopic_count": subtopic_count,
    }

    return templates.TemplateResponse(
        request,
        "project.html",
        {
            "project": project,
            "tree": tree,
            "entries": entries,
            "subtopic_path": subtopic_path,
            "stats": stats,
            "stats_map": stats_map,
        },
    )


@router.get("/subtopics/{subtopic_id}/edit", response_class=HTMLResponse)
async def edit_subtopic_form(request: Request, subtopic_id: UUID, session: AsyncSession = Depends(get_session)):
    subtopic = await session.get(Subtopic, subtopic_id)
    if subtopic is None:
        raise HTTPException(404, "unknown subtopic")
    project = await session.get(Project, subtopic.project_id)
    subtopic_path = "/".join((await get_subtopic_path_parts(session, subtopic))[1:])
    all_subtopics = await get_all_subtopics_for_project(session, project)
    descendant_count = len(collect_descendant_ids(all_subtopics, subtopic.id)) - 1
    stats_map = await projects_service.get_subtopic_stats_map(session, project)
    entry_count = stats_map.get(subtopic.id, {"entry_count": 0})["entry_count"]
    return templates.TemplateResponse(
        request,
        "subtopic_edit.html",
        {
            "project": project,
            "subtopic": subtopic,
            "subtopic_path": subtopic_path,
            "descendant_count": descendant_count,
            "entry_count": entry_count,
            "error": None,
        },
    )


@router.post("/subtopics/{subtopic_id}/edit")
async def update_subtopic_route(
    request: Request, subtopic_id: UUID, name: str = Form(...), session: AsyncSession = Depends(get_session)
):
    try:
        subtopic = await projects_service.rename_subtopic(session, subtopic_id=subtopic_id, name=name)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    project = await session.get(Project, subtopic.project_id)
    subtopic_path = "/".join((await get_subtopic_path_parts(session, subtopic))[1:])
    return RedirectResponse(f"/projects/{project.slug}/{subtopic_path}", status_code=303)


@router.post("/subtopics/{subtopic_id}/delete")
async def delete_subtopic_route(
    request: Request, subtopic_id: UUID, confirm_slug: str = Form(...), session: AsyncSession = Depends(get_session)
):
    subtopic = await session.get(Subtopic, subtopic_id)
    if subtopic is None:
        raise HTTPException(404, "unknown subtopic")
    project = await session.get(Project, subtopic.project_id)

    if confirm_slug != subtopic.slug:
        subtopic_path = "/".join((await get_subtopic_path_parts(session, subtopic))[1:])
        all_subtopics = await get_all_subtopics_for_project(session, project)
        descendant_count = len(collect_descendant_ids(all_subtopics, subtopic.id)) - 1
        stats_map = await projects_service.get_subtopic_stats_map(session, project)
        entry_count = stats_map.get(subtopic.id, {"entry_count": 0})["entry_count"]
        return templates.TemplateResponse(
            request,
            "subtopic_edit.html",
            {
                "project": project,
                "subtopic": subtopic,
                "subtopic_path": subtopic_path,
                "descendant_count": descendant_count,
                "entry_count": entry_count,
                "error": f"Bestätigung stimmt nicht überein (erwartet: {subtopic.slug})",
            },
            status_code=409,
        )

    parent_id = subtopic.parent_subtopic_id
    await projects_service.delete_subtopic(session, subtopic_id=subtopic_id, actor=web_actor(request))
    if parent_id:
        parent = await session.get(Subtopic, parent_id)
        redirect_path = f"/projects/{project.slug}/{'/'.join((await get_subtopic_path_parts(session, parent))[1:])}"
    else:
        redirect_path = f"/projects/{project.slug}"
    return RedirectResponse(redirect_path, status_code=303)


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
    source_ref: str = Form(""),
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
        sources=[("document", source_ref)] if source_ref else None,
    )
    return RedirectResponse(f"/entries/{entry.id}", status_code=303)


@router.get("/entries/upload", response_class=HTMLResponse)
async def upload_form(
    request: Request, project: str, subtopic: str = "", session: AsyncSession = Depends(get_session)
):
    try:
        proj = await get_project_by_slug(session, project)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return templates.TemplateResponse(
        request, "entry_upload.html", {"project": proj, "subtopic_path": subtopic, "error": None}
    )


@router.post("/entries/upload")
async def upload_and_extract(
    request: Request,
    project: str = Form(...),
    subtopic: str = Form(""),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
):
    try:
        proj = await get_project_by_slug(session, project)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc

    settings = get_settings()
    data = await file.read()
    if len(data) > settings.upload_max_bytes:
        return templates.TemplateResponse(
            request,
            "entry_upload.html",
            {
                "project": proj,
                "subtopic_path": subtopic,
                "error": f"Datei zu groß (max. {settings.upload_max_bytes // (1024 * 1024)} MiB).",
            },
            status_code=413,
        )

    try:
        body_markdown = await extraction_service.extract_text(file.filename or "upload", data)
    except extraction_service.ExtractionError as exc:
        return templates.TemplateResponse(
            request,
            "entry_upload.html",
            {"project": proj, "subtopic_path": subtopic, "error": str(exc)},
            status_code=422,
        )

    guessed_title = Path(file.filename).stem if file.filename else "Hochgeladenes Dokument"
    return templates.TemplateResponse(
        request,
        "entry_edit.html",
        {
            "mode": "new",
            "project": proj,
            "subtopic_path": subtopic,
            "entry": None,
            "tags_csv": "",
            "prefill_title": guessed_title,
            "prefill_body": body_markdown,
            "prefill_source_ref": file.filename or "upload",
            "error": None,
        },
    )


@router.get("/entries/{entry_id}", response_class=HTMLResponse)
async def entry_view(request: Request, entry_id: UUID, session: AsyncSession = Depends(get_session)):
    try:
        entry = await entries_service.get_entry_by_id(session, entry_id)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    project, subtopic_path = await _project_and_path_for_entry(session, entry)
    rendered_html = md.markdown(entry.body_markdown, extensions=["extra", "sane_lists", "nl2br"])
    sources = (await session.execute(select(Source).where(Source.entry_id == entry.id))).scalars()
    related_entries = await relations_service.get_related_entries(session, entry.id)
    return templates.TemplateResponse(
        request,
        "entry_view.html",
        {
            "entry": entry,
            "project": project,
            "subtopic_path": subtopic_path,
            "rendered_html": rendered_html,
            "sources": list(sources),
            "related_entries": related_entries,
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
