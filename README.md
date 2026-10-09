# Txoko · Inventario local

Aplicación web de inventario, consumos, deudas, bote e historial compartido. El servidor y la base de datos se ejecutan localmente con Python y SQLite. No usa Firebase, Google Login, reCAPTCHA, Cloud Functions ni dependencias de Python externas.

## 1. Probar en el ordenador

Instala Python 3 y, desde la carpeta del proyecto, arranca el servidor:

```sh
python3 server.py
```

En Windows, si `python3` no existe, usa `python server.py`. Abre <http://127.0.0.1:8080> en el navegador del mismo ordenador.

Por seguridad, el servidor solo escucha en `127.0.0.1` por defecto: nadie más en la red podrá conectarse. La primera vez, la consola de Python muestra un **token inicial del presidente**. En la web, abre **Acceder / registrarse → Crear cuenta** y crea tu cuenta con nombre, apellidos, usuario y un PIN numérico de 6 a 12 cifras; introduce también el token. Esa primera cuenta obtiene el rol de presidente.

Mantén abierta la consola mientras pruebas la aplicación. Para apagar el servidor, vuelve a esa consola y pulsa `Ctrl+C`. La base de datos y el token temporal se guardan en `~/.txoko/`, fuera de la carpeta del proyecto y del repositorio Git.

## 2. Subir el código a GitHub

Crea un repositorio vacío en GitHub. Desde la carpeta del proyecto, sube el código:

```sh
git init
git add .
git commit -m "Crear servidor local de Txoko"
git branch -M main
git remote add origin https://github.com/TU-USUARIO/TU-REPOSITORIO.git
git push -u origin main
```

Sustituye `TU-USUARIO` y `TU-REPOSITORIO`. GitHub guarda el código, pero no arranca el servidor. **No subas la base de datos ni el token del presidente**: ambos están en `~/.txoko/`, fuera del proyecto.

## 3. Ejecutar en Termux en el móvil

En Termux, instala Python y Git, clona el repositorio y arranca la aplicación:

```sh
pkg update
pkg install python git
git clone https://github.com/TU-USUARIO/TU-REPOSITORIO.git
cd TU-REPOSITORIO
python server.py --host 0.0.0.0
```

El móvil que ejecuta ese comando es el **único servidor y dueño de la base de datos compartida**. Abre `http://localhost:8080` en el navegador de ese mismo móvil. Los otros miembros no ejecutan `server.py` ni crean otra base: abren la dirección IP de ese móvil servidor en sus navegadores.

La **primera vez que arranques el servidor en Termux**, la consola mostrará un token inicial. Crea allí la cuenta de presidente. El token y la base del ordenador son distintos: si quieres conservar los datos de la prueba, copia antes la base de datos del ordenador a Termux como se explica abajo; luego usa el usuario y PIN ya existentes, y no registres otra cuenta de presidente.

### Dar acceso a los otros móviles

Para usarlo desde otros móviles conectados a la misma Wi-Fi, deja el servidor abierto y averigua la IP local del teléfono servidor:

```sh
ip addr show wlan0
```

En **todos los otros móviles y ordenadores**, abre la misma URL, sustituyendo la IP; por ejemplo `http://192.168.1.23:8080`. Esa URL conecta con la base única de Termux. No uses `localhost` en otros dispositivos: `localhost` siempre significa “este mismo dispositivo”. Si Android o la red bloquea la conexión, comprueba que Termux tenga permiso de red local y que todos estén conectados a la misma Wi-Fi.

El teléfono servidor debe permanecer encendido, conectado a la Wi-Fi y con Termux activo para que los demás compartan el inventario. Si necesitas la orden `termux-wake-lock`, instala la utilidad con `pkg install termux-api` y también la aplicación Android **Termux:API** desde la misma fuente que Termux; después ejecuta `termux-wake-lock` en otra sesión de Termux para pedir a Android que no suspenda la CPU. Configura además Android para permitir actividad en segundo plano y quitar la optimización de batería a Termux. Para parar el servidor, usa `Ctrl+C`; al detenerlo, los demás dispositivos dejarán de tener acceso hasta que lo arranques otra vez.

