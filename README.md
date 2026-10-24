# Txoko · Inventario y cuentas

Aplicación web para el inventario, consumos, deudas, bote, miembros e historial multimedia. La web y la API se pueden desplegar en Vercel; los datos compartidos se guardan en Neon PostgreSQL. Para desarrollo también se mantiene el servidor local con SQLite. No requiere Firebase, Google Login ni reCAPTCHA.

## Desplegar la aplicación en Vercel

### 1. Preparar Neon

1. Crea un proyecto PostgreSQL en [Neon](https://neon.tech/).
2. Copia la cadena de conexión de PostgreSQL. Para una app serverless, utiliza la cadena con pooler que proporciona Neon.
3. No publiques la cadena ni la guardes en GitHub: es una contraseña.

El plan gratuito, los límites y la disponibilidad de Neon y Vercel pueden cambiar; revísalos al crear las cuentas.

### 2. Importar el proyecto en Vercel

1. Sube el repositorio a GitHub (si aún no está allí) e impórtalo en [Vercel](https://vercel.com/).
2. En **Project Settings → Environment Variables**, configura:
   - `DATABASE_URL`: cadena de conexión de Neon.
   - `TXOKO_SETUP_TOKEN`: un secreto aleatorio para el primer registro de presidente. Puedes generar uno con `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. Incluye las variables en el entorno **Production** y vuelve a desplegar si las agregas después de la primera publicación.

`pyproject.toml` señala explícitamente `app.py` como entrada FastAPI para que Vercel no confunda el servidor local `server.py` con la aplicación desplegada. `requirements.txt` instala las dependencias. La función sirve la web y la API; el backend elige PostgreSQL cuando encuentra `DATABASE_URL`. En Vercel no se debe usar SQLite como base compartida.

Abre el dominio que Vercel asigna, selecciona **Acceder / registrarse → Crear cuenta** y usa el valor de `TXOKO_SETUP_TOKEN` como token inicial. La primera cuenta será el presidente. Tras crearla, puedes eliminar `TXOKO_SETUP_TOKEN` de la configuración de Vercel y volver a desplegar; la base conservará el rol y ya no necesitará ese secreto.

### 3. Pasar los datos locales a Neon (opcional)

Haz la importación antes de usar la base de Neon con datos nuevos. El destino debe estar vacío. El script copia cuentas, sesiones, productos, invitados, consumos, gastos y límites de acceso; verifica el número de filas al terminar y no modifica el SQLite original.

Instala las dependencias en un entorno virtual y define temporalmente las variables de entorno en el ordenador. Usa la misma cadena de Neon y un `TXOKO_SETUP_TOKEN` temporal o el configurado en Vercel:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
read -rsp "URL de conexión de Neon: " DATABASE_URL; export DATABASE_URL; printf '\n'
read -rsp "Token inicial: " TXOKO_SETUP_TOKEN; export TXOKO_SETUP_TOKEN; printf '\n'
python migrate_sqlite_to_neon.py ~/.txoko/txoko.sqlite3
```

En Windows, activa `.venv\Scripts\Activate.ps1` y define las mismas variables de entorno en PowerShell antes de ejecutar el script. Detén el servidor local mientras se migra y no pegues las credenciales en archivos del proyecto ni en GitHub. La migración copia los hash de PIN y las sesiones existentes; los usuarios podrán seguir con sus cuentas y sesiones si la importación terminó correctamente.

Si no necesitas conservar el inventario local, omite este paso: la app creará las tablas de Neon automáticamente al recibir la primera solicitud. La base local del ordenador y la base del móvil Termux son independientes; migra el archivo de datos que quieras conservar.

## Desarrollo local (solo pruebas)

El uso compartido del Txoko es el dominio de Vercel; no hay que arrancar Termux ni dejar un servidor doméstico encendido. Vercel ejecuta la API cuando recibe peticiones y Neon conserva los datos. Para probar cambios en el ordenador, desde la carpeta del proyecto:

```sh
python3 server.py
```

Abre <http://127.0.0.1:8080>. Si `DATABASE_URL` no está definido, el servidor local utiliza SQLite en `~/.txoko/` y crea el token inicial en su consola. Para probar la app FastAPI localmente, instala `requirements-test.txt`, define `DATABASE_URL` y `TXOKO_SETUP_TOKEN`, y arranca `uvicorn app:app --reload`.

La base de datos que use el modo local no se sincroniza automáticamente con Neon. Para compartir datos entre los miembros, todos deben entrar al mismo dominio de Vercel.

## Funciones y datos

- **Inventario y deudas:** los miembros consumen productos; las cuentas de invitados se identifican con nombre y apellidos. El presidente confirma que recibió un pago.
- **Bote:** suma solo pagos confirmados. Los miembros pueden registrar compras y el presidente puede aprobarlas o rechazarlas; un gasto aprobado solo se descuenta si hay saldo suficiente.
- **Miembros:** el presidente puede retirar el acceso sin borrar el historial.
- **Archivo multimedia:** los archivos añadidos se guardan en el almacenamiento local del navegador que los subió, no en Neon. El despliegue hace que la sección esté disponible desde Vercel, pero esos archivos no se comparten entre dispositivos ni se respaldan allí.
- **Acceso:** las cuentas usan nombre, apellidos, usuario y PIN de 6 a 12 cifras. El PIN se almacena como hash con sal. Las sesiones usan cookies `HttpOnly`, `SameSite=Strict` y caducan a los 30 días.

Los invitados no tienen cuenta ni PIN, por lo que alguien podría consultar o avisar una deuda con el nombre de otra persona. El presidente debe verificar personalmente el pago antes de confirmarlo.

## Pruebas

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
python -m py_compile server.py app.py migrate_sqlite_to_neon.py
node --check app.js
```

No subas archivos `.sqlite3`, credenciales de Neon ni secretos al repositorio.
