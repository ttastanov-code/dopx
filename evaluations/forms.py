# evaluations/forms.py
from django import forms
from django.core.validators import MinValueValidator, MaxValueValidator
from evaluations.models import (
    ContextEvaluation,
    MatchEvaluation,
    TeamEvaluation,
    PlayerEvaluation,
    CoachEvaluation,
    RefereeEvaluation
)
from evaluations.policies import EvaluationPolicyError, assert_team_in_match
from teams.models import Team
from lineups.models import MatchLineupPlayer


class ContextEvaluationForm(forms.ModelForm):
    """Шаг 1: контекст просмотра."""
    
    class Meta:
        model = ContextEvaluation
        fields = ['supported_team', 'watched_type', 'attended_stadium']
        labels = {
            'supported_team': 'За какую команду болеете?',
            'watched_type': 'Как вы смотрели матч?',
            'attended_stadium': 'Были на стадионе?',
        }
        widgets = {
            'supported_team': forms.Select(attrs={
                'class': 'select select-bordered w-full input-dopx',
            }),
            'watched_type': forms.RadioSelect(attrs={
                'class': 'radio radio-primary',
            }),
            'attended_stadium': forms.CheckboxInput(attrs={
                'class': 'checkbox checkbox-primary',
            }),
        }

    def __init__(self, *args, **kwargs):
        self.match = kwargs.pop('match', None)
        super().__init__(*args, **kwargs)
        
        if self.match:
            self.fields['supported_team'].queryset = Team.objects.filter(
                id__in=[self.match.home_team_id, self.match.away_team_id]
            )
            self.fields['supported_team'].required = False
            self.fields['supported_team'].empty_label = 'Не болею ни за кого'

    def clean_supported_team(self):
        """Проверка политики — на случай, если queryset когда-нибудь расширят."""
        team = self.cleaned_data.get('supported_team')
        if team is not None and self.match is not None:
            try:
                assert_team_in_match(team.id, self.match)
            except EvaluationPolicyError as e:
                raise forms.ValidationError(str(e))
        return team

    def clean(self):
        """Стадион + «только голы» -> «полный матч» (тихая нормализация)."""
        cleaned_data = super().clean()
        if cleaned_data.get('attended_stadium') and cleaned_data.get('watched_type') == 'highlights':
            cleaned_data['watched_type'] = 'full'
        return cleaned_data


