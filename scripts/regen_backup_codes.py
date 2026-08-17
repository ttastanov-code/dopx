# scripts/regen_backup_codes.py
#
# Удаляет старые backup-коды и создаёт чистую пачку из 8 новых.
# Печатает их в открытом виде — это единственный момент, когда это можно
# сделать (потом в БД остаётся только сам StaticToken, без "показать снова").
#
# Запуск: python manage.py shell < scripts/regen_backup_codes.py
# (перед запуском поменяйте USERNAME ниже на свой логин)

USERNAME = "admin"  # <-- замените, если логин другой

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
