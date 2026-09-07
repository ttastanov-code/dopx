# parsers/tasks.py
"""
Celery-задачи синхронизации с внешним KFF API.

update_match_statuses зарегистрирована в CELERY_BEAT_SCHEDULE дважды под
разными именами (update-live-matches */2m, update-scheduled-matches */10m),
но фильтрует один и тот же набор матчей (status in scheduled/live) — на
кратных 10 минутам отметках Celery Beat ставит в очередь два параллельных
запуска. import_events_and_minutes (parsers/kff/importers.py) пишет события
матча (создаёт/обновляет по совпадению минута+тип+сторона) внутри
@transaction.atomic — атомарность одной транзакции не защищает от
конкурентной второй: дубли событий, рост времени отклика или deadlock на
уровне Postgres.

Защита в две линии:
  A. Redis-лок на КАЖДЫЙ матч (не на всю задачу — иначе воркеры не могли бы
     параллелить разные матчи), через cache.add() (аналог SETNX) до сетевых
     вызовов к API; занятый лок — контролируемый skip, не исключение.
  B. select_for_update(nowait=True) на запись — defense-in-depth на случай,
     если Redis-лок не сработал (эвакуация кэша). nowait=True: без него
     второй воркер встал бы в очередь ожидания; с ним — сразу
     OperationalError, которую ловим как skip.

Redis-лок держится на всё время сетевого I/O к KFF API, select_for_update —
только на момент записи в БД. Наоборот было бы опасно: блокировка строки на
время HTTP-запроса держала бы транзакцию Postgres открытой на неопределённое
время, вплоть до исчерпания пула соединений при деградации внешнего API.
"""
from __future__ import annotations

import logging
import uuid
from collections import Counter
from datetime import timedelta
from functools import partial

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from django.db import transaction
from django.db.utils import OperationalError
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

from parsers.kff.client import KFFAPICircuitBreakerOpen, KFFClient
from parsers.kff.pipeline import import_full_match, sync_season

logger = logging.getLogger(__name__)

# TTL лока с запасом относительно самого частого расписания (*/2 минуты),
# чтобы лок гарантированно "протух", даже если воркер упал посреди работы
# и не успел выполнить `finally: cache.delete(lock_key)`.
MATCH_SYNC_LOCK_TIMEOUT_SECONDS = 180

# Матчи одного тура обычно разыгрываются в пределах одного уик-энда (2-3
# дня подряд) — см. Match.was_rescheduled в matches/models.py. Разница
# больше этого порога от "типичной" (самой частой) даты тура — надёжный
# признак переноса, не зависящий от того, что API сейчас говорит про
# is_schedule_tentative (см. _detect_rescheduled_outlier ниже).
RESCHEDULED_OUTLIER_THRESHOLD_DAYS = 3


@shared_task(bind=True, max_retries=3)
def sync_kff_premier_league(self, year: int = None):
    """Периодическая синхронизация Премьер-Лиги (авто-поиск сезона)."""
    logger.info(f"🔄 Starting Premier League sync (year={year})")

    client = KFFClient()
    season_id = client.find_premier_league_season(year=year)

    if not season_id:
        error_msg = f"❌ Could not find Premier League season (year={year})"
        logger.error(error_msg)
        _send_sync_error_alert(error_msg, "season_detection")
        return {"success": 0, "failed": 0, "error": error_msg}

    logger.info(f"✅ Syncing Premier League season {season_id} (year={year})")

    try:
        result = sync_season(season_id=season_id, tournament_code="pl", auto_detect=False)
        logger.info(f"✅ Premier League sync completed: {result}")

        if result.get("failed", 0) > 0:
            _send_sync_error_alert(
                f"Синхронизация завершена с ошибками: {result['failed']} из {result['total']}",
                "sync_errors",
                extra_data=result,
            )

        return result
    except Exception as e:
        error_msg = f"❌ Premier League sync failed: {type(e).__name__}: {e}"
        logger.error(error_msg, exc_info=True)
        _send_sync_error_alert(error_msg, "sync_critical", extra_data={"exception": str(e)})
        return {"success": 0, "failed": 0, "error": str(e)}


