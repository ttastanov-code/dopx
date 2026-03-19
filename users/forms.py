# users/forms.py
from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.contrib.auth import password_validation
from users.models import User


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