class TeamEvaluationForm(forms.Form):
    """Шаг 2: оценка команд — поля для обеих команд."""
    def __init__(self, *args, **kwargs):
        self.match = kwargs.pop('match', None)
        super().__init__(*args, **kwargs)
        
        if self.match:
            # Поля домашней команды
            home_prefix = f'team_{self.match.home_team.id}'
            self.fields[f'{home_prefix}_tactics'] = forms.IntegerField(
                min_value=1, 
                max_value=10, 
                initial=5,
                label=f'{self.match.home_team.name} — Тактика',
                widget=forms.NumberInput(attrs={
                    'type': 'range',
                    'min': 1,
                    'max': 10,
                    'class': 'range range-primary range-xs',
                })
            )
            self.fields[f'{home_prefix}_effort'] = forms.IntegerField(
                min_value=1, 
                max_value=10, 
                initial=5,
                label=f'{self.match.home_team.name} — Самоотдача',
                widget=forms.NumberInput(attrs={
                    'type': 'range',
                    'min': 1,
                    'max': 10,
                    'class': 'range range-primary range-xs',
                })
            )
            self.fields[f'{home_prefix}_organization'] = forms.IntegerField(
                min_value=1, 
                max_value=10, 
                initial=5,
                label=f'{self.match.home_team.name} — Организация',
                widget=forms.NumberInput(attrs={
                    'type': 'range',
                    'min': 1,
                    'max': 10,
                    'class': 'range range-primary range-xs',
                })
            )
            self.fields[f'{home_prefix}_mentality'] = forms.IntegerField(
                min_value=1, 
                max_value=10, 
                initial=5,
                label=f'{self.match.home_team.name} — Менталитет',
                widget=forms.NumberInput(attrs={
                    'type': 'range',
                    'min': 1,
                    'max': 10,
                    'class': 'range range-primary range-xs',
                })
            )
            
            # Поля гостевой команды
            away_prefix = f'team_{self.match.away_team.id}'
            self.fields[f'{away_prefix}_tactics'] = forms.IntegerField(
                min_value=1, 
                max_value=10, 
                initial=5,
                label=f'{self.match.away_team.name} — Тактика',
                widget=forms.NumberInput(attrs={
                    'type': 'range',
                    'min': 1,
                    'max': 10,
                    'class': 'range range-primary range-xs',
                })
            )
            self.fields[f'{away_prefix}_effort'] = forms.IntegerField(
                min_value=1, 
                max_value=10, 
                initial=5,
                label=f'{self.match.away_team.name} — Самоотдача',
                widget=forms.NumberInput(attrs={
                    'type': 'range',
                    'min': 1,
                    'max': 10,
                    'class': 'range range-primary range-xs',
                })
            )
            self.fields[f'{away_prefix}_organization'] = forms.IntegerField(
                min_value=1, 
                max_value=10, 
                initial=5,
                label=f'{self.match.away_team.name} — Организация',
                widget=forms.NumberInput(attrs={
                    'type': 'range',
                    'min': 1,
                    'max': 10,
                    'class': 'range range-primary range-xs',
                })
            )
            self.fields[f'{away_prefix}_mentality'] = forms.IntegerField(
                min_value=1, 
                max_value=10, 
                initial=5,
                label=f'{self.match.away_team.name} — Менталитет',
                widget=forms.NumberInput(attrs={
                    'type': 'range',
                    'min': 1,
                    'max': 10,
                    'class': 'range range-primary range-xs',
                })
            )


class PlayerEvaluationForm(forms.Form):
    """Шаг 3: оценка игроков — поля для всех игроков состава."""
    def __init__(self, *args, **kwargs):
        self.match = kwargs.pop('match', None)
        super().__init__(*args, **kwargs)
        
        if self.match:
            # Игроки из состава
            lineup_players = MatchLineupPlayer.objects.filter(
                lineup__match=self.match
            ).select_related('player__team').order_by('is_starting', 'shirt_number')
            
            for lp in lineup_players:
                player = lp.player
                prefix = f'player_{player.id}'
                
                # Чекбокс «оценить игрока»
                self.fields[f'{prefix}_evaluate'] = forms.BooleanField(
                    required=False,
                    initial=False,
                    label=f'Оценить {player.first_name} {player.last_name}',
                    widget=forms.CheckboxInput(attrs={
                        'class': 'toggle toggle-primary toggle-sm evaluate-toggle',
                        'data-player-id': str(player.id),
                    })
                )
                
                # Вклад (1-10)
                self.fields[f'{prefix}_contribution'] = forms.IntegerField(
                    min_value=1, 
                    max_value=10, 
                    initial=5,
                    required=False,
                    label='Вклад',
                    widget=forms.NumberInput(attrs={
                        'type': 'range',
                        'min': 1,
                        'max': 10,
                        'class': 'range range-primary range-xs',
                        'disabled': 'disabled',
                    })
                )
                
                # Риск (1-10)
                self.fields[f'{prefix}_risk'] = forms.IntegerField(
                    min_value=1, 
                    max_value=10, 
                    initial=5,
                    required=False,
                    label='Риск',
                    widget=forms.NumberInput(attrs={
                        'type': 'range',
                        'min': 1,
                        'max': 10,
                        'class': 'range range-primary range-xs',
                        'disabled': 'disabled',
                    })
                )
                
                # Потенциал (1-10)
                self.fields[f'{prefix}_potential'] = forms.IntegerField(
                    min_value=1, 
                    max_value=10, 
                    initial=5,
                    required=False,
                    label='Потенциал',
                    widget=forms.NumberInput(attrs={
                        'type': 'range',
                        'min': 1,
                        'max': 10,
                        'class': 'range range-primary range-xs',
                        'disabled': 'disabled',
                    })
                )