@shared_task(bind=True, max_retries=3)
def sync_recent_matches(self, season_id: int = None, limit: int = 10, tournament_code: str = None):
    """Синхронизация последних ЗАВЕРШЁННЫХ матчей (сортировка по дате)."""
    if tournament_code is None:
        tournament_code = KFFClient.TARGET_TOURNAMENT

    logger.info(
        f"🔄 Syncing recent finished matches (limit={limit}, season_id={season_id}, "
        f"tournament={tournament_code})"
    )

    client = KFFClient()

    if season_id is None:
        season_id = client.find_premier_league_season()
        if not season_id:
            error_msg = f"❌ Could not auto-detect season for tournament {tournament_code}"
            logger.error(error_msg)
            _send_sync_error_alert(error_msg, "season_detection")
            return {"success": 0, "total": 0, "error": error_msg}

    recent_ids = client.get_recent_finished_matches(
        season_id=season_id, limit=limit, tournament_code=tournament_code
    )

    if not recent_ids:
        logger.warning("⚠️  No recent finished matches found")
        return {"success": 0, "total": 0, "error": "No finished matches"}

    success = 0
    failed = 0
    failed_matches = []

    for mid in recent_ids:
        try:
            if import_full_match(mid, season_id, tournament_code=tournament_code):
                success += 1
            else:
                failed += 1
                failed_matches.append(mid)
        except Exception as e:
            logger.error(f"❌ Failed to import match {mid}: {type(e).__name__}: {e}")
            failed += 1
            failed_matches.append(mid)

    logger.info(f"✅ Synced {success}/{len(recent_ids)} recent finished matches")

    if failed > 0:
        _send_sync_error_alert(
            f"Ошибки при синхронизации {failed} матчей: {failed_matches[:5]}",
            "sync_errors",
            extra_data={"failed_matches": failed_matches, "success": success},
        )

    return {
        "success": success,
        "total": len(recent_ids),
        "failed": failed,
        "season_id": season_id,
        "tournament_code": tournament_code,
    }


@shared_task(bind=True, max_retries=3)
def sync_full_season(self, season_id: int = None, tournament_code: str = None):
    """ПОЛНАЯ синхронизация ВСЕХ матчей сезона."""
    if tournament_code is None:
        tournament_code = KFFClient.TARGET_TOURNAMENT

    logger.info(f"🚀 Starting FULL SEASON sync (season_id={season_id}, tournament={tournament_code})")

    client = KFFClient()

    if season_id is None:
        if tournament_code == KFFClient.TARGET_TOURNAMENT:
            season_id = client.find_premier_league_season()
        else:
            seasons = client.get_tournament_seasons(tournament_code=tournament_code)
            season_id = seasons[0]["id"] if seasons else None

        if not season_id:
            error_msg = f"❌ Could not auto-detect season for tournament '{tournament_code}'"
            logger.error(error_msg)
            _send_sync_error_alert(error_msg, "season_detection")
            return {"success": 0, "failed": 0, "error": "Season not found"}

    try:
        result = sync_season(season_id=season_id, tournament_code=tournament_code, auto_detect=False)
        logger.info(f"✅ Full season sync completed: {result}")

        if result.get("failed", 0) > 0:
            _send_sync_error_alert(
                f"Полная синхронизация завершена с ошибками: {result['failed']} из {result['total']}",
                "sync_errors",
                extra_data=result,
            )

        return result
    except Exception as e:
        error_msg = f"❌ Full season sync failed: {type(e).__name__}: {e}"
        logger.error(error_msg, exc_info=True)
        _send_sync_error_alert(error_msg, "sync_critical", extra_data={"exception": str(e)})
        return {"success": 0, "failed": 0, "error": str(e)}


def _acquire_match_sync_lock(match_id: uuid.UUID, worker_token: str) -> bool:
    """
    Атомарно берёт распределённый лок на синхронизацию конкретного матча.

    `cache.add()` на Django-редис-бэкенде транслируется в Redis `SET key
    value NX EX <timeout>` — атомарная операция "создать, только если ключа
    ещё нет", ровно то, что нужно для дистрибьютед-лока без гонок между
    воркерами, проверяющими лок одновременно.
    """
    lock_key = f"parsers:match_sync_lock:{match_id}"
    return cache.add(lock_key, worker_token, timeout=MATCH_SYNC_LOCK_TIMEOUT_SECONDS)


def _release_match_sync_lock(match_id: uuid.UUID) -> None:
    cache.delete(f"parsers:match_sync_lock:{match_id}")


