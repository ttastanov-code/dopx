# users/forms_profile.py
"""Анкета «заполните профиль»: город и почта. Новая почта начинает действовать только после подтверждения."""
from __future__ import annotations

from django import forms

from users.kz_cities import KZ_CITY_CHOICES


class CompleteProfileForm(forms.Form):
    city = forms.ChoiceField(label="Ваш город", choices=[("", "Выберите город")] + KZ_CITY_CHOICES,
                             widget=forms.Select(attrs={"class": "select select-bordered w-full"}))
    email = forms.EmailField(label="Почта", widget=forms.EmailInput(attrs={
        "class": "input input-bordered w-full", "placeholder": "name@example.com", "autocomplete": "email"}))

    def __init__(self, *args, user, **kwargs):
        self.user = user
        initial = kwargs.setdefault("initial", {})
        initial.setdefault("city", user.city)
        initial.setdefault("email", user.pending_email or (user.email if user.has_real_email else ""))
        super().__init__(*args, **kwargs)

    def clean_email(self):
        from users.emails import email_taken

        email = self.cleaned_data["email"].strip()
        if email.lower() != self.user.email.lower() and email_taken(email, exclude_user=self.user):
            # Скорее всего это его же старый аккаунт — предлагаем объединить, а не просто отказ.
            raise forms.ValidationError("Эта почта уже есть у аккаунта DOPX. Если он ваш, войдите в него ниже, и мы объединим аккаунты.")
        return email
