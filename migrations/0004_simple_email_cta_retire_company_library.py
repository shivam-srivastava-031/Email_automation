"""Simplify the email orchestrator: add the call-to-action button, retire the
company library.

What this does to the database, deliberately:
  * ADDS `cta_text` and `cta_url`, both with database-level defaults, so the
    code still running during a deploy can keep inserting rows.
  * COPIES every live company item (agent NULL) to each agent of its business
    that has email settings, sharing the same stored file, then soft-deletes
    the company original - so nothing anyone uploaded stops being sent.
  * Does NOT drop `use_company_material` or the companyemailsettings table.
    They leave Django's state only (SeparateDatabaseAndState). Dropping a
    column while the previous release is still serving has caused three
    outages in this codebase; the column keeps its database default and the
    table is unused. A later migration can drop both once this release is
    the only one running anywhere.
"""
from django.db import migrations, models
from django.utils import timezone


def company_items_to_agents(apps, schema_editor):
    EmailAsset = apps.get_model("email_orchestrator", "EmailAsset")
    AgentEmailSettings = apps.get_model("email_orchestrator", "AgentEmailSettings")
    # Historical models get a plain manager, which includes soft-deleted rows,
    # hence the explicit deleted=False everywhere.
    for item in EmailAsset.objects.filter(agent__isnull=True, deleted=False):
        if not item.business_id:
            continue
        agent_ids = AgentEmailSettings.objects.filter(
            agent__business_record_id=item.business_id,
        ).values_list("agent_id", flat=True)
        for agent_id in agent_ids:
            # An agent that already has an item of that name keeps its own.
            if EmailAsset.objects.filter(
                agent_id=agent_id, deleted=False, name__iexact=item.name,
            ).exists():
                continue
            EmailAsset.objects.create(
                agent_id=agent_id,
                business_id=item.business_id,
                kind=item.kind,
                name=item.name,
                description=item.description,
                tags=item.tags,
                file=item.file.name if item.file else None,  # same stored file, no copy
                file_size=item.file_size,
                mime_type=item.mime_type,
                url=item.url,
                subject=item.subject,
                body=item.body,
                is_active=item.is_active,
                sort_order=item.sort_order,
                created_by_id=item.created_by_id,
            )
        item.deleted = True
        item.deleted_on = timezone.now()
        item.save(update_fields=["deleted", "deleted_on"])


class Migration(migrations.Migration):

    dependencies = [
        ("email_orchestrator", "0003_admin_mailbox_and_company_library"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentemailsettings",
            name="cta_text",
            field=models.CharField(
                blank=True, db_default="Book a 15-minute call", default="Book a 15-minute call",
                max_length=80,
            ),
        ),
        migrations.AddField(
            model_name="agentemailsettings",
            name="cta_url",
            field=models.URLField(blank=True, db_default="", default="", max_length=500),
        ),
        migrations.RunPython(company_items_to_agents, migrations.RunPython.noop),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="agentemailsettings", name="use_company_material"),
                migrations.DeleteModel(name="CompanyEmailSettings"),
            ],
            database_operations=[],
        ),
    ]
