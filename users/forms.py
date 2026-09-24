# users/forms.py
"""Формы пользователей. Регистрация защищена тремя барьерами:
1. honeypot-поле website (скрыто, должно остаться пустым);
2. time-trap form_rendered_at (быстрее MIN_FORM_FILL_SECONDS — бот);
3. self-hosted капча (django-simple-captcha).
"""
from __future__ import annotations

import time

from captcha.fields import CaptchaField, CaptchaTextInput
from django import forms
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordChangeForm,
    PasswordResetForm,
    UserCreationForm,
)
from django.core.files.uploadedfile import UploadedFile
from PIL import Image, UnidentifiedImageError

from users.kz_cities import KZ_CITY_CHOICES
from users.models import User

MIN_FORM_FILL_SECONDS = 3

# Аватарки: лимит размера + проверка содержимого.
MAX_AVATAR_SIZE_BYTES = 5 * 1024 * 1024  # 5 МБ
ALLOWED_AVATAR_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}


class UserRegistrationForm(UserCreationForm):
    """Форма регистрации."""

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
    # Город — обязательный выбор из справочника (users/kz_cities.py).
    city = forms.ChoiceField(
        # Пустой первый пункт — чтобы выбор был осознанным.
        choices=[("", "— Выберите город —")] + KZ_CITY_CHOICES,
        required=True,
        label="Город",
        widget=forms.Select(attrs={"class": "input-dopx w-full"}),
    )

    # --- Антибот-поля (рендерятся в шаблоне вручную) ---
    website = forms.CharField(
        required=False,
        label="",
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
                "tabindex": "-1",
                "class": "hp-field",  # CSS: .hp-field { position:absolute; left:-9999px; }
                "aria-hidden": "true",
            }
        ),
    )
    form_rendered_at = forms.FloatField(widget=forms.HiddenInput(), required=False)

    # Капча — тоже рендерится в шаблоне отдельно.
    captcha = CaptchaField(
        label="Введите текст с картинки",
        error_messages={"invalid": "Неверный текст с картинки. Попробуйте ещё раз."},
        widget=CaptchaTextInput(
            attrs={
                "class": "input input-bordered w-full",
                "placeholder": "Текст с картинки",
                "autocomplete": "off",
            }
        ),
    )

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
        # Время рендера формы — точка отсчёта для time-trap.
        self.fields["form_rendered_at"].initial = time.time()

    def clean_email(self):
        email = self.cleaned_data.get("email")
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("Этот email уже зарегистрирован")
        return email

    def clean_website(self):
        """Honeypot должен остаться пустым."""
        value = self.cleaned_data.get("website")
        if value:
            # Общая формулировка — не подсказываем боту.
            raise forms.ValidationError("Не удалось обработать форму. Попробуйте ещё раз.")
        return value

    def clean_form_rendered_at(self):
        """Time-trap: слишком быстрая отправка."""
        rendered_at = self.cleaned_data.get("form_rendered_at")
        if rendered_at:
            elapsed = time.time() - rendered_at
            if 0 <= elapsed < MIN_FORM_FILL_SECONDS:
                raise forms.ValidationError("Не удалось обработать форму. Попробуйте ещё раз.")
        return rendered_at


class UserLoginForm(AuthenticationForm):
    """Форма входа: username или email. Email резолвим в username;
    не найден — отдаём как есть (не раскрываем существование адресов).
    """

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

    def clean_username(self):
        identifier = (self.cleaned_data.get("username") or "").strip()
        if "@" in identifier:
            try:
                return User.objects.get(email__iexact=identifier).username
            except User.DoesNotExist:
                pass
        return identifier


