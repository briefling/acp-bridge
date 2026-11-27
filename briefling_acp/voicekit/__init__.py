"""Embedded voice of a briefling agent: Pocket TTS speaks its replies; for the ACP bridge, faster-whisper also
transcribes the voice notes it receives (a Hermes agent keeps the gateway's own transcription). Shared by the plugin
and the bridge (acp-bridge/briefling_acp/voicekit is a symlink to this directory). Standard library only here: the
models run in `worker.py`, in a venv of their own.

Mode, per agent, in its own config (`BRIEFLING_VOICE_MODE`; the agent's machine installs it, so the agent decides,
never the hub):
- `embedded` (default): the agent ends its answer with a `🔊 <summary>` line, the plugin / bridge cuts it from the
  text and posts it as an audio note; no text_to_speech tool, no Edge.
- `hermes` (plugin only): as before, the gateway's text_to_speech tool and its configured provider.
- `off`: no spoken reply (a voice note still gets its text answer, and its transcript in the bridge).
Any other value reads as `off` (a typo never installs 1 GB).

Lazy: nothing is installed until the first spoken reply (or, in the bridge, the first voice note). Then `uv` builds a
venv from `voice.lock` (pinned, hashed, torch CPU-only) under `<cache>/briefling/voice/<lock hash>`, and the worker
downloads the weights from Hugging Face at pinned revisions (~0.2 GB Pocket TTS, ~0.5 GB Whisper). The venv takes
~1 GB. A new lock = a new directory; the old ones are removed once it is ready. Two processes of the same machine
(two Hermes profiles) share it: the install holds a file lock.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK = os.path.join(HERE, "voice.lock")
WORKER = os.path.join(HERE, "worker.py")
MODES = ("embedded", "hermes", "off")
MODE_ENV, VOICE_ENV, LANG_ENV = "BRIEFLING_VOICE_MODE", "BRIEFLING_VOICE_NAME", "BRIEFLING_VOICE_LANG"
DEFAULT_VOICE, DEFAULT_LANG = "juergen", "french"
PYTHON = "3.12"
INSTALL_TIMEOUT_S = 1800   # ~1 GB of wheels on a slow line
FIRST_TIMEOUT_S = 900      # a request that also downloads its weights
CALL_TIMEOUT_S = 300
IDLE_S = 600               # the warm worker holds ~1.1 GB (TTS) + ~0.5 GB (STT): stopped after 10 min unused
MAX_SPOKEN = 800           # characters of one spoken summary (~1 min)

INSTALLING = "🔊 Voice being installed on this agent (first use, ~1 GB, a few minutes)."
FAILED = "🔊 No audio note: the voice could not be installed or run on this agent ({why}); details in its voice.log."

# The agent's last line: « 🔊 » then the summary. Lines elsewhere in the text are cut too (streamed half-lines
# included), never inside a code block.
_SPOKEN = re.compile(r"^[ \t]*\U0001F50A\uFE0F?[ \t]*(.*)$")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")


# Voice note recorded with the composer's mic button: the client names it `audio-YYYY-MM-DD-HHMM.<ext>` (recName in
# client/src/audio.ts). Any other audio (a dropped mp3, a call recording) is not a voice note.
RECORDING_RE = re.compile(r"audio-\d{4}-\d{2}-\d{2}-\d{4}\.(?:webm|m4a|ogg|mp3|wav|audio)")


def is_recording(f: Any) -> bool:
    """A voice note from the composer's mic button (an audio is not necessarily a voice)."""
    return (isinstance(f, dict) and str(f.get("mime") or "").startswith("audio/")
            and bool(RECORDING_RE.fullmatch(str(f.get("name") or ""))))


def wants_voice(files: Any) -> bool:
    """A voice note whose sender ticked « Reply by voice » (`voiceReply`, set by the hub from its message)."""
    return any(is_recording(f) and f.get("voiceReply") is True for f in (files if isinstance(files, list) else []))


