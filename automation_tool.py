"""Outil d'automatisation local : crée et gère des fichiers datés dans un
répertoire dédié, avec une interface graphique colorée en façade.

Tourne en tâche de fond (thread séparé) pendant que la fenêtre tourne.
S'arrête via le bouton "Arrêter", la fermeture de la fenêtre, Ctrl+C,
le Gestionnaire des tâches, ou un fichier stop.txt dans le même dossier.
"""

import io
import json
import logging
import math
import os
import platform
import queue
import random
import signal
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import wave
from datetime import datetime, timedelta
from pathlib import Path

try:
    from PIL import Image, ImageOps, ImageTk
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

if IS_WINDOWS:
    import ctypes
    import winsound

# Toutes les données (config, cache, logs) vivent dans le profil utilisateur,
# pas à côté de l'exe : celui-ci reste un fichier unique, déplaçable partout.
if IS_WINDOWS:
    _base_data_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
elif IS_MAC:
    _base_data_dir = Path.home() / "Library" / "Application Support"
else:
    _base_data_dir = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))

DATA_DIR = _base_data_dir / "Classeur1"
CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / "automation_tool.log"
CLOWN_CACHE_DIR = DATA_DIR / "clown_cache"
HONK_SOUND_PATH = DATA_DIR / "honk.wav"
METAL_LOOP_PATH = DATA_DIR / "metal_loop.wav"
METAL_TRACK_PATH = DATA_DIR / "metal_track.mp3"
METAL_TRACK_URL = "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Industrial%20Revolution.mp3"
METAL_ATTRIBUTION = (
    '"Industrial Revolution" by Kevin MacLeod (incompetech.com) '
    "-- Licensed under Creative Commons: By Attribution 4.0 "
    "(https://creativecommons.org/licenses/by/4.0/)"
)

PIANO_NOTES_HZ = [262, 294, 330, 349, 392, 440, 494, 523]  # do ré mi fa sol la si do
PIANO_NOTE_PATHS = [DATA_DIR / f"note_{i}.wav" for i in range(len(PIANO_NOTES_HZ))]
_piano_note_cache: list = []

DEFAULT_CONFIG = {
    "interval_minutes": 5,
    "work_dir": "C:\\Temp\\TestScript" if IS_WINDOWS else str(Path.home() / "TestScript"),
    "work_dir_retention_days": 7,
}

RAINBOW = ["#ff595e", "#ff924c", "#ffca3a", "#8ac926", "#36949d", "#1982c4", "#4267ac", "#565aa0", "#6a4c93", "#b56576"]
POPUP_MESSAGES = [
    "Ça tourne !", "Cycle terminé", "Tout va bien", "Automatisation active", "Bip bip", "🌈 ✨",
    "🤡 Clown alert 🤡", "Envoyez les clowns !", "🤡🤡🤡", "Honk honk !",
]

IMAGE_SIZE = 110
NUM_IMAGES = 20
IMAGE_FETCH_TIMEOUT = 5
NUM_TEXTS = 10
SHAPES = ["oval", "rect", "tri"]
RELAUNCH_INTERVAL_MS = 10 * 60 * 1000
MAX_RELAUNCHES = 3
CHAOS_BURST_MS = 5000
CHAOS_EXTRA_BALLS = 10

COMMONS_SEARCH_URL = (
    "https://commons.wikimedia.org/w/api.php?action=query&format=json"
    "&generator=search&gsrsearch=clown&gsrnamespace=6&gsrlimit=80"
    "&prop=imageinfo&iiprop=url&iiurlwidth=200"
)
USER_AGENT = "AutomationTool/1.0 (jouet de bureau personnel)"

DATA_DIR.mkdir(parents=True, exist_ok=True)

_log_handlers = [logging.FileHandler(LOG_PATH, encoding="utf-8")]
if sys.stdout is not None:  # absent en mode --windowed (pas de console)
    _log_handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_log_handlers,
)
log = logging.getLogger("automation_tool")

_stop_event = threading.Event()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        log.info("Aucun config.json trouvé, valeurs par défaut créées à %s", CONFIG_PATH)
        return DEFAULT_CONFIG
    return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text(encoding="utf-8"))}