class UserProfileForm(forms.ModelForm):
    """Форма редактирования профиля."""

    delete_avatar = forms.BooleanField(
        required=False,
        label="Удалить текущую аватарку",
        help_text="Отметьте, чтобы удалить текущую аватарку",
    )

    class Meta:
        model = User
        fields = ["email", "city", "bio", "avatar", "delete_avatar", "is_profile_public"]
        labels = {
            "email": "Email",
            "city": "Город",
            "bio": "О себе",
            "avatar": "Новая аватарка",
            "is_profile_public": "Публичный профиль",
        }
        help_texts = {
            "is_profile_public": "Если выключить — ваш профиль по ссылке /u/<username>/ смогут видеть только вы сами (лидерборд и агрегаты работают как раньше).",
        }
        widgets = {
            "email": forms.EmailInput(attrs={"class": "input input-bordered w-full"}),
            # Город — из того же справочника.
            "city": forms.Select(attrs={"class": "select select-bordered w-full"}),
            "bio": forms.Textarea(
                attrs={
                    "class": "textarea textarea-bordered w-full",
                    "rows": 4,
                    "placeholder": "Расскажите о себе...",
                }
            ),
            "avatar": forms.FileInput(attrs={"class": "file-input file-input-bordered w-full"}),
            "delete_avatar": forms.CheckboxInput(attrs={"class": "checkbox checkbox-primary"}),
            "is_profile_public": forms.CheckboxInput(attrs={"class": "checkbox checkbox-primary"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.avatar:
            self.fields["delete_avatar"].widget = forms.HiddenInput()

    def clean_avatar(self):
        """Проверка аватара при новой загрузке: лимит размера, затем Image.verify()."""
        avatar = self.cleaned_data.get("avatar")
        if not avatar or not isinstance(avatar, UploadedFile):
            return avatar

        if avatar.size > MAX_AVATAR_SIZE_BYTES:
            raise forms.ValidationError(
                f"Файл слишком большой ({avatar.size / 1024 / 1024:.1f} МБ). "
                f"Максимум — {MAX_AVATAR_SIZE_BYTES // 1024 // 1024} МБ."
            )

        try:
            avatar.seek(0)
            image = Image.open(avatar)
            image.verify()
        except (UnidentifiedImageError, OSError, ValueError):
            raise forms.ValidationError("Файл повреждён или не является изображением.")

        if image.format not in ALLOWED_AVATAR_FORMATS:
            raise forms.ValidationError(
                f"Неподдерживаемый формат изображения: {image.format}. "
                f"Разрешены: JPEG, PNG, WEBP, GIF."
            )

        # verify() двигает указатель — возвращаем в начало.
        avatar.seek(0)
        return avatar


class CustomPasswordChangeForm(PasswordChangeForm):
    """Форма смены пароля."""

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
    """Настройки уведомлений (welcome всегда включён)."""

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
    # Дайджест вместо мгновенных писем (достижения/уровень/trust score).
    email_digest_mode = forms.BooleanField(
        required=False,
        label="Собирать уведомления в дайджест вместо письма на каждое событие",
        initial=True,
        help_text="Рекомендуется — меньше писем, никакой потери информации.",
        widget=forms.CheckboxInput(attrs={"class": "toggle toggle-primary"}),
    )
    # Retention-уведомления.
    email_prediction_closing = forms.BooleanField(
        required=False,
        label="Напоминание о закрытии приёма прогнозов",
        initial=True,
        help_text="Если вы ещё не поставили прогноз, а матч скоро начнётся.",
        widget=forms.CheckboxInput(attrs={"class": "toggle toggle-primary"}),
    )
    email_weekly_summary = forms.BooleanField(
        required=False,
        label="Персональная сводка недели",
        initial=True,
        help_text="Сколько матчей оценили, точность ваших прогнозов, топ-матч недели.",
        widget=forms.CheckboxInput(attrs={"class": "toggle toggle-primary"}),
    )
    email_prediction_result = forms.BooleanField(
        required=False,
        label="Ваш прогноз vs результат матча",
        initial=True,
        help_text="После финального свистка — совпал ли ваш прогноз и как проголосовало сообщество.",
        widget=forms.CheckboxInput(attrs={"class": "toggle toggle-primary"}),
    )
    # См. User.DEFAULT_NOTIFICATION_SETTINGS.
    email_round_results = forms.BooleanField(
        required=False,
        label="Итоги «DOPX Лучшие тура»",
        initial=True,
        help_text="Игрок тура, сборная тура и самый драматичный матч — когда голосование по туру закрывается.",
        widget=forms.CheckboxInput(attrs={"class": "toggle toggle-primary"}),
    )

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        if user and hasattr(user, "notification_settings"):
            settings = user.notification_settings
            for field_name in self.fields:
                self.fields[field_name].initial = settings.get(field_name, True)