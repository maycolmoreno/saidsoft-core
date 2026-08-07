from django.contrib import admin
from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path, re_path
from django.views.static import serve
from rest_framework.authtoken.views import obtain_auth_token

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/auth/token/', obtain_auth_token, name='api-auth-token'),
    path('api/v1/', include('apps.mantenimiento.api_urls')),
    path('', include('apps.panel.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
else:
    # static() devuelve [] con DEBUG=False, así que en producción nadie servía
    # /media/ y los agentes recibían 404 al descargar los .zip de despliegues
    # (encontrado en el primer despliegue real del piloto, 6-ago-2026). Mientras
    # no haya un proxy (nginx) delante que sirva media directo del volumen, lo
    # sirve Django: funciona, pero cada descarga ocupa un worker de gunicorn el
    # tiempo que dure — a escala real (~1.800 estaciones) esto necesita nginx o
    # similar, no solo la distribución en cascada por caché de farmacia.
    urlpatterns += [
        re_path(
            rf'^{settings.MEDIA_URL.lstrip("/")}(?P<path>.*)$',
            serve,
            {'document_root': settings.MEDIA_ROOT},
        ),
    ]