def _detect_rescheduled_outlier(match, tour, start_time) -> bool:
    """
    True, если `start_time` этого матча отличается от "типичной" (самой
    частой) даты остальных матчей его же тура больше, чем на
    RESCHEDULED_OUTLIER_THRESHOLD_DAYS — см. докстринг Match.was_rescheduled.

    Специально НЕ полагается на `is_schedule_tentative` от KFF — тот сигнал
    живёт только пока дата официально не подтверждена; матч "Каспий —
    Қайрат" (тур 6, весь тур отыгран 18-19 апреля, эта пара — 5 сентября)
    к моменту, когда мы вообще успеваем на него посмотреть, у KFF уже
    `is_schedule_tentative=False` — обычный сигнал ничего не поймал бы.
    Сравнение с датами сокомандников по туру не зависит от таймингов API.

    Требует минимум 2 других матча в туре, чтобы "типичная дата" вообще
    имела смысл — на самом первом матче импортированного тура сравнивать
    не с чем, это НЕ баг, просто отложенное обнаружение (сработает, как
    только импортируются остальные матчи тура).
    """
    from matches.models import Match

    if tour is None or start_time is None:
        return False

    sibling_dates = list(
        Match.objects.filter(season_id=match.season_id, tour=tour)
        .exclude(id=match.id)
        .values_list("start_time", flat=True)
    )
    if len(sibling_dates) < 2:
        return False

    typical_date, _count = Counter(dt.date() for dt in sibling_dates).most_common(1)[0]
    return abs((start_time.date() - typical_date).days) > RESCHEDULED_OUTLIER_THRESHOLD_DAYS


