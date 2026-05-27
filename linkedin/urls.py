# linkedin/urls.py
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path

from linkedin.api.local_metrics import connect_counts_today

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/local/connect-counts/", connect_counts_today, name="connect-counts-today"),
]

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
