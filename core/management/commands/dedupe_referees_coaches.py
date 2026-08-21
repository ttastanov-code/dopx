# core/management/commands/dedupe_referees_coaches.py
"""
manage.py dedupe_referees_coaches [--apply] [--fuzzy] [--merge ID1 ID2]

Судьи и тренеры без стабильного external_id (KFF шлёт судью свободной
строкой всегда; тренера — не всегда) матчились по first_name/last_name
через __iexact, который не считал казахские буквы-омографы ("Сакен"/
"Сәкен" — разные Unicode-символы) одинаковыми. Каждое новое написание
плодило новую запись. parsers/kff/importers.py уже переведён на
normalize_kz (core/utils.py) — это чинит НОВЫЕ импорты.

Три режима, три разных уровня доверия:

1. Без флагов — только отчёт по ТОЧНЫМ дублям (normalize_kz совпадает
   полностью — казахские омографы, регистр). Ничего не меняет.
2. --apply — реально объединяет ТОЧНЫЕ дубли из режима 1.
3. --fuzzy — отдельно, по ОПЕЧАТКАМ (не омографам): "Булат"/"Болат",
   "Ыскакаов"/"Ыскаков" — normalize_kz их не ловит, это разные буквы, а
   не варианты одной. Похожие по написанию пары ищутся через
   difflib.SequenceMatcher (порог 0.82) и только ПЕЧАТАЮТСЯ для ручной
   проверки — НИКОГДА не объединяются автоматически, даже под --apply.
   Причина: опечатка неотличима от двух РАЗНЫХ людей с похожими
   фамилиями чисто по строке, авто-слияние тут рискует смешать разных
   реальных людей. Решение остаётся за человеком.
4. --merge ID1 ID2 — после того как поверили --fuzzy-пару глазами,
   объединить ИМЕННО эти два id (оба Referee либо оба Coach, определяется
   автоматически). Используется тот же перенос ссылок, что и в режиме 2.
"""
from __future__ import annotations

import difflib
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.utils import normalize_kz
from referees.models import Referee
from coaches.models import Coach
from matches.models import Match
from evaluations.models import CoachEvaluation
from aggregates.models import CoachMatchAggregate

FUZZY_THRESHOLD = 0.82


