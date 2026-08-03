from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from aiohttp import web

from vice.config import Config, RecordingConfig, clamp_recording_limits
from vice.editor import Source, build_export_cmd, validate_project
from vice.media import probe_media
from vice.recorder import (_apply_full_mix, _gsr_advanced_args, _gsr_audio_args,
                           _mix_gain, build_full_mix_cmd)
from vice.share import (ShareServer, _trim_copy_path, _valid_trim_result,
                        audio_preview_cache_key)


ROOT = Path(__file__).resolve().parents[1]


class _JsonRequest:
    def __init__(self, body):
        self.body = body
        self.match_info = {}

    async def json(self):
        return self.body


def project(audio_item):
    return {"tracks": [{"id": "T1", "type": "text"}, {"id": "V1", "type": "video"},
                       {"id": "A1", "type": "audio"}], "items": [audio_item]}


class EditorAudioTests(unittest.TestCase):
    def setUp(self):
        self.sources = {"clip": Source(Path("/tmp/clip.mp4"), 10, 1920, 1080, True, 4)}

    def test_old_project_defaults_stream_and_volume(self):
        item = {"id": "i1", "kind": "audio", "trackId": "A1", "clipId": "clip",
                "start": 0, "dur": 2, "offset": 0}
        out, errors = validate_project(project(item), self.sources)
        self.assertEqual(errors, [])
        self.assertEqual(out["items"][0]["audioStreamIndex"], 0)
        self.assertEqual(out["items"][0]["volume"], 1.0)

    def test_stream_three_and_gain_are_exported(self):
        item = {"id": "i1", "kind": "audio", "trackId": "A1", "clipId": "clip",
                "start": 0, "dur": 2, "offset": 0, "audioStreamIndex": 3, "volume": .35}
        valid, errors = validate_project(project(item), self.sources)
        self.assertFalse(errors)
        graph = build_export_cmd(valid, self.sources, Path("/tmp/out.mp4"))[build_export_cmd(valid, self.sources, Path("/tmp/out.mp4")).index("-filter_complex") + 1]
        self.assertIn("[0:a:3]", graph)
        self.assertIn("volume=0.35", graph)

    def test_missing_stream_is_controlled(self):
        item = {"id": "i1", "kind": "audio", "trackId": "A1", "clipId": "clip",
                "start": 0, "dur": 2, "offset": 0, "audioStreamIndex": 9}
        _, errors = validate_project(project(item), self.sources)
        self.assertTrue(any("does not exist" in error for error in errors))

    def test_frontend_detach_and_copy_contract(self):
        core = (ROOT / "vice/ui/scripts/editor-core.js").read_text()
        self.assertIn("streams.forEach", core)
        self.assertIn("existing.has(streamIndex)", core)
        self.assertIn("Object.assign({}, it", core)  # split/duplicate retain fields
        self.assertIn("JSON.parse(JSON.stringify(it))", core)  # clipboard retains fields
        self.assertIn("audioStreamIndex: streamIndex, volume: 1.0", core)

    def test_preview_uses_stream_endpoint_and_web_audio_gain(self):
        preview = (ROOT / "vice/ui/scripts/editor-preview.js").read_text()
        self.assertIn("/api/editor/audio/", preview)
        self.assertIn("createMediaElementSource(el)", preview)
        self.assertIn("state.gain.gain.value", preview)


