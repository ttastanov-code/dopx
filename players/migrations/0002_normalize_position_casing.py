# players/migrations/0002_normalize_position_casing.py
"""
Бэкафилл: приводит уже сохранённые Player.position к единому регистру
(upper+strip) — до этой миграции KFF-импорт писал сырые коды как есть,
из-за чего в БД одновременно жили "GK" и "Gk" для одного и того же
амплуа. Не меняет СМЫСЛ данных, только регистр/пробелы — сопоставление
буквенных кодов с русскими подписями делается на лету при отображении
(players/positions.py::position_label), в БД по-прежнему хранится код.

Намеренно НЕ импортирует players.positions.clean_position_code() —
миграции не должны зависеть от прикладного кода приложения, который может
измениться в будущем; логика здесь тривиальна и продублирована безопасно.
"""
from django.db import migrations


def normalize_positions(apps, schema_editor):
    Player = apps.get_model("players", "Player")
    for player in Player.objects.exclude(position="").exclude(position__isnull=True):
        cleaned = player.position.strip().upper()
        if cleaned != player.position:
            Player.objects.filter(pk=player.pk).update(position=cleaned)


def noop_reverse(apps, schema_editor):
    # Обратной операции нет — исходный "сырой" регистр не был осмысленным
    # значением, откатывать нечего.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("players", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(normalize_positions, noop_reverse),
    ]
