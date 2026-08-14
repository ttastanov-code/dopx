# users/forms.py
"""
АНТИ-ФРОД ФИКС (`UserRegistrationForm`): регистрация была полностью открыта
для автоматического заполнения — ни honeypot-поля, ни минимального времени
заполнения формы. Добавлены два дешёвых, не требующих внешних сервисов
барьера:

1. **Honeypot-поле** `website` — скрытое CSS (`display:none` + вне потока
   табуляции), которое реальный пользователь никогда не увидит и не
   заполнит, а простой бот, слепо заполняющий все поля формы, скорее всего
   заполнит. Если оно непустое — форма считается невалидной без явного
   объяснения причины (ботоводу не подсказываем, что его вычислили).
2. **Time-trap** `form_rendered_at` — скрытое поле с серверным timestamp'ом
   момента рендера формы. Если форма отправлена быстрее `MIN_FORM_FILL_
   SECONDS` секунд после рендера — это физически невозможно для человека,
   читающего форму (регистрация требует минимум прочитать 5 полей и
   придумать пароль), верный признак скрипта, который сразу шлёт POST.

Оба фильтра — мягкие первые барьеры против примитивного бот-фарминга
аккаунтов, а не замена полноценной CAPTCHA/Turnstile (см. рекомендацию в
продуктовом аудите — CAPTCHA стоит подключать отдельно, когда будут ключи
провайдера). Ничего не меняют для обычных пользователей.
"""
from __future__ import annotations

import time

from django import forms
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordChangeForm,
    PasswordResetForm,
    UserCreationForm,
)

from users.models import User

MIN_FORM_FILL_SECONDS = 3