def mode(env: Optional[Dict[str, str]] = None, bridge: bool = False) -> str:
    raw = str((env if env is not None else os.environ).get(MODE_ENV) or "").strip().lower() or "embedded"
    m = raw if raw in MODES else "off"
    if raw not in MODES:
        logger.warning("%s=%r unknown: voice off (embedded, hermes or off)", MODE_ENV, raw[:40])
    return "off" if bridge and m == "hermes" else m  # the bridge has no gateway TTS to fall back on


def reply_hint(m: str) -> str:
    """What a « Reply by voice » note asks of the agent, by mode ("" = no spoken reply)."""
    if m == "embedded":
        return ("[They ticked « Reply by voice »: end your answer with one last line that starts with 🔊 then a short "
                "spoken summary of it (2 or 3 sentences, same language, plain words: no markdown, code, links or "
                "file paths). briefling cuts that line from your text and posts it as an audio note. Do not call a "
                "text-to-speech tool.]\n")
    if m == "hermes":
        return ("[They ticked « Reply by voice »: if you have a text-to-speech tool, add after your text a short "
                "spoken summary of your answer (2 or 3 sentences, same language), sent as audio.]\n")
    return ""


def split_spoken(text: str) -> Tuple[str, Optional[str]]:
    """(text without its 🔊 lines, the last summary or None). The text is returned as is when it has none."""
    if "\U0001F50A" not in (text or ""):
        return text, None
    kept, said, fence = [], None, False
    for line in text.split("\n"):
        if _FENCE.match(line):
            fence = not fence
        m = None if fence else _SPOKEN.match(line)
        if m:
            said = m.group(1).strip() or said
        else:
            kept.append(line)
    return "\n".join(kept).rstrip(), (said[:MAX_SPOKEN] if said else None)


def cache_root() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "briefling", "voice")


def lock_hash() -> str:
    with open(LOCK, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]


def find_uv() -> Optional[str]:
    home = os.path.expanduser("~")
    for p in (shutil.which("uv"), os.path.join(home, ".local", "bin", "uv"), os.path.join(home, ".cargo", "bin", "uv")):
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


class _FileLock:
    """Exclusive lock on a file, between the processes of this machine (none on Windows: one install at a time there
    is left to luck, an open point of the design)."""

    def __init__(self, path: str):
        self.path, self.fd = path, None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            import fcntl
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        except ImportError:
            pass
        return self

    def __exit__(self, *_):
        os.close(self.fd)


class VoiceError(Exception):
    pass


