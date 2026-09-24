# scripts/simulate_staff_idle_timeout.py
#
# Состаривает _staff_last_activity в вашей сессии, чтобы проверить idle-таймаут staff.
# Запуск: python manage.py shell < scripts/simulate_staff_idle_timeout.py,
# затем откройте /staff/dashboard/ — должен быть logout и редирект с next.

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
