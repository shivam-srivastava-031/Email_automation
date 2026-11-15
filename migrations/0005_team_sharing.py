"""Team sharing: the admin's email and email content apply to the whole team.

What this does to the database, deliberately:
  * CREATES `email_orchestrator_teamemailsettings` (the admin's two switches
    and the team content) - a new table nothing old touches.
  * ADDS `use_team_mailbox` / `use_team_content` to agent settings, both
    with database defaults (on), so the code still running during a deploy
    can keep inserting rows. On is the owner's instruction: by default the
    admin's email and content apply to every member of the team.
  * SEEDS each business's team content from the admin's own agent (text,
    button and attachments - the same stored files), so the content the
    admin already wrote is what the team receives, and the admin's agent
    sends exactly what it sent before. Another of the admin's own agents
    with content of its own keeps it (its switch is turned off).
  * Does NOT drop `use_admin_mailbox`. It leaves Django's state only: the
    release still serving during a deploy writes it, and dropping a column
    under a running release has caused three outages in this codebase. It
    keeps its database default and is unused; drop it later, together with
    `use_company_material` and the companyemailsettings table (0004).
"""
from collections import defaultdict

import django.db.models.deletion
from django.db import migrations, models

DEFAULT_CTA = "Book a 15-minute call"
SENDABLE = ("file", "link")
CONTENT_FIELDS = (
    "body_mode", "playbook", "fixed_subject", "fixed_body",
    "include_booking_link", "cta_text", "cta_url",
)


def _has_content(s, EmailAsset):
    return bool(
        (s.playbook or "").strip() or (s.fixed_subject or "").strip() or (s.fixed_body or "").strip()
        or s.body_mode == "fixed" or (s.cta_url or "").strip() or not s.include_booking_link
        or (s.cta_text or DEFAULT_CTA) != DEFAULT_CTA
        or EmailAsset.objects.filter(agent_id=s.agent_id, deleted=False, kind__in=SENDABLE).exists()
    )


def seed_team_content(apps, schema_editor):
    AgentEmailSettings = apps.get_model("email_orchestrator", "AgentEmailSettings")
    TeamEmailSettings = apps.get_model("email_orchestrator", "TeamEmailSettings")
    EmailAsset = apps.get_model("email_orchestrator", "EmailAsset")
    BusinessRecord = apps.get_model("lead", "BusinessRecord")

    by_business = defaultdict(list)
    for s in AgentEmailSettings.objects.select_related("agent", "agent__partner"):
        business_id = s.agent.business_record_id or s.business_id
        if business_id:
            by_business[business_id].append(s)

    for business_id, rows in by_business.items():
        # A re-run must never overwrite what an admin has set since.
        if TeamEmailSettings.objects.filter(business_id=business_id).exists():
            continue
        admin_id = BusinessRecord.objects.filter(pk=business_id).values_list("user_id", flat=True).first()

        def admin_owned(s):
            # Same rule as sharing.is_admin_agent: the partner's user, else the admin.
            a = s.agent
            owner = a.partner.user_id if (a.partner_id and a.partner and a.partner.user_id) else admin_id
            return admin_id is not None and owner == admin_id

        with_content = [s for s in rows if admin_owned(s) and _has_content(s, EmailAsset)]
        source = max(with_content, key=lambda s: s.updated_at) if with_content else None

        team = TeamEmailSettings(business_id=business_id)
        if source is not None:
            for field in CONTENT_FIELDS:
                setattr(team, field, getattr(source, field))
            team.cta_text = team.cta_text or DEFAULT_CTA
        team.save()

        if source is not None:
            for item in EmailAsset.objects.filter(agent_id=source.agent_id, deleted=False, kind__in=SENDABLE):
                EmailAsset.objects.create(
                    agent_id=None, business_id=business_id, kind=item.kind, name=item.name,
                    description=item.description, tags=item.tags,
                    file=item.file.name if item.file else None,  # same stored file, no copy
                    file_size=item.file_size, mime_type=item.mime_type, url=item.url,
                    subject=item.subject, body=item.body, is_active=item.is_active,
                    sort_order=item.sort_order, created_by_id=item.created_by_id,
                )
        for s in with_content:
            if s.pk != source.pk:
                s.use_team_content = False
                s.save(update_fields=["use_team_content"])


class Migration(migrations.Migration):

    dependencies = [
        ('email_orchestrator', '0004_simple_email_cta_retire_company_library'),
        ('lead', '0141_merge_20261016_0543'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name='agentemailsettings', name='use_admin_mailbox'),
            ],
            database_operations=[],
        ),
        migrations.AddField(
            model_name='agentemailsettings',
            name='use_team_content',
            field=models.BooleanField(db_default=True, default=True),
        ),
        migrations.AddField(
            model_name='agentemailsettings',
            name='use_team_mailbox',
            field=models.BooleanField(db_default=True, default=True),
        ),
        migrations.CreateModel(
            name='TeamEmailSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('share_mailbox', models.BooleanField(default=True)),
                ('share_content', models.BooleanField(default=True)),
                ('body_mode', models.CharField(choices=[('ai', 'AI writes it'), ('fixed', 'Fixed template')], default='ai', max_length=8)),
                ('playbook', models.TextField(blank=True, default='')),
                ('fixed_subject', models.CharField(blank=True, default='', max_length=300)),
                ('fixed_body', models.TextField(blank=True, default='')),
                ('include_booking_link', models.BooleanField(default=True)),
                ('cta_text', models.CharField(blank=True, default='Book a 15-minute call', max_length=80)),
                ('cta_url', models.URLField(blank=True, default='', max_length=500)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('business', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='team_email_settings', to='lead.businessrecord')),
            ],
            options={
                'verbose_name': 'Team email settings',
                'verbose_name_plural': 'Team email settings',
            },
        ),
        migrations.RunPython(seed_team_content, migrations.RunPython.noop),
    ]
