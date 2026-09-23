# aggregates/management/commands/calibrate_player_objective_weights.py
"""
Подбор весов запасной формулы «оценки игры по статистике» — 2026-09-24.

Зачем: когда у игрока в матче нет готовой оценки по статистике (RATING в
MatchPlayerStatistics.raw), детектор расхождения (aggregates/tasks.py::
_player_objective_score) считает свою сумму баллов. Раньше веса этой суммы
были подобраны «на глаз». Эта команда подбирает их по данным: линейная
регрессия (с небольшим сглаживанием, ridge) метрик матча на готовую оценку
по статистике — на тех матчах, где есть и то, и другое. Итоговая формула
выдаёт число в той же шкале ~1-10.

Результат сохраняется в «Настройки платформы» (ключ
player_objective_weights) — формула начинает его использовать без деплоя.
Удалить ключ в настройках = вернуться к весам «на глаз».

Использование:
    python manage.py calibrate_player_objective_weights          # только показать результат
    python manage.py calibrate_player_objective_weights --save   # сохранить в настройки
"""
import json

from django.core.management.base import BaseCommand

from matches.models import MatchPlayerStatistics

FEATURES = [
    "GOALS", "ASSISTS", "SHOTS_ON_TARGET", "SHOTS_TOTAL", "KEY_PASSES", "BIG_CHANCES_CREATED",
    "ACCURATE_PASSES", "SUCCESSFUL_DRIBBLES", "TACKLES", "INTERCEPTIONS", "CLEARANCES",
    "DUELS_WON", "DUELS_LOST", "AERIALS_WON", "AERIALS_LOST", "SAVES", "SAVES_INSIDE_BOX",
    "FOULS", "FOULS_DRAWN", "YELLOWCARDS", "DISPOSSESSED", "POSSESSION_LOST",
    "ERROR_LEAD_TO_SHOT", "GOALS_CONCEDED", "MINUTES_PLAYED",
]
RIDGE_LAMBDA = 1.0
MIN_SAMPLES = 200
SETTING_KEY = "player_objective_weights"


def _num(v) -> float:
    if isinstance(v, bool):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _solve(a, b):
    """Решение СЛАУ a·x = b методом Гаусса с выбором главного элемента."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[pivot] = m[pivot], m[col]
        if abs(m[col][col]) < 1e-12:
            continue
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


class Command(BaseCommand):
    help = "Подобрать веса запасной формулы оценки игры по статистике (регрессия на готовую оценку)"

    def add_arguments(self, parser):
        parser.add_argument("--save", action="store_true", help="Сохранить веса в «Настройки платформы»")

    def handle(self, *args, **options):
        rows = []
        for raw in MatchPlayerStatistics.objects.filter(raw__has_key="RATING").values_list("raw", flat=True):
            rating = _num(raw.get("RATING"))
            if rating <= 0 or _num(raw.get("MINUTES_PLAYED")) < 30:
                continue
            rows.append(([1.0] + [_num(raw.get(f)) for f in FEATURES], rating))

        n = len(rows)
        self.stdout.write(f"Матчей игроков с готовой оценкой (≥30 минут): {n}")
        if n < MIN_SAMPLES:
            self.stderr.write(self.style.WARNING(
                f"Мало данных (нужно ≥{MIN_SAMPLES}). Сначала догрузите статистику: python manage.py sportmonks_backfill_stats"
            ))
            return

        k = len(FEATURES) + 1
        xtx = [[0.0] * k for _ in range(k)]
        xty = [0.0] * k
        for x, y in rows:
            for i in range(k):
                xty[i] += x[i] * y
                for j in range(k):
                    xtx[i][j] += x[i] * x[j]
        for i in range(1, k):  # свободный член не штрафуем
            xtx[i][i] += RIDGE_LAMBDA
        w = _solve(xtx, xty)

        ys = [y for _, y in rows]
        mean_y = sum(ys) / n
        preds = [sum(wi * xi for wi, xi in zip(w, x)) for x, _ in rows]
        ss_res = sum((y - p) ** 2 for y, p in zip(ys, preds))
        ss_tot = sum((y - mean_y) ** 2 for y in ys) or 1.0
        r2 = 1 - ss_res / ss_tot
        mae = sum(abs(y - p) for y, p in zip(ys, preds)) / n

        self.stdout.write(f"Свободный член: {w[0]:.3f}")
        for name, weight in sorted(zip(FEATURES, w[1:]), key=lambda t: -abs(t[1])):
            self.stdout.write(f"  {name:<22} {weight:+.4f}")
        self.stdout.write(f"Качество: R² = {r2:.2f}, средняя ошибка = {mae:.2f} балла")

        if r2 < 0.3:
            self.stderr.write(self.style.WARNING("Формула объясняет оценку плохо (R² < 0.3) — не сохраняю."))
            return

        if options["save"]:
            from core.models import PlatformSetting

            payload = {"intercept": round(w[0], 4), "weights": {f: round(v, 4) for f, v in zip(FEATURES, w[1:])},
                       "r2": round(r2, 3), "samples": n}
            PlatformSetting.objects.update_or_create(
                key=SETTING_KEY,
                defaults={
                    "value": json.dumps(payload),
                    "value_type": PlatformSetting.TYPE_STRING,
                    "description": "Веса запасной формулы оценки игры по статистике (команда calibrate_player_objective_weights). Удалить — вернуться к весам по умолчанию.",
                },
            )
            from django.core.cache import cache
            cache.delete(f"platform_setting:{SETTING_KEY}")
            self.stdout.write(self.style.SUCCESS("Сохранено в «Настройки платформы» — применяется со следующей проверки."))
        else:
            self.stdout.write("Чтобы применить — запустите с --save.")
