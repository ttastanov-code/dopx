# 0030 — "Почему он в сборной?": обогащённое пояснение слота

Дата: 2026-09-07
Статус: Accepted

## Контекст

`docs/PRODUCT_SCOPE_MATCH_DNA_AND_EXPLAINABILITY.md` (раздел 1): `_build_explanation`
(season_squad/services.py) и `_build_round_explanation` (round_squad/services.py)
уже объясняли ЧИСЛО (season_score/round_score, размер выборки, изменение
места), но не то, ЗА ЧТО именно игрок получил высокие оценки — не было
разбивки по конкретным матчам/событиям.

Обе функции получают только уже агрегированный `Candidate`/`RoundCandidate`
(`raw_avg`, `matches`/`votes`) — исходные `PlayerMatchAggregate`, из которых
эти числа посчитаны, к этому моменту уже "потеряны" (агрегация происходит
через `.values().annotate()` на уровне БД, без сохранения списка строк).

## Решение

Добавлена вторая функция-обогатитель, отдельная от `_build_explanation`/
`_build_round_explanation` (не переписывает их — дополняет результат):

- **season_squad** — `_describe_top_matches(player_id, season)`: топ-2
  матча по `performance_score` из `PlayerMatchAggregate.objects.filter(
  player_id=..., match__season=season)`, с датой и соперником (соперник
  определяется через `MatchLineupPlayer` — факт участия в составе, а не
  `player.team`, та же причина, что в `_player_season_team_name`: переход
  в другой клуб не должен искажать историю прошлых матчей). Если в этих
  матчах есть заметное `MatchEvent` (`NOTABLE_EVENT_TYPES` — тот же список,
  что и в `evaluations/views.py::_compute_key_player_ids`, ADR-0006) —
  добавляется строкой "гол (78'), жёлтая карточка (12')".
- **round_squad** — `_describe_notable_events_in_round(player_id, season,
  tour)`: в туре игрок физически участвует ровно в одном матче, поэтому
  вместо "топ-2 матча" — просто заметные события этого единственного
  матча. Подключено и к обычным слотам формации, и к "игроку тура"
  (`player_of_round_explanation`).
- Обогащение применяется только к игрокам (`slot_code not in ("COACH",
  "REFEREE")`) — у тренера/судьи нет персональных `MatchEvent`.
- Показ — та же иконка-тултип (`components/_tooltip_icon.html`), что уже
  используется для `explanation` на карточках слотов
  (`templates/season_squad/_best_xi_content.html`,
  `templates/round_squad/_round_content.html`) — просто более длинный
  текст внутри неё, без изменения разметки карточек.

## Альтернативы и почему не они

- **Протащить список `PlayerMatchAggregate` через весь путь
  `_build_player_pool_by_code` → `Candidate` → `_apply_slot` →
  `_build_explanation`** — отклонено: раздуло бы `Candidate` (сейчас
  простой плоский dataclass для ранжирования) деталями, нужными только
  для UI-обогащения одного occupant'а на слот, а не для расчёта самого
  байесовского скора. Отдельный точечный запрос на occupant'а (11 игроков
  + "игрок тура" на пересчёт, не на каждого кандидата пула) дешевле, чем
  усложнение структуры данных ранжирования.
- **Показывать ВСЕ матчи, а не топ-2** — отклонено: тултип должен
  оставаться коротким; топ-2 — тот объём, который умещается в одно-два
  предложения, не превращая подсказку в отдельную страницу.

## Последствия

- Один дополнительный запрос на occupant'а слота (не на весь пул
  кандидатов) при каждом пересчёте `recompute_best_xi`/`recompute_round` —
  таких пересчётов немного (Celery Beat периодически + ручное "пересчитать
  сейчас"), не hot path страницы.
- Если понадобится показать обогащение и для тренера/судьи в будущем —
  нужен отдельный источник "заметности" (у них нет `MatchEvent.player`),
  это НЕ тривиальное снятие условия `slot_code not in (...)`.

## Addendum (2026-09-07, тот же день) — v2 добавил конкурента и rank_change тура

`docs/adr/0032-squad-explainability-v2.md` добавил "сравнение с ближайшим
конкурентом" (season_squad и round_squad) и rank_change для тура (которого
не было вообще — только для сезона). Обогащение этого документа
(`_describe_top_matches`/`_describe_notable_events_in_round`) не изменилось
— новые строки добавляются в `_build_explanation`/`_build_round_explanation`
отдельно, тем же паттерном "дополняем, не переписываем".
