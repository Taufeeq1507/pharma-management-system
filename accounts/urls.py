from django.urls import path
from .views import LoginView, RegisterPharmacyView, UserDetailView, UpdatePharmacyView, StaffCreateView, LogoutView
from rest_framework_simplejwt.views import TokenRefreshView

urlpatterns = [
    path('register/', RegisterPharmacyView.as_view(), name='register_pharmacy'),
    path('login/', LoginView.as_view(), name='token_obtain_pair'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('me/', UserDetailView.as_view(), name='user_detail'),
    path('pharmacy/', UpdatePharmacyView.as_view(), name='update_pharmacy'),
    path('staff/', StaffCreateView.as_view(), name='staff_create'),
]