class Command(BaseCommand):
    help = "Находит/объединяет дубли судей и тренеров (омографы точно, опечатки — на ручное подтверждение)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Реально объединить ТОЧНЫЕ дубли (омографы). Не трогает --fuzzy кандидатов.",
        )
        parser.add_argument(
            "--fuzzy",
            action="store_true",
            help="Показать пары ПОХОЖИХ (но не идентичных после normalize_kz) имён — вероятные опечатки. Только отчёт.",
        )
        parser.add_argument(
            "--merge",
            nargs=2,
            metavar=("ID1", "ID2"),
            help="Вручную объединить два конкретных id (после проверки --fuzzy-пары глазами). "
                 "Оба должны быть одного типа — оба судьи либо оба тренера.",
        )

    def handle(self, *args, **options):
        if options["merge"]:
            self._manual_merge(options["merge"][0], options["merge"][1])
            return

        if options["fuzzy"]:
            self._fuzzy_report(Referee.objects.all(), "Судьи")
            self._fuzzy_report(Coach.objects.all(), "Тренеры")
            return

        apply_changes = options["apply"]
        mode = "ПРИМЕНИТЬ" if apply_changes else "ТОЛЬКО ОТЧЁТ (dry-run, --apply чтобы применить)"
        self.stdout.write(self.style.WARNING(f"Режим: {mode}"))

        self._dedupe_referees(apply_changes)
        self._dedupe_coaches(apply_changes)

    # ============================================================
    # Точные дубли (normalize_kz совпадает полностью)
    # ============================================================

    @staticmethod
    def _group_by_normalized_name(queryset):
        groups = defaultdict(list)
        for obj in queryset:
            key = normalize_kz(f"{obj.first_name} {obj.last_name}")
            if key:
                groups[key].append(obj)
        return {k: v for k, v in groups.items() if len(v) > 1}

    def _dedupe_referees(self, apply_changes: bool):
        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Судьи ==="))
        groups = self._group_by_normalized_name(Referee.objects.all())
        if not groups:
            self.stdout.write("Точных дублей не найдено.")
            return

        for dupes in groups.values():
            canonical, others = self._rank_referees(dupes)
            summary = ", ".join(
                f'"{r.full_name}" (id={r.id}, матчей={Match.objects.filter(referee=r).count()})'
                for r in dupes
            )
            self.stdout.write(f"  Группа: {summary}")
            self.stdout.write(f"    -> канонический: \"{canonical.full_name}\" (id={canonical.id})")
            if apply_changes:
                self._merge_referees(canonical, others)
                self.stdout.write(self.style.SUCCESS("    Объединено."))

    def _dedupe_coaches(self, apply_changes: bool):
        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Тренеры ==="))
        groups = self._group_by_normalized_name(Coach.objects.all())
        if not groups:
            self.stdout.write("Точных дублей не найдено.")
            return

        affected_match_ids: set = set()

        for dupes in groups.values():
            canonical, others = self._rank_coaches(dupes)
            summary = ", ".join(
                f'"{c.first_name} {c.last_name}" (id={c.id}, ext_id={c.external_id})'
                for c in dupes
            )
            self.stdout.write(f"  Группа: {summary}")
            self.stdout.write(f"    -> канонический: id={canonical.id} (ext_id={canonical.external_id})")
            if apply_changes:
                affected_match_ids |= self._merge_coaches(canonical, others)
                self.stdout.write(self.style.SUCCESS("    Объединено."))

        if apply_changes and affected_match_ids:
            self._recalculate_coach_aggregates(affected_match_ids)

    # ============================================================
    # Похожие, но не идентичные — только отчёт, руками через --merge
    # ============================================================

    def _fuzzy_report(self, queryset, label: str):
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== {label}: похожие написания (проверить руками) ==="))
        objs = [o for o in queryset if (o.first_name or o.last_name)]
        # Точные normalize_kz-совпадения уже покрыты обычным режимом — здесь
        # интересны только пары, которые НЕ совпадают после normalize_kz, но
        # почти совпадают по написанию (опечатка на 1-2 буквы).
        found = False
        for i in range(len(objs)):
            name_i = normalize_kz(f"{objs[i].first_name} {objs[i].last_name}")
            for j in range(i + 1, len(objs)):
                name_j = normalize_kz(f"{objs[j].first_name} {objs[j].last_name}")
                if name_i == name_j:
                    continue  # это точный дубль — им занимается основной режим
                ratio = difflib.SequenceMatcher(None, name_i, name_j).ratio()
                if ratio >= FUZZY_THRESHOLD:
                    found = True
                    self.stdout.write(
                        f'  {ratio:.2f}  "{objs[i].first_name} {objs[i].last_name}" (id={objs[i].id})'
                        f'  <->  "{objs[j].first_name} {objs[j].last_name}" (id={objs[j].id})'
                    )
                    self.stdout.write(
                        f"        Если это один человек: python manage.py dedupe_referees_coaches "
                        f"--merge {objs[i].id} {objs[j].id}"
                    )
        if not found:
            self.stdout.write("Похожих пар не найдено.")

    def _manual_merge(self, id1: str, id2: str):
        ref1 = Referee.objects.filter(pk=id1).first()
        ref2 = Referee.objects.filter(pk=id2).first()
        if ref1 and ref2:
            canonical, others = self._rank_referees([ref1, ref2])
            self.stdout.write(f'Объединяю "{ref1.full_name}" и "{ref2.full_name}" -> канонический id={canonical.id}')
            self._merge_referees(canonical, others)
            self.stdout.write(self.style.SUCCESS("Готово."))
            return

        coach1 = Coach.objects.filter(pk=id1).first()
        coach2 = Coach.objects.filter(pk=id2).first()
        if coach1 and coach2:
            canonical, others = self._rank_coaches([coach1, coach2])
            self.stdout.write(f"Объединяю тренеров id={coach1.id} и id={coach2.id} -> канонический id={canonical.id}")
            affected = self._merge_coaches(canonical, others)
            if affected:
                self._recalculate_coach_aggregates(affected)
            self.stdout.write(self.style.SUCCESS("Готово."))
            return

        raise CommandError(
            "Не нашёл пару id одного типа: либо оба должны быть судьями, либо оба тренерами "
            "(проверьте, что id скопированы верно)."
        )

    # ============================================================
    # Ранжирование группы (кто канонический) — общее для точного и
    # ручного (--merge) путей
    # ============================================================

    @staticmethod
    def _rank_referees(dupes):
        ranked = sorted(
            ((r, Match.objects.filter(referee=r).count()) for r in dupes),
            key=lambda t: (-t[1], t[0].created_at),
        )
        canonical = ranked[0][0]
        others = [r for r, _ in ranked[1:]]
        return canonical, others

    @staticmethod
    def _rank_coaches(dupes):
        def match_count(c):
            return Match.objects.filter(home_coach=c).count() + Match.objects.filter(away_coach=c).count()

        ranked = sorted(
            ((c, match_count(c)) for c in dupes),
            # запись с external_id приоритетнее (надёжный идентификатор),
            # затем — у кого больше сыгранных матчей, затем — старше
            key=lambda t: (t[0].external_id is None, -t[1], t[0].created_at),
        )
        canonical = ranked[0][0]
        others = [c for c, _ in ranked[1:]]
        return canonical, others

    # ============================================================
    # Собственно перенос ссылок + удаление дубля
    # ============================================================

    def _merge_referees(self, canonical, others):
        with transaction.atomic():
            for dup in others:
                moved = Match.objects.filter(referee=dup).update(referee=canonical)
                self.stdout.write(f"    id={dup.id}: перенесено матчей={moved}")
                dup.delete()

    def _merge_coaches(self, canonical, others) -> set:
        affected_match_ids: set = set()
        with transaction.atomic():
            for dup in others:
                dup_match_ids = set(
                    Match.objects.filter(home_coach=dup).values_list("id", flat=True)
                ) | set(
                    Match.objects.filter(away_coach=dup).values_list("id", flat=True)
                )
                affected_match_ids |= dup_match_ids

                home_moved = Match.objects.filter(home_coach=dup).update(home_coach=canonical)
                away_moved = Match.objects.filter(away_coach=dup).update(away_coach=canonical)

                # CoachEvaluation.coach: unique(user, match, coach) — если
                # пользователь уже оценил канонического тренера за этот же
                # матч, дубль-оценку просто убираем, а не переносим (иначе
                # save() упадёт на constraint).
                for ev in CoachEvaluation.objects.filter(coach=dup):
                    if CoachEvaluation.objects.filter(user=ev.user, match=ev.match, coach=canonical).exists():
                        ev.delete()
                    else:
                        ev.coach = canonical
                        ev.save(update_fields=["coach"])
                    affected_match_ids.add(ev.match_id)

                # Агрегаты дубля больше не актуальны — пересчитаем для
                # канонического ниже, из перенесённых CoachEvaluation.
                CoachMatchAggregate.objects.filter(coach=dup).delete()

                self.stdout.write(f"    id={dup.id}: перенесено матчей home={home_moved} away={away_moved}")
                dup.delete()
        return affected_match_ids

    def _recalculate_coach_aggregates(self, affected_match_ids: set):
        from aggregates.tasks import recalculate_coach_aggregates

        self.stdout.write(f"\nПересчитываю агрегаты тренеров для {len(affected_match_ids)} матчей...")
        for match_id in affected_match_ids:
            recalculate_coach_aggregates(str(match_id))
        self.stdout.write(self.style.SUCCESS("Готово."))
