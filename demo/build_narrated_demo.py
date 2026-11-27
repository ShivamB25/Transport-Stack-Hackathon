#!/usr/bin/env python3
"""Generate Sarvam cloned-voice / Bulbul narration WAVs and assemble a Full HD MP4 demo.

Sarvam Voice Cloning best practices implemented here (per docs.sarvam.ai):
  - Splits multi-sentence beats into single-chunk requests (<= max_chunk_chars) so every
    request runs through Sarvam's built-in QC pipeline (ASR + CER + prompt-leak check),
    which is silently skipped for long multi-sentence server-side chunked generations.
  - Disables aggressive server-side VAD chopping (enable_vad=false) and instead applies
    uniform ffmpeg silence trimming, 15 ms micro-fades, consistent inter-sentence breaths,
    and EBU R128 loudness normalization (-16 LUFS) across every beat for continuous flow.
  - Disables server-side text normalization when tts_text is already spoken-form English,
    eliminating mangled hyphens, dates, route codes, and homographs.
  - Logs Sarvam request_id values into demo/audio/manifest.json and supports --verify-stt
    for automated round-trip transcript QA via POST /speech-to-text.

Secrets are loaded exclusively via delhi_transit.core.config.load_env_key and never logged.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from delhi_transit.core.config import load_env_key

SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"
SARVAM_CLONE_URL = "https://api.sarvam.ai/voices/clone"
SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def probe_duration(path: Path) -> float:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
    ).strip()
    return float(out)


def fmt_srt_ts(seconds: float) -> str:
    ms_total = max(0, round(seconds * 1000))
    h, rem = divmod(ms_total, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def split_qc_chunks(text: str, max_chars: int) -> list[str]:
    """Split text on sentence boundaries so each call stays single-chunk and runs QC."""
    sentences = [s.strip() for s in SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]
    if not sentences:
        return [text.strip()]
    chunks: list[str] = []
    buf = ""
    for sent in sentences:
        if not buf:
            buf = sent
        elif len(buf) + 1 + len(sent) <= max_chars:
            buf = f"{buf} {sent}"
        else:
            chunks.append(buf)
            buf = sent
    if buf:
        chunks.append(buf)
    return chunks


def beat_fingerprint(tts_cfg: dict[str, Any], text: str, voice_id: str | None) -> str:
    payload = {
        "pipeline_v": 2,
        "model": "voice_cloning" if voice_id else tts_cfg["model"],
        "language_code": tts_cfg["language_code"],
        "speaker": None if voice_id else tts_cfg["speaker"],
        "voice_id": voice_id,
        "pace": tts_cfg["pace"],
        "pitch": None if voice_id else tts_cfg["pitch"],
        "loudness": None if voice_id else tts_cfg["loudness"],
        "temperature": None if voice_id else tts_cfg["temperature"],
        "speech_sample_rate": tts_cfg["speech_sample_rate"],
        "enable_preprocessing": None if voice_id else tts_cfg["enable_preprocessing"],
        "enable_text_normalization": tts_cfg.get("enable_text_normalization", False) if voice_id else None,
        "enable_qc": tts_cfg.get("enable_qc", True) if voice_id else None,
        "enable_vad": tts_cfg.get("enable_vad", False) if voice_id else None,
        "max_chunk_chars": tts_cfg.get("max_chunk_chars", 185) if voice_id else None,
        "sentence_gap_s": tts_cfg.get("sentence_gap_s", 0.16) if voice_id else None,
        "text": text,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def synthesize_chunk(
    client: httpx.Client,
    api_key: str,
    tts_cfg: dict[str, Any],
    text: str,
    out_wav: Path,
    *,
    voice_id: str | None = None,
) -> str | None:
    headers = {"api-subscription-key": api_key}
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            if voice_id:
                fields = [
                    ("voice_id", (None, voice_id)),
                    ("text", (None, text)),
                    ("language_code", (None, str(tts_cfg["language_code"]))),
                    ("pace", (None, str(tts_cfg["pace"]))),
                    ("output_audio_codec", (None, "wav")),
                    ("speech_sample_rate", (None, str(tts_cfg["speech_sample_rate"]))),
                    (
                        "enable_text_normalization",
                        (None, str(bool(tts_cfg.get("enable_text_normalization", False))).lower()),
                    ),
                    ("enable_qc", (None, str(bool(tts_cfg.get("enable_qc", True))).lower())),
                    ("enable_vad", (None, str(bool(tts_cfg.get("enable_vad", False))).lower())),
                ]
                resp = client.post(SARVAM_CLONE_URL, headers=headers, files=fields, timeout=120.0)
                if resp.status_code in {429, 500, 502, 503, 504} and attempt < 3:
                    time.sleep(2**attempt)
                    continue
                if resp.status_code >= 400:
                    raise RuntimeError(f"Sarvam Voice Cloning failed ({resp.status_code}): {resp.text[:400]}")
                body = resp.json()
                raw_b64 = body.get("audio_b64") or body.get("audio")
                if not raw_b64:
                    raise RuntimeError(f"Unexpected clone response keys: {sorted(body)}")
                out_wav.write_bytes(base64.b64decode(raw_b64))
                return body.get("request_id")

            payload = {
                "text": text,
                "language_code": tts_cfg["language_code"],
                "model": tts_cfg["model"],
                "speaker": tts_cfg["speaker"],
                "pace": tts_cfg["pace"],
                "pitch": tts_cfg["pitch"],
                "loudness": tts_cfg["loudness"],
                "temperature": tts_cfg["temperature"],
                "speech_sample_rate": tts_cfg["speech_sample_rate"],
                "enable_preprocessing": tts_cfg["enable_preprocessing"],
                "output_audio_codec": "wav",
            }
            resp = client.post(SARVAM_TTS_URL, headers=headers, json=payload, timeout=90.0)
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < 3:
                time.sleep(2**attempt)
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"Sarvam TTS failed ({resp.status_code}): {resp.text[:400]}")
            body = resp.json()
            audios = body.get("audios") or []
            if not audios:
                raise RuntimeError(f"Sarvam TTS returned no audio (request_id={body.get('request_id')})")
            out_wav.write_bytes(base64.b64decode(audios[0]))
            return body.get("request_id")
        except httpx.HTTPError as exc:
            last_err = exc
            if attempt < 3:
                time.sleep(2**attempt)
                continue
            raise
    if last_err is not None:
        raise last_err
    return None


def polish_and_stitch_chunks(
    chunk_wavs: list[Path],
    out_wav: Path,
    *,
    sample_rate: int,
    sentence_gap_s: float,
) -> None:
    """Trim dead air gently, apply 15 ms micro-fades, stitch with natural breath, normalize loudness."""
    with tempfile.TemporaryDirectory(prefix="delhi-tts-polish-") as tmp_str:
        tmp_dir = Path(tmp_str)
        clean_parts: list[Path] = []
        for idx, raw_wav in enumerate(chunk_wavs):
            clean = tmp_dir / f"clean_{idx:02d}.wav"
            trim_af = (
                "silenceremove=start_periods=1:start_duration=0:start_threshold=-42dB:start_silence=0.05,"
                "areverse,"
                "silenceremove=start_periods=1:start_duration=0:start_threshold=-42dB:start_silence=0.07,"
                "areverse,"
                "afade=t=in:st=0:d=0.015,"
                f"aresample={sample_rate},aformat=channel_layouts=mono"
            )
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(raw_wav),
                    "-af", trim_af,
                    "-c:a", "pcm_s16le", "-ar", str(sample_rate), "-ac", "1",
                    str(clean),
                ],
                check=True,
            )
            dur = probe_duration(clean)
            faded = tmp_dir / f"faded_{idx:02d}.wav"
            fade_st = max(0.0, dur - 0.02)
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(clean),
                    "-af", f"afade=t=out:st={fade_st:.3f}:d=0.02",
                    "-c:a", "pcm_s16le", "-ar", str(sample_rate), "-ac", "1",
                    str(faded),
                ],
                check=True,
            )
            clean_parts.append(faded)

        if len(clean_parts) == 1:
            stitched = clean_parts[0]
        else:
            gap_wav = tmp_dir / "gap.wav"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=mono",
                    "-t", f"{sentence_gap_s:.3f}",
                    "-c:a", "pcm_s16le",
                    str(gap_wav),
                ],
                check=True,
            )
            concat_txt = tmp_dir / "chunks.txt"
            lines: list[str] = []
            for idx, part in enumerate(clean_parts):
                lines.append(f"file '{part}'\n")
                if idx < len(clean_parts) - 1:
                    lines.append(f"file '{gap_wav}'\n")
            concat_txt.write_text("".join(lines), encoding="utf-8")
            stitched = tmp_dir / "stitched.wav"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "concat", "-safe", "0", "-i", str(concat_txt),
                    "-c:a", "pcm_s16le", "-ar", str(sample_rate), "-ac", "1",
                    str(stitched),
                ],
                check=True,
            )

        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(stitched),
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=9",
                "-c:a", "pcm_s16le", "-ar", str(sample_rate), "-ac", "1",
                str(out_wav),
            ],
            check=True,
        )


def synthesize_beat(
    client: httpx.Client,
    api_key: str,
    tts_cfg: dict[str, Any],
    text: str,
    out_wav: Path,
    *,
    voice_id: str | None = None,
) -> list[str]:
    sample_rate = int(tts_cfg["speech_sample_rate"])
    sentence_gap_s = float(tts_cfg.get("sentence_gap_s", 0.16))
    max_chunk_chars = int(tts_cfg.get("max_chunk_chars", 185))
    chunks = split_qc_chunks(text, max_chunk_chars) if voice_id else [text]
    request_ids: list[str] = []

    with tempfile.TemporaryDirectory(prefix="delhi-tts-raw-") as tmp_str:
        tmp_dir = Path(tmp_str)
        raw_wavs: list[Path] = []
        for idx, chunk in enumerate(chunks):
            chunk_wav = tmp_dir / f"raw_{idx:02d}.wav"
            req_id = synthesize_chunk(client, api_key, tts_cfg, chunk, chunk_wav, voice_id=voice_id)
            if req_id:
                request_ids.append(req_id)
            raw_wavs.append(chunk_wav)
        polish_and_stitch_chunks(raw_wavs, out_wav, sample_rate=sample_rate, sentence_gap_s=sentence_gap_s)

    return request_ids


def ensure_audio_beats(
    script: dict[str, Any],
    audio_dir: Path,
    *,
    speaker: str | None,
    voice_id: str | None,
    pace: float | None,
    force_tts: bool,
    allow_synthesize: bool,
) -> list[tuple[dict[str, Any], Path, float]]:
    audio_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = audio_dir / "manifest.json"
    manifest: dict[str, Any] = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    tts_cfg = dict(script["tts"])
    if speaker:
        tts_cfg["speaker"] = speaker
        effective_voice_id = voice_id
    else:
        effective_voice_id = voice_id if voice_id is not None else tts_cfg.get("voice_id")
    if pace is not None:
        tts_cfg["pace"] = pace

    results: list[tuple[dict[str, Any], Path, float]] = []
    client: httpx.Client | None = None
    api_key: str | None = None
    total_chars = 0

    try:
        for beat in script["beats"]:
            beat_id = beat["id"]
            text = (beat.get("tts_text") or beat["text"]).strip()
            wav_path = audio_dir / f"{beat_id}.wav"
            fp = beat_fingerprint(tts_cfg, text, effective_voice_id)
            cached_fp = manifest.get(beat_id, {}).get("fingerprint")
            req_ids: list[str] = manifest.get(beat_id, {}).get("request_ids", [])

            needs_gen = force_tts or not wav_path.exists() or (allow_synthesize and cached_fp != fp)
            if needs_gen:
                if not allow_synthesize:
                    raise FileNotFoundError(
                        f"Missing pre-generated audio {wav_path} (run with --tts-only where SARVAM_API_KEY is set)."
                    )
                if api_key is None:
                    api_key = load_env_key("SARVAM_API_KEY")
                    if not api_key:
                        raise RuntimeError(
                            "SARVAM_API_KEY not found in environment or repo .env. "
                            "Set it or pass --use-existing-audio with pre-generated WAVs."
                        )
                    client = httpx.Client()
                chunks = split_qc_chunks(text, int(tts_cfg.get("max_chunk_chars", 185))) if effective_voice_id else [text]
                label = effective_voice_id or tts_cfg["speaker"]
                print(f"[tts] Synthesizing {beat_id} ({len(text)} chars, {len(chunks)} QC chunk(s), voice={label})...")
                req_ids = synthesize_beat(client, api_key, tts_cfg, text, wav_path, voice_id=effective_voice_id)
                total_chars += len(text)

            dur = probe_duration(wav_path)
            manifest[beat_id] = {
                "fingerprint": fp,
                "duration_s": round(dur, 3),
                "chars": len(text),
                "speaker": effective_voice_id or tts_cfg["speaker"],
                "model": "voice_cloning" if effective_voice_id else tts_cfg["model"],
                "request_ids": req_ids,
                "file": wav_path.name,
            }
            results.append((beat, wav_path, dur))
    finally:
        if client is not None:
            client.close()

    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    total_audio = sum(d for _, _, d in results)
    print(f"[tts] Ready: {len(results)} beats, speech={total_audio:.1f}s (synthesized {total_chars} chars this run)")
    return results


def verify_audio_stt(beats: list[tuple[dict[str, Any], Path, float]], language_code: str) -> None:
    api_key = load_env_key("SARVAM_API_KEY")
    if not api_key:
        raise RuntimeError("SARVAM_API_KEY required for --verify-stt.")
    with httpx.Client(timeout=60.0) as client:
        for beat, wav_path, dur in beats:
            resp = client.post(
                SARVAM_STT_URL,
                headers={"api-subscription-key": api_key},
                data={"language_code": language_code},
                files={"file": (wav_path.name, wav_path.read_bytes(), "audio/wav")},
            )
            resp.raise_for_status()
            transcript = resp.json().get("transcript", "")
            expected = (beat.get("tts_text") or beat["text"]).strip()
            print(f"\n[stt] {beat['id']} ({dur:.1f}s)")
            print(f"  EXP: {expected}")
            print(f"  GOT: {transcript}")


def render_beat_segment(
    beat: dict[str, Any],
    wav_path: Path,
    audio_dur: float,
    out_mp4: Path,
    *,
    segments_dir: Path | None,
    source_webm: Path | None,
    width: int,
    height: int,
    fps: int,
    lead_in_s: float,
    tail_pad_s: float,
) -> float:
    target_dur = lead_in_s + audio_dur + tail_pad_s
    scale_vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x0b1120,"
        f"fps={fps},setsar=1,format=yuv420p"
    )
    delay_ms = round(lead_in_s * 1000)
    audio_af = (
        f"adelay={delay_ms}|{delay_ms},"
        f"apad=whole_dur={target_dur:.3f},"
        f"atrim=0:{target_dur:.3f},"
        f"aresample=48000,aformat=channel_layouts=stereo"
    )

    clip_path = (segments_dir / beat["source_clip"]) if segments_dir else None

    if beat["kind"] == "slide":
        if not clip_path or not clip_path.exists():
            raise FileNotFoundError(
                f"Slide PNG not found: {clip_path}. Capture slides via demo/record_demo.sh --narrate."
            )
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-loop", "1", "-framerate", str(fps), "-t", f"{target_dur:.3f}", "-i", str(clip_path),
            "-i", str(wav_path),
            "-vf", scale_vf,
            "-af", audio_af,
            "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-t", f"{target_dur:.3f}",
            str(out_mp4),
        ]
        subprocess.run(cmd, check=True)
        return target_dur

    if clip_path and clip_path.exists():
        vid_dur = probe_duration(clip_path)
        pad_s = max(0.0, target_dur - vid_dur + 0.25)
        vf = f"{scale_vf},tpad=stop_mode=clone:stop_duration={pad_s:.3f},trim=duration={target_dur:.3f},setpts=PTS-STARTPTS"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(clip_path),
            "-i", str(wav_path),
            "-vf", vf,
            "-af", audio_af,
            "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-t", f"{target_dur:.3f}",
            str(out_mp4),
        ]
        subprocess.run(cmd, check=True)
        return target_dur

    if source_webm and source_webm.exists() and "fallback_slice" in beat:
        start_s, end_s = beat["fallback_slice"]
        slice_dur = max(0.2, float(end_s) - float(start_s))
        pad_s = max(0.0, target_dur - slice_dur + 0.25)
        vf = f"{scale_vf},tpad=stop_mode=clone:stop_duration={pad_s:.3f},trim=duration={target_dur:.3f},setpts=PTS-STARTPTS"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{start_s:.3f}", "-t", f"{slice_dur:.3f}", "-i", str(source_webm),
            "-i", str(wav_path),
            "-vf", vf,
            "-af", audio_af,
            "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-t", f"{target_dur:.3f}",
            str(out_mp4),
        ]
        subprocess.run(cmd, check=True)
        return target_dur

    raise FileNotFoundError(
        f"No video source for beat {beat['id']} (expected {clip_path} or --source-webm)."
    )


def write_srt(entries: list[tuple[float, float, str]], out_srt: Path) -> None:
    lines: list[str] = []
    for idx, (start_s, end_s, text) in enumerate(entries, start=1):
        lines.append(f"{idx}\n{fmt_srt_ts(start_s)} --> {fmt_srt_ts(end_s)}\n{text}\n")
    out_srt.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", type=Path, default=REPO_ROOT / "demo" / "script.json")
    parser.add_argument("--audio-dir", type=Path, default=REPO_ROOT / "demo" / "audio")
    parser.add_argument("--segments-dir", type=Path, default=None)
    parser.add_argument("--source-webm", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "demo" / "demo_narrated.mp4")
    parser.add_argument("--also-webm", action="store_true", help="Also transcode a WebM copy alongside MP4")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--speaker", type=str, default=None, help="Override Bulbul v4 Flash speaker ID")
    parser.add_argument("--voice-id", type=str, default=None, help="Use a Sarvam Instant Voice Cloning voice_id")
    parser.add_argument("--pace", type=float, default=None, help="Override speech pace (0.5-2.0)")
    parser.add_argument("--tts-only", action="store_true", help="Only synthesize/verify WAVs in --audio-dir")
    parser.add_argument("--verify-stt", action="store_true", help="Round-trip verify generated WAVs via Sarvam STT")
    parser.add_argument("--force-tts", action="store_true", help="Re-synthesize all WAVs even if cached")
    parser.add_argument("--use-existing-audio", action="store_true", help="Require pre-generated WAVs; never call Sarvam API")
    args = parser.parse_args()

    script = json.loads(args.script.read_text(encoding="utf-8"))
    allow_synth = not args.use_existing_audio
    beats = ensure_audio_beats(
        script,
        args.audio_dir,
        speaker=args.speaker,
        voice_id=args.voice_id,
        pace=args.pace,
        force_tts=args.force_tts,
        allow_synthesize=allow_synth,
    )

    if args.verify_stt:
        verify_audio_stt(beats, script["tts"]["language_code"])

    if args.tts_only:
        total = sum(d for _, _, d in beats)
        pad_total = len(beats) * (script.get("lead_in_s", 0.2) + script.get("tail_pad_s", 0.35))
        print(f"[tts] Done. Pure speech: {total:.1f}s | With beat padding: {total + pad_total:.1f}s")
        return

    lead_in_s = float(script.get("lead_in_s", 0.2))
    tail_pad_s = float(script.get("tail_pad_s", 0.35))

    with tempfile.TemporaryDirectory(prefix="delhi-demo-") as tmp_str:
        tmp_dir = Path(tmp_str)
        part_files: list[Path] = []
        srt_entries: list[tuple[float, float, str]] = []
        cursor_s = 0.0

        for beat, wav_path, audio_dur in beats:
            part_mp4 = tmp_dir / f"{beat['id']}.mp4"
            print(f"[video] Assembling {beat['id']} ({audio_dur:.1f}s speech -> {lead_in_s + audio_dur + tail_pad_s:.1f}s)...")
            seg_dur = render_beat_segment(
                beat,
                wav_path,
                audio_dur,
                part_mp4,
                segments_dir=args.segments_dir,
                source_webm=args.source_webm,
                width=args.width,
                height=args.height,
                fps=args.fps,
                lead_in_s=lead_in_s,
                tail_pad_s=tail_pad_s,
            )
            part_files.append(part_mp4)
            caption = (beat.get("caption") or beat.get("text") or beat["tts_text"]).strip()
            srt_entries.append((cursor_s + lead_in_s, cursor_s + lead_in_s + audio_dur, caption))
            cursor_s += seg_dur

        concat_txt = tmp_dir / "concat.txt"
        concat_txt.write_text("".join(f"file '{p}'\n" for p in part_files), encoding="utf-8")

        args.output.parent.mkdir(parents=True, exist_ok=True)
        print(f"[video] Concatenating & loudness-normalizing -> {args.output}")
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(concat_txt),
                "-c:v", "copy",
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=9",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                "-movflags", "+faststart",
                str(args.output),
            ],
            check=True,
        )

        srt_path = args.output.with_suffix(".srt")
        write_srt(srt_entries, srt_path)
        print(f"[video] Wrote subtitles -> {srt_path}")

        if args.also_webm:
            webm_out = args.output.with_suffix(".webm")
            print(f"[video] Transcoding WebM -> {webm_out}")
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(args.output),
                    "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "30",
                    "-c:a", "libopus", "-b:a", "128k",
                    str(webm_out),
                ],
                check=True,
            )

    final_dur = probe_duration(args.output)
    print(f"[video] Complete: {args.output} ({final_dur:.1f}s)")


if __name__ == "__main__":
    main()
