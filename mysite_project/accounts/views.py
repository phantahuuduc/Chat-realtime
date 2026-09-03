"""
Accounts — JWT Authentication views.
Skill: api-security-best-practices (authentication, token management)
Implements: Plan Section 2.1 (register/login/refresh) + ADR-004 (JWT)
"""

import datetime

import jwt
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

User = get_user_model()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_tokens(user):
    """Generate access + refresh JWT tokens for a user."""
    now = datetime.datetime.now(datetime.timezone.utc)
    access_payload = {
        'user_id': user.pk,
        'username': user.username,
        'type': 'access',
        'iat': now,
        'exp': now + datetime.timedelta(minutes=settings.JWT_ACCESS_TOKEN_LIFETIME_MINUTES),
    }
    refresh_payload = {
        'user_id': user.pk,
        'type': 'refresh',
        'iat': now,
        'exp': now + datetime.timedelta(days=settings.JWT_REFRESH_TOKEN_LIFETIME_DAYS),
    }
    access_token = jwt.encode(access_payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    refresh_token = jwt.encode(refresh_payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return access_token, refresh_token


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

class RegisterSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150, min_length=3)
    password = serializers.CharField(min_length=6, write_only=True)
    first_name = serializers.CharField(max_length=150, required=False, default='')
    last_name = serializers.CharField(max_length=150, required=False, default='')

    def validate_username(self, value):
        if User.objects.filter(username=value).exists():
            raise serializers.ValidationError('Username already exists.')
        return value


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True)


class RefreshSerializer(serializers.Serializer):
    refresh_token = serializers.CharField()


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class RegisterView(APIView):
    """POST /api/auth/register/ — Create a new user account."""
    permission_classes = [AllowAny]

    def post(self, request):
        s = RegisterSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        user = User.objects.create_user(
            username=s.validated_data['username'],
            password=s.validated_data['password'],
            first_name=s.validated_data.get('first_name', ''),
            last_name=s.validated_data.get('last_name', ''),
        )
        access, refresh = _generate_tokens(user)
        return Response({
            'user': {'id': user.pk, 'username': user.username},
            'access_token': access,
            'refresh_token': refresh,
        }, status=status.HTTP_201_CREATED)


class LoginView(APIView):
    """POST /api/auth/login/ — Authenticate and receive JWT tokens."""
    permission_classes = [AllowAny]

    def post(self, request):
        s = LoginSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        user = authenticate(
            username=s.validated_data['username'],
            password=s.validated_data['password'],
        )
        if not user:
            return Response(
                {'error': 'Invalid credentials.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        access, refresh = _generate_tokens(user)
        return Response({
            'user': {'id': user.pk, 'username': user.username},
            'access_token': access,
            'refresh_token': refresh,
        })


class RefreshView(APIView):
    """POST /api/auth/refresh/ — Get a new access token using refresh token."""
    permission_classes = [AllowAny]

    def post(self, request):
        s = RefreshSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            payload = jwt.decode(
                s.validated_data['refresh_token'],
                settings.JWT_SECRET_KEY,
                algorithms=[settings.JWT_ALGORITHM],
            )
            if payload.get('type') != 'refresh':
                raise jwt.InvalidTokenError('Not a refresh token')
            user = User.objects.get(pk=payload['user_id'])
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, User.DoesNotExist):
            return Response(
                {'error': 'Invalid or expired refresh token.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        access, refresh = _generate_tokens(user)
        return Response({
            'access_token': access,
            'refresh_token': refresh,
        })


class MeView(APIView):
    """GET /api/auth/me/ — Get current user info (requires auth)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            'id': request.user.pk,
            'username': request.user.username,
            'first_name': request.user.first_name,
            'last_name': request.user.last_name,
        })
