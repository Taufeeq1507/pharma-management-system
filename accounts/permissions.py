from rest_framework import permissions

class IsClerkOrHigher(permissions.BasePermission):
    """Level 1+: Clerks, Owners, Support, and Admins can access."""
    def has_permission(self, request, view):
        # 1. Must be logged in via JWT
        # 2. Privilege level must be 1 or greater
        return bool(request.user and request.user.is_authenticated and request.user.privilege_level >= 1)

class IsOwnerOrHigher(permissions.BasePermission):
    """Level 2+: Only Owners, Support, and Admins can access."""
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.privilege_level >= 2)

class IsSupportOrHigher(permissions.BasePermission):
    """Level 3+: Only Support and Admins can access."""
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.privilege_level >= 3)

class IsAdmin(permissions.BasePermission):
    """Level 4: ONLY System Admins can access."""
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.privilege_level == 4)

class IsPharmacyOwnerOrSupport(permissions.BasePermission):
    """Level 2/3: ONLY Owners and Support staff belonging to a pharmacy can access. Admins are blocked."""
    def has_permission(self, request, view):
        # Must be logged in, must be level 2 or 3, AND must actually have a pharmacy attached.
        user = request.user
        return bool(
            user and 
            user.is_authenticated and 
            user.privilege_level in [2, 3] and 
            getattr(user, 'pharmacy', None) is not None
        )