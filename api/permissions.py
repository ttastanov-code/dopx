# api/permissions.py
from rest_framework import permissions
from django.utils import timezone


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
        # Безопасные методы всегда разрешены
        if request.method in permissions.SAFE_METHODS:
            return True
        
        # Проверка voting_open_until
        if hasattr(obj, 'voting_open_until'):
            return timezone.now() <= obj.voting_open_until
        
        # Если у объекта есть match
        if hasattr(obj, 'match') and hasattr(obj.match, 'voting_open_until'):
            return timezone.now() <= obj.match.voting_open_until
        
        return True


class IsAuthenticatedAndVerified(permissions.BasePermission):
    """Только авторизованные и верифицированные пользователи"""
    message = "Требуется верифицированный аккаунт"
    
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated and request.user.is_verified