class FullMixTests(unittest.TestCase):
    def rc(self):
        return SimpleNamespace(audio_tracks=["app-inverse:Zen", "app:Zen", "device:mic"],
            audio_tracks_mix_first=True, audio_track_mix_gains={"device:mic": .35},
            audio_track_names={}, capture_audio=True, capture_microphone=False)

    def test_gsr_keeps_three_separate_raw_arguments(self):
        args = _gsr_audio_args(self.rc())
        self.assertEqual(args, ["-a", "app-inverse:Zen", "-a", "app:Zen", "-a", "device:mic"])
        self.assertNotIn("|", "".join(args))

    def test_gsr_accepts_explicit_combined_full_mix_as_one_track(self):
        mic = "device:alsa_input.usb-3142_FIFINE_Microphone-00.mono-fallback"
        rc = self.rc()
        rc.audio_tracks = [f"default_output|{mic}", "app-inverse:Zen", "app:Zen", mic]
        rc.audio_tracks_mix_first = False
        self.assertEqual(_gsr_audio_args(rc), [
            "-a", f"default_output|{mic}",
            "-a", "app-inverse:Zen",
            "-a", "app:Zen",
            "-a", mic,
        ])
        rc.audio_tracks_mix_first = True
        rc.audio_track_mix_gains = {"app-inverse:Zen": 1, "app:Zen": 1, mic: .35}
        cmd = build_full_mix_cmd(Path("in.mkv"), Path("out.mkv"), rc)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("[0:a:1]", graph)
        self.assertIn("[0:a:3]", graph)
        self.assertIn("volume=0.35", graph)
        self.assertNotIn("[0:a:0]aformat", graph)
        maps = [cmd[i + 1] for i, value in enumerate(cmd[:-1]) if value == "-map"]
        self.assertEqual(maps, ["0:v:0", "[fullmix]", "0:a:1", "0:a:2", "0:a:3"])

    def test_full_mix_maps_semantic_order_and_gain(self):
        cmd = build_full_mix_cmd(Path("in.mp4"), Path("out.mp4"), self.rc())
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("[0:a:2]", graph)
        self.assertIn("volume=0.35", graph)
        maps = [cmd[i + 1] for i, value in enumerate(cmd[:-1]) if value == "-map"]
        self.assertEqual(maps, ["0:v:0", "[fullmix]", "0:a:0", "0:a:1", "0:a:2"])
        self.assertIn("title=Full Mix", cmd)

    def test_gains_clamp_and_invalid_is_safe(self):
        rc = self.rc(); rc.audio_track_mix_gains = {"device:mic": 9, "app:Zen": "bad"}
        self.assertEqual(_mix_gain(rc, "device:mic"), 2.0)
        self.assertEqual(_mix_gain(rc, "app:Zen"), 1.0)


class GsrAdvancedSettingsTests(unittest.TestCase):
    def test_disabled_emits_nothing(self):
        self.assertEqual(_gsr_advanced_args(RecordingConfig(), []), [])

    def test_cbr_fields_generate_flags(self):
        rc = RecordingConfig(gsr_advanced_enabled=True, gsr_bitrate_mode="cbr",
            gsr_video_bitrate=40000, gsr_framerate_mode="cfr", gsr_keyint=1,
            gsr_tune="quality", gsr_audio_bitrate=192)
        self.assertEqual(_gsr_advanced_args(rc, []), [
            "-bm", "cbr", "-q", "40000", "-fm", "cfr", "-keyint", "1",
            "-tune", "quality", "-ab", "192",
        ])

    def test_manual_extra_args_have_priority(self):
        rc = RecordingConfig(gsr_advanced_enabled=True, gsr_bitrate_mode="cbr",
            gsr_video_bitrate=40000, gsr_framerate_mode="cfr")
        generated = _gsr_advanced_args(rc, ["-bm", "qp", "-q", "ultra", "-fm", "vfr"])
        self.assertNotIn("-bm", generated)
        self.assertNotIn("-q", generated)
        self.assertNotIn("-fm", generated)

    def test_invalid_values_are_clamped(self):
        cfg = Config(recording=RecordingConfig(gsr_bitrate_mode="broken",
            gsr_video_bitrate=999999, gsr_audio_bitrate=-1, gsr_keyint=0))
        clamp_recording_limits(cfg)
        self.assertEqual(cfg.recording.gsr_bitrate_mode, "qp")
        self.assertEqual(cfg.recording.gsr_video_bitrate, 200000)
        self.assertEqual(cfg.recording.gsr_audio_bitrate, 32)
        self.assertEqual(cfg.recording.gsr_keyint, 0.1)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class FullMixIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_raw_tracks_become_four_and_reprocessing_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            clip = Path(td) / "raw.mp4"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=160x90:r=10:d=0.5",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.5",
                "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=0.5",
                "-f", "lavfi", "-i", "sine=frequency=1320:sample_rate=48000:duration=0.5",
                "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:a",
                "-c:v", "libx264", "-c:a", "aac", "-shortest", str(clip),
            ], check=True)
            rc = FullMixTests().rc()
            self.assertTrue(await _apply_full_mix(clip, rc))
            first_hash = __import__("hashlib").sha256(clip.read_bytes()).hexdigest()
            meta = await probe_media(clip)
            self.assertEqual(meta["audio_streams"], 4)
            self.assertEqual([s["title"] for s in meta["audio_stream_info"]],
                             ["Full Mix", "Game + Discord", "Browser — Zen", "Microphone — device:mic"])
            self.assertTrue(await _apply_full_mix(clip, rc))
            self.assertEqual(first_hash, __import__("hashlib").sha256(clip.read_bytes()).hexdigest())


