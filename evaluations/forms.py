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
from teams.models import Team
from lineups.models import MatchLineupPlayer


class ContextEvaluationForm(forms.ModelForm):
    """Шаг 1: Контекст просмотра матча"""
    
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


class TeamEvaluationForm(forms.Form):
    """
    Шаг 2: Оценка команд
    Динамически создаёт поля для ОБЕИХ команд матча
    """
    def __init__(self, *args, **kwargs):
        self.match = kwargs.pop('match', None)
        super().__init__(*args, **kwargs)
        
        if self.match:
            # Создаём поля для домашней команды
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
            
            # Создаём поля для гостевой команды
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
    """
    Шаг 3: Оценка игроков
    Динамически создаёт поля для ВСЕХ игроков в составе
    """
    def __init__(self, *args, **kwargs):
        self.match = kwargs.pop('match', None)
        super().__init__(*args, **kwargs)
        
        if self.match:
            # Получаем игроков из состава
            lineup_players = MatchLineupPlayer.objects.filter(
                lineup__match=self.match
            ).select_related('player__team').order_by('is_starting', 'shirt_number')
            
            for lp in lineup_players:
                player = lp.player
                prefix = f'player_{player.id}'
                
                # Чекбокс для включения оценки игрока
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
    """
    Шаг 4: Оценка тренеров
    Динамически создаёт поля для ОБОИХ тренеров матча
    """
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
    """Шаг 6: Общая оценка матча"""
    
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
    """Шаг 5: Оценка судейства"""
    
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