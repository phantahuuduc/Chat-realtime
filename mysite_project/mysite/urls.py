"""
URL configuration — Real-time Chat Platform.
"""
from django.contrib import admin
from django.urls import include, path
from django.views.generic import TemplateView

urlpatterns = [
    path('admin/', admin.site.urls),

    # Auth API
    path('api/auth/', include('accounts.urls')),

    # Chat API
    path('api/', include('chat.urls')),

    # Chat UI
    path('', TemplateView.as_view(template_name='chat.html'), name='chat'),
]
