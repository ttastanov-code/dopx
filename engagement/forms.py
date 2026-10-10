# engagement/forms.py
"""Формы дашборда: эксперты и их мнения о матчах."""
from __future__ import annotations

from datetime import timedelta

from django import forms
from django.db.models import Q
from django.utils import timezone

from matches.models import Match
from players.models import Player

from .models import Expert, ExpertInvite, ExpertTake

MAX_PHOTO_BYTES = 2 * 1024 * 1024
# Дашборд: кроме туров, ещё группа «Раньше» за столько дней.
MATCH_WINDOW_BACK = 45


def _style(form):
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, forms.CheckboxInput):
            widget.attrs["class"] = "checkbox checkbox-sm"
        elif isinstance(widget, forms.ClearableFileInput):
            widget.attrs.update({"class": "file-input file-input-sm file-input-bordered w-full", "accept": "image/*"})
        elif isinstance(widget, forms.Select):
            widget.attrs["class"] = "select select-sm select-bordered w-full"
        elif isinstance(widget, forms.Textarea):
            widget.attrs["class"] = "textarea textarea-bordered w-full text-sm leading-relaxed"
        else:
            widget.attrs["class"] = "input input-sm input-bordered w-full"


def match_label(match) -> str:
    score = "–" if match.status == "scheduled" else f"{match.home_score}:{match.away_score}"
    tour = f" · {match.tour}-й тур" if match.tour else ""
    return f"{timezone.localtime(match.start_time):%d.%m} · {match.home_team.name} {score} {match.away_team.name}{tour}"


WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _short_label(match, with_tour: bool) -> str:
    start = timezone.localtime(match.start_time)
    score = "–" if match.status in ("scheduled", "live") else f"{match.home_score}:{match.away_score}"
    tour = f" · из {match.tour}-го тура" if with_tour and match.tour else ""
    return f"{WEEKDAYS[start.weekday()]} {match.kickoff_text(sep=' ')} · {match.home_team.name} {score} {match.away_team.name}{tour}"


def group_match_choices(field, extra_back_days: int = 0, keep=None, empty_label=None):
    """Выбор матча по турам (expert_invites.match_groups) + выбранный ранее матч, если он вне групп."""
    from .expert_invites import match_groups

    groups = match_groups(extra_back_days)
    ids = [m.pk for _, matches, _ in groups for m in matches]
    if keep is not None and keep.pk not in ids:
        groups.append(("Выбранный ранее", [keep], True))
        ids.append(keep.pk)
    field.queryset = Match.objects.filter(pk__in=ids).select_related("home_team", "away_team")
    choices = [("", empty_label if empty_label is not None else "Выберите матч")]
    for label, matches, with_tour in groups:
        choices.append((label, [(m.pk, _short_label(m, with_tour)) for m in matches]))
    field.choices = choices


def match_or_none(value):
    """Матч по id из формы; мусор в запросе — None."""
    from django.core.exceptions import ValidationError

    if not value:
        return None
    try:
        return Match.objects.select_related("home_team", "away_team").filter(pk=value).first()
    except (ValueError, ValidationError):
        return None


def match_players(match):
    """Игроки обеих команд матча для выбора ключевого игрока."""
    if match is None:
        return Player.objects.none()
    return (Player.objects.filter(team_id__in=[match.home_team_id, match.away_team_id], is_active=True)
            .select_related("team").order_by("team__name", "last_name", "first_name"))


class MatchChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return match_label(obj)


class PlayerChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.full_name} · {obj.team.name}" if obj.team_id else obj.full_name


