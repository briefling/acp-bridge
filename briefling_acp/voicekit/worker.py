"""Voice worker of voicekit: runs in the voice venv (voice.lock), never in the agent's own Python.

One JSON object per line on stdin, one answer per line on stdout, in order:
  {"op": "tts", "text": "...", "out": "/path.wav", "voice": "juergen", "lang": "french"} -> {"ok": true, "seconds": 4.6}
  {"op": "stt", "path": "/note.webm", "lang": null}                                   -> {"ok": true, "text": "...", "lang": "fr"}
  {"op": "ping"}                                                                        -> {"ok": true}
A failure answers {"ok": false, "error": "<type>: <message>"} and the worker keeps running. Models load at their
first use and stay loaded (the warm worker is the point: Pocket TTS takes ~10 s to load on a CPU, a reply ~1x real
time). Weights come from Hugging Face at pinned revisions: Pocket TTS pins its own in the config of the locked
package (`hf://...@<sha>`), Whisper below. stdout is the protocol only: what a library prints goes to stderr (the log).
"""
import json
import os
import sys
import wave

OUT = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# faster-whisper `small` (MIT), int8 on the CPU: 10 s for a 4.6 s French note in a 10-core container, where
# large-v3-turbo took 37 s (measured Nov 27 2026).
WHISPER_REPO, WHISPER_REV = "Systran/faster-whisper-small", "536b0662742c02347bc0e980a01041f333bce120"

_tts = {"lang": None, "model": None, "voices": {}}
_stt = {"model": None}


def tts(req):
    import numpy as np
    from pocket_tts import TTSModel
    lang = str(req.get("lang") or "french")
    if _tts["lang"] != lang:
        _tts.update(lang=lang, model=TTSModel.load_model(language=lang), voices={})
    model, voices = _tts["model"], _tts["voices"]
    voice = str(req.get("voice") or "juergen")
    if voice not in voices:
        voices[voice] = model.get_state_for_audio_prompt(voice)
    text = str(req["text"])
    if lang == "french":
        from speech_text import normalize_fr  # Pocket TTS misreads digits: numbers written out in words
        text = normalize_fr(text)
    audio = model.generate_audio(voices[voice], text)
    pcm = (np.clip(audio.numpy().reshape(-1), -1, 1) * 32767).astype("<i2")
    with wave.open(str(req["out"]), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(model.sample_rate)
        w.writeframes(pcm.tobytes())
    return {"seconds": round(len(pcm) / model.sample_rate, 1)}


def stt(req):
    if _stt["model"] is None:
        from faster_whisper import WhisperModel
        from huggingface_hub import snapshot_download
        _stt["model"] = WhisperModel(snapshot_download(WHISPER_REPO, revision=WHISPER_REV), device="cpu", compute_type="int8")
    segments, info = _stt["model"].transcribe(str(req["path"]), language=req.get("lang") or None, vad_filter=True)
    return {"text": " ".join(s.text.strip() for s in segments).strip(), "lang": info.language}


OPS = {"tts": tts, "stt": stt, "ping": lambda _req: {}}


def main():
    for line in sys.stdin:
        try:
            req = json.loads(line)
            res = {"ok": True, **OPS[req["op"]](req)}
        except Exception as e:  # one bad request must not kill the warm models
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        OUT.write(json.dumps(res) + "\n")


if __name__ == "__main__":
    main()
