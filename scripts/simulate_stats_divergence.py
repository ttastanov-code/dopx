# scripts/simulate_stats_divergence.py
#
# Проверка детектора расхождения рейтинга команды со статистикой и затухания поправки.
# Создаёт тестовую команду и 16 матчей (external_id "test-divergence-*"),
# запускает детектор, затем «чинит» статистику и запускает снова.
#
# Запуск:  python manage.py shell < scripts/simulate_stats_divergence.py
# Очистка:
#   python manage.py shell -c "from matches.models import Match; Match.objects.filter(external_id__startswith='test-divergence-').delete()"

from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from aggregates.models import TeamMatchAggregate, TeamRatingCorrection
from aggregates.tasks import _check_team_stats_divergence
from leagues.models import League
from matches.models import Match, MatchTeamStatistics
from seasons.models import Season
from teams.models import Team
from users.models import SuspiciousActivityFlag

MARKER = "test-divergence-"

# --- чистим данные прошлого запуска ---
Match.objects.filter(external_id__startswith=MARKER).delete()

league = League.objects.first()
season = Season.objects.first()
if not league or not season:
    raise SystemExit(
        "Нужна хотя бы одна существующая Лига и Сезон в БД — "
        "тестовые матчи цепляются к ним, отдельные заводить незачем."
    )

team, _ = Team.objects.get_or_create(
    name="Тестовая команда (расхождение)", defaults={"is_active": True}
)
opponent, _ = Team.objects.get_or_create(
    name="Тестовый соперник (расхождение)", defaults={"is_active": True}
)

now = timezone.now()


def make_match(index, offset_days, performance_score, dominant):
    start = now - timedelta(days=offset_days)
    match = Match.objects.create(
        league=league, season=season, home_team=team, away_team=opponent,
        start_time=start, status="finished",
        voting_open_until=start + timedelta(hours=2),
        external_id=f"{MARKER}{index}",
    )
    TeamMatchAggregate.objects.create(
        team=team, match=match,
        avg_tactics=performance_score, avg_effort=performance_score,
        avg_organization=performance_score, avg_mentality=performance_score,
        total_votes=12, performance_score=performance_score,
    )
    if dominant is not None:
        own_shots, opp_shots = (15, 3) if dominant else (7, 7)
        own_corners, opp_corners = (8, 2) if dominant else (4, 4)
        MatchTeamStatistics.objects.create(match=match, team=team, shots_on_goal=own_shots, corners=own_corners)
        MatchTeamStatistics.objects.create(match=match, team=opponent, shots_on_goal=opp_shots, corners=opp_corners)
    return match


print("Создаю 8 «старых» матчей с нормальным рейтингом (7.5) — это уйдёт в базовую норму команды...")
for i in range(8):
    make_match(i, offset_days=30 + i, performance_score=7.5, dominant=None)

print("Создаю 8 «недавних» матчей: команда объективно доминирует, а рейтинг у сообщества низкий (4.5)...")
for i in range(8, 16):
    make_match(i, offset_days=15 - (i - 8), performance_score=4.5, dominant=True)

content_type = ContentType.objects.get_for_model(Team)
result = _check_team_stats_divergence(team.id, content_type, SuspiciousActivityFlag)
correction = TeamRatingCorrection.objects.filter(team=team).first()

print("\n--- Результат детекта (должен найти паттерн «underrated_despite_dominance») ---")
print(f"Флагов создано за этот прогон: {result}")
if correction:
    print(f"Поправка в TeamRatingCorrection: {correction.correction:+.3f} (паттерн: {correction.last_pattern})")
else:
    print("Поправка не создалась — паттерн не сработал, проверьте константы в aggregates/tasks.py")

print("\nТеперь делаю недавние матчи «нормальными» (статистика и рейтинг выравниваются) — проверяю затухание...")
MatchTeamStatistics.objects.filter(match__external_id__startswith=MARKER, team=team).update(shots_on_goal=7, corners=4)
MatchTeamStatistics.objects.filter(match__external_id__startswith=MARKER, team=opponent).update(shots_on_goal=7, corners=4)
TeamMatchAggregate.objects.filter(
    match__external_id__startswith=MARKER, match__start_time__gte=now - timedelta(days=15)
).update(performance_score=7.0, avg_tactics=7.0, avg_effort=7.0, avg_organization=7.0, avg_mentality=7.0)

_check_team_stats_divergence(team.id, content_type, SuspiciousActivityFlag)
correction.refresh_from_db()
print(f"Поправка после следующего прогона (должна была уменьшиться вдвое сама, без вас): {correction.correction:+.3f}")

print("\nГотово. Очистить тестовые данные:")
print("  Match.objects.filter(external_id__startswith='test-divergence-').delete()")
