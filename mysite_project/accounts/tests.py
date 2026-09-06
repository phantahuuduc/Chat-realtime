"""Test cho tuỳ chọn hiển thị và tải ảnh nền (mục 7.1)."""

import io

from PIL import Image
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import UserPreference

User = get_user_model()


def make_image_bytes(size=(400, 300), image_format='PNG'):
    buffer = io.BytesIO()
    Image.new('RGB', size, (18, 24, 40)).save(buffer, format=image_format)
    return buffer.getvalue()


class PreferenceAPITest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user('pref_user', password='pass123')
        resp = self.client.post('/api/auth/login/', {
            'username': 'pref_user', 'password': 'pass123',
        })
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {resp.data["access_token"]}')

    def test_get_creates_default_preference(self):
        resp = self.client.get('/api/preferences/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['theme_id'], 'moscow-rain')
        self.assertIsNone(resp.data['custom_bg_url'])
        self.assertTrue(UserPreference.objects.filter(user=self.user).exists())

    def test_patch_updates_theme(self):
        resp = self.client.patch(
            '/api/preferences/', {'theme_id': 'deep-space'}, format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['theme_id'], 'deep-space')

    def test_only_background_fields_are_exposed(self):
        """Người dùng chỉ được đổi hình nền, không chỉnh được hiệu ứng."""
        resp = self.client.get('/api/preferences/')
        self.assertEqual(set(resp.data.keys()), {'theme_id', 'custom_bg_url'})

    def test_requires_authentication(self):
        anonymous = APIClient()
        self.assertIn(anonymous.get('/api/preferences/').status_code, [401, 403])


@override_settings(MEDIA_ROOT='/tmp/test-media')
class BackgroundUploadTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        User.objects.create_user('bg_user', password='pass123')
        resp = self.client.post('/api/auth/login/', {
            'username': 'bg_user', 'password': 'pass123',
        })
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {resp.data["access_token"]}')

    def _post(self, upload):
        return self.client.post(
            '/api/preferences/background/', {'image': upload}, format='multipart',
        )

    def test_valid_png_is_accepted(self):
        upload = SimpleUploadedFile('bg.png', make_image_bytes(), 'image/png')
        resp = self._post(upload)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['custom_bg_url'].endswith('.png'))
        self.assertEqual(resp.data['theme_id'], 'custom')

    def test_fake_image_is_rejected(self):
        """Đuôi .jpg nhưng nội dung là text — Pillow phải bắt được."""
        upload = SimpleUploadedFile('fake.jpg', b'khong phai anh', 'image/jpeg')
        resp = self._post(upload)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('không phải ảnh', resp.data['error'])

    def test_oversized_file_is_rejected(self):
        upload = SimpleUploadedFile('big.png', b'x' * (5 * 1024 * 1024 + 1), 'image/png')
        resp = self._post(upload)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('5MB', resp.data['error'])

    def test_missing_file_is_rejected(self):
        resp = self.client.post('/api/preferences/background/', {}, format='multipart')
        self.assertEqual(resp.status_code, 400)

    def test_large_image_is_downscaled(self):
        upload = SimpleUploadedFile(
            'huge.png', make_image_bytes(size=(3000, 2000)), 'image/png',
        )
        resp = self._post(upload)
        self.assertEqual(resp.status_code, 200)

        preference = UserPreference.objects.get(user__username='bg_user')
        with Image.open(preference.custom_bg.path) as image:
            self.assertLessEqual(max(image.size), 1920)
