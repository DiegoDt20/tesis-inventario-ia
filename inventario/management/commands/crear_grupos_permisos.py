"""Comando de gestión: crea (o actualiza) los grupos de usuarios
"administrador" y "operador" con sus permisos.

No crea usuarios ni toca el flag is_staff, que es lo que de verdad controla
el acceso al panel de administración de Django (es un atributo por usuario,
no de grupo): eso se hace al crear cada usuario (por ejemplo con
"createsuperuser", o marcando is_staff manualmente para un administrador
desde el propio admin). Este comando solo deja los grupos listos para
asignárselos a los usuarios que correspondan.
"""
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand

# Permisos exactos del grupo "operador": puede registrar pedidos,
# movimientos y conteos (y actualizar los que ya existen, para completar la
# atención de un pedido o continuar un conteo guardado a medias), pero no
# puede borrar nada ni tocar el resto del sistema (predicciones,
# recomendaciones, anomalías, catálogo de productos, etc. — de eso se
# encarga "administrador").
PERMISOS_OPERADOR = [
    ('pedido', ['add', 'change', 'view']),
    ('pedidodetalle', ['add', 'change', 'view']),
    ('movimiento', ['add', 'view']),
    ('conteofisico', ['add', 'change', 'view']),
    ('conteodetalle', ['add', 'change', 'view']),
    ('producto', ['view']),
]


class Command(BaseCommand):
    help = (
        'Crea o actualiza los grupos "administrador" (acceso total a los '
        'modelos de inventario) y "operador" (solo pedidos, movimientos y '
        'conteos, sin poder borrar nada).'
    )

    def handle(self, *args, **options):
        self._crear_administrador()
        self._crear_operador()
        self.stdout.write(self.style.WARNING(
            '\nRecuerda: este comando no crea usuarios ni cambia is_staff. '
            'Un usuario del grupo "administrador" solo entra al panel de '
            'administración de Django si además tiene is_staff=True (o es '
            'superusuario); el grupo "operador" nunca debe tenerlo.'
        ))

    def _crear_administrador(self):
        grupo, _ = Group.objects.get_or_create(name='administrador')
        permisos = Permission.objects.filter(content_type__app_label='inventario')
        grupo.permissions.set(permisos)
        self.stdout.write(self.style.SUCCESS(
            f'Grupo "administrador": {permisos.count()} permiso(s) (todos los de inventario).'
        ))

    def _crear_operador(self):
        grupo, _ = Group.objects.get_or_create(name='operador')
        permisos = []
        faltantes = []
        for nombre_modelo, acciones in PERMISOS_OPERADOR:
            try:
                content_type = ContentType.objects.get(app_label='inventario', model=nombre_modelo)
            except ContentType.DoesNotExist:
                faltantes.append(nombre_modelo)
                continue
            for accion in acciones:
                codename = f'{accion}_{nombre_modelo}'
                try:
                    permisos.append(Permission.objects.get(content_type=content_type, codename=codename))
                except Permission.DoesNotExist:
                    faltantes.append(codename)

        grupo.permissions.set(permisos)
        self.stdout.write(self.style.SUCCESS(f'Grupo "operador": {len(permisos)} permiso(s).'))
        if faltantes:
            self.stdout.write(self.style.WARNING(
                f'No se encontraron estos permisos (revisa que las migraciones estén al día): '
                f'{", ".join(faltantes)}'
            ))
