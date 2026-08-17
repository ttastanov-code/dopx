# scripts/reset_2fa_throttle.py
#
# django-otp троттлит КАЖДОЕ устройство отдельно: после любой неудачной
# попытки verify_token() ставит throttling_failure_count += 1 и
# throttling_failure_timestamp = сейчас, а следующая проверка блокируется на
# throttle_factor * 2^(count-1) секунд С МОМЕНТА последней неудачи — даже
# если код в этот раз абсолютно верный. Именно это ловили как "код же верный,
# а всё равно не пускает": за время дебага накопилось много неверных попыток
# (наших тестовых + ваших), и у TOTP-устройства сейчас скорее всего
# многочасовая задержка.
#
# Скрипт печатает текущее состояние и снимает троттлинг (count=0). Коды при
# этом НЕ меняются — просто снова начинают проверяться сразу.
#
# Запуск: python manage.py shell < scripts/reset_2fa_throttle.py
# (перед запуском поменяйте USERNAME ниже на свой логин)

USERNAME = "admin"  # <-- замените, если логин другой

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