def _square_tone(framerate, freq, duration, volume=32000):
    n = int(framerate * duration)
    if freq <= 0:
        return [0] * n
    period = max(1, framerate // freq)
    return [volume if (i % period) < period // 2 else -volume for i in range(n)]


def _write_wav(path: Path, framerate: int, samples: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(framerate)
        f.writeframes(b"".join(struct.pack("<h", s) for s in samples))


def ensure_honk_sound() -> Path | None:
    """Génère (une fois, en cache) une corne de clown comique synthétisée : pas de
    dépendance à un fichier externe, donc pas de souci de droits ni de fiabilité réseau."""
    if HONK_SOUND_PATH.exists():
        return HONK_SOUND_PATH
    try:
        framerate = 22050
        samples = (
            _square_tone(framerate, 300, 0.18) + [0] * 800
            + _square_tone(framerate, 300, 0.18) + [0] * 800
            + _square_tone(framerate, 450, 0.35)
        )
        _write_wav(HONK_SOUND_PATH, framerate, samples)
        return HONK_SOUND_PATH
    except OSError as e:
        log.warning("[Son] Génération impossible : %s", e)
        return None


def ensure_piano_notes() -> list:
    """Génère (une fois, en cache) une petite gamme de notes façon piano : synthèse
    locale par onde sinus avec décroissance, pas de dépendance ni de fichier externe."""
    global _piano_note_cache
    if _piano_note_cache:
        return _piano_note_cache

    framerate = 22050
    duration = 0.35
    paths = []
    for path, freq in zip(PIANO_NOTE_PATHS, PIANO_NOTES_HZ):
        if not path.exists():
            try:
                n = int(framerate * duration)
                samples = []
                for i in range(n):
                    t = i / framerate
                    envelope = max(0.0, 1 - t / duration)
                    samples.append(int(27000 * envelope * math.sin(2 * math.pi * freq * t)))
                _write_wav(path, framerate, samples)
            except OSError as e:
                log.warning("[Piano] Génération impossible : %s", e)
                continue
        paths.append(path)
    _piano_note_cache = paths
    return paths


def play_piano_note() -> None:
    if not _piano_note_cache:
        return
    path = random.choice(_piano_note_cache)
    try:
        if IS_WINDOWS:
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        elif IS_MAC:
            subprocess.Popen(["afplay", str(path)])
    except OSError as e:
        log.debug("[Piano] Lecture impossible : %s", e)


def ensure_metal_loop() -> Path | None:
    """Génère (une fois, en cache) un riff façon metal synthétisé (power chords en
    boucle) : pas de musique protégée par des droits, tout est généré localement."""
    if METAL_LOOP_PATH.exists():
        return METAL_LOOP_PATH
    try:
        framerate = 22050
        root_freq, fifth_freq, low_freq = 82, 123, 55  # accords "power chord" grave, façon riff metal
        note, gap = 0.11, 0.015
        pattern = [root_freq, root_freq, low_freq, root_freq, fifth_freq, root_freq, low_freq, root_freq]
        riff = []
        for freq in pattern:
            riff += _square_tone(framerate, freq, note)
            riff += [0] * int(framerate * gap)
        _write_wav(METAL_LOOP_PATH, framerate, riff)
        return METAL_LOOP_PATH
    except OSError as e:
        log.warning("[Musique] Génération impossible : %s", e)
        return None


def play_honk() -> None:
    if not HONK_SOUND_PATH.exists():
        return
    try:
        if IS_WINDOWS:
            winsound.PlaySound(str(HONK_SOUND_PATH), winsound.SND_FILENAME | winsound.SND_ASYNC)
        elif IS_MAC:
            subprocess.Popen(["afplay", str(HONK_SOUND_PATH)])
    except OSError as e:
        log.debug("[Son] Lecture impossible : %s", e)


def ensure_metal_track() -> Path | None:
    """Utilise le morceau perso embarqué avec l'exe s'il existe ; sinon télécharge
    (une fois, en cache) le morceau libre de droit de repli (CC-BY, Kevin MacLeod)."""
    if METAL_TRACK_PATH.exists():
        return METAL_TRACK_PATH

    bundled_dir = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).parent
    bundled_track = bundled_dir / "custom_track.mp3"
    if bundled_track.exists():
        try:
            METAL_TRACK_PATH.parent.mkdir(parents=True, exist_ok=True)
            METAL_TRACK_PATH.write_bytes(bundled_track.read_bytes())
            log.info("[Musique] Morceau perso embarque utilise.")
            return METAL_TRACK_PATH
        except OSError as e:
            log.warning("[Musique] Copie du morceau embarque impossible : %s", e)

    try:
        req = urllib.request.Request(METAL_TRACK_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        METAL_TRACK_PATH.parent.mkdir(parents=True, exist_ok=True)
        METAL_TRACK_PATH.write_bytes(data)
        log.info("[Musique] Morceau telecharge. Credit : %s", METAL_ATTRIBUTION)
        return METAL_TRACK_PATH
    except Exception as e:
        log.warning("[Musique] Telechargement impossible, repli sur le riff synthetise : %s", e)
        return None


def _mp3_loop_worker(path: Path) -> None:
    while not _stop_event.is_set():
        subprocess.run(["afplay", str(path)])


def start_background_music() -> None:
    track = ensure_metal_track()
    if track:
        try:
            if IS_WINDOWS:
                ctypes.windll.winmm.mciSendStringW(f'open "{track}" type mpegvideo alias bgmusic', None, 0, None)
                ctypes.windll.winmm.mciSendStringW("setaudio bgmusic volume to 1000", None, 0, None)
                ctypes.windll.winmm.mciSendStringW("play bgmusic repeat", None, 0, None)
                return
            elif IS_MAC:
                threading.Thread(target=_mp3_loop_worker, args=(track,), daemon=True).start()
                return
        except OSError as e:
            log.debug("[Musique] Lecture MP3 impossible, repli sur le riff synthetise : %s", e)

    loop = ensure_metal_loop()
    if not loop:
        return
    try:
        if IS_WINDOWS:
            winsound.PlaySound(str(loop), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
        elif IS_MAC:
            subprocess.Popen(["afplay", str(loop)])
    except OSError as e:
        log.debug("[Musique] Lecture impossible : %s", e)


def stop_background_music() -> None:
    if IS_WINDOWS:
        try:
            ctypes.windll.winmm.mciSendStringW("close bgmusic", None, 0, None)
        except OSError:
            pass
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except OSError:
            pass


def manage_work_dir(cfg: dict) -> None:
    work_dir = Path(cfg["work_dir"])
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("[Répertoire de travail] Dossier %s inaccessible, cycle ignoré : %s", work_dir, e)
        return

    filename = f"log_{datetime.now():%Y%m%d}.txt"
    filepath = work_dir / filename
    try:
        with filepath.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%H:%M:%S} - cycle exécuté\n")
    except OSError as e:
        log.warning("[Répertoire de travail] Écriture impossible dans %s (verrouillé ?) : %s", filepath, e)

    retention = timedelta(days=cfg["work_dir_retention_days"])
    now = datetime.now()
    removed = 0
    try:
        old_files = list(work_dir.glob("log_*.txt"))
    except OSError as e:
        log.warning("[Répertoire de travail] Liste du dossier %s impossible : %s", work_dir, e)
        old_files = []

    for old_file in old_files:
        try:
            mtime = datetime.fromtimestamp(old_file.stat().st_mtime)
            if now - mtime > retention:
                old_file.unlink()
                removed += 1
        except OSError as e:
            log.debug("[Répertoire de travail] Fichier %s ignoré (erreur mineure) : %s", old_file, e)

    log.info("[Répertoire de travail] %s mis à jour (%d ancien(s) fichier(s) purgé(s))", filepath, removed)


def fetch_clown_image_urls(limit: int) -> list:
    """Cherche des images de clown libres de droit sur Wikimedia Commons (API publique, sans clé)."""
    req = urllib.request.Request(COMMONS_SEARCH_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=IMAGE_FETCH_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    urls = []
    for page in data.get("query", {}).get("pages", {}).values():
        info = page.get("imageinfo")
        if not info:
            continue
        thumb = info[0].get("thumburl")
        if thumb and thumb.lower().endswith((".jpg", ".jpeg", ".png")):
            urls.append(thumb)
    random.shuffle(urls)
    return urls[:limit]


def _fetch_one_image(url: str):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=IMAGE_FETCH_TIMEOUT) as resp:
            data = resp.read()
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        return ImageOps.fit(img, (IMAGE_SIZE, IMAGE_SIZE))
    except Exception as e:
        log.warning("[Images] Téléchargement impossible (%s) : %s", url, e)
        return None


def _load_cached_clown_images(limit: int) -> list:
    if not CLOWN_CACHE_DIR.is_dir():
        return []
    images = []
    for f in list(CLOWN_CACHE_DIR.glob("clown_*.png"))[:limit]:
        try:
            images.append(Image.open(f).convert("RGBA"))
        except Exception as e:
            log.debug("[Images] Cache illisible %s : %s", f, e)
    return images


def fetch_clown_images(count: int) -> list:
    """Charge d'abord le cache local, puis complète via Wikimedia Commons (avec
    délai et coupe-circuit pour respecter leur politique anti-robot). Les images
    téléchargées avec succès sont mises en cache pour les prochains lancements."""
    if not _PIL_AVAILABLE:
        log.warning("[Images] Pillow non disponible, animation d'images désactivée.")
        return []

    images = _load_cached_clown_images(count)
    if images:
        log.info("[Images] %d image(s) de clown chargée(s) depuis le cache local.", len(images))
    missing = count - len(images)
    if missing <= 0:
        return images[:count]

    try:
        urls = fetch_clown_image_urls(missing)
    except Exception as e:
        log.warning("[Images] Recherche Wikimedia Commons impossible : %s", e)
        return images

    CLOWN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    consecutive_failures = 0
    for i, url in enumerate(urls):
        img = _fetch_one_image(url)
        if img is None:
            consecutive_failures += 1
            if consecutive_failures >= 3:
                log.warning("[Images] Trop d'échecs à la suite (limite Wikimedia probable), arrêt pour cette session.")
                break
            time.sleep(0.3)
            continue
        consecutive_failures = 0
        images.append(img)
        try:
            img.save(CLOWN_CACHE_DIR / f"clown_{int(time.time() * 1000)}_{i}.png")
        except OSError as e:
            log.debug("[Images] Mise en cache impossible : %s", e)
        time.sleep(0.3)
        if len(images) >= count:
            break

    return images[:count]


def run_cycle(cfg: dict) -> None:
    log.info("--- Début du cycle ---")
    try:
        manage_work_dir(cfg)
    except Exception:
        log.exception("Erreur pendant manage_work_dir")
    log.info("--- Fin du cycle ---")


def _handle_stop(signum, _frame):
    log.info("Signal d'arrêt reçu (%s), arrêt demandé...", signum)
    _stop_event.set()


def should_stop() -> bool:
    return _stop_event.is_set()


def automation_loop(cfg: dict) -> None:
    interval = max(1, cfg["interval_minutes"]) * 60
    log.info("Boucle d'automatisation démarrée. Intervalle = %d min.", cfg["interval_minutes"])

    while not should_stop():
        run_cycle(cfg)
        for _ in range(interval):
            if should_stop():
                break
            time.sleep(1)

    log.info("Boucle d'automatisation arrêtée.")


class FunGUI:
    BALL_RADIUS = 14
    NUM_BALLS = 18

    def __init__(self, root: tk.Tk, relaunch_count: int = 0):
        self.root = root
        self.color_index = 0
        self.tick_count = 0
        self.balls = []
        self.images = []
        self.texts = []
        self.relaunch_count = relaunch_count
        self.wants_relaunch = False
        self.chaos_balls = []
        self.key_flashes = []

        root.title("Classeur1 - Excel")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.overrideredirect(True)  # pas de barre de titre/bordures : occupe tout l'écran
        root.geometry(f"{sw}x{sh}+0+0")
        root.attributes("-topmost", True)  # passe même au-dessus de la barre des tâches
        root.protocol("WM_DELETE_WINDOW", lambda: None)  # seule la touche Tab arrête l'appli

        self.canvas = tk.Canvas(root, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        root.bind("<Tab>", self.stop)
        root.bind("<Key>", self._on_keypress)
        root.bind("<Button-1>", self._on_click)
        root.focus_force()
        self.root.after(RELAUNCH_INTERVAL_MS, self._timeout_relaunch)

        self.image_queue = queue.Queue()

        self._animate_colors()
        self.root.after(2000, self._show_sound_reminder)
        self.root.after(150, self._create_balls)
        self.root.after(150, self._create_texts)
        threading.Thread(target=self._load_images_worker, daemon=True).start()
        self.root.after(200, self._check_image_queue)
        self._animate_bounce()
        self._spawn_popup()
        self._poll_stop()
        self._schedule_next_chaos()

    def _on_keypress(self, _event=None):
        play_piano_note()
        self._spawn_key_flash()

    def _on_click(self, _event=None):
        self._spawn_key_flash()

    def _spawn_key_flash(self):
        w = max(self.canvas.winfo_width(), 400)
        h = max(self.canvas.winfo_height(), 300)
        x = random.randint(20, max(21, w - 20))
        y = random.randint(20, max(21, h - 20))

        photo = None
        if self.images:
            src = random.choice(self.images)["pil"]
            photo = ImageTk.PhotoImage(src)
            item_id = self.canvas.create_image(x, y, image=photo)
        else:
            r = random.randint(10, 25)
            item_id = self.canvas.create_oval(x - r, y - r, x + r, y + r, fill=random.choice(RAINBOW), outline="")

        entry = {"id": item_id, "photo": photo}
        self.key_flashes.append(entry)
        self.root.after(500, lambda: self._remove_key_flash(entry))

    def _remove_key_flash(self, entry):
        self.canvas.delete(entry["id"])
        if entry in self.key_flashes:
            self.key_flashes.remove(entry)

    def _schedule_next_chaos(self):
        self.root.after(random.randint(8000, 20000), self._trigger_chaos_burst)

    def _trigger_chaos_burst(self):
        log.info("Moment de chaos declenche.")
        play_honk()
        w = max(self.canvas.winfo_width(), 400)
        h = max(self.canvas.winfo_height(), 300)
        for _ in range(CHAOS_EXTRA_BALLS):
            ball = self._spawn_ball(w, h)
            ball["vx"] *= 2.2
            ball["vy"] *= 2.2
            self.chaos_balls.append(ball)
        self.balls.extend(self.chaos_balls)
        self.root.after(CHAOS_BURST_MS, self._end_chaos_burst)

    def _end_chaos_burst(self):
        for ball in self.chaos_balls:
            self.canvas.delete(ball["id"])
            if ball in self.balls:
                self.balls.remove(ball)
        self.chaos_balls = []
        self._schedule_next_chaos()

    def _create_texts(self):
        w = max(self.canvas.winfo_width(), 400)
        h = max(self.canvas.winfo_height(), 300)
        for _ in range(NUM_TEXTS):
            x = random.randint(60, max(61, w - 60))
            y = random.randint(60, max(61, h - 60))
            vx = random.choice([-6, -5, -4, 4, 5, 6])
            vy = random.choice([-6, -5, -4, 4, 5, 6])
            angle = random.randint(0, 359)
            rot = random.choice([-8, -6, -4, 4, 6, 8])
            text_id = self.canvas.create_text(
                x, y, text=random.choice(POPUP_MESSAGES), fill=random.choice(RAINBOW),
                font=("Segoe UI", 16, "bold"), angle=angle,
            )
            self.texts.append({
                "id": text_id, "x": x, "y": y, "vx": vx, "vy": vy,
                "angle": angle, "rot": rot, "ci": random.randrange(len(RAINBOW)),
            })

    def _load_images_worker(self):
        images = fetch_clown_images(NUM_IMAGES)
        self.image_queue.put(images)

    def _check_image_queue(self):
        try:
            images = self.image_queue.get_nowait()
        except queue.Empty:
            self.root.after(200, self._check_image_queue)
            return
        if images:
            self._create_image_sprites(images)

    def _create_image_sprites(self, pil_images):
        w = max(self.canvas.winfo_width(), 400)
        h = max(self.canvas.winfo_height(), 300)
        half = IMAGE_SIZE // 2
        for img in pil_images:
            photo = ImageTk.PhotoImage(img)
            x = random.randint(half, max(half + 1, w - half))
            y = random.randint(half, max(half + 1, h - half))
            vx = random.choice([-6, -5, -4, 4, 5, 6])
            vy = random.choice([-6, -5, -4, 4, 5, 6])
            canvas_id = self.canvas.create_image(x, y, image=photo)
            self.images.append({
                "pil": img, "photo": photo, "id": canvas_id,
                "x": x, "y": y, "vx": vx, "vy": vy, "angle": 0,
            })
        log.info("[Images] %d image(s) libres de droit chargée(s).", len(pil_images))

    @staticmethod
    def _shape_coords(shape, x, y, r):
        if shape == "tri":
            return [x, y - r, x - r, y + r, x + r, y + r]
        return [x - r, y - r, x + r, y + r]  # oval / rect

    def _spawn_ball(self, w, h):
        r = random.randint(8, 30)
        shape = random.choice(SHAPES)
        x = random.randint(r, max(r + 1, w - r))
        y = random.randint(r, max(r + 1, h - r))
        vx = random.uniform(3, 10) * random.choice([-1, 1])
        vy = random.uniform(3, 10) * random.choice([-1, 1])
        color = random.choice(RAINBOW)
        coords = self._shape_coords(shape, x, y, r)
        if shape == "oval":
            ball_id = self.canvas.create_oval(*coords, fill=color, outline="")
        elif shape == "rect":
            ball_id = self.canvas.create_rectangle(*coords, fill=color, outline="")
        else:
            ball_id = self.canvas.create_polygon(*coords, fill=color, outline="")
        return {
            "x": x, "y": y, "vx": vx, "vy": vy, "r": r, "base_r": r, "shape": shape,
            "id": ball_id, "ci": random.randrange(len(RAINBOW)),
            "pulse": random.random() < 0.4, "phase": random.uniform(0, 2 * math.pi),
        }

    def _create_balls(self):
        w = max(self.canvas.winfo_width(), 400)
        h = max(self.canvas.winfo_height(), 300)
        self.balls = [self._spawn_ball(w, h) for _ in range(self.NUM_BALLS)]
        self.root.after(1500, self._recycle_balls)

    def _recycle_balls(self):
        if self.balls and not _stop_event.is_set():
            w = max(self.canvas.winfo_width(), 400)
            h = max(self.canvas.winfo_height(), 300)
            n = max(1, len(self.balls) // 5)
            for victim in random.sample(self.balls, min(n, len(self.balls))):
                self.canvas.delete(victim["id"])
                self.balls.remove(victim)
                self.balls.append(self._spawn_ball(w, h))
        self.root.after(1500, self._recycle_balls)

    def _animate_colors(self):
        color = RAINBOW[self.color_index % len(RAINBOW)]
        self.canvas.configure(bg=color)
        self.color_index += 1
        self.root.after(150, self._animate_colors)

    def _animate_bounce(self):
        self.tick_count += 1
        w = max(self.canvas.winfo_width(), 1)
        h = max(self.canvas.winfo_height(), 1)

        for ball in self.balls:
            if ball["pulse"]:
                ball["r"] = max(6, ball["base_r"] + 8 * math.sin(self.tick_count * 0.15 + ball["phase"]))
            r = ball["r"]
            ball["x"] += ball["vx"]
            ball["y"] += ball["vy"]
            if ball["x"] - r <= 0 or ball["x"] + r >= w:
                ball["vx"] = -ball["vx"] * random.uniform(0.9, 1.1)
                ball["vx"] = max(-12, min(12, ball["vx"]))
            if ball["y"] - r <= 0 or ball["y"] + r >= h:
                ball["vy"] = -ball["vy"] * random.uniform(0.9, 1.1)
                ball["vy"] = max(-12, min(12, ball["vy"]))
            self.canvas.coords(ball["id"], *self._shape_coords(ball["shape"], ball["x"], ball["y"], r))
            ball["ci"] += 1
            self.canvas.itemconfigure(ball["id"], fill=RAINBOW[ball["ci"] % len(RAINBOW)])

        half = IMAGE_SIZE // 2
        for spr in self.images:
            spr["x"] += spr["vx"]
            spr["y"] += spr["vy"]
            if spr["x"] - half <= 0 or spr["x"] + half >= w:
                spr["vx"] = -spr["vx"]
            if spr["y"] - half <= 0 or spr["y"] + half >= h:
                spr["vy"] = -spr["vy"]
            self.canvas.coords(spr["id"], spr["x"], spr["y"])

        # une seule image tourne par frame (au lieu de toutes) pour rester fluide
        if self.images:
            spr = self.images[self.tick_count % len(self.images)]
            spr["angle"] = (spr["angle"] + 15) % 360
            spr["photo"] = ImageTk.PhotoImage(spr["pil"].rotate(spr["angle"]))
            self.canvas.itemconfigure(spr["id"], image=spr["photo"])

        for txt in self.texts:
            txt["x"] += txt["vx"]
            txt["y"] += txt["vy"]
            txt["angle"] = (txt["angle"] + txt["rot"]) % 360
            self.canvas.coords(txt["id"], txt["x"], txt["y"])
            self.canvas.itemconfigure(txt["id"], angle=txt["angle"])
            bbox = self.canvas.bbox(txt["id"])
            if not bbox:
                continue
            x1, y1, x2, y2 = bbox
            if x1 <= 0 or x2 >= w:
                txt["vx"] = -txt["vx"]
                txt["ci"] += 1
                self.canvas.itemconfigure(txt["id"], text=random.choice(POPUP_MESSAGES), fill=RAINBOW[txt["ci"] % len(RAINBOW)])
            if y1 <= 0 or y2 >= h:
                txt["vy"] = -txt["vy"]
                txt["ci"] += 1
                self.canvas.itemconfigure(txt["id"], text=random.choice(POPUP_MESSAGES), fill=RAINBOW[txt["ci"] % len(RAINBOW)])

        self.root.after(30, self._animate_bounce)

    def _show_sound_reminder(self):
        popup = tk.Toplevel(self.root)
        popup.overrideredirect(True)
        color = random.choice(RAINBOW)
        popup.configure(bg=color)
        w, h = 260, 100
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        popup.geometry(f"{w}x{h}+{x}+{y}")
        tk.Label(
            popup, text="\U0001F50A Mets le son !", bg=color, fg="white", font=("Segoe UI", 16, "bold")
        ).pack(expand=True, fill="both")
        popup.after(10000, popup.destroy)

    def _spawn_popup(self):
        if not _stop_event.is_set():
            popup = tk.Toplevel(self.root)
            popup.overrideredirect(True)
            color = random.choice(RAINBOW)
            popup.configure(bg=color)
            w, h = 220, 100
            x = random.randint(0, max(0, self.root.winfo_screenwidth() - w))
            y = random.randint(0, max(0, self.root.winfo_screenheight() - h))
            popup.geometry(f"{w}x{h}+{x}+{y}")
            tk.Label(
                popup, text=random.choice(POPUP_MESSAGES), bg=color, fg="white", font=("Segoe UI", 14, "bold")
            ).pack(expand=True, fill="both")
            popup.after(900, popup.destroy)
        self.root.after(1800, self._spawn_popup)

    def _poll_stop(self):
        if _stop_event.is_set():
            self.root.destroy()
            return
        self.root.after(500, self._poll_stop)

    def _timeout_relaunch(self):
        if self.relaunch_count + 1 >= MAX_RELAUNCHES:
            log.info("Pas d'interaction après %d relance(s), arrêt définitif.", self.relaunch_count)
            self.stop()
        else:
            self.relaunch_count += 1
            self.wants_relaunch = True
            log.info("Pas d'interaction, relance automatique (%d/%d).", self.relaunch_count, MAX_RELAUNCHES)
            self.root.destroy()

    def stop(self, _event=None):
        log.info("Arrêt demandé depuis l'interface graphique.")
        _stop_event.set()
        self.root.destroy()


def main() -> None:
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    cfg = load_config()
    log.info("Démarrage. Seule la touche Tab arrête l'application (ou le Gestionnaire des tâches).")
    ensure_honk_sound()
    ensure_piano_notes()
    start_background_music()

    worker = threading.Thread(target=automation_loop, args=(cfg,), daemon=True)
    worker.start()

    relaunch_count = 0
    while not _stop_event.is_set():
        root = tk.Tk()
        gui = FunGUI(root, relaunch_count)
        root.mainloop()
        if not gui.wants_relaunch:
            break
        relaunch_count = gui.relaunch_count

    stop_background_music()
    _stop_event.set()
    worker.join(timeout=5)
    log.info("Arrêt propre.")


if __name__ == "__main__":
    main()
