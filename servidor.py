#!/usr/bin/env python3
"""Servidor local del Diseñador de Ejercicios con asistente de IA (API de Claude).

Uso (PowerShell):
    $env:ANTHROPIC_API_KEY = "tu-clave"     # o crea un archivo .env con la línea ANTHROPIC_API_KEY=tu-clave
    python servidor.py                       # abre http://127.0.0.1:8090

Opcional: ANTHROPIC_MODEL=claude-sonnet-5-5 (o claude-haiku-5-5) para gastar menos.

Qué hace:
  - Sirve únicamente index.html (no expone .env, .git ni el resto de archivos).
  - Expone POST /api/asistente: recibe la instrucción y el estado del campo,
    llama a Claude con la clave guardada AQUÍ (nunca en el navegador) y devuelve
    el campo resultante ya validado y saneado.

Seguridad: solo escucha en 127.0.0.1, comprueba Host y Origin (anti-CSRF y
DNS-rebinding), limita tamaño y ritmo de peticiones, y solo acepta campos
conocidos. Al modelo solo se envían posiciones y tipos de elementos genéricos
(sin nombres de jugadores ni datos de partidos).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else int(os.environ.get("PORT", "8090"))
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
MAX_BODY = 40_000          # bytes
MAX_INSTRUCTION = 600      # caracteres
MAX_ITEMS = 80
MAX_ARROWS = 60
RATE_LIMIT = 20            # peticiones por minuto (global; uso local)

ITEM_KINDS = ["player", "ball", "cone", "flat", "goal", "minigoal", "pole", "ring",
              "mannequin", "hurdle", "tallhurdle", "ladder", "text"]
ARROW_KINDS = ["pass", "dribble", "run"]
KITS = ["color", "athletic"]
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

SYSTEM_PROMPT = """Eres el asistente de un diseñador de ejercicios tácticos de fútbol del Athletic Club.
Recibes la instrucción del entrenador y el estado actual del campo, y devuelves el estado COMPLETO resultante.

Reglas del campo:
- Coordenadas en porcentaje: x de 0 (izquierda) a 100 (derecha), y de 0 (arriba) a 100 (abajo). El centro es (50, 50).
- Los elementos que ya existen conservan su "id". Los nuevos llevan un id inventado y único (por ejemplo "n1", "n2").
  Los elementos que no aparezcan en tu respuesta se eliminarán del campo, así que repite todos los que deban quedarse.
- kind de elemento: player, ball, cone, flat (seta), goal (portería), minigoal, pole (pica), ring (aro),
  mannequin (maniquí), hurdle (valla), tallhurdle, ladder (escalera), text (rótulo).
- player: "num" del 1 al 11 y "kit": "athletic" (equipo rojiblanco) o "color" (el otro equipo). El portero es el 1.
  Un mismo kit no debe repetir num. Usa "dir" (grados, 0 = hacia arriba, 90 = hacia la derecha) solo para orientar.
- cone: "color" en formato #rrggbb (rojo #ef4444, azul #3b82f6, verde #22c55e). text: usa "text" (máximo 40 caracteres).
- Flechas ("arrows"): kind "pass" (pase), "dribble" (conducción) o "run" (desplazamiento); "curve" true para trazo curvo.
  Van de (x1, y1) a (x2, y2). Los ids de flechas existentes se conservan.
