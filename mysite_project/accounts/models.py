"""Tuỳ chọn hiển thị của người dùng — đồng bộ giữa các thiết bị."""

from django.conf import settings
from django.db import models


def background_upload_path(instance, filename):
    return f'backgrounds/{instance.user_id}/{filename}'


class UserPreference(models.Model):
    """
    Một bản ghi mỗi user. Người dùng CHỈ được đổi hình nền — mọi thông số
    hiệu ứng do ứng dụng quyết định, không mở ra cho người dùng chỉnh.
    Client giữ bản sao trong localStorage để áp dụng tức thì; bản ghi này
    chỉ để đồng bộ giữa các thiết bị.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='preference',
    )
    theme_id = models.CharField(max_length=32, default='moscow-rain')
    custom_bg = models.ImageField(upload_to=background_upload_path, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Preference of {self.user}'
