from rest_framework import serializers
from django.db import transaction
from .models import CustomUser, Pharmacy, Organization


class PharmacySerializer(serializers.ModelSerializer):
    class Meta:
        model = Pharmacy
        fields = ['id', 'name', 'gstin', 'drug_license_no', 'subscription_plan', 'settings']
        read_only_fields = ['id', 'subscription_plan', 'settings']


class UserSerializer(serializers.ModelSerializer):
    pharmacy = PharmacySerializer(read_only=True)

    class Meta:
        model = CustomUser
        fields = ['id', 'phone_number', 'privilege_level', 'pharmacy']


class RegisterPharmacySerializer(serializers.Serializer):
    pharmacy_name = serializers.CharField(max_length=255)
    gstin = serializers.CharField(max_length=15, required=False, allow_blank=True)
    drug_license_no = serializers.CharField(max_length=100, required=False, allow_blank=True)
    phone_number = serializers.CharField(max_length=15)
    password = serializers.CharField(write_only=True)

    def validate_phone_number(self, value):
        if CustomUser.objects.filter(phone_number=value).exists():
            raise serializers.ValidationError("A user with this phone number already exists.")
        return value

    def create(self, validated_data):
        with transaction.atomic():
            gstin = validated_data.get('gstin', '')
            drug_license_no = validated_data.get('drug_license_no', '')

            # Create Organization first — every pharmacy belongs to one
            org = Organization.objects.create(
                name=validated_data['pharmacy_name']
            )

            pharmacy = Pharmacy.objects.create(
                name=validated_data['pharmacy_name'],
                gstin=gstin if gstin else None,
                drug_license_no=drug_license_no if drug_license_no else None,
                organization=org
            )

            user = CustomUser.objects.create_user(
                phone_number=validated_data['phone_number'],
                password=validated_data['password'],
                pharmacy=pharmacy,
                organization=org,
                privilege_level=2  # Standalone owner
            )

        return user


class StaffCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = CustomUser
        fields = ['id', 'phone_number', 'password', 'privilege_level']
        read_only_fields = ['id']

    def create(self, validated_data):
        request = self.context.get('request')
        owner_pharmacy = request.user.pharmacy

        privilege_level = validated_data.get('privilege_level', 1)
        # Owners (level 2) can only create clerks (1) or co-owners (2)
        if privilege_level > 2:
            raise serializers.ValidationError({
                "privilege_level": "You cannot create users with privilege level higher than 2."
            })

        user = CustomUser.objects.create_user(
            phone_number=validated_data['phone_number'],
            password=validated_data['password'],
            pharmacy=owner_pharmacy,
            organization=owner_pharmacy.organization,
            privilege_level=privilege_level
        )
        return user