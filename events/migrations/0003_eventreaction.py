import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('events', '0002_alter_matchevent_score_after'),
    ]

    operations = [
        migrations.CreateModel(
            name='EventReaction',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('reaction', models.CharField(choices=[('like', '👍'), ('dislike', '👎')], max_length=10)),
                ('match_event', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='reactions', to='events.matchevent')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='event_reactions', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Реакция на событие',
                'verbose_name_plural': 'Реакции на события',
            },
        ),
        migrations.AddIndex(
            model_name='eventreaction',
            index=models.Index(fields=['match_event', 'reaction'], name='event_reaction_type_idx'),
        ),
        migrations.AddConstraint(
            model_name='eventreaction',
            constraint=models.UniqueConstraint(fields=('match_event', 'user'), name='unique_event_reaction'),
        ),
    ]
