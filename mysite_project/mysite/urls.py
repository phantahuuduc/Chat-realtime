"""
URL configuration — Real-time Chat Platform.
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic import TemplateView

from accounts import preferences

urlpatterns = [
    path('admin/', admin.site.urls),

    # Auth API
    path('api/auth/', include('accounts.urls')),

    # Tuỳ chọn hiển thị
    path('api/preferences/', preferences.PreferenceView.as_view(), name='preferences'),
    path(
        'api/preferences/background/',
        preferences.BackgroundUploadView.as_view(),
        name='preference-background',
    ),

    # Chat API
    path('api/', include('chat.urls')),

    # Chat UI
    path('', TemplateView.as_view(template_name='chat.html'), name='chat'),
]

# Ảnh nền do người dùng tải lên. Dự án chạy sau Daphne nên serve luôn ở đây;
# production thật sẽ đặt sau CDN/nginx.
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
