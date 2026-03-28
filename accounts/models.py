import uuid
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from .utils import get_current_pharmacy


class PharmacyManager(models.Manager):
    def get_queryset(self):
        from .utils import get_current_pharmacy, get_current_organization, is_current_user_superuser

        if is_current_user_superuser():
            return super().get_queryset()

        current_pharmacy = get_current_pharmacy()
        if current_pharmacy:
            return super().get_queryset().filter(pharmacy=current_pharmacy)

        current_org = get_current_organization()
        if current_org:
            return super().get_queryset().filter(pharmacy__organization=current_org)

        return super().get_queryset().none()


class TenantModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    pharmacy = models.ForeignKey('accounts.Pharmacy', on_delete=models.CASCADE, related_name="%(class)s_set")

    objects = PharmacyManager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self._state.adding:
            from .utils import get_current_pharmacy, is_current_user_superuser
            current_pharmacy = get_current_pharmacy()

            if is_current_user_superuser() and getattr(self, 'pharmacy_id', None) is not None:
                pass
            else:
                if current_pharmacy:
                    self.pharmacy = current_pharmacy
                elif getattr(self, 'pharmacy_id', None) is None:
                    raise ValueError(
                        f"Cannot save {self.__class__.__name__}: no pharmacy in context. "
                        "Chain owners must select a branch before creating records."
                    )

        super().save(*args, **kwargs)


class Organization(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    subscription_plan = models.CharField(max_length=50, default="Tier 2")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Pharmacy(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='branches'
    )
    name = models.CharField(max_length=255)
    gstin = models.CharField(max_length=15, unique=True, null=True, blank=True)
    state = models.CharField(max_length=100, default="Maharashtra", help_text="Used for CGST/SGST vs IGST calculation")
    drug_license_no = models.CharField(max_length=100, unique=True, null=True, blank=True)
    subscription_plan = models.CharField(max_length=50, default="Tier 2")
    settings = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class CustomUserManager(BaseUserManager):
    def create_user(self, phone_number, name='', password=None, **extra_fields):
        if not phone_number:
            raise ValueError('The Phone Number must be set')
        user = self.model(phone_number=phone_number, name=name, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, phone_number, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('privilege_level', 5)
        return self.create_user(phone_number, name="Superuser", password=password, **extra_fields)


class CustomUser(AbstractUser):
    username = None

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, blank=True)
    phone_number = models.CharField(max_length=15, unique=True)
    pharmacy = models.ForeignKey(Pharmacy, on_delete=models.CASCADE, null=True, blank=True)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='members'
    )
    privilege_level = models.IntegerField(default=1)
    # 1: Clerk
    # 2: Branch Owner / Standalone Owner
    # 3: Support Staff
    # 4: Chain Owner
    # 5: SaaS Admin

    USERNAME_FIELD = 'phone_number'
    REQUIRED_FIELDS = []

    objects = CustomUserManager()

    def __str__(self):
        return f"{self.phone_number} (Level {self.privilege_level})"