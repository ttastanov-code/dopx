# core/forms.py
"""
Формы core-приложения.

ContactAntiBotForm — набор анти-бот полей для формы обращения
(/contacts/), тот же трёхуровневый паттерн, что и в
users/forms.py::UserRegistrationForm (honeypot + time-trap + self-hosted
CAPTCHA от django-simple-captcha, уже подключён в INSTALLED_APPS и
dopx/urls.py — см. докстринг там для полного обоснования выбора именно
такой связки).

Это осознанно НЕ полноценная ModelForm под ContactSubmission: остальные
поля обращения (category/email/subject/message/screenshot) продолжает
вручную читать и валидировать ContactsView.post() — этот рабочий код
менять не нужно, чтобы не тащить за собой риск регресса уже
протестированной логики. ContactAntiBotForm валидируется отдельно, до
разбора остальных полей, и отвечает только за одно: отличить человека
от бота.
"""
from __future__ import annotations

import time

from captcha.fields import CaptchaField, CaptchaTextInput
from django import forms

# Тот же порог, что и MIN_FORM_FILL_SECONDS в users/forms.py — держим
# отдельной константой (а не общим импортом), т.к. это два независимых
# анти-бот барьера на разных формах, которые могут разойтись по времени
# в будущем (например, форма обращения объективно длиннее и её можно
# осмысленно заполнить чуть быстрее).
MIN_FORM_FILL_SECONDS = 3


class ContactAntiBotForm(forms.Form):
    """
    Валидируется первой, до чтения остальных полей формы обращения.
    Три независимых барьера:

    1. website — honeypot. Обычный пользователь его не видит и не
       заполняет (скрыт в шаблоне через CSS, не через type="hidden" —
       часть ботов игнорирует hidden-поля именно потому, что их принято
       игнорировать). Заполнено — почти наверняка бот.
    2. form_rendered_at — время-ловушка. Штампуется на сервере в момент
       рендера формы, сверяется в clean(). Отправка быстрее
       MIN_FORM_FILL_SECONDS секунд после показа формы — тоже почти
       наверняка бот, человек физически не успевает прочитать и
       заполнить форму обращения так быстро.
    3. captcha — self-hosted картинка-капча (django-simple-captcha),
       основной барьер для тех ботов, что осознанно обходят первые два.
    """

    website = forms.CharField(required=False, label="")
    form_rendered_at = forms.FloatField(widget=forms.HiddenInput(), required=False)
    captcha = CaptchaField(
        label="Введите текст с картинки",
        error_messages={"invalid": "Неверный текст с картинки. Попробуйте ещё раз."},
        widget=CaptchaTextInput(attrs={
            "class": "input input-bordered w-full",
            "placeholder": "Текст с картинки",
            "autocomplete": "off",
        }),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Штампуем именно на построении формы для GET-рендера (страница
        # показана пользователю прямо сейчас) — при POST это поле придёт
        # уже заполненным значением из скрытого инпута, и initial здесь
        # не используется вообще (bound-форма читает cleaned_data из
        # data, а не из initial).
        self.fields["form_rendered_at"].initial = time.time()

    def clean_website(self):
        value = self.cleaned_data.get("website")
        if value:
            # Намеренно generic-сообщение — не подсказываем боту, что
            # конкретно его выдало.
            raise forms.ValidationError("Не удалось обработать форму. Попробуйте ещё раз.")
        return value

    def clean_form_rendered_at(self):
        rendered_at = self.cleaned_data.get("form_rendered_at")
        if rendered_at:
            elapsed = time.time() - rendered_at
            if 0 <= elapsed < MIN_FORM_FILL_SECONDS:
                raise forms.ValidationError("Не удалось обработать форму. Попробуйте ещё раз.")
        return rendered_at
