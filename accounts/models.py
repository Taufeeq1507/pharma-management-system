import uuid
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from .utils import get_current_pharmacy



class PharmacyManager(models.Manager):
    """
    This is the Magic Translator. Any model that uses this manager 
    will automatically filter its data based on the logged-in user.
    """
    def get_queryset(self):
        from .utils import get_current_pharmacy, is_current_user_superuser
        
        # Superadmins see everything
        if is_current_user_superuser():
            return super().get_queryset()
            
        current_pharmacy = get_current_pharmacy()
        if current_pharmacy:
            # If a pharmacy is on the notepad, filter the data!
            return super().get_queryset().filter(pharmacy=current_pharmacy)
        
        # Unauthenticated users (no pharmacy, no superuser) get NOTHING
        return super().get_queryset().none()


class TenantModel(models.Model):
    """
    MASTER BLUEPRINT: Automatically filters reads AND automatically stamps writes.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # 1. Removed editable=False so it shows up in forms/admin panels. 
    # Normal users have this forced via save(), but Superadmins can now edit it directly.
    pharmacy = models.ForeignKey('accounts.Pharmacy', on_delete=models.CASCADE, related_name="%(class)s_set")
    
    objects = PharmacyManager()

    class Meta:
        abstract = True

    # 2. THE AUTOMATIC WRITER
    def save(self, *args, **kwargs):
        # If this is a brand new object being created
        if self._state.adding:
            from .utils import get_current_pharmacy, is_current_user_superuser
            current_pharmacy = get_current_pharmacy()
            
            # 1. Check if the person is a superuser (e.g. Django Admin). 
            # If so, and they MANUALLY picked a pharmacy from the dropdown, let them keep it!
            if is_current_user_superuser() and getattr(self, 'pharmacy_id', None) is not None:
                pass # Don't overwrite the Superadmin's choice
            else:
                # 2. For everyone else (or if Superadmin left it blank), 
                # strictly crush any submitted ID and force it to their own actual pharmacy.
                if current_pharmacy:
                    self.pharmacy = current_pharmacy
                
        # Now proceed with the normal Django save process
        super().save(*args, **kwargs)
class Pharmacy(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    gstin = models.CharField(max_length=15, unique=True, null=True, blank=True)
    drug_license_no = models.CharField(max_length=100, unique=True, null=True, blank=True)
    subscription_plan = models.CharField(max_length=50, default="Tier 2")
    settings = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class CustomUserManager(BaseUserManager):
    def create_user(self, phone_number, password=None, **extra_fields):
        if not phone_number:
            raise ValueError('The Phone Number must be set')
        user = self.model(phone_number=phone_number, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, phone_number, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('privilege_level', 4) # 4 = Admin
        return self.create_user(phone_number, password, **extra_fields)

class CustomUser(AbstractUser):
    username = None 
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    phone_number = models.CharField(max_length=15, unique=True)
    pharmacy = models.ForeignKey(Pharmacy, on_delete=models.CASCADE, null=True, blank=True)
    privilege_level = models.IntegerField(default=1) # 1: Clerk, 2: Owner, 3: Support, 4: Admin

    USERNAME_FIELD = 'phone_number'
    REQUIRED_FIELDS = []

    objects = CustomUserManager()

    def __str__(self):
        return f"{self.phone_number} (Level {self.privilege_level})"