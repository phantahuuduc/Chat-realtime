"""Biến dùng chung cho template."""

from django.conf import settings


def asset_version(request):
    """
    Dấu vân tay của CSS/JS, gắn làm `?v=` cho mọi <link>/<script>.

    Tên file static không được băm (mục 2.2 cấm ManifestStaticFilesStorage),
    nên nếu không có tham số này, trình duyệt đã cache bản cũ sẽ dùng lại
    mãi — JS cũ chạy trên HTML mới là nguồn gốc của lỗi trang trắng.
    """
    return {'asset_version': settings.ASSET_VERSION}
