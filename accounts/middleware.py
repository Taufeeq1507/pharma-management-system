from django.utils.deprecation import MiddlewareMixin
from rest_framework_simplejwt.authentication import JWTAuthentication
from .utils import set_current_user_context

class PharmacyMiddleware(MiddlewareMixin):
    def process_request(self, request):
        # 1. Default assumption: No wristband, not a superuser
        set_current_user_context(None, False) 
        
        # 2. Check standard session authentication first (for Django Admin)
        if hasattr(request, 'user') and request.user.is_authenticated:
            set_current_user_context(
                getattr(request.user, 'pharmacy', None),
                getattr(request.user, 'is_superuser', False)
            )
            return

        # 3. Put on "JWT Goggles" to check the headers for API requests
        auth = JWTAuthentication()
        try:
            # This decodes the token and finds the user
            user_auth_tuple = auth.authenticate(request)
            
            if user_auth_tuple is not None:
                user, token = user_auth_tuple
                
                # 4. Write the current context on the notepad!
                set_current_user_context(
                    getattr(user, 'pharmacy', None),
                    getattr(user, 'is_superuser', False)
                )
        except Exception:
            # If the token is expired or invalid, just move on.
            # The Views will reject them later anyway.
            pass