class IsolationTests(unittest.TestCase):
    def test_production_defaults(self):
        code = "from vice.config import CONFIG_PATH,Config; from vice.instance import DATA_DIR; print(CONFIG_PATH,DATA_DIR,Config().sharing.port,Config().output.directory)"
        env = {k: v for k, v in os.environ.items() if not k.startswith("VICE_")}
        out = subprocess.check_output(["python3", "-c", code], cwd=ROOT, env=env, text=True)
        self.assertIn(".config/vice/config.toml", out)
        self.assertIn(".local/share/vice", out)
        self.assertIn("8765", out)
        self.assertIn("Videos/Vice", out)

    def test_patch_defaults(self):
        code = "from vice.config import CONFIG_PATH,Config; from vice.instance import DATA_DIR,CACHE_DIR,RUNTIME_DIR; print(CONFIG_PATH,DATA_DIR,CACHE_DIR,RUNTIME_DIR,Config().sharing.port,Config().output.directory)"
        env = dict(os.environ, VICE_INSTANCE="vice-patch")
        out = subprocess.check_output(["python3", "-c", code], cwd=ROOT, env=env, text=True)
        for expected in (".config/vice-patch", ".local/share/vice-patch", ".cache/vice-patch", "/tmp/vice-patch", "8875", "Videos/Vice-Patch"):
            self.assertIn(expected, out)

    def test_cache_key_changes_with_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "clip.mp4"; path.write_bytes(b"one")
            first = audio_preview_cache_key(path, 2)
            path.write_bytes(b"different")
            self.assertNotEqual(first, audio_preview_cache_key(path, 2))

    def test_installers_only_target_patch_namespaces(self):
        installer = (ROOT / "install-patch.sh").read_text()
        uninstaller = (ROOT / "uninstall-patch.sh").read_text()
        self.assertNotIn("vice.service; then", installer)
        self.assertNotIn('rm -f "$HOME/.local/bin/vice"', installer + uninstaller)
        self.assertNotIn(".config/vice\"", uninstaller)


class AudioEndpointSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_blocks_path_traversal(self):
        server = ShareServer.__new__(ShareServer)
        server._clips = {}
        request = SimpleNamespace(match_info={"slug": "../../etc/passwd", "stream_index": "0"})
        with self.assertRaises(web.HTTPNotFound):
            await server._editor_audio_stream(request)


class SafeTrimTests(unittest.TestCase):
    def test_trim_always_uses_a_new_collision_free_path(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "hunt.mp4"
            source.write_bytes(b"original")
            self.assertEqual(_trim_copy_path(source).name, "hunt-trimmed.mp4")
            (Path(td) / "hunt-trimmed.mp4").write_bytes(b"existing")
            self.assertEqual(_trim_copy_path(source).name, "hunt-trimmed-2.mp4")
            self.assertEqual(source.read_bytes(), b"original")

    def test_one_frame_trim_is_rejected(self):
        source = {"duration": 120, "width": 1920, "audio_streams": 4}
        broken = {"duration": 0.008, "width": 1920, "audio_streams": 1}
        self.assertFalse(_valid_trim_result(source, broken, 30, 291349))

    def test_sane_trim_with_all_streams_is_accepted(self):
        source = {"duration": 120, "width": 1920, "audio_streams": 4}
        result = {"duration": 30.01, "width": 1920, "audio_streams": 4}
        self.assertTrue(_valid_trim_result(source, result, 30, 10_000_000))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class SafeTrimIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_trim_creates_copy_and_preserves_original_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "hunt.mp4"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "testsrc2=s=160x90:r=20:d=2", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=2", "-map", "0:v", "-map", "1:a",
                "-c:v", "libx264", "-c:a", "aac", "-shortest", str(source),
            ], check=True)
            original = source.read_bytes()
            server = ShareServer.__new__(ShareServer)
            server._clips = {"hunt": source}
            server._meta = {}
            server._broadcast_clip = mock.AsyncMock()
            request = _JsonRequest({"start": .5, "end": 1.5})
            request.match_info = {"slug": "hunt"}
            response = await server._api_trim(request)
            payload = __import__("json").loads(response.body)
            self.assertTrue(payload["copied"])
            self.assertEqual(source.read_bytes(), original)
            copy_path = Path(td) / payload["name"]
            self.assertTrue(copy_path.exists())
            self.assertNotEqual(copy_path, source)

    async def test_missing_stream_returns_not_found(self):
        server = ShareServer.__new__(ShareServer)
        server._clips = {"clip": Path("/tmp/clip.mp4")}
        server._get_meta = mock.AsyncMock(return_value={"audio_stream_info": [{"index": 0}]})
        request = SimpleNamespace(match_info={"slug": "clip", "stream_index": "4"})
        with self.assertRaises(web.HTTPNotFound):
            await server._editor_audio_stream(request)


if __name__ == "__main__":
    unittest.main()