class CoachEvaluationForm(forms.Form):
    """Шаг 4: оценка тренеров — поля для обоих тренеров."""
    def __init__(self, *args, **kwargs):
        self.match = kwargs.pop('match', None)
        super().__init__(*args, **kwargs)
        
        if self.match:
            coaches = [c for c in [self.match.home_coach, self.match.away_coach] if c]
            
            for coach in coaches:
                prefix = f'coach_{coach.id}'
                
                # Тактика (1-10)
                self.fields[f'{prefix}_tactics'] = forms.IntegerField(
                    min_value=1, 
                    max_value=10, 
                    initial=5,
                    label=f'{coach.first_name} {coach.last_name} — Тактика',
                    widget=forms.NumberInput(attrs={
                        'type': 'range',
                        'min': 1,
                        'max': 10,
                        'class': 'range range-primary range-xs',
                    })
                )
                
                # Замены (1-10)
                self.fields[f'{prefix}_substitutions'] = forms.IntegerField(
                    min_value=1, 
                    max_value=10, 
                    initial=5,
                    label=f'{coach.first_name} {coach.last_name} — Замены',
                    widget=forms.NumberInput(attrs={
                        'type': 'range',
                        'min': 1,
                        'max': 10,
                        'class': 'range range-primary range-xs',
                    })
                )
                
                # Управление (1-10)
                self.fields[f'{prefix}_management'] = forms.IntegerField(
                    min_value=1, 
                    max_value=10, 
                    initial=5,
                    label=f'{coach.first_name} {coach.last_name} — Управление',
                    widget=forms.NumberInput(attrs={
                        'type': 'range',
                        'min': 1,
                        'max': 10,
                        'class': 'range range-primary range-xs',
                    })
                )
                
                # Влияние (1-10)
                self.fields[f'{prefix}_impact'] = forms.IntegerField(
                    min_value=1, 
                    max_value=10, 
                    initial=5,
                    label=f'{coach.first_name} {coach.last_name} — Влияние',
                    widget=forms.NumberInput(attrs={
                        'type': 'range',
                        'min': 1,
                        'max': 10,
                        'class': 'range range-primary range-xs',
                    })
                )


class MatchEvaluationForm(forms.ModelForm):
    """Шаг 6: общая оценка матча."""
    
    class Meta:
        model = MatchEvaluation
        fields = ['entertainment', 'tension', 'turning_point', 'fairness']
        labels = {
            'entertainment': 'Зрелищность (1-10)',
            'tension': 'Напряжение (1-10)',
            'turning_point': 'Был переломный момент?',
            'fairness': 'Справедливость (1-10)',
        }
        widgets = {
            'entertainment': forms.NumberInput(attrs={
                'type': 'range',
                'min': 1, 
                'max': 10,
                'class': 'range range-primary',
            }),
            'tension': forms.NumberInput(attrs={
                'type': 'range',
                'min': 1, 
                'max': 10,
                'class': 'range range-primary',
            }),
            'fairness': forms.NumberInput(attrs={
                'type': 'range',
                'min': 1, 
                'max': 10,
                'class': 'range range-primary',
            }),
            'turning_point': forms.CheckboxInput(attrs={
                'class': 'toggle toggle-primary',
            }),
        }


class RefereeEvaluationForm(forms.ModelForm):
    """Шаг 5: оценка судейства."""
    
    class Meta:
        model = RefereeEvaluation
        fields = ['influence_score', 'decision_quality']
        labels = {
            'influence_score': 'Влияние на матч (0-100)',
            'decision_quality': 'Качество решений (1-10)',
        }
        widgets = {
            'influence_score': forms.NumberInput(attrs={
                'type': 'range',
                'min': 0, 
                'max': 100,
                'class': 'range range-primary',
            }),
            'decision_quality': forms.NumberInput(attrs={
                'type': 'range',
                'min': 1, 
                'max': 10,
                'class': 'range range-primary',
            }),
        }