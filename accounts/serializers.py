from rest_framework import serializers
from django.db import transaction
from .models import CustomUser, Pharmacy


class PharmacySerializer(serializers.ModelSerializer):
    class Meta:
        model = Pharmacy
        fields = ['id', 'name', 'gstin', 'drug_license_no', 'subscription_plan', 'settings']
        # Critical Security: Prevent Owners from upgrading their own plan for free
        read_only_fields = ['id', 'subscription_plan', 'settings']

class UserSerializer(serializers.ModelSerializer):
    # This tells Django: "Don't just give me the ID, give me the whole Pharmacy object"
    pharmacy = PharmacySerializer(read_only=True) 

    class Meta:
        model = CustomUser
        fields = ['id', 'phone_number', 'privilege_level', 'pharmacy']



class RegisterPharmacySerializer(serializers.Serializer):
    # 1. Pharmacy specific fields
    pharmacy_name = serializers.CharField(max_length=255)
    gstin = serializers.CharField(max_length=15, required=False, allow_blank=True)
    drug_license_no = serializers.CharField(max_length=100, required=False, allow_blank=True)
    
    # 2. User specific fields
    phone_number = serializers.CharField(max_length=15)
    password = serializers.CharField(write_only=True) # write_only ensures it's never sent back in API responses

    def validate_phone_number(self, value):
        """Check if the phone number is already registered in the database"""
        if CustomUser.objects.filter(phone_number=value).exists():
            raise serializers.ValidationError("A user with this phone number already exists.")
        return value

    def create(self, validated_data):
        # The Atomic block ensures both are created, or neither is created.
        with transaction.atomic():
            # Step A: Create the Pharmacy in the database
            gstin = validated_data.get('gstin', '')
            drug_license_no = validated_data.get('drug_license_no', '')
            
            pharmacy = Pharmacy.objects.create(
                name=validated_data['pharmacy_name'],
                gstin=gstin if gstin else None,
                drug_license_no=drug_license_no if drug_license_no else None
            )
            
            # Step B: Create the Owner in the database and link them to the Pharmacy
            user = CustomUser.objects.create_user(
                phone_number=validated_data['phone_number'],
                password=validated_data['password'],
                pharmacy=pharmacy,
                privilege_level=2  # Privilege Level 2 = Owner
            )
            
        return user


class StaffCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = CustomUser
        fields = ['id', 'phone_number', 'password', 'privilege_level']
        read_only_fields = ['id']

    def create(self, validated_data):
        # 1. Grab the logged-in Owner's pharmacy from the request context
        request = self.context.get('request')
        owner_pharmacy = request.user.pharmacy

        # 2. Safety Check: Force the privilege level to 1 (Clerk) if not provided, 
        # and prevent them from creating an Admin or Support (Level > 2)
        privilege_level = validated_data.get('privilege_level', 1)
        if privilege_level > 2:
            raise serializers.ValidationError({"privilege_level": "You do not have permission to create users with a privilege level higher than Owner (2)."})

        # 3. Create the user safely with a hashed password
        user = CustomUser.objects.create_user(
            phone_number=validated_data['phone_number'],
            password=validated_data['password'],
            pharmacy=owner_pharmacy, # Lock them to the exact same pharmacy
            privilege_level=privilege_level
        )
        return user