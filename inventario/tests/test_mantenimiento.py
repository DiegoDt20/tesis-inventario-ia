"""Tests de los comandos de mantenimiento: respaldo de la base (sin llamar
a pg_dump de verdad) y ciclo diario (sin correr los cuatro pasos)."""
import subprocess
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from ..management.commands import ciclo_diario, respaldar_base


def _pg_falso(falla=None):
    """Sustituto de subprocess.run: pg_dump crea el archivo que se le pide;
    'falla' es el nombre del programa que debe terminar con error."""
    llamadas = []

    def ejecutar(comando, env=None, **_):
        llamadas.append((comando, env))
        if comando[0] == falla:
            return subprocess.CompletedProcess(comando, 1, '', 'conexión rechazada')
        if comando[0] == 'pg_dump':
            Path(comando[comando.index('--file') + 1]).write_bytes(b'PGDMP')
        return subprocess.CompletedProcess(comando, 0, '', '')

    return ejecutar, llamadas


class RespaldarBaseTests(SimpleTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def _respaldar(self, falla=None, **opciones):
        ejecutar, llamadas = _pg_falso(falla)
        salida = StringIO()
        with mock.patch.object(respaldar_base.subprocess, 'run', ejecutar):
            call_command('respaldar_base', destino=str(self.dir), stdout=salida, **opciones)
        return salida.getvalue(), llamadas

    def test_crea_el_respaldo_y_lo_verifica(self):
        texto, llamadas = self._respaldar()
        archivos = list(self.dir.iterdir())
        self.assertEqual(len(archivos), 1)
        self.assertRegex(archivos[0].name, r'^auto_\d{8}_\d{6}\.dump$')
        self.assertEqual([c[0][0] for c in llamadas], ['pg_dump', 'pg_restore'])
        self.assertIn('--format=custom', llamadas[0][0])
        self.assertIn('Respaldo guardado', texto)
        self.assertIn('pg_restore --clean', texto)

    def test_la_contrasena_va_por_entorno_no_en_el_comando(self):
        with mock.patch.dict(settings.DATABASES['default'], {'PASSWORD': 'secreto'}):
            _, llamadas = self._respaldar()
        comando, entorno = llamadas[0]
        self.assertNotIn('secreto', ' '.join(comando))
        self.assertEqual(entorno['PGPASSWORD'], 'secreto')

    def test_si_pg_dump_falla_no_queda_ningun_archivo(self):
        with self.assertRaisesMessage(CommandError, 'pg_dump falló: conexión rechazada'):
            self._respaldar(falla='pg_dump')
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_si_el_respaldo_no_se_puede_leer_se_descarta(self):
        with self.assertRaisesMessage(CommandError, 'pg_restore falló'):
            self._respaldar(falla='pg_restore')
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_rota_solo_los_automaticos(self):
        for dia in range(1, 6):
            (self.dir / f'auto_2026010{dia}_000000.dump').write_bytes(b'x')
        manual = self.dir / 'respaldo_antes_postest_20261029_0046.dump'
        manual.write_bytes(b'x')

        self._respaldar(conservar=3)

        nombres = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(len(nombres), 4)
        self.assertIn(manual.name, nombres)
        self.assertEqual(nombres[:2], ['auto_20260104_000000.dump', 'auto_20260105_000000.dump'])

    def test_un_respaldo_etiquetado_no_rota_nada(self):
        for dia in range(1, 4):
            (self.dir / f'auto_2026010{dia}_000000.dump').write_bytes(b'x')
        self._respaldar(etiqueta='Antes del postest', conservar=1)
        nombres = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(len(nombres), 4)
        self.assertRegex(nombres[-1], r'^respaldo_antes_del_postest_\d{8}_\d{6}\.dump$')

    def test_conservar_invalido(self):
        with self.assertRaisesMessage(CommandError, '--conservar debe ser 1 o más'):
            self._respaldar(conservar=0)


class CicloDiarioTests(SimpleTestCase):
    def _ciclo(self, falla_en=None, **opciones):
        llamadas = []

        def falso(comando, **argumentos):
            argumentos.pop('stdout', None)
            argumentos.pop('stderr', None)
            llamadas.append((comando, argumentos))
            if comando == falla_en:
                raise CommandError('sin modelo activo')

        salida = StringIO()
        with mock.patch.object(ciclo_diario, 'call_command', falso):
            try:
                call_command('ciclo_diario', stdout=salida, **opciones)
            finally:
                self.llamadas = llamadas
        return salida.getvalue()

    def test_corre_los_cuatro_pasos_en_orden_con_el_mismo_origen(self):
        texto = self._ciclo()
        self.assertEqual(self.llamadas, [
            ('predecir_demanda', {'dias': 30, 'origen': 'real'}),
            ('generar_recomendaciones', {'origen': 'real'}),
            ('detectar_anomalias', {'origen': 'real'}),
            ('indexar_conocimiento', {}),
        ])
        self.assertIn('Ciclo completo (4 pasos', texto)

    def test_sin_indexar(self):
        self._ciclo(sin_indexar=True, dias=7)
        self.assertEqual([c for c, _ in self.llamadas],
                         ['predecir_demanda', 'generar_recomendaciones', 'detectar_anomalias'])
        self.assertEqual(self.llamadas[0][1]['dias'], 7)

    def test_si_un_paso_falla_no_corren_los_siguientes(self):
        with self.assertRaisesMessage(
            CommandError,
            'Falló "generar_recomendaciones": sin modelo activo. Completados: predecir_demanda. '
            'Sin ejecutar: detectar_anomalias, indexar_conocimiento.',
        ):
            self._ciclo(falla_en='generar_recomendaciones')
        self.assertEqual([c for c, _ in self.llamadas], ['predecir_demanda', 'generar_recomendaciones'])

    @override_settings(ASISTENTE_ORIGEN='real')
    def test_avisa_si_el_asistente_indexa_otro_origen(self):
        self.assertIn('El asistente indexa el origen "real"', self._ciclo(origen='prueba'))
