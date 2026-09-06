"""
API tuỳ chọn hiển thị (mục 7.1).

- GET  /api/preferences/            đọc tuỳ chọn
- PATCH /api/preferences/           cập nhật từng phần
- POST /api/preferences/background/ tải ảnh nền riêng
"""

import io

from PIL import Image, UnidentifiedImageError
from django.core.files.base import ContentFile
from rest_framework import serializers, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import UserPreference

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_EDGE_PX = 1920
ALLOWED_FORMATS = {'JPEG': 'jpg', 'PNG': 'png', 'WEBP': 'webp'}


class PreferenceSerializer(serializers.ModelSerializer):
    custom_bg_url = serializers.SerializerMethodField()

    class Meta:
        model = UserPreference
        fields = ['theme_id', 'custom_bg_url']

    def get_custom_bg_url(self, obj):
        return obj.custom_bg.url if obj.custom_bg else None


def _get_preference(user):
    preference, _ = UserPreference.objects.get_or_create(user=user)
    return preference


class PreferenceView(APIView):
    """Đọc và cập nhật tuỳ chọn hiển thị."""

    def get(self, request):
        return Response(PreferenceSerializer(_get_preference(request.user)).data)

    def patch(self, request):
        preference = _get_preference(request.user)
        serializer = PreferenceSerializer(preference, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class BackgroundUploadView(APIView):
    """
    Tải ảnh nền riêng. Chỉ nhận jpg/png/webp, tối đa 5MB, và phải mở được
    bằng Pillow — không tin phần mở rộng hay Content-Type do client khai.
    """

    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        upload = request.FILES.get('image')
        if upload is None:
            return Response({'error': 'Thiếu file ảnh.'}, status=status.HTTP_400_BAD_REQUEST)

        if upload.size > MAX_UPLOAD_BYTES:
            return Response(
                {'error': f'Ảnh tối đa 5MB, ảnh của bạn {upload.size / 1048576:.1f}MB.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            image = Image.open(upload)
            image.verify()          # phát hiện file hỏng / giả mạo đuôi
            upload.seek(0)
            image = Image.open(upload)
        except (UnidentifiedImageError, OSError):
            return Response(
                {'error': 'File không phải ảnh hợp lệ.'}, status=status.HTTP_400_BAD_REQUEST,
            )

        if image.format not in ALLOWED_FORMATS:
            return Response(
                {'error': 'Chỉ nhận JPG, PNG hoặc WEBP.'}, status=status.HTTP_400_BAD_REQUEST,
            )

        image_format = image.format
        extension = ALLOWED_FORMATS[image_format]
        if image.mode not in ('RGB', 'RGBA'):
            image = image.convert('RGB')
        image.thumbnail((MAX_EDGE_PX, MAX_EDGE_PX), Image.LANCZOS)

        buffer = io.BytesIO()
        save_format = 'JPEG' if image_format == 'JPEG' else image_format
        if save_format == 'JPEG' and image.mode == 'RGBA':
            image = image.convert('RGB')
        image.save(buffer, format=save_format, quality=88)

        preference = _get_preference(request.user)
        if preference.custom_bg:
            preference.custom_bg.delete(save=False)
        preference.custom_bg.save(
            f'background.{extension}', ContentFile(buffer.getvalue()), save=False,
        )
        preference.theme_id = 'custom'
        preference.save(update_fields=['custom_bg', 'theme_id', 'updated_at'])

        return Response(PreferenceSerializer(preference).data)
