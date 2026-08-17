# scripts/simulate_staff_idle_timeout.py
#
# Чтобы не ждать реальные "несколько часов" простоя — искусственно
# состариваем метку активности (_staff_last_activity) в ВАШЕЙ текущей
# сессии, чтобы на следующий заход на /staff/dashboard/ или /admin/
# сработала ветка idle-таймаута в StaffSessionSecurityMiddleware. Ничего не
# ломает: просто выставляет одно значение в сессии на "давно".
#
# Запуск: python manage.py shell < scripts/simulate_staff_idle_timeout.py
# Затем в БРАУЗЕРЕ (в котором вы залогинены) откройте /staff/dashboard/ —
# должно сработать: logout -> редирект на /admin/login/?next=/staff/dashboard/&session_expired=1
# (next теперь ЕСТЬ — раньше его тут не было, в этом был баг).
# Войдите заново — должны попасть на страницу ввода 2FA-кода с next,
# а после кода — прямо на /staff/dashboard/, а не на 404.

from datetime import timedelta

from django.contrib.sessions.models import Session
from django.utils import timezone

old_timestamp = (timezone.now() - timedelta(hours=3)).isoformat()

updated = 0
for s in Session.objects.filter(expire_date__gte=timezone.now()):
    data = s.get_decoded()
    if "_staff_last_activity" in data:
        data["_staff_last_activity"] = old_timestamp
        s.session_data = Session.objects.encode(data)
        s.save(update_fields=["session_data"])
        updated += 1

print(f"Состарено сессий: {updated}")
print("Теперь откройте /staff/dashboard/ в браузере, где вы залогинены, и проверьте редирект.")
