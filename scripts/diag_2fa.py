# scripts/diag_2fa.py
#
# Диагностика текущего состояния 2FA-устройств для одного пользователя.
# Только чтение, ничего не меняет.
#
# Запуск: python manage.py shell < scripts/diag_2fa.py
# (перед запуском поменяйте USERNAME ниже на свой логин)

USERNAME = "admin"  # <-- замените

from django.contrib.auth import get_user_model
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice

User = get_user_model()
u = User.objects.get(username=USERNAME)

print(f"=== TOTP-устройства для {u.username} ===")
totp_qs = TOTPDevice.objects.filter(user=u)
print(f"Всего: {totp_qs.count()}")
for d in totp_qs:
    print(f"  id={d.id} name={d.name!r} confirmed={d.confirmed} key={d.key} last_t={d.last_t} tolerance={d.tolerance} sync={d.sync_dropped_drift if hasattr(d, 'sync_dropped_drift') else '—'}")

print(f"\n=== Static (backup) устройства для {u.username} ===")
static_qs = StaticDevice.objects.filter(user=u)
print(f"Всего: {static_qs.count()}")
for sd in static_qs:
    tokens = list(sd.token_set.values_list("token", flat=True))
    print(f"  id={sd.id} name={sd.name!r} confirmed={sd.confirmed} осталось_кодов={len(tokens)}")
    for t in tokens:
        print(f"    {t}")
