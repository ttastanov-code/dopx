# core/forms.py
"""Формы core. ContactAntiBotForm — антибот-поля формы обращения
(honeypot + time-trap + капча); остальные поля читает ContactsView.post().
"""
from __future__ import annotations


from captcha.fields import CaptchaField, CaptchaTextInput
from django import forms

from core.utils import form_timestamp_is_valid, sign_form_timestamp

# Минимальное время заполнения формы, сек.
MIN_FORM_FILL_SECONDS = 3


class ContactAntiBotForm(forms.Form):
    """Антибот-проверка формы обращения:
    1. website — honeypot, должно быть пустым;
    2. form_rendered_at — отправка не быстрее MIN_FORM_FILL_SECONDS;
    3. captcha.
    """

    website = forms.CharField(required=False, label="")
    # Подписанная метка времени рендера (core.utils.sign_form_timestamp).
    form_rendered_at = forms.CharField(widget=forms.HiddenInput(), required=False)
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
        # Время рендера — только для GET; при POST приходит из скрытого поля.
        self.fields["form_rendered_at"].initial = sign_form_timestamp()

    def clean_website(self):
        value = self.cleaned_data.get("website")
        if value:
            # Общая формулировка — не подсказываем боту.
            raise forms.ValidationError("Не удалось обработать форму. Попробуйте ещё раз.")
        return value

    def clean_form_rendered_at(self):
        token = self.cleaned_data.get("form_rendered_at")
        if not form_timestamp_is_valid(token, MIN_FORM_FILL_SECONDS):
            raise forms.ValidationError("Не удалось обработать форму. Попробуйте ещё раз.")
        return token