@shared_task(bind=True, max_retries=3, rate_limit="30/m")
def update_match_statuses(self):
    """
    Полная синхронизация незавершённых матчей с внешним API.

    Что обновляется:
    - Статус матча (scheduled → live → finished)
    - Счёт (home_score, away_score)
    - Время окончания (end_time)
    - События матча (голы, карточки, замены, VAR)
    - Составы (если ещё не загружены)
    - Статистика матча

    Запускается каждые 2-5 минут для live-матчей, каждые 10-15 мин для
    scheduled (см. CELERY_BEAT_SCHEDULE в dopx/settings.py — ОБА расписания
    зовут именно эту функцию, поэтому защита от гонок ниже обязательна, а
    не опциональна).
    """
    from events.models import MatchEvent
    from matches.models import Match
    from parsers.kff.importers import (
        STATUS_MAP,
        import_coaches,
        import_events_and_minutes,
        import_lineups,
        import_stats,
        parse_match_datetime,
    )
    from parsers.models import ParserSyncRun

    logger.info("🔄 Starting match status & data sync...")

    started_at = timezone.now()
    client = KFFClient()
    worker_token = self.request.id or str(uuid.uuid4())

    # "postponed" включён сюда же — иначе перенесённый матч навсегда
    # выпадает из опроса и мы никогда не узнаем ни о новой дате, ни об
    # окончательной отмене (см. STATUS_MAP выше и docs/BACKLOG.md).
    active_matches = Match.objects.filter(status__in=["scheduled", "live", "postponed"]).select_related(
        "home_team", "away_team", "season", "league", "stadium"
    )

    stats = {
        "total": active_matches.count(),
        "updated": 0,
        "unchanged": 0,
        "errors": 0,
        "new_events": 0,
        "status_changes": 0,
        "skipped_locked": 0,
        "skipped_manual_override": 0,
    }
    # Дашборд ("Здоровье данных") показывает ПОСЛЕДНИЕ N ошибок с именем
    # матча, не только счётчик — иначе "5 ошибок" ничего не говорит о том,
    # что чинить. Капаем список, чтобы не раздувать JSONField при плохом
    # прогоне (100 ошибок за раз всё равно нечитаемы на дашборде).
    error_samples: list[dict] = []
    MAX_ERROR_SAMPLES = 20

    for match in active_matches:
        # === УРОВЕНЬ A: распределённый лок на весь цикл синхронизации матча,
        # включая сетевые вызовы к внешнему API. ===
        if not _acquire_match_sync_lock(match.id, worker_token):
            logger.warning(
                f"⏭️  Match {match.id}: синхронизация уже выполняется другим воркером — пропуск"
            )
            stats["skipped_locked"] += 1
            continue

        # === УРОВЕНЬ A.5: ручная пометка "не трогать" ===
        # Матч, который staff поправил вручную (см. Match.manual_override) —
        # KFF показывает баннер "перенесён" раньше, чем реально меняет свои
        # структурные status/date, поэтому без этой проверки следующий же
        # цикл увидел бы api_status="scheduled" и молча откатил бы ручную
        # правку. Пропускаем ДО сетевого вызова — лишний смысл дёргать API
        # за данными, которые мы всё равно проигнорируем.
        if match.manual_override:
            logger.info(f"⏭️  Match {match.id}: manual_override=True — пропуск автосинка статуса/даты")
            stats["skipped_manual_override"] += 1
            _release_match_sync_lock(match.id)
            continue

        try:
            tournament_code = getattr(match.season, "tournament_code", "pl")

            game_data = client.get_game_details(match.external_id, tournament_code=tournament_code)
            if not game_data:
                logger.warning(f"⚠️ No data for match {match.external_id} from API")
                stats["errors"] += 1
                continue

            # === Вычисляем изменения ПОЛЕЙ матча в памяти — без записи в БД. ===
            updated_fields: list[str] = []
            field_values: dict[str, object] = {}

            api_status = game_data.get("status", "scheduled")
            new_status = STATUS_MAP.get(api_status, match.status)
            # Тот же фикс, что в parsers/kff/importers.py::import_match_core
            # (см. комментарий там за подробным разбором): KFF никогда не
            # шлёт status="postponed" сам по себе — перенос сигнализируется
            # только `is_schedule_tentative=True` поверх status="upcoming".
            # Без этой строки уже импортированный матч, который позже
            # перенесли, навсегда остаётся видимым как 'scheduled' — именно
            # так и получилось с 23-м туром. Когда KFF в итоге твёрдо
            # подтвердит дату (is_schedule_tentative снова false), матч сам
            # вернётся в 'scheduled' на следующем цикле — тем же условием.
            if game_data.get("is_schedule_tentative") and new_status == "scheduled":
                new_status = "postponed"
            if new_status != match.status:
                field_values["status"] = new_status
                updated_fields.append("status")
                stats["status_changes"] += 1
                logger.info(f"📊 Match {match.id}: {match.status} → {new_status}")

            # Backfill номера тура для матчей, импортированных до того, как
            # мы начали его читать (см. Match.tour, matches/migrations/0005).
            api_tour = game_data.get("tour")
            if api_tour is not None and match.tour != api_tour:
                field_values["tour"] = api_tour
                updated_fields.append("tour")

            # Дата/время матча РАНЬШЕ никогда не пересинхронизировались
            # здесь (только на самом первом импорте) — если KFF переносит
            # матч на другую дату, сайт продолжал показывать старую
            # навсегда. Синкаем только для scheduled/postponed: у finished/
            # live/cancelled start_time — уже свершившийся факт, трогать
            # его задним числом (и тем самым тихо переписывать реальную
            # историю) не нужно.
            if new_status in ("scheduled", "postponed") and game_data.get("date"):
                api_start_time = parse_match_datetime(
                    game_data.get("date"), game_data.get("time"),
                    tz=timezone.get_current_timezone(),
                )
                if abs((api_start_time - match.start_time).total_seconds()) > 60:
                    field_values["start_time"] = api_start_time
                    updated_fields.append("start_time")
                    logger.info(
                        f"📅 Match {match.id}: дата перенесена {match.start_time} → {api_start_time}"
                    )

            # НАЙДЕНО (2026-09-01): статус-based признак "Перенесён" выше
            # (is_schedule_tentative) ловит только КОРОТКОЕ окно, пока KFF
            # сам не определился с датой. Матч, у которого дата уже давно
            # твёрдая, но при этом сильно выбивается из дат своего тура
            # (весь тур сыгран, а этот — через 4 месяца), этим признаком не
            # ловится вообще — см. Match.was_rescheduled и докстринг
            # _detect_rescheduled_outlier. Липкий: once True, дальше не
            # перепроверяем (не нужно — исторический факт не меняется).
            if not match.was_rescheduled:
                effective_tour = field_values.get("tour", match.tour)
                effective_start = field_values.get("start_time", match.start_time)
                if _detect_rescheduled_outlier(match, effective_tour, effective_start):
                    field_values["was_rescheduled"] = True
                    updated_fields.append("was_rescheduled")
                    logger.info(
                        f"🔀 Match {match.id}: помечен как перенесённый относительно тура "
                        f"{effective_tour} (дата {effective_start.date()} выбивается из остальных)"
                    )

            api_home_score = game_data.get("home_score")
            api_away_score = game_data.get("away_score")

            if api_home_score is not None and match.home_score != api_home_score:
                field_values["home_score"] = api_home_score
                updated_fields.append("home_score")

            if api_away_score is not None and match.away_score != api_away_score:
                field_values["away_score"] = api_away_score
                updated_fields.append("away_score")

            if new_status == "finished" and not match.end_time:
                api_end_time = game_data.get("end_time") or game_data.get("finished_at")
                if api_end_time:
                    from parsers.kff.importers import parse_match_datetime

                    field_values["end_time"] = parse_match_datetime(
                        api_end_time.split("T")[0] if "T" in str(api_end_time) else api_end_time,
                        None,
                        tz=timezone.get_current_timezone(),
                    )
                else:
                    field_values["end_time"] = match.start_time + timedelta(minutes=110)
                updated_fields.append("end_time")

            # just_finished — сравнение старого/нового статуса, НЕ через
            # `not match.voting_open_until` (испорчено для матчей, импортированных
            # до фикса — см. docs/adr/0007-kff-importer-status-and-timing-fixes.md,
            # дополнение).
            just_finished = new_status == "finished" and match.status != "finished"
            if just_finished:
                field_values["voting_open_until"] = match.start_time + timedelta(hours=48)
                updated_fields.append("voting_open_until")

            if game_data.get("has_lineup") and not match.has_lineup:
                field_values["has_lineup"] = True
                updated_fields.append("has_lineup")

            # === УРОВЕНЬ B: короткая транзакция с построчной блокировкой Postgres
            # ИМЕННО на момент записи, а НЕ на время сетевого I/O выше. ===
            if updated_fields:
                try:
                    with transaction.atomic():
                        locked_match = Match.objects.select_for_update(nowait=True).get(
                            pk=match.pk
                        )
                        for field_name, value in field_values.items():
                            setattr(locked_match, field_name, value)
                        locked_match.save(update_fields=[*updated_fields, "updated_at"])
                        if just_finished:
                            # Продуктовый аудит, раздел 5b ("Follow-граф"):
                            # уведомляем ТОЛЬКО подписчиков команд/игроков
                            # этого матча — в отличие от `send_voting_open_
                            # notification` (широковещательная рассылка ВСЕМ
                            # верифицированным пользователям), это адресный
                            # канал. on_commit — чтобы не поставить задачу в
                            # очередь раньше, чем реально закоммитится смена
                            # статуса (иначе воркер уведомлений мог бы
                            # прочитать матч ДО commit и получить старый
                            # статус).
                            from notifications.tasks import notify_followers_match_activity

                            transaction.on_commit(
                                partial(notify_followers_match_activity.delay, str(match.id))
                            )
                    stats["updated"] += 1
                    logger.debug(f"✅ Match {match.id} updated: {updated_fields}")
                except OperationalError:
                    logger.warning(
                        f"⏭️  Match {match.id}: строка заблокирована другой транзакцией — "
                        f"пропуск записи в этом цикле"
                    )
                    stats["skipped_locked"] += 1
                    continue
            else:
                stats["unchanged"] += 1

            # Обновляем локальный объект, чтобы последующие шаги (события,
            # составы, статистика) видели актуальный статус без лишнего SELECT.
            match.status = new_status
            if "home_score" in field_values:
                match.home_score = field_values["home_score"]
            if "away_score" in field_values:
                match.away_score = field_values["away_score"]
            if "has_lineup" in field_values:
                match.has_lineup = field_values["has_lineup"]

            # === Синхронизация событий матча ===
            # Существующие события читаются одним запросом и группируются в
            # памяти по минуте — не по отдельному .exists() на каждое событие
            # из ответа API.
            events_data = client.get_events(match.external_id, tournament_code=tournament_code)
            if events_data and events_data.get("events"):
                existing_types_by_minute: dict[int, set[str]] = {}
                for minute_value, event_type_value in MatchEvent.objects.filter(
                    match=match
                ).values_list("minute", "event_type"):
                    existing_types_by_minute.setdefault(minute_value, set()).add(event_type_value)

                api_events = events_data.get("events", [])

                new_events = []
                for evt in api_events:
                    minute = evt.get("minute")
                    event_type = (evt.get("event_type") or "").lower()
                    normalized_type = (
                        event_type.split("_")[0] if "_" in event_type else event_type
                    )
                    already_exists = any(
                        normalized_type in existing_type
                        for existing_type in existing_types_by_minute.get(minute, set())
                    )
                    if not already_exists:
                        new_events.append(evt)

                if new_events:
                    # Лок уровня A уже держится на match.id, конкурентная запись
                    # другим воркером исключена. replace_existing=False — сюда
                    # передаётся только дельта, import_events_and_minutes не
                    # должна стирать уже сохранённые события прошлых циклов.
                    #
                    # created_now собирает РЕАЛЬНО новые MatchEvent (не
                    # обновления уже существующих) через on_event_created —
                    # см. её докстринг в parsers/kff/importers.py. Нужно
                    # для live push-уведомлений о голах/карточках (2026-09-01,
                    # прямой запрос пользователя): "если я не увидел гол
                    # в приложении, я хочу узнать о нём пушем".
                    created_now: list = []
                    if import_events_and_minutes(
                        match, {"events": new_events}, replace_existing=False,
                        on_event_created=created_now.append,
                    ):
                        stats["new_events"] += len(new_events)
                        logger.info(f"⚡ Added {len(new_events)} new events for match {match.id}")

                        # Ленивый импорт (тот же паттерн, что и у
                        # notify_followers_match_activity ниже по файлу) —
                        # notify_followers_match_event ставится в очередь
                        # СРАЗУ, не через transaction.on_commit:
                        # import_events_and_minutes сама обёрнута в
                        # @transaction.atomic и коммитит при возврате, так
                        # что к этой строке событие уже видно снаружи —
                        # задержка on_commit тут не нужна.
                        from notifications.tasks import (
                            PUSH_WORTHY_EVENT_TYPES,
                            notify_followers_match_event,
                        )

                        for created_event in created_now:
                            if created_event.event_type in PUSH_WORTHY_EVENT_TYPES:
                                notify_followers_match_event.delay(str(match.id), str(created_event.id))

            # === Загрузка составов (если ещё нет) ===
            if match.has_lineup and not match.lineups.exists():
                lineup_data = client.get_lineup(match.external_id, tournament_code=tournament_code)
                if lineup_data:
                    if import_coaches(match, lineup_data):
                        logger.info(f"👨‍💼 Coaches imported for match {match.id}")
                    if import_lineups(match, lineup_data):
                        logger.info(f"👥 Lineups imported for match {match.id}")

            # === Статистика матча (опционально) ===
            if match.status == "finished":
                stats_data = client.get_stats(match.external_id, tournament_code=tournament_code)
                if stats_data:
                    import_stats(match, stats_data)

        except KFFAPICircuitBreakerOpen as e:
            # НАЙДЕНО (2026-09-01, kffleague.kz забанил клиент): раньше это
            # ловилось как обычная ошибка ОДНОГО матча (except Exception ниже)
            # и цикл продолжал молотить ОСТАЛЬНЫЕ ~60 матчей — каждый со
            # своими 3 ретраями внутри client._get(), то есть до сотни лишних
            # ударов по уже заблокировавшему нас хосту за один прогон каждые
            # 2 минуты. Явный break — прогон останавливается сразу, одна
            # понятная запись в error_samples вместо ~60 задублированных.
            logger.error(f"🚫 KFF API circuit breaker: {e}")
            stats["errors"] += 1
            error_samples.append({
                "match_id": str(match.id),
                "match": f"{match.home_team.name} — {match.away_team.name}",
                "error_type": "KFFAPICircuitBreakerOpen",
                "error": str(e)[:300],
                "at": timezone.now().isoformat(),
            })
            break
        except Exception as e:
            logger.error(f"❌ Error syncing match {match.id}: {type(e).__name__}: {e}", exc_info=True)
            stats["errors"] += 1
            if len(error_samples) < MAX_ERROR_SAMPLES:
                error_samples.append({
                    "match_id": str(match.id),
                    "match": f"{match.home_team.name} — {match.away_team.name}",
                    "error_type": type(e).__name__,
                    "error": str(e)[:300],
                    "at": timezone.now().isoformat(),
                })
            continue
        finally:
            # Лок ОБЯЗАТЕЛЬНО снимается в finally — иначе матч останется
            # "залоченным" до истечения TTL даже при успешном завершении цикла.
            _release_match_sync_lock(match.id)

    logger.info(f"🏁 Match sync completed: {stats}")

    # Персистим итог ОДНОЙ строкой на весь запуск (не на матч) — см.
    # parsers/models.py::ParserSyncRun. Обёрнуто в try — сбой записи метрики
    # НЕ должен ронять уже выполненную синхронизацию через retry задачи.
    try:
        ParserSyncRun.objects.create(
            task_name="update_match_statuses",
            started_at=started_at,
            total=stats["total"],
            updated=stats["updated"],
            unchanged=stats["unchanged"],
            errors=stats["errors"],
            new_events=stats["new_events"],
            status_changes=stats["status_changes"],
            skipped_locked=stats["skipped_locked"],
            error_samples=error_samples,
        )
    except Exception:
        logger.error("⚠️ Не удалось записать ParserSyncRun", exc_info=True)

    if stats["total"] and stats["errors"] > stats["total"] * 0.3:
        _send_sync_error_alert(
            f"Высокий процент ошибок при синхронизации матчей: {stats['errors']}/{stats['total']}",
            "match_sync_errors",
            extra_data=stats,
        )

    return stats


