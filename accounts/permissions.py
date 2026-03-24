from rest_framework import permissions
from django.core.exceptions import ObjectDoesNotExist


class IsClerkOrHigher(permissions.BasePermission):
    """Level 1+"""
    def has_permission(self, request, view):
        return bool(
            request.user and
            request.user.is_authenticated and
            request.user.privilege_level >= 1
        )


class IsOwnerOrHigher(permissions.BasePermission):
    """Level 2+"""
    def has_permission(self, request, view):
        return bool(
            request.user and
            request.user.is_authenticated and
            request.user.privilege_level >= 2
        )


class IsSupportOrHigher(permissions.BasePermission):
    """Level 3+"""
    def has_permission(self, request, view):
        return bool(
            request.user and
            request.user.is_authenticated and
            request.user.privilege_level >= 3
        )


class IsChainOwnerOrHigher(permissions.BasePermission):
    """Level 4+: Chain Owners and SaaS Admin"""
    def has_permission(self, request, view):
        return bool(
            request.user and
            request.user.is_authenticated and
            request.user.privilege_level >= 4
        )


class IsAdmin(permissions.BasePermission):
    """Level 5: ONLY SaaS Admins"""
    def has_permission(self, request, view):
        return bool(
            request.user and
            request.user.is_authenticated and
            request.user.privilege_level == 5
        )


class IsPharmacyOwnerOrSupport(permissions.BasePermission):
    """
    Level 2 or 3, must have a pharmacy attached.
    Used for pharmacy-specific management endpoints.
    """
    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated and user.privilege_level in [2, 3]):
            return False
        # Bug 9 fix: guard against a deleted pharmacy (FK set but row gone)
        try:
            return user.pharmacy is not None
        except ObjectDoesNotExist:
            return False