class UserRegistrationForm(UserCreationForm):
    """Форма регистрации пользователя."""

    email = forms.EmailField(
        required=True,
        label="Email",
        widget=forms.EmailInput(
            attrs={
                "class": "input-dopx w-full",
                "placeholder": "email@example.com",
                "autocomplete": "email",
            }
        ),
    )
    city = forms.CharField(
        max_length=120,
        required=False,
        label="Город",
        widget=forms.TextInput(
            attrs={
                "class": "input-dopx w-full",
                "placeholder": "Алматы",
                "autocomplete": "address-level2",
            }
        ),
    )

    # --- Анти-бот поля (не показываются в списке fields ниже намеренно,
    # рендерятся отдельно в шаблоне вручную, см. комментарий в docstring) ---
    website = forms.CharField(
        required=False,
        label="",
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
                "tabindex": "-1",
                "class": "hp-field",  # в CSS: .hp-field { position:absolute; left:-9999px; }
                "aria-hidden": "true",
            }
        ),
    )
    form_rendered_at = forms.FloatField(widget=forms.HiddenInput(), required=False)

    class Meta:
        model = User
        fields = ["username", "email", "city", "password1", "password2"]
        labels = {
            "username": "Имя пользователя",
            "password1": "Пароль",
            "password2": "Подтверждение пароля",
        }
        widgets = {
            "username": forms.TextInput(
                attrs={
                    "class": "input-dopx w-full",
                    "placeholder": "username",
                    "autocomplete": "username",
                }
            ),
            "password1": forms.PasswordInput(
                attrs={
                    "class": "input-dopx w-full",
                    "placeholder": "••••••••",
                    "autocomplete": "new-password",
                }
            ),
            "password2": forms.PasswordInput(
                attrs={
                    "class": "input-dopx w-full",
                    "placeholder": "••••••••",
                    "autocomplete": "new-password",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Текущий момент "прошивается" в скрытое поле при каждом рендере
        # формы (GET на страницу регистрации) — точка отсчёта для time-trap.
        self.fields["form_rendered_at"].initial = time.time()

    def clean_email(self):
        email = self.cleaned_data.get("email")
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("Этот email уже зарегистрирован")
        return email

    def clean_website(self):
        """Honeypot: поле обязано остаться пустым."""
        value = self.cleaned_data.get("website")
        if value:
            # Намеренно генетическая формулировка ошибки — не даём боту
            # понять, что именно его выдало.
            raise forms.ValidationError("Не удалось обработать форму. Попробуйте ещё раз.")
        return value

    def clean_form_rendered_at(self):
        """Time-trap: форма не может быть отправлена мгновенно после рендера."""
        rendered_at = self.cleaned_data.get("form_rendered_at")
        if rendered_at:
            elapsed = time.time() - rendered_at
            if 0 <= elapsed < MIN_FORM_FILL_SECONDS:
                raise forms.ValidationError("Не удалось обработать форму. Попробуйте ещё раз.")
        return rendered_at


class UserLoginForm(AuthenticationForm):
    """Форма входа пользователя."""

    username = forms.CharField(
        label="Имя пользователя или Email",
        widget=forms.TextInput(
            attrs={
                "class": "input-dopx w-full",
                "placeholder": "username или email",
                "autocomplete": "username",
            }
        ),
    )
    password = forms.CharField(
        label="Пароль",
        widget=forms.PasswordInput(
            attrs={
                "class": "input-dopx w-full",
                "placeholder": "••••••••",
                "autocomplete": "current-password",
            }
        ),
    )


class UserProfileForm(forms.ModelForm):
    """Форма редактирования профиля."""

    delete_avatar = forms.BooleanField(
        required=False,
        label="Удалить текущую аватарку",
        help_text="Отметьте, чтобы удалить текущую аватарку",
    )

    class Meta:
        model = User
        fields = ["email", "city", "bio", "avatar", "delete_avatar"]
        labels = {
            "email": "Email",
            "city": "Город",
            "bio": "О себе",
            "avatar": "Новая аватарка",
        }
        widgets = {
            "email": forms.EmailInput(attrs={"class": "input input-bordered w-full"}),
            "city": forms.TextInput(
                attrs={"class": "input input-bordered w-full", "placeholder": "Алматы"}
            ),
            "bio": forms.Textarea(
                attrs={
                    "class": "textarea textarea-bordered w-full",
                    "rows": 4,
                    "placeholder": "Расскажите о себе...",
                }
            ),
            "avatar": forms.FileInput(attrs={"class": "file-input file-input-bordered w-full"}),
            "delete_avatar": forms.CheckboxInput(attrs={"class": "checkbox checkbox-primary"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.avatar:
            self.fields["delete_avatar"].widget = forms.HiddenInput()


class CustomPasswordChangeForm(PasswordChangeForm):
    """Форма изменения пароля."""

    old_password = forms.CharField(
        label="Текущий пароль",
        widget=forms.PasswordInput(
            attrs={
                "class": "input input-bordered w-full",
                "placeholder": "••••••••",
                "autocomplete": "current-password",
            }
        ),
    )
    new_password1 = forms.CharField(
        label="Новый пароль",
        widget=forms.PasswordInput(
            attrs={
                "class": "input input-bordered w-full",
                "placeholder": "••••••••",
                "autocomplete": "new-password",
            }
        ),
    )
    new_password2 = forms.CharField(
        label="Подтверждение нового пароля",
        widget=forms.PasswordInput(
            attrs={
                "class": "input input-bordered w-full",
                "placeholder": "••••••••",
                "autocomplete": "new-password",
            }
        ),
    )


class CustomPasswordResetForm(PasswordResetForm):
    """Форма сброса пароля."""

    email = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(
            attrs={"class": "input input-bordered w-full", "placeholder": "email@example.com"}
        ),
    )


class NotificationSettingsForm(forms.Form):
    """Форма настроек уведомлений (без welcome — он всегда включён)."""

    email_match_finished = forms.BooleanField(
        required=False,
        label="Матч завершён / Открытие голосования",
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "toggle toggle-primary"}),
    )
    email_voting_closing = forms.BooleanField(
        required=False,
        label="Напоминание о закрытии голосования",
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "toggle toggle-primary"}),
    )
    email_new_badge = forms.BooleanField(
        required=False,
        label="Получение достижений",
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "toggle toggle-primary"}),
    )
    email_level_up = forms.BooleanField(
        required=False,
        label="Повышение уровня",
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "toggle toggle-primary"}),
    )
    email_system = forms.BooleanField(
        required=False,
        label="Системные новости платформы",
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "toggle toggle-primary"}),
    )
    # НОВОЕ: дайджест вместо мгновенных писем на каждое мелкое событие
    # (достижения/уровень/trust score) — см. notifications_tasks.py.
    email_digest_mode = forms.BooleanField(
        required=False,
        label="Собирать уведомления в дайджест вместо письма на каждое событие",
        initial=True,
        help_text="Рекомендуется — меньше писем, никакой потери информации.",
        widget=forms.CheckboxInput(attrs={"class": "toggle toggle-primary"}),
    )

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        if user and hasattr(user, "notification_settings"):
            settings = user.notification_settings
            for field_name in self.fields:
                self.fields[field_name].initial = settings.get(field_name, True)