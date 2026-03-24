from django.contrib.auth import get_user_model
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework.response import Response
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.exceptions import NotFound
from .serializers import (
    RegisterPharmacySerializer, UserSerializer,
    PharmacySerializer, StaffCreateSerializer
)
from .permissions import IsOwnerOrHigher, IsPharmacyOwnerOrSupport


class LoginView(TokenObtainPairView):
    pass


class RegisterPharmacyView(generics.CreateAPIView):
    serializer_class = RegisterPharmacySerializer
    permission_classes = [AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        refresh = RefreshToken.for_user(user)

        return Response({
            "message": "Pharmacy and Owner registered successfully!",
            "user": {
                "id": str(user.id),
                "phone_number": user.phone_number,
                # Bug 1 fix: guard against pharmacy being None
                "pharmacy_name": user.pharmacy.name if user.pharmacy else None,
                "privilege_level": user.privilege_level
            },
            "tokens": {
                "refresh": str(refresh),
                "access": str(refresh.access_token),
            }
        }, status=status.HTTP_201_CREATED)


class UserDetailView(generics.RetrieveAPIView):
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user


class UpdatePharmacyView(generics.RetrieveUpdateAPIView):
    serializer_class = PharmacySerializer
    permission_classes = [IsPharmacyOwnerOrSupport]

    def get_object(self):
        # Bug 5 fix: guard against Chain Owner / users with no pharmacy FK
        pharmacy = self.request.user.pharmacy
        if pharmacy is None:
            raise NotFound("No pharmacy is linked to your account.")
        return pharmacy


class StaffCreateView(generics.ListCreateAPIView):
    permission_classes = [IsPharmacyOwnerOrSupport]

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return StaffCreateSerializer
        return UserSerializer

    def get_queryset(self):
        User = get_user_model()
        pharmacy = self.request.user.pharmacy
        if pharmacy is None:
            return User.objects.none()
        return User.objects.filter(
            pharmacy=pharmacy
        ).order_by('privilege_level', 'phone_number')


class LogoutView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            refresh_token = request.data["refresh"]
            token = RefreshToken(refresh_token)
            token.blacklist()
            return Response(
                {"message": "Successfully logged out."},
                status=status.HTTP_205_RESET_CONTENT
            )
        except Exception:
            return Response(
                {"error": "Invalid token or token not provided."},
                status=status.HTTP_400_BAD_REQUEST
            )