@shared_task
def health_check_kff_api():
    """Проверка доступности KFF API."""
    client = KFFClient()

    try:
        response = client._get("/seasons", params={"tournament": client.TARGET_TOURNAMENT}, retries=1)

        if response:
            logger.info("✅ KFF API is reachable")
            return {"status": "ok", "api": "reachable"}
        logger.warning("⚠️  KFF API returned empty response")
        return {"status": "warning", "api": "empty_response"}
    except Exception as e:
        error_msg = f"❌ KFF API health check failed: {e}"
        logger.error(error_msg)
        return {"status": "error", "error": str(e)}


@shared_task
def check_sync_errors_and_alert():
    """Проверка ошибок синхронизации за последние 24 часа и алерт при необходимости."""
    from matches.models import Match

    now = timezone.now()
    cutoff = now - timedelta(hours=24)

    matches_without_lineups = Match.objects.filter(
        status="finished", created_at__gte=cutoff, has_lineup=False
    ).count()

    from events.models import MatchEvent

    matches_without_events = (
        Match.objects.filter(status="finished", created_at__gte=cutoff)
        .exclude(
            id__in=MatchEvent.objects.filter(created_at__gte=cutoff).values_list(
                "match_id", flat=True
            )
        )
        .count()
    )

    threshold_lineups = 5
    threshold_events = 10

    alerts = []

    if matches_without_lineups > threshold_lineups:
        alerts.append(f"⚠️ {matches_without_lineups} матчей без составов за 24ч")

    if matches_without_events > threshold_events:
        alerts.append(f"⚠️ {matches_without_events} матчей без событий за 24ч")

    if alerts:
        error_msg = "Проблемы с синхронизацией:\n" + "\n".join(alerts)
        logger.warning(error_msg)
        _send_sync_error_alert(
            error_msg,
            "sync_monitoring",
            extra_data={
                "matches_without_lineups": matches_without_lineups,
                "matches_without_events": matches_without_events,
            },
        )
        return {"status": "alert_sent", "alerts": alerts}

    logger.info("✅ Sync monitoring: No critical issues detected")
    return {"status": "ok"}


