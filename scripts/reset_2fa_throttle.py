# scripts/reset_2fa_throttle.py
#
# Сбрасывает троттлинг django-otp у устройств пользователя (коды не меняются).
# Запуск: python manage.py shell < scripts/reset_2fa_throttle.py (поменяйте USERNAME).

USERNAME = "admin"  # ваш логин

from django.contrib.auth import get_user_model
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice

User = get_user_model()
u = User.objects.get(username=USERNAME)

for d in list(TOTPDevice.objects.filter(user=u)) + list(StaticDevice.objects.filter(user=u)):
    print(f"{d.__class__.__name__} id={d.id}: failures={d.throttling_failure_count} last_failure={d.throttling_failure_timestamp}")
    if d.throttling_failure_count:
        d.throttle_reset()
        print("  -> сброшено")

print("Готово. Пробуйте войти снова — код из приложения и любой backup-код должны сработать сразу.")
