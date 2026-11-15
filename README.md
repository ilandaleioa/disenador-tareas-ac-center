# AC FOOTBALL CENTER · Diseñador de Ejercicios

Aplicación local para diseñar tareas y ejercicios de entrenamiento sobre un campo de fútbol, con un asistente de IA opcional (API de Claude).

## Requisitos

- [Git](https://git-scm.com/download/win)
- [Python 3.9 o superior](https://www.python.org/downloads/) (marca "Add Python to PATH" al instalar)
- Opcional: una clave de la API de Claude (para el asistente de IA)
- Opcional: [ffmpeg](https://ffmpeg.org/download.html) (para convertir vídeo)

## Instalación

Abre PowerShell y ejecuta:

```powershell
git clone https://github.com/ilandaleioa/disenador-tareas-ac-center.git
cd disenador-tareas-ac-center
pip install -r requirements.txt
```

## Configuración

1. Copia el archivo de ejemplo:

   ```powershell
   Copy-Item env.example .env
   ```

2. Abre `.env` con el Bloc de notas y rellena `ANTHROPIC_API_KEY` con tu clave.
   Si no tienes clave, déjala vacía: el diseñador funciona igual, pero sin IA.

| Variable | Obligatoria | Descripción |
|---|---|---|
| `ANTHROPIC_API_KEY` | No | Clave de la API de Claude. Activa el asistente de IA. |
| `ANTHROPIC_MODEL` | No | Modelo a usar (`claude-opus-5-5` por defecto). |
| `PORT` | No | Puerto local (`8090` por defecto). |
| `FFMPEG` | No | Ruta a `ffmpeg.exe` si no está en el PATH. |

> **Seguridad:** el archivo `.env` está en `.gitignore` y nunca debe subirse a GitHub ni compartirse por chat o correo. Cada persona usa su propia clave.

## Uso

```powershell
python servidor.py
```

Abre en el navegador: <http://127.0.0.1:8090>

Para parar el servidor, pulsa `Ctrl+C` en la terminal. También puedes elegir otro puerto: `python servidor.py 8091`.

Hay una guía para usuarios en <http://127.0.0.1:8090/tutorial.html> (con el servidor arrancado).

## Datos

Las tareas, consignas y el escudo se guardan en `ac-center-ejercicios.json`, en esta carpeta. Ese archivo **no se sube al repositorio** (está en `.gitignore`), así que cada instalación tiene los suyos. Para pasar ejercicios a otra persona, usa un canal interno autorizado del club, no GitHub.

## Seguridad

- El servidor solo escucha en `127.0.0.1` (únicamente accesible desde tu propio equipo).
- Comprueba las cabeceras `Host` y `Origin`, limita el tamaño y el ritmo de las peticiones y solo acepta campos conocidos.
- La clave de la API vive solo en el servidor, nunca en el navegador.
- A la IA solo se envían posiciones y tipos de elementos genéricos, sin nombres de jugadores ni datos de partidos.

## Contribuir

Tras aceptar la invitación de colaborador en GitHub:

```powershell
git pull origin main
git add .
git commit -m "Descripción del cambio"
git push origin main
```

Haz `git pull` antes de empezar a trabajar para no pisar cambios de otras personas.
