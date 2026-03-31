# core/templatetags/math_extras.py
from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    """Получение значения из словаря по ключу"""
    return dictionary.get(key) if dictionary else None

@register.filter
def div(value, arg):
    """Деление значения на аргумент"""
    try:
        return float(value) / float(arg)
    except (ValueError, ZeroDivisionError, TypeError):
        return 0

@register.filter
def mul(value, arg):
    """Умножение значения на аргумент"""
    try:
        return float(value) * float(arg)
    except (ValueError, TypeError):
        return 0
    
    
@register.filter
def subtract(value, arg):
    """Вычитание: value - arg"""
    try:
        return float(value) - float(arg)
    except (ValueError, TypeError):
        return 0