class Voice:
    """The venv, the warm worker and its calls, one at a time. One per plugin adapter / bridge."""

    def __init__(self, root: Optional[str] = None, env: Optional[Dict[str, str]] = None):
        self.root = root or cache_root()
        self.env = env if env is not None else os.environ
        self.dir = os.path.join(self.root, lock_hash())
        self.log = os.path.join(self.root, "voice.log")
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.lock = asyncio.Lock()
        self.used: set = set()   # ops whose weights are loaded in the running worker
        self.idle: Optional[asyncio.Task] = None

    @property
    def python(self) -> str:
        return os.path.join(self.dir, "Scripts", "python.exe") if sys.platform == "win32" else os.path.join(self.dir, "bin", "python")

    def installed(self) -> bool:
        return os.path.isfile(os.path.join(self.dir, ".ready"))

    async def _run(self, *cmd: str) -> None:
        with open(self.log, "ab") as log:
            p = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log)
            try:
                code = await asyncio.wait_for(p.wait(), INSTALL_TIMEOUT_S)
            except asyncio.TimeoutError:
                p.kill()
                raise VoiceError("install timed out")
        if code != 0:
            raise VoiceError(f"{os.path.basename(cmd[0])} {cmd[1]} failed ({code})")

    async def install(self) -> None:
        if self.installed():
            return
        uv = find_uv()
        if not uv:
            raise VoiceError("uv not found: install it (https://docs.astral.sh/uv/) or set BRIEFLING_VOICE_MODE=off")
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        lock = _FileLock(os.path.join(self.root, ".install.lock"))
        await asyncio.to_thread(lock.__enter__)
        try:
            if self.installed():  # another process of this machine did it meanwhile
                return
            logger.info("voice: installing into %s", self.dir)
            shutil.rmtree(self.dir, ignore_errors=True)  # an interrupted install
            await self._run(uv, "venv", "-q", "-p", PYTHON, self.dir)
            # --torch-backend cpu: the locked torch is the CPU build (a Linux default one pulls ~4 GB of CUDA).
            await self._run(uv, "pip", "sync", "-q", "--require-hashes", "--torch-backend", "cpu", "--python", self.python, LOCK)
            open(os.path.join(self.dir, ".ready"), "w").close()
            for d in os.listdir(self.root):  # venvs of older locks
                p = os.path.join(self.root, d)
                if os.path.isdir(p) and p != self.dir and re.fullmatch(r"[0-9a-f]{12}", d):
                    shutil.rmtree(p, ignore_errors=True)
        finally:
            lock.__exit__()

    async def _worker(self) -> asyncio.subprocess.Process:
        if self.proc is None or self.proc.returncode is not None:
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "HF_HUB_DISABLE_TELEMETRY": "1"}
            env.pop("PYTHONPATH", None)  # the agent's own path must not leak into the voice venv
            log = open(self.log, "ab")
            self.proc = await asyncio.create_subprocess_exec(self.python, WORKER, stdin=asyncio.subprocess.PIPE,
                                                             stdout=asyncio.subprocess.PIPE, stderr=log, env=env,
                                                             limit=1 << 20)
            log.close()
            self.used = set()
        return self.proc

    async def call(self, op: str, **req: Any) -> Dict[str, Any]:
        """One worker request (installs first if needed). Raises VoiceError."""
        async with self.lock:
            await self.install()
            proc = await self._worker()
            timeout = CALL_TIMEOUT_S if op in self.used else FIRST_TIMEOUT_S
            try:
                proc.stdin.write((json.dumps({"op": op, **req}) + "\n").encode())
                await proc.stdin.drain()
                line = await asyncio.wait_for(proc.stdout.readline(), timeout)
            except (asyncio.TimeoutError, ConnectionError, BrokenPipeError) as e:
                self.stop()
                raise VoiceError(f"worker {type(e).__name__}")
            if not line:
                self.stop()
                raise VoiceError("worker died (see the log)")
            res = json.loads(line)
            if not res.get("ok"):
                raise VoiceError(str(res.get("error") or "failed"))
            self.used.add(op)
            self._arm_idle()
            return res

    def _arm_idle(self) -> None:
        if self.idle:
            self.idle.cancel()
        self.idle = asyncio.get_running_loop().create_task(self._idle_stop())

    async def _idle_stop(self) -> None:
        await asyncio.sleep(IDLE_S)
        async with self.lock:
            self.stop()

    def stop(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
        self.proc = None

    async def speak(self, text: str) -> str:
        """A WAV of `text` in the agent's voice (BRIEFLING_VOICE_NAME: a Pocket TTS voice or a prompt file), in a
        private temp file the caller removes."""
        fd, out = tempfile.mkstemp(prefix="briefling-voice-", suffix=".wav")
        os.close(fd)
        try:
            await self.call("tts", text=text[:MAX_SPOKEN], out=out, voice=self.env.get(VOICE_ENV) or DEFAULT_VOICE,
                            lang=self.env.get(LANG_ENV) or DEFAULT_LANG)
        except BaseException:
            os.unlink(out)
            raise
        return out

    async def transcribe(self, path: str) -> str:
        return str((await self.call("stt", path=path)).get("text") or "")


def failed_line(why: Any) -> str:
    """The grey line posted instead of the audio (never a local path: members read it)."""
    return FAILED.format(why=re.sub(r"(?:[A-Za-z]:)?[\\/][^\s'\"]+", "<path>", str(why))[:200])


def spoken_name() -> str:
    return time.strftime("voice-%Y-%m-%d-%H%M%S.wav")
