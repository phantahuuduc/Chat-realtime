"""
JWT Authentication backend for DRF.
Validates JWT access tokens in Authorization: Bearer <token> header.
"""

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework import authentication, exceptions

User = get_user_model()


class JWTAuthentication(authentication.BaseAuthentication):
    """
    DRF authentication class for JWT Bearer tokens.
    Usage: Authorization: Bearer <access_token>
    """

    keyword = 'Bearer'

    def authenticate_header(self, request):
        """
        Bắt buộc phải có. Thiếu nó, DRF trả 403 cho mọi lỗi xác thực thay vì
        401 — client không nhận ra token hết hạn nên không bao giờ refresh,
        và mọi request sau đó hỏng vĩnh viễn.
        """
        return f'{self.keyword} realm="api"'

    def authenticate(self, request):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith(f'{self.keyword} '):
            return None

        token = auth_header[len(self.keyword) + 1:]
        try:
            payload = jwt.decode(
                token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM],
            )
            if payload.get('type') != 'access':
                raise exceptions.AuthenticationFailed('Invalid token type.')
            user = User.objects.get(pk=payload['user_id'])
        except jwt.ExpiredSignatureError:
            raise exceptions.AuthenticationFailed('Token has expired.')
        except (jwt.InvalidTokenError, User.DoesNotExist):
            raise exceptions.AuthenticationFailed('Invalid token.')

        return (user, payload)
