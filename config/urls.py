from django.contrib import admin
from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path
from rest_framework.authtoken.views import obtain_auth_token

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/auth/token/', obtain_auth_token, name='api-auth-token'),
    path('api/v1/', include('apps.mantenimiento.api_urls')),
    path('', include('apps.panel.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