def _send_sync_error_alert(error_message: str, alert_type: str, extra_data: dict = None):
    """Отправка email-алерта админу при критических ошибках."""
    if not getattr(settings, "ENABLE_SYNC_ERROR_ALERTS", True):
        return

    admin_email = getattr(settings, "ADMIN_ALERT_EMAIL", settings.CONTACT_EMAIL)
    site_url = getattr(settings, "SITE_URL", "https://dopx.kz")

    subject = f"DOPX Sync Alert [{alert_type}]"

    html_message = render_to_string(
        "emails/sync_error_alert.html",
        {
            "error_message": error_message,
            "alert_type": alert_type,
            "extra_data": extra_data,
            "timestamp": timezone.now(),
            "site_url": site_url,
        },
    )

    try:
        send_mail(
            subject=subject,
            message=strip_tags(html_message),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[admin_email],
            html_message=html_message,
            fail_silently=True,
        )
        logger.info(f"✅ Sync error alert sent to {admin_email}")
    except Exception as e:
        logger.error(f"❌ Failed to send sync error alert: {e}")


@shared_task(bind=True, max_retries=2)
def sync_kff_player_meta(self):
    """
    Периодическая синхронизация МЕТАДАННЫХ игроков с kffleague.kz — ID
    (Player.kff_website_id) и бэкафилл пустой позиции (см.
    parsers/kff/photo_scraper.py, та же логика, что у management-команды
    sync_kff_player_meta --apply).

    НЕ СКАЧИВАЕТ ФОТО (переименована из sync_kff_photos 2026-08-21) — от
    автоматического импорта фото с KFF отказались: нестабильный источник,
    кривой авто-кроп/компоновка лица в круге на живых данных. Вместо фото
    везде, где его нет, показывается генеративный аватар (градиент +
    инициалы, см. core/templatetags/avatar_extras.py) — так что фото-пайплайн
    (season_squad/photo_processing.py) удалён вместе с этой веткой задачи.

    ЗАЧЕМ ПЕРИОДИЧЕСКИ, А НЕ ОДИН РАЗ: база DOPX пополняется — трансферы,
    новые заявленные игроки академий и т.п., — а разовый прогон скрапера
    видит только тот срез состава, что был на момент запуска. Раз в
    несколько дней достаточно: составы команд в межсезонье и по ходу
    сезона меняются не ежедневно, а чаще, чем recompute-live-best-xi
    (каждые 15 мин), гонять скрапинг чужого сайта незачем.

    ОЖИДАЕМО НЕПОЛНОЕ ПОКРЫТИЕ — не считается ошибкой: у KFF на публичном
    сайте не у каждого игрока DOPX есть карточка (воспитанники академий,
    сыгравшие 1-2 матча, могут не попасть в публичный состав команды на
    сайте вообще). Задача просто не находит пару и репортит это в
    результатах — не исключение, не повод для алерта.

    НОВОЕ (2026-08-31, "ушедшие игроки"): та же функция теперь ещё и
    автоматически снимает is_active игрокам, которых N прогонов подряд не
    находит в свежем составе команды на kffleague.kz (см.
    parsers/kff/photo_scraper.py::ROSTER_ABSENCE_THRESHOLD и докстринг
    match_and_fetch_players_for_team) — решает жалобу "на странице команды
    висят игроки, которые уже ушли" (Дастан Сатпаев, Хуан Себастьян
    Зебальос и т.п. — не отражались как ушедшие, потому что Player.team
    раньше обновлялся только реактивно, при появлении в протоколе матча за
    новую команду). Если в этом прогоне кого-то реально деактивировали —
    шлём staff email-уведомление тем же каналом, что и другие sync-алерты,
    чтобы был шанс вручную отменить, если это ложное срабатывание (см.
    players/admin.py — действие "Вернуть в активный состав").
    """
    from teams.models import Team

    from parsers.kff.photo_scraper import match_and_fetch_players_for_team, match_teams

    logger.info("🔄 Starting periodic KFF player meta sync (ID + позиция, без фото)...")

    try:
        team_report = match_teams(dry_run=False)
    except Exception as e:
        logger.error(f"❌ sync_kff_player_meta: match_teams failed: {type(e).__name__}: {e}", exc_info=True)
        raise self.retry(exc=e, countdown=300)

    totals = {
        "teams": 0, "matched": 0, "fuzzy_matched": 0, "positions_backfilled": 0,
    }
    all_deactivated: list[tuple[str, str]] = []  # (команда, игрок)
    all_absence_warnings: list[tuple[str, str, int]] = []  # (команда, игрок, счётчик)

    teams_qs = Team.objects.filter(is_active=True, kff_website_id__isnull=False)
    for team in teams_qs:
        try:
            report = match_and_fetch_players_for_team(team, dry_run=False)
        except Exception as e:
            logger.error(f"❌ sync_kff_player_meta: команда {team.name}: {type(e).__name__}: {e}", exc_info=True)
            continue
        if "error" in report:
            logger.warning(f"⚠️ sync_kff_player_meta: {report['error']}")
            continue
        totals["teams"] += 1
        totals["matched"] += len(report["matched"])
        totals["fuzzy_matched"] += len(report["fuzzy_matched"])
        totals["positions_backfilled"] += len(report["positions_backfilled"])
        for name in report.get("roster_deactivated", []):
            all_deactivated.append((team.name, name))
        for name, streak in report.get("roster_absence_warnings", []):
            all_absence_warnings.append((team.name, name, streak))

    if all_deactivated:
        logger.warning(
            "🚪 sync_kff_player_meta: автоматически деактивированы (не найдены в составе KFF %d+ прогонов подряд): %s",
            2, ", ".join(f"{p} ({t})" for t, p in all_deactivated),
        )
        _send_sync_error_alert(
            "Автоматически сняты с активного состава (не найдены в актуальном составе на kffleague.kz "
            "несколько прогонов подряд):\n" + "\n".join(f"— {p} ({t})" for t, p in all_deactivated) +
            "\n\nЕсли это ложное срабатывание — верните вручную в админке (Игроки → выбрать → действие "
            "«Вернуть в активный состав»).",
            "roster_departures",
            extra_data={"deactivated": all_deactivated, "absence_warnings": all_absence_warnings},
        )

    logger.info(f"✅ KFF player meta sync completed: teams_unmatched={len(team_report['unmatched_kff'])} {totals}")
    return {
        **totals,
        "teams_unmatched_kff": team_report["unmatched_kff"],
        "roster_deactivated": all_deactivated,
        "roster_absence_warnings": all_absence_warnings,
    }


@shared_task
def sync_all_enabled_tournaments():
    """Синхронизация всех включённых турниров из настроек."""
    enabled_tournaments = getattr(settings, "PARSER_SETTINGS", {}).get(
        "ENABLED_TOURNAMENTS", ["pl"]
    )

    results = {}

    for tournament_code in enabled_tournaments:
        logger.info(f"🔄 Starting sync for tournament: {tournament_code}")

        try:
            if tournament_code == "pl":
                result = sync_kff_premier_league.delay()
            else:
                result = sync_full_season.delay(tournament_code=tournament_code)

            results[tournament_code] = {"status": "queued", "task_id": result.id}
        except Exception as e:
            logger.error(f"❌ Failed to queue sync for {tournament_code}: {e}")
            results[tournament_code] = {"status": "error", "error": str(e)}

    return results