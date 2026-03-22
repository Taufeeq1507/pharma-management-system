from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework.response import Response
from rest_framework import generics, status
from rest_framework.permissions import AllowAny,IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken
from .serializers import RegisterPharmacySerializer, UserSerializer, PharmacySerializer, StaffCreateSerializer
from .permissions import IsOwnerOrHigher, IsPharmacyOwnerOrSupport
# This view will take phone_number and password and return a JWT Token
class LoginView(TokenObtainPairView):
    # We can customize the response here later if we want to 
    # return the user's privilege level along with the token.
    pass


class RegisterPharmacyView(generics.CreateAPIView):
    serializer_class = RegisterPharmacySerializer
    permission_classes = [AllowAny] # We must allow unauthenticated users to register

    def create(self, request, *args, **kwargs):
        # 1. Pass the incoming JSON data to the Serializer
        serializer = self.get_serializer(data=request.data)
        
        # 2. Validate the data (checks phone number uniqueness, missing fields, etc.)
        serializer.is_valid(raise_exception=True)
        
        # 3. Trigger the `create()` method in the serializer (The Atomic Transaction)
        user = serializer.save()

        # 4. Generate the JWT Tokens for the brand new user
        refresh = RefreshToken.for_user(user)

        # 5. Send a beautiful, structured JSON response back to the frontend
        return Response({
            "message": "Pharmacy and Owner registered successfully!",
            "user": {
                "id": str(user.id),
                "phone_number": user.phone_number,
                "pharmacy_name": user.pharmacy.name,
                "privilege_level": user.privilege_level
            },
            "tokens": {
                "refresh": str(refresh),
                "access": str(refresh.access_token),
            }
        }, status=status.HTTP_201_CREATED)



class UserDetailView(generics.RetrieveAPIView):
    """
    Returns the details of the currently logged-in user.
    """
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated] # MUST have a valid JWT token in the header

    def get_object(self):
        # We override this method because normally Django looks for an ID in the URL (e.g., /me/5/).
        # But we don't need an ID! The JWT Middleware already identified the user 
        # and attached them to `self.request.user`. We just return that exact user.
        return self.request.user



class UpdatePharmacyView(generics.RetrieveUpdateAPIView):
    """
    Allows an Owner to view and update their Pharmacy details (e.g., GSTIN).
    A Level 1 Clerk will be completely blocked from accessing this API.
    """
    serializer_class = PharmacySerializer
    
    # The Bouncer: Only Level 2, or 3 users belonging to the pharmacy are allowed in.
    permission_classes = [IsPharmacyOwnerOrSupport]

    def get_object(self):
        # We don't look up the pharmacy by an ID in the URL. 
        # We guarantee absolute security by only fetching the pharmacy attached to the logged-in user!
        return self.request.user.pharmacy




class StaffCreateView(generics.CreateAPIView):
    """
    Allows an Owner (Level 2+) to create new staff accounts (Clerks) for their pharmacy.
    """
    serializer_class = StaffCreateSerializer
    permission_classes = [IsPharmacyOwnerOrSupport]


class LogoutView(generics.GenericAPIView):
    """
    Blacklists the given refresh token, effectively logging the user out.
    """
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        try:
            refresh_token = request.data["refresh"]
            token = RefreshToken(refresh_token)
            token.blacklist()
            return Response({"message": "Successfully logged out."}, status=status.HTTP_205_RESET_CONTENT)
        except Exception as e:
            return Response({"error": "Invalid token or token not provided."}, status=status.HTTP_400_BAD_REQUEST)