# scripts/regen_backup_codes.py
#
# Пересоздаёт 8 backup-кодов 2FA и печатает их (показ только сейчас).
# Запуск: python manage.py shell < scripts/regen_backup_codes.py (поменяйте USERNAME).

USERNAME = "admin"  # ваш логин

from django.contrib.auth import get_user_model
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken

User = get_user_model()
u = User.objects.get(username=USERNAME)

StaticDevice.objects.filter(user=u).delete()
sd = StaticDevice.objects.create(user=u, name="backup", confirmed=True)

print(f"Новые backup-коды для {u.username}:")
for _ in range(8):
    t = StaticToken.random_token()
    StaticToken.objects.create(device=sd, token=t)
    print(f"  {t}")
