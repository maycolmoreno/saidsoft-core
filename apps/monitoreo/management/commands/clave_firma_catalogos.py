"""Clave Ed25519 con la que `web` firma los catálogos del agente (ver apps.monitoreo.firma_catalogo).

    python manage.py clave_firma_catalogos             # muestra la pública de la clave configurada
    python manage.py clave_firma_catalogos --generar   # genera un par nuevo (no toca nada)

`--generar` solo imprime: no escribe el `.env` ni cambia la clave en uso. Se corre en el
NUC, para que la privada no salga de ahí salvo hacia la copia en KeePass:

1. `docker compose exec web python manage.py clave_firma_catalogos --generar`
2. La línea `CATALOGO_FIRMA_PRIVADA=...` va a `deploy/.env` y a KeePass.
3. La pública se pega en `CLAVES_PUBLICAS_CATALOGO` del agente antes de compilarlo.
4. `docker compose up -d web` para que `web` tome la variable.
5. Sin argumentos, este comando muestra la pública de la clave cargada: tiene que
   coincidir con la del agente.

Perder la privada obliga a redistribuir el agente con una pública nueva. Por eso la
copia en KeePass no es opcional.
"""
from django.core.management.base import BaseCommand

from apps.monitoreo import firma_catalogo


class Command(BaseCommand):
    help = 'Muestra la clave pública de firma de catálogos, o genera un par nuevo (--generar).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--generar', action='store_true',
            help='Genera un par nuevo y lo muestra. No cambia la clave en uso.',
        )

    def handle(self, *args, **options):
        if options['generar']:
            privada, publica = firma_catalogo.generar()
            self.stdout.write('Clave NUEVA (todavía no está en uso).')
            self.stdout.write('')
            self.stdout.write('Privada: va a deploy/.env y a KeePass. No la pegues en otro lado.')
            self.stdout.write(f'CATALOGO_FIRMA_PRIVADA={privada}')
            self.stdout.write('')
            self.stdout.write('Pública: va en CLAVES_PUBLICAS_CATALOGO del agente.')
            self.stdout.write(publica)
            return
        publica = firma_catalogo.clave_publica()
        if publica is None:
            self.stdout.write(self.style.WARNING(
                'CATALOGO_FIRMA_PRIVADA no está configurada (o no es válida): los catálogos salen '
                'solo con la firma HMAC.'
            ))
            return
        self.stdout.write('Clave pública en uso (tiene que coincidir con la del agente):')
        self.stdout.write(publica)