class ExpertTakeForm(forms.ModelForm):
    match = MatchChoiceField(queryset=Match.objects.none(), label="Матч")
    key_player = PlayerChoiceField(queryset=Player.objects.none(), required=False, label="Ключевой игрок",
                                   empty_label="Без ключевого игрока")

    class Meta:
        model = ExpertTake
        fields = ["match", "expert", "author_title", "headline", "text", "key_player", "is_published"]
        widgets = {"text": forms.Textarea(attrs={"rows": 9, "maxlength": 3000})}
        labels = {"author_title": "Подпись без эксперта", "is_published": "Опубликовано"}

    def __init__(self, *args, match_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Выбранный матч остаётся в списке, даже если он вне туров.
        current = self.data.get("match") or match_id or (self.instance.match_id if self.instance.pk else None)
        keep = match_or_none(current)
        group_match_choices(self.fields["match"], MATCH_WINDOW_BACK, keep)
        if match_id and not self.is_bound:
            self.initial["match"] = match_id
        self.fields["expert"].queryset = Expert.objects.filter(Q(is_active=True) | Q(pk=self.instance.expert_id))
        self.fields["expert"].empty_label = "Без эксперта (подпись ниже)"
        players = match_players(keep)
        if self.instance.key_player_id:
            players = players | Player.objects.filter(pk=self.instance.key_player_id)
        self.fields["key_player"].queryset = players.select_related("team")
        self.fields["author_title"].required = False
        self.fields["text"].help_text = "Абзацы через пустую строку. Длинный текст на сайте сворачивается."
        _style(self)

    def clean(self):
        data = super().clean()
        if not data.get("expert") and not (data.get("author_title") or "").strip():
            data["author_title"] = "Редакция DOPX"
        player, match = data.get("key_player"), data.get("match")
        if player and match and player.team_id not in (match.home_team_id, match.away_team_id):
            self.add_error("key_player", "Игрок не из команд этого матча.")
        return data


class ExpertForm(forms.ModelForm):
    class Meta:
        model = Expert
        fields = ["name", "title", "photo", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)

    def clean_photo(self):
        photo = self.cleaned_data.get("photo")
        if photo and getattr(photo, "size", 0) > MAX_PHOTO_BYTES:
            raise forms.ValidationError("Фото до 2 МБ.")
        return photo


class ExpertSubmitForm(forms.Form):
    """Форма эксперта по ссылке. Поля матча и «о себе» — только когда их нет в приглашении."""

    match = MatchChoiceField(queryset=Match.objects.none(), label="Матч")
    name = forms.CharField(label="Ваше имя", max_length=80)
    title = forms.CharField(label="Кто вы", max_length=120, required=False,
                            help_text="Коротко, это увидят читатели: «экс-игрок сборной», «тренер UEFA A»")
    photo = forms.ImageField(label="Фото", required=False, help_text="Квадратное, до 2 МБ. Можно без фото.")
    headline = forms.CharField(label="Главная мысль", max_length=140, required=False,
                               help_text="Одна фраза, её покажем крупно")
    text = forms.CharField(label="Ваше мнение", max_length=3000, min_length=40,
                           widget=forms.Textarea(attrs={"rows": 10, "maxlength": 3000}),
                           help_text="Абзацы разделяйте пустой строкой. От 40 до 3000 символов.")
    key_player = PlayerChoiceField(queryset=Player.objects.none(), required=False, label="Ключевой игрок матча",
                                   empty_label="Не выбирать")
    # Ловушка для ботов: людям поле не видно.
    website = forms.CharField(required=False, widget=forms.TextInput(attrs={"tabindex": "-1", "autocomplete": "off"}))

    def __init__(self, *args, invite, take=None, **kwargs):
        if take is not None and "initial" not in kwargs:
            kwargs["initial"] = {"match": take.match_id, "headline": take.headline, "text": take.text,
                                 "key_player": take.key_player_id}
        super().__init__(*args, **kwargs)
        self.invite = invite
        if invite.match_id or take is not None:
            del self.fields["match"]
        else:
            group_match_choices(self.fields["match"])
        if invite.expert_id:
            del self.fields["name"], self.fields["title"]
        match_id = invite.match_id or (take.match_id if take else None) or self.data.get("match")
        match = match_or_none(match_id)
        self.fields["key_player"].queryset = match_players(match).select_related("team")
        _style(self)

    def clean_website(self):
        if self.cleaned_data.get("website"):
            raise forms.ValidationError("spam")
        return ""

    def clean_photo(self):
        photo = self.cleaned_data.get("photo")
        if photo and getattr(photo, "size", 0) > MAX_PHOTO_BYTES:
            raise forms.ValidationError("Фото до 2 МБ.")
        return photo

    def clean(self):
        data = super().clean()
        match = self.invite.match or data.get("match")
        player = data.get("key_player")
        if player and match and player.team_id not in (match.home_team_id, match.away_team_id):
            self.add_error("key_player", "Игрок не из команд этого матча.")
        return data


class ExpertInviteForm(forms.ModelForm):
    """Новая ссылка для эксперта (дашборд)."""

    match = MatchChoiceField(queryset=Match.objects.none(), required=False, label="Матч")
    days = forms.TypedChoiceField(label="Ссылка действует", coerce=int, initial=3, choices=())

    class Meta:
        model = ExpertInvite
        fields = ["expert", "match", "max_takes", "auto_publish", "note"]

    def __init__(self, *args, match_id=None, expert_id=None, **kwargs):
        from .expert_invites import EXPIRY_CHOICES

        super().__init__(*args, **kwargs)
        current = self.data.get("match") or match_id
        keep = match_or_none(current)
        group_match_choices(self.fields["match"], MATCH_WINDOW_BACK, keep,
                            empty_label="Эксперт выберет сам: ближайший тур, прошедший и перенесённые")
        self.fields["expert"].queryset = Expert.objects.filter(is_active=True)
        self.fields["expert"].empty_label = "Новый эксперт: представится сам"
        self.fields["days"].choices = EXPIRY_CHOICES
        self.fields["max_takes"].widget.attrs.update({"min": 1, "max": 20})
        self.fields["max_takes"].help_text = "Для одного матча хватит 1. Для «выберет сам» — по числу матчей."
        self.fields["auto_publish"].help_text = "Только для проверенных экспертов. Иначе мнение ждёт вашей проверки."
        if not self.is_bound:
            self.initial.update({k: v for k, v in (("match", match_id), ("expert", expert_id)) if v})
        _style(self)

    def clean_max_takes(self):
        value = self.cleaned_data["max_takes"]
        if not 1 <= value <= 20:
            raise forms.ValidationError("От 1 до 20.")
        return value

    def save(self, commit=True):
        invite = super().save(commit=False)
        invite.expires_at = timezone.now() + timedelta(days=self.cleaned_data["days"])
        if commit:
            invite.save()
        return invite
