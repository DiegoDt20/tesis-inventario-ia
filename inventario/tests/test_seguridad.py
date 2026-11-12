"""Tests del bloqueo del inicio de sesión por intentos fallidos y de la
configuración para publicar el sistema por HTTPS."""
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse


@override_settings(LOGIN_MAX_INTENTOS=3, LOGIN_BLOQUEO_MINUTOS=15)
class BloqueoLoginTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        User.objects.create_user('ana', password='clave-correcta-1')
        User.objects.create_user('luis', password='clave-correcta-2')
        self.url = reverse('login')

    def _ingresar(self, usuario, clave):
        return self.client.post(self.url, {'username': usuario, 'password': clave})

    def test_ingreso_correcto(self):
        self.assertRedirects(
            self._ingresar('ana', 'clave-correcta-1'), reverse('inventario:dashboard'),
            fetch_redirect_response=False,
        )

    def test_se_bloquea_tras_el_maximo_de_fallos(self):
        for _ in range(3):
            respuesta = self._ingresar('ana', 'mala')
            self.assertContains(respuesta, 'Usuario o contraseña incorrectos.')

        # Ya bloqueado: ni la contraseña correcta entra.
        respuesta = self._ingresar('ana', 'clave-correcta-1')
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, 'Demasiados intentos fallidos. Espera 15 minutos')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_no_distingue_mayusculas_ni_espacios_del_usuario(self):
        for usuario in ('ana', 'ANA', ' Ana '):
            self._ingresar(usuario, 'mala')
        self.assertContains(self._ingresar('ana', 'clave-correcta-1'), 'Demasiados intentos fallidos')

    def test_el_bloqueo_es_por_usuario(self):
        for _ in range(3):
            self._ingresar('ana', 'mala')
        self.assertEqual(self._ingresar('luis', 'clave-correcta-2').status_code, 302)

    def test_un_ingreso_correcto_reinicia_el_contador(self):
        self._ingresar('ana', 'mala')
        self._ingresar('ana', 'mala')
        self.assertEqual(self._ingresar('ana', 'clave-correcta-1').status_code, 302)
        self.client.logout()
        self._ingresar('ana', 'mala')
        self._ingresar('ana', 'mala')
        self.assertEqual(self._ingresar('ana', 'clave-correcta-1').status_code, 302)

    def test_tambien_cuenta_usuarios_que_no_existen(self):
        for _ in range(3):
            self._ingresar('nadie', 'x')
        self.assertContains(self._ingresar('nadie', 'x'), 'Demasiados intentos fallidos')


class EstaticosSinDebugTests(TestCase):
    @override_settings(DEBUG=False)
    def test_los_estilos_se_sirven_con_debug_apagado(self):
        respuesta = self.client.get('/static/inventario/css/estilos.css')
        self.assertEqual(respuesta.status_code, 200)