El acceso por Wi-Fi usa HTTP: utilízalo solo en una red de confianza y **no publiques el puerto directamente en Internet**. Para entrar desde fuera de casa sin abrir el puerto, instala y configura una VPN privada como Tailscale en el móvil servidor y los dispositivos miembros; conectados a esa VPN, todos siguen usando la IP privada del servidor.

## La base de datos única y cómo trasladarla desde el ordenador

En funcionamiento normal solo importa la base del móvil servidor: `~/.txoko/txoko.sqlite3`. Los navegadores de los miembros no guardan una copia del inventario. La prueba del ordenador sí tiene otra base, porque la creó el servidor del ordenador; no se sincroniza automáticamente con Termux.

Si quieres que los datos introducidos durante la prueba en el ordenador aparezcan también en Termux, traslada **una sola vez** el archivo SQLite antes de iniciar el servidor en el móvil. En Linux, puedes copiarlo por una conexión SSH/SFTP al almacenamiento privado de Termux; también puedes usar un cable USB y `termux-setup-storage`. Colócalo en `~/.txoko/txoko.sqlite3` en Termux. Haz una copia de respaldo y detén el servidor antes de copiarlo. No copies la carpeta de datos a GitHub.

Si no trasladamos esa base, al iniciar Termux por primera vez se creará una base nueva en el móvil: en ese caso hay que crear el presidente y los productos en esa base nueva. A partir de entonces todos usan el mismo servidor del móvil.

Para respaldar la base de datos activa, lo más sencillo es detener el servidor con `Ctrl+C` y copiar el archivo:

```sh
cp ~/.txoko/txoko.sqlite3 ~/txoko-respaldo.sqlite3
```

La biblioteca de imágenes, vídeos y audio permanece en el navegador donde se añadió; no forma parte de la base SQLite ni se transfiere junto con ella.

## Cuentas e invitados

Las cuentas locales usan nombre, apellidos, usuario y un PIN numérico. El PIN se almacena como hash con sal; no se guarda en texto claro. Las sesiones permanecen activas hasta cerrar sesión o hasta que transcurren 30 días. Ocho intentos fallidos desde la misma IP bloquean nuevos intentos durante 15 minutos.

El presidente puede desactivar cuentas de miembros. Se conserva su historial y sus deudas, pero ya no pueden iniciar sesión. Si aún no hay presidente y pierdes el token inicial, desde la carpeta del proyecto ejecuta:

```sh
python3 server.py --reset-setup-token
```

No compartas ese token: quien lo use primero será presidente.

Los invitados pueden consumir sin crear una cuenta. La primera vez ponen nombre y apellidos; después pueden seleccionarse en una lista. Tal como se eligió, no se pide PIN a invitados, así que alguien podría consultar o avisar una deuda usando el nombre de otra persona. El presidente debe verificar cualquier pago en persona antes de confirmarlo. Invitados con el mismo nombre y apellidos comparten su historial.

## Bote y gastos del Txoko

El bote suma únicamente los consumos cuyo pago haya confirmado el presidente; las deudas y los avisos de pago pendientes no se consideran dinero ingresado. En la pestaña **Bote**, cualquier miembro con cuenta activa puede registrar una compra indicando concepto e importe. La solicitud queda pendiente y no resta saldo hasta que el presidente la apruebe. Al aprobarla, el gasto queda asociado al solicitante y al presidente que lo revisó y se descuenta del saldo disponible. El servidor impide aprobar un gasto si el bote no tiene fondos suficientes. El presidente también puede rechazar una solicitud; el historial y el estado de cada solicitud quedan visibles para los miembros con sesión iniciada.

## Pruebas automáticas

Con Python 3, desde la carpeta del proyecto:

```sh
python3 -m unittest discover -s tests -v
```