- Pon null en los campos que no apliquen a un elemento.
- "message": una frase corta en español que explique qué has hecho.
Diseña ejercicios realistas, con espacios y distancias coherentes con el tipo de campo indicado. Si la instrucción no tiene
relación con el diseño del ejercicio, no cambies nada y explícalo en "message"."""

NUM_OR_NULL = {"type": ["number", "null"]}
STR_OR_NULL = {"type": ["string", "null"]}
RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "message": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string", "enum": ITEM_KINDS},
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "num": {"type": ["integer", "null"]},
                    "kit": STR_OR_NULL,
                    "color": STR_OR_NULL,
                    "dir": NUM_OR_NULL,
                    "text": STR_OR_NULL,
                },
                "required": ["id", "kind", "x", "y", "num", "kit", "color", "dir", "text"],
            },
        },
        "arrows": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string", "enum": ARROW_KINDS},
                    "curve": {"type": "boolean"},
                    "x1": {"type": "number"}, "y1": {"type": "number"},
                    "x2": {"type": "number"}, "y2": {"type": "number"},
                },
                "required": ["id", "kind", "curve", "x1", "y1", "x2", "y2"],
            },
        },
    },
    "required": ["message", "items", "arrows"],
}


# ---------------------------------------------------------------- configuración
def load_env_file():
    """Lee .env (CLAVE=valor) sin pisar variables ya definidas en el entorno."""
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- validación
def num(v, lo=0.0, hi=100.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return max(lo, min(hi, f))


def clean_request(body):
    """Valida y reduce la petición del navegador a campos conocidos."""
    if not isinstance(body, dict):
        raise ValueError("Petición no válida.")
    text = body.get("instruction")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Escribe una instrucción.")
    text = text.strip()[:MAX_INSTRUCTION]
    pitch = body.get("pitch") if isinstance(body.get("pitch"), str) else "libre"
    pitch = re.sub(r"[^A-Za-z0-9_\- áéíóúñ]", "", pitch)[:30] or "libre"
    items, arrows = [], []
    for it in (body.get("items") or [])[:MAX_ITEMS]:
        if not isinstance(it, dict) or it.get("kind") not in ITEM_KINDS:
            continue
        iid = it.get("id")
        x, y = num(it.get("x")), num(it.get("y"))
        if not isinstance(iid, str) or not ID_RE.match(iid) or x is None or y is None:
            continue
        row = {"id": iid, "kind": it["kind"], "x": round(x, 1), "y": round(y, 1)}
        if it["kind"] == "player":
            if isinstance(it.get("num"), int) and 1 <= it["num"] <= 11:
                row["num"] = it["num"]
            if it.get("kit") in KITS:
                row["kit"] = it["kit"]
        if isinstance(it.get("color"), str) and COLOR_RE.match(it["color"]):
            row["color"] = it["color"]
        d = num(it.get("dir"), -360, 360)
        if d is not None:
            row["dir"] = round(d)
        items.append(row)
    for a in (body.get("arrows") or [])[:MAX_ARROWS]:
        if not isinstance(a, dict) or a.get("kind") not in ARROW_KINDS:
            continue
        aid = a.get("id")
        c = [num(a.get(k)) for k in ("x1", "y1", "x2", "y2")]
        if not isinstance(aid, str) or not ID_RE.match(aid) or None in c:
            continue
        arrows.append({"id": aid, "kind": a["kind"], "curve": bool(a.get("curve")),
                       "x1": round(c[0], 1), "y1": round(c[1], 1), "x2": round(c[2], 1), "y2": round(c[3], 1)})
    return text, pitch, items, arrows


def clean_result(data):
    """Sanea la respuesta del modelo antes de devolverla al navegador."""
    out_items, out_arrows = [], []
    seen = set()
    for it in (data.get("items") or [])[:MAX_ITEMS]:
        if not isinstance(it, dict) or it.get("kind") not in ITEM_KINDS:
            continue
        iid = it.get("id")
        x, y = num(it.get("x")), num(it.get("y"))
        if not isinstance(iid, str) or not ID_RE.match(iid) or iid in seen or x is None or y is None:
            continue
        seen.add(iid)
        row = {"id": iid, "kind": it["kind"], "x": round(x, 1), "y": round(y, 1)}
        if it["kind"] == "player":
            n = it.get("num")
            row["num"] = n if isinstance(n, int) and 1 <= n <= 11 else 1
            row["kit"] = it.get("kit") if it.get("kit") in KITS else "color"
        if isinstance(it.get("color"), str) and COLOR_RE.match(it["color"]):
            row["color"] = it["color"]
        d = num(it.get("dir"), -360, 360)
        if d is not None:
            row["dir"] = round(d)
        if it["kind"] == "text":
            t = it.get("text")
            row["text"] = (t if isinstance(t, str) else "")[:40]
        out_items.append(row)
    seen = set()
    for a in (data.get("arrows") or [])[:MAX_ARROWS]:
        if not isinstance(a, dict) or a.get("kind") not in ARROW_KINDS:
            continue
        aid = a.get("id")
        c = [num(a.get(k)) for k in ("x1", "y1", "x2", "y2")]
        if not isinstance(aid, str) or not ID_RE.match(aid) or aid in seen or None in c:
            continue
        seen.add(aid)
        out_arrows.append({"id": aid, "kind": a["kind"], "curve": bool(a.get("curve")),
                           "x1": round(c[0], 1), "y1": round(c[1], 1), "x2": round(c[2], 1), "y2": round(c[3], 1)})
    msg = data.get("message")
    msg = msg.strip()[:300] if isinstance(msg, str) and msg.strip() else "Hecho."
    return {"message": msg, "items": out_items, "arrows": out_arrows}


# ---------------------------------------------------------------- llamada a Claude
_client = None
_client_lock = threading.Lock()


def get_client():
    global _client
    with _client_lock:
        if _client is None:
            _client = anthropic.Anthropic(timeout=60.0, max_retries=2)  # lee ANTHROPIC_API_KEY
        return _client


def ask_claude(instruction, pitch, items, arrows):
    state = {"pitch": pitch, "items": items, "arrows": arrows}
    response = get_client().messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
        messages=[{
            "role": "user",
            "content": "Estado actual del campo (JSON):\n" + json.dumps(state, ensure_ascii=False)
                       + "\n\nInstrucción del entrenador:\n" + instruction,
        }],
    )
    if response.stop_reason == "refusal":
        raise PermissionError("La IA no ha podido atender esta petición.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("La respuesta de la IA era demasiado larga. Pide algo más sencillo.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    return clean_result(json.loads(text))


# ---------------------------------------------------------------- servidor HTTP
_hits = deque()
_hits_lock = threading.Lock()


def rate_ok():
    now = time.time()
    with _hits_lock:
        while _hits and now - _hits[0] > 60:
            _hits.popleft()
        if len(_hits) >= RATE_LIMIT:
            return False
        _hits.append(now)
        return True


DATA_FILE = ROOT / "ac-center-ejercicios.json"   # tareas, consignas y escudo
MAX_DATA = 5_000_000       # bytes (incluye el escudo en base64)
MAX_TASKS = 2000
MAX_REQS = 200
LOGO_RE = re.compile(r"^(data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+)?$")


def read_datos():
    try:
        d = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    return {"formato": "ac-center", "version": 2, "nombre": "AC CENTER", "escudo": "", "tareas": [], "consignas": []}


def clean_datos(body):
    if not isinstance(body, dict):
        raise ValueError("Formato no válido.")
    tareas, consignas, escudo = body.get("tareas"), body.get("consignas"), body.get("escudo", "")
    if not isinstance(tareas, list) or len(tareas) > MAX_TASKS or not all(isinstance(x, dict) for x in tareas):
        raise ValueError("Lista de tareas no válida.")
    if not isinstance(consignas, list) or len(consignas) > MAX_REQS or not all(isinstance(x, dict) for x in consignas):
        raise ValueError("Lista de consignas no válida.")
    if not isinstance(escudo, str) or len(escudo) > 300_000 or not LOGO_RE.match(escudo):
        raise ValueError("Escudo no válido.")
    return {"formato": "ac-center", "version": 2, "nombre": "AC CENTER", "escudo": escudo,
            "tareas": tareas, "consignas": consignas}


def write_datos(obj):
    DATA_FILE.parent.mkdir(exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, DATA_FILE)


# ---------------------------------------------------------------- vídeo (WebM -> MP4)
MAX_VIDEO = 200_000_000    # bytes de la grabación WebM
FFMPEG_TIMEOUT = 300       # segundos


def find_ffmpeg():
    """Busca ffmpeg en FFMPEG (ruta opcional en .env) o en el PATH."""
    exe = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    return exe if exe and Path(exe).is_file() else None


def webm_to_mp4(data):
    """Convierte la grabación WebM del navegador en un MP4 H.264 reproducible en cualquier reproductor."""
    exe = find_ffmpeg()
    if not exe:
        raise RuntimeError("ffmpeg no está instalado en este equipo (o no está en el PATH).")
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp, "entrada.webm"), Path(tmp, "salida.mp4")
        src.write_bytes(data)
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", "-an", str(dst)]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise RuntimeError("La conversión a MP4 tardó demasiado.")
        if r.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
            sys.stderr.write("ffmpeg terminó con código %s\n" % r.returncode)
            raise RuntimeError("No se pudo convertir el vídeo a MP4.")
        return dst.read_bytes()


ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}


class Handler(BaseHTTPRequestHandler):
    server_version = "Disenador/1.0"

    def log_message(self, fmt, *args):  # sin cuerpos ni cabeceras en el log
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _host_ok(self):
        return self.headers.get("Host", "") in ALLOWED_HOSTS

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "Host no permitido."})
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/estado":
            return self._json(200, {"ia": bool(os.environ.get("ANTHROPIC_API_KEY"))})
        if path == "/api/datos":
            return self._json(200, read_datos())
        self._json(404, {"error": "No encontrado."})

    def do_POST(self):
        if not self._host_ok() or self.headers.get("Origin", "") not in ALLOWED_ORIGINS:
            return self._json(403, {"error": "Origen no permitido."})
        if self.path.split("?", 1)[0] == "/api/datos":
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self._json(415, {"error": "Se esperaba JSON."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_DATA:
                return self._json(413, {"error": "Datos demasiado grandes."})
            try:
                write_datos(clean_datos(json.loads(self.rfile.read(length))))
            except (ValueError, UnicodeDecodeError) as e:
                return self._json(400, {"error": str(e) if isinstance(e, ValueError) and not isinstance(e, json.JSONDecodeError) else "Datos no válidos."})
            except OSError:
                return self._json(500, {"error": "No se pudo escribir el archivo de datos."})
            return self._json(200, {"ok": True})
        if self.path.split("?", 1)[0] == "/api/convertir-video":
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "video/webm":
                return self._json(415, {"error": "Se esperaba video/webm."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_VIDEO:
                return self._json(413, {"error": "Vídeo demasiado grande."})
            if not rate_ok():
                return self._json(429, {"error": "Demasiadas peticiones. Espera un momento."})
            data = self.rfile.read(length)
            try:
                mp4 = webm_to_mp4(data)
            except RuntimeError as e:
                return self._json(503 if "ffmpeg" in str(e) else 500, {"error": str(e)})
            except OSError:
                return self._json(500, {"error": "No se pudo ejecutar ffmpeg."})
            return self._send(200, mp4, "video/mp4")
        if self.path.split("?", 1)[0] != "/api/asistente":
            return self._json(404, {"error": "No encontrado."})
        if not (self.headers.get("Content-Type", "").split(";")[0].strip() == "application/json"):
            return self._json(415, {"error": "Se esperaba JSON."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY:
            return self._json(413, {"error": "Petición demasiado grande."})
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return self._json(503, {"error": "La IA no está configurada: falta ANTHROPIC_API_KEY en el servidor."})
        if not rate_ok():
            return self._json(429, {"error": "Demasiadas peticiones. Espera un momento."})
        try:
            instruction, pitch, items, arrows = clean_request(json.loads(self.rfile.read(length)))
        except (ValueError, UnicodeDecodeError) as e:
            return self._json(400, {"error": str(e) if isinstance(e, ValueError) and e.args and not isinstance(e, json.JSONDecodeError) else "Petición no válida."})
        try:
            return self._json(200, ask_claude(instruction, pitch, items, arrows))
        except anthropic.AuthenticationError:
            return self._json(502, {"error": "La clave de API no es válida."})
        except anthropic.RateLimitError:
            return self._json(429, {"error": "La API está saturada. Inténtalo en unos segundos."})
        except anthropic.APIConnectionError:
            return self._json(502, {"error": "No se pudo conectar con la API. Revisa la conexión."})
        except anthropic.APIStatusError as e:
            sys.stderr.write("Error API %s (request-id %s)\n" % (e.status_code, getattr(e, "request_id", "?")))
            return self._json(502, {"error": "La API devolvió un error (%s)." % e.status_code})
        except (PermissionError, RuntimeError) as e:
            return self._json(422, {"error": str(e)})
        except (json.JSONDecodeError, KeyError, TypeError):
            return self._json(502, {"error": "La IA devolvió una respuesta que no se pudo leer."})


def main():
    load_env_file()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("AVISO: ANTHROPIC_API_KEY no está definida. El diseñador funcionará, pero sin IA.")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Diseñador en http://{HOST}:{PORT}  (modelo: {MODEL})  — Ctrl+C para parar")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
