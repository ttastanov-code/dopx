# users/forms.py
"""
UserRegistrationForm — три антибот-барьера, ни один не требует внешнего сервиса:

1. Honeypot-поле website — скрыто CSS, вне табуляции; реальный пользователь
   его не заполнит, простой бот, слепо заполняющий все поля, заполнит.
   Непустое значение — форма невалидна без объяснения причины.
2. Time-trap form_rendered_at — серверный timestamp рендера формы; отправка
   быстрее MIN_FORM_FILL_SECONDS физически невозможна для человека.
3. CAPTCHA (django-simple-captcha, self-hosted, без стороннего провайдера) —
   основной барьер; honeypot/time-trap выше — доп. фильтр для ботов, не
   доходящих даже до решения капчи.
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

from users.models import User

MIN_FORM_FILL_SECONDS = 3

# Аватарки: лимит размера + проверка реального содержимого файла.
MAX_AVATAR_SIZE_BYTES = 5 * 1024 * 1024  # 5 МБ
ALLOWED_AVATAR_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}


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

    # НОВОЕ: self-hosted капча (см. пункт 3 докстринга модуля). Рендерится
    # отдельно в шаблоне (`{{ form.captcha }}`), как и остальные анти-бот
    # поля выше, а не через Meta.fields — так уже устроены website/
    # form_rendered_at, оставляем единый паттерн.
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
    """
    Форма входа. Лейбл поля обещает "Имя пользователя или Email" —
    clean_username() сначала пытается резолвить введённое значение как
    email в username, иначе ModelBackend (ищет строго по username) отклонит
    любой ввод почты. Если email не найден, значение уходит как есть —
    намеренно не различаем "нет такого email" и "нет такого username" в
    ответе, чтобы не палить существующие адреса.
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
            "is_profile_public": forms.CheckboxInput(attrs={"class": "checkbox checkbox-primary"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.avatar:
            self.fields["delete_avatar"].widget = forms.HiddenInput()

    def clean_avatar(self):
        """
        Раньше avatar принимался без единой проверки: ImageField без
        validators=, без clean_avatar(), без лимита размера — можно было
        залить что угодно с расширением .jpg (вплоть до файла, который
        Pillow потом уронит при генерации шер-карточки, или огромный файл,
        забивающий диск).

        Срабатывает только на свежую загрузку (UploadedFile) — если поле
        не тронуто, cleaned_data содержит уже сохранённый ImageFieldFile
        с диска, повторно валидировать (и заново читать в память) его не
        нужно.

        Два барьера:
        1. Лимит размера — до чтения содержимого файла.
        2. Image.verify() (Pillow, уже используется для шер-карточек) —
           проверяет РЕАЛЬНУЮ структуру файла, а не расширение/content-type
           из формы, которые легко подделать.
        """
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

        # verify() потребляет файловый указатель — возвращаем в начало,
        # иначе ImageField.save() запишет на диск пустой/обрезанный файл.
        avatar.seek(0)
        return avatar


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
    # НОВОЕ (4 петли удержания, 2026-08-21) — см. users/models.py::User.
    # DEFAULT_NOTIFICATION_SETTINGS и notifications/tasks.py.
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
    # НОВОЕ (2026-08-22): см. users/models.py::User.DEFAULT_NOTIFICATION_SETTINGS.
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