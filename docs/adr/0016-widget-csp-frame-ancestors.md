# 0016 — Embed-виджеты: CSP frame-ancestors перекрывал X-Frame-Options exempt

Дата: 2026-08-21 (расширено 2026-08-22, 2026-09-04)
Статус: Accepted

## Контекст

Embed-виджеты для партнёров (`players/views.py::player_rating_widget`,
`teams/views.py::team_rating_widget`, `core/views.py::standings_widget`,
`season_squad/views.py::best_xi_widget`, `round_squad/views.py::round_widget`)
снимают `X-Frame-Options` через `@xframe_options_exempt`, но
`ContentSecurityPolicyMiddleware` всё равно шлёт `frame-ancestors 'self'`
на все страницы без исключения — а `frame-ancestors` у современных
браузеров главнее устаревшего `X-Frame-Options`. В итоге партнёр вставляет
`<iframe>` на свой сайт (embed-код есть, ссылка работает), а браузер
молча рисует пустой прямоугольник — виджет физически не может показаться
нигде, кроме `dopx.kz`.

## Решение

Отдельная CSP-политика (`WIDGET_POLICY_BASE`/`_widget_policy()`) для путей,
подпадающих под `WIDGET_PATH_PATTERN` — точечный regex на конкретные
embed-роуты, а не общий `startswith('/widget')`/`'/best-xi'`, чтобы
случайно не ослабить `frame-ancestors` на будущей странице, у которой в
пути просто встретится похожее слово. Директива `frame-ancestors` для
этих путей строится из `settings.WIDGET_ALLOWED_ORIGINS` (см.
ADR о widget domain allow-list) — пусто → `*`, заполнено → точный список
доменов. Остальные CSP-директивы для виджетов не ослабляются — виджет
всё равно не должен грузить чужие скрипты.

`WIDGET_PATH_PATTERN` пополнялся дважды по мере добавления новых
embeddable-роутов (`best-xi/widget`, `round/widget`) — каждый раз тем же
способом: явные альтернативы в regex, а не расширение общего префикса.

## Последствия

- Добавление нового embeddable-роута требует явного расширения
  `WIDGET_PATH_PATTERN` — этот шаг дважды забывали при первом релизе
  соответствующей view, стоит проверять в код-ревью новых `@xframe_options_exempt`.
