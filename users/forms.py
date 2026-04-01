# users/forms.py
from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm, PasswordChangeForm, SetPasswordForm, PasswordResetForm
from django.contrib.auth import password_validation
from users.models import User
from django.utils.translation import gettext_lazy as _

class UserRegistrationForm(UserCreationForm):
    """Форма регистрации пользователя"""
    email = forms.EmailField(
        required=True,
        label='Email',
        widget=forms.EmailInput(attrs={
            'class': 'input-dopx w-full',
            'placeholder': 'email@example.com',
            'autocomplete': 'email',
        })
    )
    city = forms.CharField(
        max_length=120,
        required=False,
        label='Город',
        widget=forms.TextInput(attrs={
            'class': 'input-dopx w-full',
            'placeholder': 'Алматы',
            'autocomplete': 'address-level2',
        })
    )

    class Meta:
        model = User
        fields = ['username', 'email', 'city', 'password1', 'password2']
        labels = {
            'username': 'Имя пользователя',
            'password1': 'Пароль',
            'password2': 'Подтверждение пароля',
        }
        widgets = {
            'username': forms.TextInput(attrs={
                'class': 'input-dopx w-full',
                'placeholder': 'username',
                'autocomplete': 'username',
            }),
            'password1': forms.PasswordInput(attrs={
                'class': 'input-dopx w-full',
                'placeholder': '••••••••',
                'autocomplete': 'new-password',
            }),
            'password2': forms.PasswordInput(attrs={
                'class': 'input-dopx w-full',
                'placeholder': '••••••••',
                'autocomplete': 'new-password',
            }),
        }

    def clean_email(self):
        email = self.cleaned_data.get('email')
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError('Этот email уже зарегистрирован')
        return email


class UserLoginForm(AuthenticationForm):
    """Форма входа пользователя"""
    username = forms.CharField(
        label='Имя пользователя или Email',
        widget=forms.TextInput(attrs={
            'class': 'input-dopx w-full',
            'placeholder': 'username или email',
            'autocomplete': 'username',
        })
    )
    password = forms.CharField(
        label='Пароль',
        widget=forms.PasswordInput(attrs={
            'class': 'input-dopx w-full',
            'placeholder': '••••••••',
            'autocomplete': 'current-password',
        })
    )


class UserProfileForm(forms.ModelForm):
    """Форма редактирования профиля"""
    class Meta:
        model = User
        fields = ['email', 'city', 'bio', 'avatar']
        labels = {
            'email': 'Email',
            'city': 'Город',
            'bio': 'О себе',
            'avatar': 'Аватар',
        }
        widgets = {
            'email': forms.EmailInput(attrs={
                'class': 'input input-bordered w-full',
            }),
            'city': forms.TextInput(attrs={
                'class': 'input input-bordered w-full',
                'placeholder': 'Алматы',
            }),
            'bio': forms.Textarea(attrs={
                'class': 'textarea textarea-bordered w-full',
                'rows': 4,
                'placeholder': 'Расскажите о себе...',
            }),
            'avatar': forms.FileInput(attrs={
                'class': 'file-input file-input-bordered w-full',
            }),
        }


class CustomPasswordChangeForm(PasswordChangeForm):
    """Форма изменения пароля"""
    old_password = forms.CharField(
        label='Текущий пароль',
        widget=forms.PasswordInput(attrs={
            'class': 'input input-bordered w-full',
            'placeholder': '••••••••',
            'autocomplete': 'current-password',
        })
    )
    new_password1 = forms.CharField(
        label='Новый пароль',
        widget=forms.PasswordInput(attrs={
            'class': 'input input-bordered w-full',
            'placeholder': '••••••••',
            'autocomplete': 'new-password',
        })
    )
    new_password2 = forms.CharField(
        label='Подтверждение нового пароля',
        widget=forms.PasswordInput(attrs={
            'class': 'input input-bordered w-full',
            'placeholder': '••••••••',
            'autocomplete': 'new-password',
        })
    )


class CustomPasswordResetForm(PasswordResetForm):
    """Форма сброса пароля"""
    email = forms.EmailField(
        label='Email',
        widget=forms.EmailInput(attrs={
            'class': 'input input-bordered w-full',
            'placeholder': 'email@example.com',
        })
    )


class NotificationSettingsForm(forms.Form):
    """Форма настроек уведомлений"""
    email_match_finished = forms.BooleanField(
        required=False,
        label='Матч завершён',
        initial=True
    )
    email_voting_open = forms.BooleanField(
        required=False,
        label='Голосование открыто',
        initial=True
    )
    email_voting_closing = forms.BooleanField(
        required=False,
        label='Голосование закрывается',
        initial=True
    )
    email_top_performance = forms.BooleanField(
        required=False,
        label='Игрок в топ-3',
        initial=True
    )
    email_system = forms.BooleanField(
        required=False,
        label='Системные уведомления',
        initial=True
    )

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        if user and hasattr(user, 'notification_settings'):
            settings = user.notification_settings
            self.fields['email_match_finished'].initial = settings.get('email_match_finished', True)
            self.fields['email_voting_open'].initial = settings.get('email_voting_open', True)
            self.fields['email_voting_closing'].initial = settings.get('email_voting_closing', True)
            self.fields['email_top_performance'].initial = settings.get('email_top_performance', True)
            self.fields['email_system'].initial = settings.get('email_system', True)