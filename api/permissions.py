# api/permissions.py
from rest_framework import permissions, throttling
from django.utils import timezone
from evaluations.models import ContextEvaluation

class IsOwnerOrReadOnly(permissions.BasePermission):
    """Разрешение: владелец или только чтение"""
    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        return hasattr(obj, 'user') and obj.user == request.user

class VotingOpenPermission(permissions.BasePermission):
    """Проверка: голосование открыто (48 часов)"""
    message = "Голосование для этого матча закрыто"
    
    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        
        if hasattr(obj, 'voting_open_until'):
            return timezone.now() <= obj.voting_open_until
        
        if hasattr(obj, 'match') and hasattr(obj.match, 'voting_open_until'):
            return timezone.now() <= obj.match.voting_open_until
        
        return True

class IsAuthenticatedAndVerified(permissions.BasePermission):
    """Только авторизованные и верифицированные пользователи"""
    message = "Требуется верифицированный аккаунт"
    
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated and request.user.is_verified

class HasCompletedContext(permissions.BasePermission):
    """Проверка: пользователь создал ContextEvaluation для матча"""
    message = "Сначала укажите контекст просмотра матча"
    
    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        
        if hasattr(obj, 'match'):
            match = obj.match
        else:
            match = obj
        
        return ContextEvaluation.objects.filter(
            user=request.user,
            match=match
        ).exists()

class DocsNoThrottle(throttling.AnonRateThrottle):
    """Отключает throttle для эндпоинтов документации"""
    def allow_request(self, request, view):
        if request.path in ['/api/docs/', '/api/docs.json', '/api/schema/']:
            return True
        return super().allow_request(request, view)

class EvaluationRateThrottle(throttling.UserRateThrottle):
    """Лимит для оценок — 20 оценок в минуту"""
    rate = '20/minute'

class AggregateRateThrottle(throttling.AnonRateThrottle):
    """Лимит для агрегатов — 100 запросов в час"""
    rate = '100/hour'