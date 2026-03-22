from django.utils.deprecation import MiddlewareMixin
from rest_framework_simplejwt.authentication import JWTAuthentication
from .utils import set_current_user_context

class PharmacyMiddleware(MiddlewareMixin):
    def process_request(self, request):
        set_current_user_context(None, False)

        # Only use session auth for Django Admin
        if request.path.startswith('/admin/'):
            if hasattr(request, 'user') and request.user.is_authenticated:
                set_current_user_context(
                    getattr(request.user, 'pharmacy', None),
                    getattr(request.user, 'is_superuser', False)
                )
            return

        # For all API requests — use JWT only
        auth = JWTAuthentication()
        try:
            user_auth_tuple = auth.authenticate(request)
            if user_auth_tuple is not None:
                user, token = user_auth_tuple
                set_current_user_context(
                    getattr(user, 'pharmacy', None),
                    getattr(user, 'is_superuser', False)
                )
        except Exception:
            pass