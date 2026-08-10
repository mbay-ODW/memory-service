"""Seed the 5 Cowork projects + a representative nested-subtopic tree, plus a few sample
entries, for local testing / manual UI review.

Idempotent: re-running just leaves existing rows (matched by slug/title) untouched. Sample
entries go through entries_service.upsert_entry -- the same DB+git write path real usage
takes -- not raw ORM inserts, so seed data is a realistic exercise of the whole write path.
"""

import asyncio

from sqlalchemy import select

from app.db.base import get_session_factory
from app.db.models import Project, Subtopic
from app.services import entries as entries_service

PROJECTS: list[dict] = [
    {
        "slug": "ferienhaus",
        "name": "Ferienhaus",
        "sensitivity_level": "niedrig",
        "subtopics": ["estrich", "instagram"],
    },
    {
        "slug": "steuer",
        "name": "Selbstständigkeit & Steuererklärung",
        "sensitivity_level": "hoch",
        "subtopics": ["einkommensteuer-2026"],
    },
    {
        "slug": "geb",
        "name": "GEB / Energieberatung",
        "sensitivity_level": "hoch",
        "subtopics": [
            {"slug": "kunde-mueller", "name": "Kunde Müller", "children": ["vorgang-2026-08"]},
        ],
    },
    {
        "slug": "privat",
        "name": "Familie Bayram / Privat",
        "sensitivity_level": "mittel",
        "subtopics": [],
    },
    {
        "slug": "interne-it",
        "name": "Interne IT",
        "sensitivity_level": "niedrig",
        "subtopics": [],
    },
]


SAMPLE_ENTRIES: list[dict] = [
    {
        "project": "ferienhaus",
        "subtopic": "estrich",
        "title": "Abstimmung Estrich-Firma",
        "body_markdown": "Termin für den Estrich-Einbau mit der Firma abgestimmt. Material wird "
        "eine Woche vorher geliefert.",
        "tags": ["baustelle"],
        "sources": [("mail", "seed-estrich-1")],
    },
    {
        "project": "ferienhaus",
        "subtopic": "instagram",
        "title": "Content-Plan August",
        "body_markdown": "Drei Posts für August geplant: Baufortschritt, Umgebung, Testimonial "
        "vom letzten Gast.",
        "tags": ["marketing"],
    },
    {
        "project": "steuer",
        "subtopic": "einkommensteuer-2026",
        "title": "Unterlagen-Checkliste",
        "body_markdown": "Belege für Fahrtkosten und Arbeitsmittel fehlen noch. Steuerberater "
        "bis Ende des Monats zusenden.",
        "tags": ["steuerberater"],
        "follow_up_status": "offen",
    },
    {
        "project": "geb",
        "subtopic": "kunde-mueller/vorgang-2026-08",
        "title": "Statusnotiz Vorgang",
        "body_markdown": "Angebot verschickt, Kunde hat um Bedenkzeit bis nächste Woche gebeten.",
        "tags": ["angebot"],
        "follow_up_status": "wartet",
    },
    {
        "project": "privat",
        "subtopic": "allgemein",
        "title": "Familientermine",
        "body_markdown": "Zahnarzttermin der Kinder und Elternabend in der Schule notiert.",
    },
    {
        "project": "interne-it",
        "subtopic": "allgemein",
        "title": "Server-Wartung Notiz",
        "body_markdown": "Monatliches Backup-Check auf der TrueNAS durchgeführt, alles grün.",
        "tags": ["wartung"],
    },
]


def _title(slug: str) -> str:
    return slug.replace("-", " ").title()


async def seed() -> None:
    session_factory = get_session_factory()
    async with session_factory() as session:
        for project_def in PROJECTS:
            project = (
                await session.execute(select(Project).where(Project.slug == project_def["slug"]))
            ).scalar_one_or_none()
            if project is None:
                project = Project(
                    slug=project_def["slug"],
                    name=project_def["name"],
                    sensitivity_level=project_def["sensitivity_level"],
                )
                session.add(project)
                await session.flush()
                print(f"created project {project.slug}")

            for sub in project_def["subtopics"]:
                sub_slug = sub if isinstance(sub, str) else sub["slug"]
                sub_name = _title(sub_slug) if isinstance(sub, str) else sub["name"]
                subtopic = (
                    await session.execute(
                        select(Subtopic).where(
                            Subtopic.project_id == project.id,
                            Subtopic.parent_subtopic_id.is_(None),
                            Subtopic.slug == sub_slug,
                        )
                    )
                ).scalar_one_or_none()
                if subtopic is None:
                    subtopic = Subtopic(project_id=project.id, slug=sub_slug, name=sub_name)
                    session.add(subtopic)
                    await session.flush()
                    print(f"  created subtopic {project.slug}/{sub_slug}")

                if isinstance(sub, dict):
                    for child_slug in sub.get("children", []):
                        child = (
                            await session.execute(
                                select(Subtopic).where(
                                    Subtopic.project_id == project.id,
                                    Subtopic.parent_subtopic_id == subtopic.id,
                                    Subtopic.slug == child_slug,
                                )
                            )
                        ).scalar_one_or_none()
                        if child is None:
                            session.add(
                                Subtopic(
                                    project_id=project.id,
                                    parent_subtopic_id=subtopic.id,
                                    slug=child_slug,
                                    name=_title(child_slug),
                                )
                            )
                            print(f"    created subtopic {project.slug}/{sub_slug}/{child_slug}")

        await session.commit()

    async with session_factory() as session:
        for sample in SAMPLE_ENTRIES:
            entry = await entries_service.upsert_entry(
                session,
                project_slug=sample["project"],
                subtopic_path=sample["subtopic"],
                title=sample["title"],
                body_markdown=sample["body_markdown"],
                actor="seed-script",
                sources=sample.get("sources"),
                tags=sample.get("tags"),
                follow_up_status=sample.get("follow_up_status"),
            )
            print(f"seeded entry {sample['project']}/{sample['subtopic']}/{entry.slug}")


if __name__ == "__main__":
    asyncio.run(seed())
