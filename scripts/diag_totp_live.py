# scripts/diag_totp_live.py
#
# Печатает текущий TOTP-код, который ждёт сервер, — сравнить с приложением.
# Запуск: python manage.py shell < scripts/diag_totp_live.py (поменяйте USERNAME).

USERNAME = "admin"  # ваш логин

import time
from django.contrib.auth import get_user_model
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice

User = get_user_model()
u = User.objects.get(username=USERNAME)
d = TOTPDevice.objects.get(user=u, confirmed=True)

now = int(time.time())
code = totp(d.bin_key, step=d.step, t0=d.t0, digits=d.digits, drift=0)

print(f"Серверное время (unix):        {now}")
print(f"Серверное время (читаемо):     {time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(now))}")
print(f"Код, который сервер ждёт СЕЙЧАС: {code:0{d.digits}d}")
print(f"step={d.step} digits={d.digits} tolerance={d.tolerance} last_t={d.last_t}")
print()
print("Сравните код выше с тем, что прямо сейчас показывает приложение-аутентификатор.")
