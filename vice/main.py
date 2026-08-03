"""
Vice — Linux game clip recorder daemon + CLI.

Commands:
  vice start          Start the daemon (recorder + hotkey listener + share server)
  vice ui             Open the web UI in the default browser
  vice clip           Manually save a clip right now (daemon must be running)
  vice stop           Stop the daemon
  vice status         Show daemon status and recent clips
  vice doctor         Print startup diagnostics for environment/package issues
  vice config         Print the current config path and contents
  vice list-keys      Show available hotkey names (KEY_*)
  vice open-config    Open config in $EDITOR
  vice uninstall      Remove Vice cleanly (service, config, optionally clips)
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from dataclasses import asdict
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import click

from . import __version__
from .config import (
    Config,
    CONFIG_DIR,
    CONFIG_PATH,
    effective_clip_bindings,
    load as load_config,
    save as save_config,
)
from .hotkey import HotkeyListener, can_access_hotkeys, list_available_keys
from .media import cleanup_temp_files
from .recorder import create_recorder
from .runtime import (
    actual_home_dir,
    normalize_runtime_environment,
    resolve_path,
    runtime_env_snapshot,
    user_systemd_env_snapshot,
)
from .share import ShareServer
from . import audio
from . import updates
from .instance import (APP_CLI_NAME, APP_NAME, CLI_NAME, DATA_DIR, INSTANCE,
                       RUNTIME_DIR, SERVICE_NAME)

log = logging.getLogger("vice")


def _load_default_games() -> list[dict]:
    """Load the bundled games.json. Returns [] if missing/corrupt rather
    than crashing the daemon."""
    try:
        from importlib.resources import files
        text = (files("vice") / "data" / "games.json").read_text(encoding="utf-8")
        data = json.loads(text)
        if isinstance(data, list):
            return [g for g in data if isinstance(g, dict) and g.get("name")]
    except Exception as exc:
        log.warning("Failed to load bundled games.json: %s", exc)
    return []


_DEFAULT_GAMES: list[dict] = _load_default_games()

PID_FILE    = RUNTIME_DIR / f"{INSTANCE}.pid"
SOCKET_FILE = RUNTIME_DIR / f"{INSTANCE}.sock"
USER_BIN_DIR = actual_home_dir() / ".local" / "bin"
INSTALL_VENV_DIR = DATA_DIR / "venv"
USER_DESKTOP_FILE = actual_home_dir() / ".local" / "share" / "applications" / f"{INSTANCE}.desktop"
USER_ICON_FILE = (
    actual_home_dir()
    / ".local"
    / "share"
    / "icons"
    / "hicolor"
    / "scalable"
    / "apps"
    / f"{INSTANCE}.svg"
)
DAEMON_LOG_FILE = DATA_DIR / f"{INSTANCE}.log"

# Consecutive unexpected recorder deaths before the watchdog starts backing
# off. Two is normal turbulence (a driver reset, a suspend edge); a third in a
# row means the recorder is not coming back on its own.
_RECORDER_DEATH_BACKOFF_AFTER = 3

# How often follow-the-pointer capture samples which monitor the pointer is on.
# Two samples must agree before the recorder is retargeted, so a switch costs
# up to twice this.
FOLLOW_MOUSE_INTERVAL = 2.0


def _patch_recorder_conflict() -> Optional[str]:
    """Refuse capture coexistence; never signal or otherwise alter the owner."""
    if INSTANCE != "vice-patch":
        return None
    production_socket = Path("/tmp/vice/vice.sock")
    if production_socket.exists():
        return "production Vice appears to be running; stop vice.service manually first"
    try:
        found = subprocess.run(
            ["pgrep", "-x", "gpu-screen-recorder"], capture_output=True, text=True,
            timeout=2, check=False)
        pids = [p for p in found.stdout.split() if p.isdigit() and int(p) != os.getpid()]
        if pids:
            return "another gpu-screen-recorder process is already running; it was not stopped"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Daemon
# ──────────────────────────────────────────────────────────────────────────────

class ViceDaemon:
    def __init__(self) -> None:
        self.cfg      = load_config()
        self.recorder = create_recorder(self.cfg)
        self.hotkeys  = HotkeyListener()
        self.share:   Optional[ShareServer] = None
        self.hotkeys_available = can_access_hotkeys()
        self._clip_lock  = asyncio.Lock()
        self._clip_count = 0
        # Session recording state
        self._session_active   = False
        self._session_path:    Optional[Path] = None
        self._session_highlights: list[dict] = []  # {time, label, color}
        self._recording_sig = self._recording_signature()
        self._pending_recording_apply = False
        self._config_apply_lock = asyncio.Lock()
        self._clip_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._update_task: Optional[asyncio.Task] = None
        self._update: Optional[dict] = None
        self._ready = False
        # Discord Rich Presence — default enabled, but only shown for matched games.
        self._discord_rpc = None  # type: ignore[var-annotated]
        self._discord_task: Optional[asyncio.Task] = None
        self._discord_client_id: Optional[str] = None
        self._discord_current_game: Optional[str] = None
        self._discord_started_at: float = 0.0
        self._discord_last_activity: Optional[dict] = None
        self._discord_current_pid = 0
        self._discord_game_comm = ""
        self._discord_scan_tick = 0
        self._discord_no_socket_logged = False
        self._discord_no_window_adapter_logged = False
        # Game detected while the most recent clip was being saved, consumed
        # by _on_clip_saved to file the clip into its auto playlist.
        self._last_clip_game: Optional[str] = None
        # Monitor the pointer is on, when follow-the-pointer capture is on.
        # None means "use recording.display".
        self._display_override: Optional[str] = None
        self._follow_mouse_task: Optional[asyncio.Task] = None

    async def run(self) -> None:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        out_dir = resolve_path(self.cfg.output.directory)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            probe = out_dir / ".vice-write-test"
            probe.touch()
            probe.unlink()
        except OSError as exc:
            raise RuntimeError(
                f"Clip output directory {out_dir} is not writable: {exc}. "
                "Fix permissions or change output.directory in "
                f"{CONFIG_PATH}."
            ) from exc
        # Remove half-written temp files (trim/watermark/remux) from a
        # previous run that was interrupted mid-edit.
        cleanup_temp_files(out_dir)

        # Share server (web UI + REST API + WebSocket)
        if self.cfg.sharing.enabled:
            self.share = ShareServer(self.cfg)
            self.share.trigger_clip_cb = self._handle_clip_hotkey
            self.share.check_update_cb = lambda: self.run_update_check(force=True)
            self.share.get_status_cb   = self._get_status
            self.share.apply_config_cb = self._apply_live_config
            try:
                await self.share.start()
            except Exception:
                log.exception(
                    "Failed to start share server on 127.0.0.1:%s",
                    self.cfg.sharing.port,
                )
                raise

        # Wire the recorder's callbacks. Must also run when a settings change
        # swaps in a new recorder, or game tagging and auto playlists silently
        # stop working.
        self._wire_recorder(self.recorder)

        # Hotkeys
        self._bind_hotkeys()
        self.hotkeys.on_availability_change = self._on_hotkey_availability
        clip_key = self.cfg.hotkeys.clip

        PID_FILE.write_text(str(os.getpid()))

        server = await asyncio.start_unix_server(
            self._handle_ipc, path=str(SOCKET_FILE)
        )

        try:
            await self.hotkeys.start()
            self.hotkeys_available = self.hotkeys.available
            conflict = _patch_recorder_conflict()
            if conflict:
                raise RuntimeError(f"Vice Patch recorder refused to start: {conflict}")
            await self.recorder.start()
            self._ready = True
        except Exception as exc:
            log.error(
                "Vice daemon failed during startup (backend=%s): %s",
                self.recorder.name, exc,
            )
            log.exception("Startup traceback")
            try:
                server.close()
                await server.wait_closed()
            except Exception:
                pass
            try:
                await self.hotkeys.stop()
            except Exception:
                pass
            try:
                await self.recorder.stop()
            except Exception:
                pass
            if self.share:
                try:
                    await self.share.stop()
                except Exception:
                    pass
            for p in (PID_FILE, SOCKET_FILE):
                try:
                    if p.exists():
                        p.unlink()
                except OSError:
                    pass
            raise
        if self.share:
            log.info("Vice local control UI: %s", self.share.local_base_url())
        else:
            log.info("Vice local control UI disabled by config")
        log.info(
            "Vice daemon ready (backend=%s, share_enabled=%s)",
            self.recorder.name,
            bool(self.share),
        )

        if self.share:
            asyncio.create_task(
                self.share.broadcast({
                    "type": "status", "recording": True, "ready": self._ready,
                    "backend": self.recorder.name,
                    "session_active": self._session_active,
                    "clip_key": self.cfg.hotkeys.clip,
                    "hotkeys_available": self.hotkeys_available,
                })
            )

        click.echo(f"[Vice {__version__}] Recording started.")
        click.echo(f"  Backend   : {self.recorder.name}")
        click.echo(f"  Clip key  : {clip_key or '(none)'}")
        click.echo(f"  Output    : {self.cfg.output.directory}")
        if self.share and self.share.local_base_url():
            click.echo(f"  UI URL    : {self.share.local_base_url()}/")
        if self.share and self.share.public_base_url():
            click.echo(f"  Share URL : {self.share.public_base_url()}/")
        click.echo("Press Ctrl-C to stop.\n")

        if self.cfg.discord.enabled:
            self._discord_task = asyncio.create_task(self._discord_presence_loop())

        self._watchdog_task = asyncio.create_task(self._recorder_watchdog_loop())
        self._sync_follow_mouse_task()

        if self.cfg.updates.check_on_start:
            self._update_task = asyncio.create_task(self._update_check_soon())

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
        loop.add_signal_handler(signal.SIGINT,  stop_event.set)

        await stop_event.wait()
        await self._shutdown(server)

    def _on_hotkey_availability(self, available: bool) -> None:
        """Keep the UI banner truthful when keyboards unplug/replug."""
        self.hotkeys_available = available
        if self.share:
            asyncio.create_task(
                self.share.broadcast({
                    "type": "status", "recording": self._ready, "ready": self._ready,
                    "backend": self.recorder.name,
                    "session_active": self._session_active,
                    "clip_key": self.cfg.hotkeys.clip,
                    "hotkeys_available": available,
                })
            )

    def _recording_signature(self) -> str:
        """Stable representation of recording config for live-apply checks."""
        return json.dumps(asdict(self.cfg.recording), sort_keys=True)

    def _on_clip_saved(self, path: Path) -> None:
        self._clip_count += 1
        click.echo(f"\n[Vice] Clip saved: {path}")
        if self.share:
            # Session clips are added to the share server inside _stop_session;
            # only add here for regular replay-buffer clips (not sessions).
            if not path.name.startswith("Vice_Session_"):
                url = self.share.add_clip(path, game=self._last_clip_game)
                self._last_clip_game = None
                click.echo(f"[Vice] Share URL:  {url}\n")
            asyncio.create_task(
                self.share.broadcast({
                    "type": "status", "recording": True, "ready": self._ready,
                    "backend": self.recorder.name,
                    "session_active": self._session_active,
                    "clip_key": self.cfg.hotkeys.clip,
                    "hotkeys_available": self.hotkeys_available,
                })
            )

    def _broadcast_status(self, recording: bool) -> None:
        if not self.share:
            return
        asyncio.create_task(
            self.share.broadcast({
                "type": "status", "recording": recording, "ready": self._ready,
                "backend": self.recorder.name,
                "session_active": self._session_active,
                "clip_key": self.cfg.hotkeys.clip,
                "hotkeys_available": self.hotkeys_available,
            })
        )

    async def _recorder_watchdog_loop(self) -> None:
        """Restart the recorder when its capture process dies (driver reset,
        crash) or after suspend/resume, which kills GPU encoder contexts even
        when the process survives (#116). asyncio.sleep runs on the monotonic
        clock, which stands still during suspend, so a wall-clock jump across
        one tick means the machine slept."""
        interval = 5.0
        backoff = interval
        deaths = 0
        last_wall = time.time()
        while True:
            await asyncio.sleep(interval)
            now = time.time()
            resumed = (now - last_wall) > interval + 30.0
            last_wall = now
            if self.recorder.is_healthy() and not resumed:
                backoff = interval
                deaths = 0
                continue
            if resumed:
                log.info("Resume from suspend detected — restarting the recorder")
            else:
                deaths += 1
                # The capture process's own output is the only thing that says
                # why it died. Without it a fatal encoder error is invisible at
                # default log level and the UI still claims to be recording
                # (#129).
                tail = self.recorder.last_output()
                if tail:
                    log.error(
                        "Recorder process died unexpectedly — restarting. Last output from %s:\n%s",
                        self.recorder.name, tail,
                    )
                else:
                    log.error("Recorder process died unexpectedly — restarting")
            try:
                async with self._config_apply_lock:
                    async with self._clip_lock:
                        if not resumed and self.recorder.is_healthy():
                            continue  # a config apply already replaced it
                        await self.recorder.stop()
                        await self.recorder.start()
            except Exception as exc:
                log.error("Recorder restart failed: %s — retrying in %.0f s", exc, backoff)
                self._broadcast_status(recording=False)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300.0)
                last_wall = time.time()
                continue
            log.info("Recorder restarted (backend=%s)", self.recorder.name)
            self._broadcast_status(recording=True)
            # A process that clears the startup probe and then dies seconds
            # later never reaches the failed-start path above, so without this
            # an unrecoverably broken recorder is retried at full speed
            # forever (#129).
            if not resumed and deaths >= _RECORDER_DEATH_BACKOFF_AFTER:
                log.error(
                    "Recorder has died %d times in a row — waiting %.0f s before the next attempt",
                    deaths, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300.0)
                last_wall = time.time()

    def _wire_recorder(self, recorder) -> None:
        """Attach the daemon's callbacks to a recorder. Fires for the initial
        recorder and for every replacement built on a config change, so game
        filename tagging and auto playlists survive settings changes."""
        recorder.on_clip_saved(self._on_clip_saved)
        # Tag clip filenames with the focused game (curated list, same
        # detection as Discord Rich Presence); also feeds the auto playlists.
        recorder.clip_tag_cb = self._clip_game_tag
        recorder.display_override = self._display_override

    async def _restart_recorder_for_config(self) -> bool:
        """Restart recorder without running two capture processes at once."""
        if self._session_active:
            self._pending_recording_apply = True
            log.info("Recording config changed during active session; applying after session ends")
            return False

        old_recorder = self.recorder
        # Snapshot config before live-apply mutates recorder behavior.
        old_cfg = copy.deepcopy(self.cfg)

        new_recorder = create_recorder(self.cfg)
        self._wire_recorder(new_recorder)

        await old_recorder.stop()
        try:
            await new_recorder.start()
        except Exception:
            # Restore old config on the current recorder object before restart.
            for field in ("recording", "hotkeys", "output", "sharing", "discord"):
                setattr(self.cfg, field, getattr(old_cfg, field))

            # Try to restore the previous recorder so capture keeps running.
            try:
                await old_recorder.start()
            except Exception as restore_exc:
                log.error("Failed to restore previous recorder: %s", restore_exc)
            raise

        self.recorder = new_recorder
        self._recording_sig = self._recording_signature()
        self._pending_recording_apply = False
        return True

    # ── follow-the-pointer capture (#133) ─────────────────────────────────────
    # No capture backend can retarget a running replay buffer, so following the
    # pointer means restarting the recorder. Two agreeing samples in a row are
    # required so dragging the mouse across a screen edge does not restart
    # anything, and the restart is skipped while a clip or session is in flight.

    def _sync_follow_mouse_task(self) -> None:
        wanted = bool(self.cfg.recording.follow_mouse_display)
        running = self._follow_mouse_task is not None and not self._follow_mouse_task.done()
        if wanted and not running:
            self._follow_mouse_task = asyncio.create_task(self._follow_mouse_loop())
        elif not wanted and running:
            self._follow_mouse_task.cancel()
            self._follow_mouse_task = None
            self._display_override = None
            self.recorder.display_override = None

    async def _follow_mouse_loop(self) -> None:
        from .active_window import pointer_display

        pending: Optional[str] = None
        try:
            while True:
                await asyncio.sleep(FOLLOW_MOUSE_INTERVAL)
                if self._session_active or (self._clip_task and not self._clip_task.done()):
                    continue
                try:
                    current = await asyncio.to_thread(pointer_display)
                except Exception:
                    log.debug("Pointer display detection failed", exc_info=True)
                    continue
                if not current or current == self._display_override:
                    pending = None
                    continue
                if current != pending:
                    pending = current
                    continue
                pending = None
                log.info("Pointer moved to %s; retargeting capture", current)
                previous = self._display_override
                self._display_override = current
                async with self._config_apply_lock:
                    async with self._clip_lock:
                        try:
                            await self._restart_recorder_for_config()
                        except Exception as exc:
                            self._display_override = previous
                            log.warning("Could not retarget capture to %s: %s", current, exc)
        except asyncio.CancelledError:
            pass

    async def _hotkeys_suppressed(self) -> bool:
        """Whether the focused app is one the user asked Vice to keep its hands
        off (#130). Only the keyboard path checks this, so the UI's clip button
        and the CLI stay live regardless."""
        matches = self.cfg.hotkeys.disable_while_focused
        if not matches:
            return False
        try:
            from .active_window import get_active_window
            win = await asyncio.to_thread(get_active_window)
        except Exception:
            log.debug("Focused-app check for hotkey suppression failed", exc_info=True)
            return False
        if not win:
            return False
        haystacks = ((win.get("process") or "").lower(), (win.get("class") or "").lower())
        for needle in matches:
            n = (needle or "").strip().lower()
            if n and any(n in h for h in haystacks):
                log.info("Hotkey ignored: %r is focused (matched %r)", haystacks[1] or haystacks[0], needle)
                return True
        return False

    def _bind_hotkeys(self) -> None:
        """(Re)bind runtime hotkeys from current config."""
        self.hotkeys.clear_bindings()
        for clip_key, duration in effective_clip_bindings(self.cfg):
            # Single tap → save clip (or add session highlight)
            async def _clip(duration=duration) -> None:
                if await self._hotkeys_suppressed():
                    return
                await self._handle_clip_hotkey(duration)

            async def _session_toggle() -> None:
                if await self._hotkeys_suppressed():
                    return
                await self._handle_session_toggle()

            self.hotkeys.on(clip_key, _clip)
            # Double tap → toggle session recording
            self.hotkeys.on_double(clip_key, _session_toggle)

    async def _apply_live_config(self) -> None:
        """Apply config changes and restart recorder when recording settings changed."""
        async with self._config_apply_lock:
            self._bind_hotkeys()
            # Before the restart check, so turning follow-the-pointer off drops
            # the override and the recorder goes back to the saved display.
            self._sync_follow_mouse_task()

            async with self._clip_lock:
                if self._recording_signature() != self._recording_sig:
                    await self._restart_recorder_for_config()

            await self._sync_discord_presence_task()

            if self.share:
                await self.share.broadcast({
                    "type": "status",
                    "recording": True,
                    "ready": self._ready,
                    "backend": self.recorder.name,
                    "session_active": self._session_active,
                    "clip_key": self.cfg.hotkeys.clip,
                    "hotkeys_available": self.hotkeys_available,
                })

    # ── Discord Rich Presence ────────────────────────────────────────────
    def _discord_configured_client_id(self) -> str:
        from .discord_rpc import DEFAULT_CLIENT_ID
        return self.cfg.discord.client_id_override or DEFAULT_CLIENT_ID

    async def _stop_discord_presence_task(self) -> None:
        if self._discord_task and not self._discord_task.done():
            self._discord_task.cancel()
            try:
                await self._discord_task
            except (asyncio.CancelledError, Exception):
                pass
        self._discord_task = None
        await self._clear_discord_presence()

    async def _sync_discord_presence_task(self) -> None:
        desired_client_id = self._discord_configured_client_id()
        if not self.cfg.discord.enabled:
            await self._stop_discord_presence_task()
            return

        if (
            self._discord_task
            and not self._discord_task.done()
            and self._discord_client_id != desired_client_id
        ):
            await self._stop_discord_presence_task()

        if self._discord_task is None or self._discord_task.done():
            self._discord_task = asyncio.create_task(self._discord_presence_loop())

    def _discord_activity(self, game: str) -> dict:
        return {
            "details": f"Clipping {game} with Vice",
            "state": game,
            "timestamps": {"start": int(self._discord_started_at)},
            "assets": {
                "large_image": "vice_logo",
                "large_text": "Vice — Linux clip recorder",
            },
        }

    async def _discord_presence_loop(self) -> None:
        """Poll the active window every 5s. When a configured game is focused,
        push "Clipping <Game> with Vice" to Discord. Clear when no game is
        focused. Exits when discord.enabled flips off."""
        from .active_window import (
            detection_tools_status,
            get_active_window,
            supported_compositor,
            uses_x11_adapter,
        )
        from .discord_rpc import DiscordRPC
        cid = self._discord_configured_client_id()
        if not cid:
            log.info("Discord RPC enabled but no client_id is set; presence disabled.")
            return
        self._discord_client_id = cid
        self._discord_rpc = DiscordRPC(cid)
        backoff = 5.0
        if not supported_compositor() and not self._discord_no_window_adapter_logged:
            log.info(
                "Discord Rich Presence is enabled, but active-window detection "
                "is unavailable on this Wayland session (no XWayland/DISPLAY). "
                "RPC still connects; games launched via XWayland are detected "
                "when DISPLAY is set."
            )
            self._discord_no_window_adapter_logged = True
        if uses_x11_adapter():
            tools = detection_tools_status()
            if not (tools["xdotool"] and tools["xprop"]):
                log.warning(
                    "Game detection on this compositor needs xdotool and xprop "
                    "(wmctrl helps too) — install them for Discord RPC and "
                    "game-tagged clips."
                )
        try:
            while True:
                if not self.cfg.discord.enabled:
                    await self._clear_discord_presence()
                    return
                connected_now = False
                if not self._discord_rpc.is_connected:
                    if not await self._discord_rpc.connect():
                        if not self._discord_no_socket_logged:
                            log.info("Discord Rich Presence is enabled, but no Discord IPC socket is reachable.")
                            self._discord_no_socket_logged = True
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 60.0)
                        continue
                    backoff = 5.0
                    connected_now = True
                    self._discord_no_socket_logged = False
                try:
                    win = get_active_window()
                    matched = self._match_game(win) if win else None
                    if matched is not None:
                        self._remember_discord_game_process(win)
                    else:
                        matched = await self._discord_unfocused_game()
                    if matched is None:
                        self._discord_current_game = None
                        self._discord_current_pid = 0
                        self._discord_game_comm = ""
                        if connected_now or self._discord_last_activity is not None:
                            if await self._discord_rpc.set_activity(None):
                                self._discord_last_activity = None
                    else:
                        if matched != self._discord_current_game:
                            self._discord_current_game = matched
                            self._discord_started_at = time.time()
                        activity = self._discord_activity(matched)
                        if connected_now or activity != self._discord_last_activity:
                            if await self._discord_rpc.set_activity(activity):
                                self._discord_last_activity = activity
                except Exception as exc:
                    log.warning("Discord presence tick failed: %s", exc)
                await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            await self._clear_discord_presence()
            raise

    def _remember_discord_game_process(self, win: Optional[dict]) -> None:
        """Snapshot the matched window's pid and comm so presence can outlive
        focus. The comm comparison guards against pid reuse."""
        from .active_window import _read_proc_comm
        pid = int((win or {}).get("pid") or 0)
        if pid <= 0:
            return
        comm = _read_proc_comm(pid)
        if comm:
            self._discord_current_pid = pid
            self._discord_game_comm = comm

    async def _discord_unfocused_game(self) -> Optional[str]:
        """The game to keep showing when no matched window is focused: the
        remembered one while its process lives (#112), else a visible-window
        scan for compositors that can't report focus reliably (#102)."""
        from .active_window import _read_proc_comm, list_candidate_windows
        if not self.cfg.discord.persist_while_running:
            return None
        if self._discord_current_game and self._discord_current_pid > 0:
            comm = _read_proc_comm(self._discord_current_pid)
            if comm and comm == self._discord_game_comm:
                return self._discord_current_game
        self._discord_scan_tick += 1
        if self._discord_scan_tick % 3:
            return None
        for win in await asyncio.to_thread(list_candidate_windows):
            matched = self._match_game(win)
            if matched:
                self._remember_discord_game_process(win)
                return matched
        return None

    async def _clear_discord_presence(self) -> None:
        if self._discord_rpc is None:
            return
        try:
            if self._discord_last_activity is not None:
                await self._discord_rpc.set_activity(None)
            await self._discord_rpc.close()
        except Exception as exc:
            log.debug("Discord clear/close raised: %s", exc)
        finally:
            self._discord_rpc = None
            self._discord_client_id = None
            self._discord_current_game = None
            self._discord_last_activity = None
            self._discord_current_pid = 0
            self._discord_game_comm = ""

    def _clip_game_tag(self) -> Optional[str]:
        """Focused game name for clip filename tagging, or None.

        Sync (the recorder runs it in a thread — window detection shells
        out to the compositor). Detection only matches the curated games
        list, so arbitrary window titles never end up in filenames.

        Detection always runs so auto playlists work even with filename
        tagging turned off; the tag itself is only returned when enabled.
        """
        game = None
        win = None
        try:
            from .active_window import get_active_window
            win = get_active_window()
            game = self._match_game(win) if win else None
        except Exception:
            log.debug("Game detection for clip tagging failed", exc_info=True)
        # One line per clip so an unmatched game or a compositor miss is
        # diagnosable from vice.log. Local only, never leaves the machine.
        log.info(
            "Clip game detection: process=%r class=%r matched=%r",
            (win or {}).get("process"), (win or {}).get("class"), game,
        )
        self._last_clip_game = game
        if not getattr(self.cfg.output, "tag_clips_with_game", False):
            return None
        return game

    def _match_game(self, win: dict) -> Optional[str]:
        proc = (win.get("process") or "").lower()
        cls  = (win.get("class") or "").lower()
        haystacks = (proc, cls)
        # User custom games first — explicit user intent beats the bundled list.
        for g in self.cfg.discord.custom_games:
            for needle in g.matches:
                n = (needle or "").lower()
                if n and any(n in h for h in haystacks):
                    return g.name
        for g in _DEFAULT_GAMES:
            for needle in g.get("matches") or []:
                n = (needle or "").lower()
                if n and any(n in h for h in haystacks):
                    return g["name"]
        return None

    def _get_status(self) -> dict:
        return {
            "ready":          self._ready,
            "recording":      True,
            "backend":          self.recorder.name,
            "clips":            self._clip_count,
            "session_active":   self._session_active,
            "clip_key":         self.cfg.hotkeys.clip,
            "hotkeys_available": self.hotkeys_available,
            # None unless a newer release is known, so a UI opened long after
            # the check still learns about it.
            "update":           self._update,
        }

    # ── update check ─────────────────────────────────────────────────────────

    def _update_install_hint(self) -> dict:
        """How this machine should update, so the notice can say it exactly."""
        if _installed_via_aur():
            return {"method": "aur", "command": "yay -Syu vice-clipper"}
        if _using_install_script_venv():
            return {"method": "script", "command": "cd Vice && git pull && ./install.sh"}
        return {"method": "unknown", "command": ""}

    async def run_update_check(self, force: bool = False) -> Optional[dict]:
        """Ask GitHub whether there is a newer release. Silent about
        everything: no network, a rate limit or a malformed reply all just
        leave the last known answer in place."""
        cache = updates.UpdateCache()
        stored = cache.load()
        if force or cache.stale():
            fetched = await asyncio.to_thread(updates.fetch_latest, stored.get("etag"))
            if fetched:
                stored = dict(fetched, checked_at=time.time())
            else:
                stored["checked_at"] = time.time()
            cache.save(stored)

        found = updates.available(stored)
        if found:
            found["install"] = await asyncio.to_thread(self._update_install_hint)
        self._update = found
        if found and self.share:
            await self.share.broadcast(dict(found, type="update_available"))
        return found

    async def _update_check_soon(self) -> None:
        # Well clear of startup: the window, the recorder and the first clip
        # scan all matter more than this.
        await asyncio.sleep(20)
        try:
            await self.run_update_check()
        except Exception as exc:
            log.debug("Update check failed: %s", exc)

    async def _shutdown(self, server) -> None:
        click.echo("\n[Vice] Shutting down…")
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
            try:
                await self._update_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._follow_mouse_task and not self._follow_mouse_task.done():
            self._follow_mouse_task.cancel()
            try:
                await self._follow_mouse_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.share:
            try:
                await self.share.broadcast({"type": "status", "recording": False, "ready": False, "backend": ""})
            except Exception as exc:
                log.warning("Failed to broadcast shutdown status: %s", exc)

        if self._discord_task and not self._discord_task.done():
            self._discord_task.cancel()
            try:
                await self._discord_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._clear_discord_presence()

        server.close()

        try:
            await self.recorder.stop()
        except Exception as exc:
            log.error("Recorder stop failed during shutdown: %s", exc)

        try:
            await self.hotkeys.stop()
        except Exception as exc:
            log.warning("Hotkey stop failed during shutdown: %s", exc)

        if self.share:
            try:
                await self.share.stop()
            except Exception as exc:
                log.warning("Share server stop failed during shutdown: %s", exc)

        for p in (PID_FILE, SOCKET_FILE):
            try:
                if p.exists():
                    p.unlink()
            except OSError as exc:
                log.warning("Failed to remove %s during shutdown: %s", p, exc)

        click.echo("[Vice] Stopped.")

    async def _handle_clip_hotkey(self, duration: Optional[int] = None) -> None:
        if self._session_active:
            # During a session, single tap = add a highlight at current timestamp
            elapsed = self.recorder.session_elapsed()
            label   = f"Highlight {len(self._session_highlights) + 1}" if self._session_highlights else "Highlight"
            color   = "#f59e0b"
            entry   = {"time": round(elapsed, 3), "label": label, "color": color}
            self._session_highlights.append(entry)
            click.echo(f"[Vice] Session highlight at {elapsed:.1f}s", err=True)
            audio.play_highlight(self.cfg.notifications.sound_volume)
            if self.share:
                asyncio.create_task(
                    self.share.broadcast({
                        "type": "session_highlight",
                        "time": entry["time"],
                        "label": entry["label"],
                        "color": entry["color"],
                    })
                )
        else:
            if self._clip_task and not self._clip_task.done():
                log.info("Clip save already in progress; ignoring new trigger")
                return
            self._clip_task = asyncio.create_task(self._save_clip(duration))
            self._clip_task.add_done_callback(self._clip_task_done)

    async def _save_clip(self, duration: Optional[int] = None) -> None:
        async with self._clip_lock:
            click.echo("[Vice] Clip triggered!", err=True)
            if self.share:
                await self.share.broadcast({"type": "clip_saving"})
            audio.play_clip(self.cfg.notifications.sound_volume)
            saved = await self.recorder.save_clip(duration)
            if saved is None and self.share:
                await self.share.broadcast({
                    "type": "clip_error",
                    "error": "Clip save failed. Check vice.log for details.",
                })

    def _clip_task_done(self, task: asyncio.Task) -> None:
        if self._clip_task is task:
            self._clip_task = None
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            log.exception("Clip save task failed")
            if self.share:
                asyncio.create_task(self.share.broadcast({
                    "type": "clip_error",
                    "error": "Clip save failed. Check vice.log for details.",
                }))

    async def _handle_session_toggle(self) -> None:
        if self._session_active:
            await self._stop_session()
        else:
            await self._start_session()

    async def _start_session(self) -> None:
        click.echo("[Vice] Starting session recording…", err=True)
        self._session_highlights = []
        path = await self.recorder.start_session()
        if path is None:
            click.echo("[Vice] Session recording failed to start", err=True)
            return
        self._session_active = True
        self._session_path   = path
        audio.play_session_start(self.cfg.notifications.sound_volume)
        click.echo(f"[Vice] Session recording started → {path}", err=True)
        if self.share:
            asyncio.create_task(
                self.share.broadcast({
                    "type": "session_start",
                    "path": str(path),
                })
            )

    async def _stop_session(self) -> None:
        click.echo("[Vice] Stopping session recording…", err=True)
        self._session_active = False
        slug_before_stop = self._session_path.stem if self._session_path else None
        path = await self.recorder.stop_session()
        self._session_path = None

        audio.play_session_end(self.cfg.notifications.sound_volume)
        if path and self.share:
            slug = path.stem
            url  = self.share.add_clip(path)
            click.echo(f"[Vice] Session clip saved: {path}", err=True)
            click.echo(f"[Vice] Share URL: {url}", err=True)
            # Persist the highlights that were collected during the session
            if self._session_highlights:
                from .share import HIGHLIGHTS_DIR, _save_highlights
                HIGHLIGHTS_DIR.mkdir(parents=True, exist_ok=True)
                # Assign IDs
                hl_with_ids = [
                    {**h, "id": str(i + 1)}
                    for i, h in enumerate(self._session_highlights)
                ]
                _save_highlights(slug, hl_with_ids)
                click.echo(
                    f"[Vice] {len(hl_with_ids)} highlight(s) saved for {slug}", err=True
                )
            self._session_highlights = []

        if self.share:
            asyncio.create_task(
                self.share.broadcast({
                    "type": "session_stop",
                })
            )

        # Apply deferred recording config changes after session ends.
        if self._pending_recording_apply and self._recording_signature() != self._recording_sig:
            try:
                await self._apply_live_config()
            except Exception as exc:
                log.error("Deferred recording config apply failed: %s", exc)

    async def _handle_ipc(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5)
            cmd = raw.decode().strip()
            if cmd == "clip":
                asyncio.create_task(self._handle_clip_hotkey())
                writer.write(b"ok\n")
            elif cmd == "stop":
                writer.write(b"ok\n")
                await writer.drain()
                os.kill(os.getpid(), signal.SIGTERM)
            elif cmd == "status":
                writer.write(json.dumps({
                    "running":        True,
                    "ready":          self._ready,
                    "version":        __version__,
                    "backend":        self.recorder.name,
                    "clips":          self._clip_count,
                    "output":         self.cfg.output.directory,
                    "local_url":      self.share.local_base_url() if self.share else None,
                    "public_url":     self.share.public_base_url() if self.share else None,
                    "share_url":      self.share.public_base_url() if self.share else None,
                    "session_active":  self._session_active,
                    "clip_key":        self.cfg.hotkeys.clip,
                    "hotkeys_available": self.hotkeys_available,
                }).encode() + b"\n")
            elif cmd == "url":
                url = self.share.local_base_url() if self.share else ""
                writer.write((url or "").encode() + b"\n")
            else:
                writer.write(b"unknown command\n")
            await writer.drain()
        except Exception as exc:
            log.debug("IPC error: %s", exc)
        finally:
            writer.close()


# ──────────────────────────────────────────────────────────────────────────────
# IPC client
# ──────────────────────────────────────────────────────────────────────────────

async def _ipc(command: str, timeout: float = 5.0) -> Optional[str]:
    if not SOCKET_FILE.exists():
        return None
    try:
        reader, writer = await asyncio.open_unix_connection(str(SOCKET_FILE))
        writer.write(command.encode() + b"\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.readline(), timeout=timeout)
        writer.close()
        return response.decode().strip()
    except Exception as exc:
        log.debug("IPC failed: %s", exc)
        return None


def _vice_command_path() -> Optional[Path]:
    exe = shutil.which("vice")
    if not exe:
        return None
    try:
        return Path(exe).resolve()
    except OSError:
        return Path(exe)


def _installed_via_aur() -> bool:
    pacman = shutil.which("pacman")
    vice_path = _vice_command_path()
    if not pacman or not vice_path:
        return False

    query = subprocess.run(
        [pacman, "-Q", "vice-clipper"],
        capture_output=True,
        text=True,
    )
    if query.returncode != 0:
        return False

    owner = subprocess.run(
        [pacman, "-Qo", str(vice_path)],
        capture_output=True,
        text=True,
    )
    if owner.returncode != 0:
        return False
    return "vice-clipper" in owner.stdout


def _using_install_script_venv() -> bool:
    for name in ("vice", "vice-app"):
        cmd = USER_BIN_DIR / name
        if not cmd.exists():
            continue
        try:
            resolved = cmd.resolve()
        except OSError:
            continue
        if INSTALL_VENV_DIR == resolved or INSTALL_VENV_DIR in resolved.parents:
            return True
    return INSTALL_VENV_DIR.exists()


def _remove_local_install_artifacts() -> list[Path]:
    removed: list[Path] = []
    for path in (
        USER_BIN_DIR / "vice",
        USER_BIN_DIR / "vice-app",
        USER_DESKTOP_FILE,
        USER_ICON_FILE,
    ):
        if not path.exists() and not path.is_symlink():
            continue
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed


def _remove_legacy_user_site_artifacts() -> list[Path]:
    removed: list[Path] = []
    user_lib = actual_home_dir() / ".local" / "lib"
    if not user_lib.exists():
        return removed

    for pattern in (
        "python*/site-packages/vice",
        "python*/site-packages/vice-*.dist-info",
        "python*/site-packages/vice.egg-info",
    ):
        for path in sorted(user_lib.glob(pattern)):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            removed.append(path)
    return removed


def _refresh_desktop_caches() -> None:
    commands = [
        ["update-desktop-database", str(USER_DESKTOP_FILE.parent)],
        ["gtk-update-icon-cache", "-f", "-t", str(USER_ICON_FILE.parents[2])],
    ]
    for cmd in commands:
        exe = shutil.which(cmd[0])
        if not exe:
            continue
        subprocess.run([exe, *cmd[1:]], capture_output=True)


def _setup_daemon_logging(debug: bool) -> None:
    DAEMON_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(DAEMON_LOG_FILE)]
    if sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


def _tail_text_file(path: Path, lines: int = 20) -> str:
    try:
        content = path.read_text(errors="replace").splitlines()
    except Exception:
        return ""
    if not content:
        return ""
    return "\n".join(content[-lines:])


def _http_probe(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        with urlopen(url, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            return 200 <= status < 400, f"HTTP {status}"
    except HTTPError as exc:
        return 200 <= exc.code < 400, f"HTTP {exc.code}"
    except URLError as exc:
        return False, str(exc.reason)
    except Exception as exc:
        return False, str(exc)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="vice")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Vice — Linux game clip recorder (Medal.tv for Linux)."""
    normalize_runtime_environment()
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command()
@click.option("--debug", is_flag=True, help="Enable verbose logging.")
@click.option("--open-ui/--no-open-ui", default=True,
              help="Open the web UI in the browser on start.")
def start(debug: bool, open_ui: bool) -> None:
    """Start the Vice recording daemon."""
    _setup_daemon_logging(debug)
    log.info("Vice daemon startup requested (python=%s)", sys.executable)
    log.info("Runtime environment at daemon start: %s", runtime_env_snapshot())

    if SOCKET_FILE.exists():
        resp = asyncio.run(_ipc("status", timeout=1.5))
        if resp is not None:
            click.echo("Vice is already running. Use `vice stop` or `vice status`.", err=True)
            sys.exit(1)

        log.warning("Found stale IPC socket at %s, removing it", SOCKET_FILE)
        try:
            SOCKET_FILE.unlink()
        except OSError as exc:
            click.echo(f"Found stale socket at {SOCKET_FILE}, but could not remove it: {exc}", err=True)
            sys.exit(1)

    try:
        daemon = ViceDaemon()
    except Exception:
        log.exception("Vice daemon failed during startup")
        raise

    if open_ui and daemon.cfg.sharing.enabled:
        port = daemon.cfg.sharing.port
        from threading import Timer
        def _open():
            subprocess.Popen(
                ["xdg-open", f"http://localhost:{port}/"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        Timer(1.5, _open).start()

    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass


@cli.command()
def ui() -> None:
    """Open the Vice web UI in your browser."""
    raw = asyncio.run(_ipc("url"))
    if raw and raw.startswith("http"):
        url = raw
    else:
        cfg = load_config()
        url = f"http://localhost:{cfg.sharing.port}/"
        if not raw:
            click.echo("Daemon may not be running — opening default port anyway.")
    subprocess.Popen(
        ["xdg-open", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    click.echo(f"Opening {url}")


@cli.command()
def clip() -> None:
    """Save a clip right now (daemon must be running)."""
    resp = asyncio.run(_ipc("clip"))
    if resp is None:
        click.echo("Vice is not running. Start it with `vice start`.", err=True)
        sys.exit(1)
    click.echo("Clip triggered!")


@cli.command()
def stop() -> None:
    """Stop the Vice daemon."""
    resp = asyncio.run(_ipc("stop"))
    if resp is None:
        click.echo("Vice is not running.", err=True)
        sys.exit(1)
    click.echo("Stopped.")


@cli.command()
def status() -> None:
    """Show daemon status."""
    raw = asyncio.run(_ipc("status"))
    if raw is None:
        click.echo("Vice is not running.")
        return
    try:
        info = json.loads(raw)
        click.echo(f"Status   : {'running' if info['running'] else 'stopped'}")
        click.echo(f"Backend  : {info['backend']}")
        click.echo(f"Clips    : {info['clips']}")
        click.echo(f"Output   : {info['output']}")
        if info.get("local_url"):
            click.echo(f"UI URL   : {info['local_url']}/")
        if info.get("public_url"):
            click.echo(f"Share URL: {info['public_url']}/")
    except Exception:
        click.echo(raw)


@cli.command()
def doctor() -> None:
    """Print startup diagnostics for environment, install, and service issues."""
    cfg_error = ""
    try:
        cfg = load_config()
    except Exception as exc:
        cfg = Config()
        cfg_error = str(exc)
    vice_cmd = shutil.which("vice") or "(not found)"
    vice_app_cmd = shutil.which("vice-app") or "(not found)"
    package_file = Path(sys.modules["vice"].__file__).resolve()
    systemd_env = user_systemd_env_snapshot()
    service_file = actual_home_dir() / ".config" / "systemd" / "user" / SERVICE_NAME
    running_status = asyncio.run(_ipc("status"))

    click.echo("Vice doctor")
    click.echo(f"Version         : {__version__}")
    click.echo(f"Python          : {sys.executable}")
    click.echo(f"Package         : {package_file}")
    click.echo(f"vice            : {vice_cmd}")
    click.echo(f"vice-app        : {vice_app_cmd}")
    click.echo(f"Config          : {CONFIG_PATH}")
    if cfg_error:
        click.echo(f"Config error    : {cfg_error}")
    click.echo(f"Daemon log      : {DAEMON_LOG_FILE}")
    click.echo("")

    click.echo("Environment")
    for key, value in runtime_env_snapshot().items():
        click.echo(f"  {key}={value or '(unset)'}")
    click.echo("")

    click.echo("User systemd environment")
    if systemd_env:
        for key in sorted(systemd_env):
            click.echo(f"  {key}={systemd_env[key]}")
    else:
        click.echo("  (unavailable)")
    click.echo("")

    click.echo("Service")
    if service_file.exists():
        click.echo(f"  File: {service_file}")
        service_tail = _tail_text_file(service_file, lines=30)
        if service_tail:
            for line in service_tail.splitlines():
                click.echo(f"    {line}")
    else:
        click.echo("  File: (not installed)")
    click.echo("")

    click.echo("Recorder probe")
    try:
        recorder = create_recorder(cfg)
        click.echo(f"  OK: {type(recorder).__name__} ({recorder.name})")
    except Exception as exc:
        click.echo(f"  ERROR: {exc}")
    click.echo("")

    click.echo("Daemon status")
    if running_status is None:
        click.echo("  IPC: not running")
        local_url = f"http://localhost:{cfg.sharing.port}/"
    else:
        click.echo(f"  IPC: {running_status}")
        try:
            info = json.loads(running_status)
            local_url = f"{str(info.get('local_url') or f'http://localhost:{cfg.sharing.port}').rstrip('/')}/"
        except Exception:
            local_url = f"http://localhost:{cfg.sharing.port}/"
    ok, detail = _http_probe(local_url)
    click.echo(f"  HTTP: {'ok' if ok else 'error'} ({detail}) {local_url}")
    click.echo("")

    click.echo("Dependencies")
    for tool in ("gpu-screen-recorder", "wf-recorder", "ffmpeg", "xdg-open", "systemctl",
                 "xdotool", "xprop", "wmctrl"):
        click.echo(f"  {tool}: {shutil.which(tool) or '(not found)'}")
    click.echo("")

    click.echo("Recent daemon log")
    log_tail = _tail_text_file(DAEMON_LOG_FILE, lines=20)
    if log_tail:
        for line in log_tail.splitlines():
            click.echo(f"  {line}")
    else:
        click.echo("  (no log output yet)")


@cli.command("config")
def show_config() -> None:
    """Print the config file path and its contents."""
    click.echo(f"Config: {CONFIG_PATH}\n")
    if CONFIG_PATH.exists():
        click.echo(CONFIG_PATH.read_text())
    else:
        click.echo("(no config file yet — will be created on first `vice start`)")


@cli.command("open-config")
def open_config() -> None:
    """Open the config file in $EDITOR."""
    if not CONFIG_PATH.exists():
        from .config import Config
        save_config(Config())
        click.echo(f"Created default config at {CONFIG_PATH}")
    editor = os.environ.get("EDITOR", "nano")
    os.execlp(editor, editor, str(CONFIG_PATH))


@cli.command("list-keys")
@click.option("--filter", "filt", default="", help="Filter by substring.")
def list_keys(filt: str) -> None:
    """List available hotkey names for use in config."""
    keys = list_available_keys()
    if filt:
        keys = [k for k in keys if filt.upper() in k]
    for k in keys:
        click.echo(k)


@cli.command()
def clips() -> None:
    """List saved clips in the output directory."""
    cfg = load_config()
    out_dir = resolve_path(cfg.output.directory)
    if not out_dir.exists():
        click.echo("No clips directory found.")
        return
    files = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        click.echo("No clips saved yet.")
        return
    for f in files:
        size_mb = f.stat().st_size / (1024 * 1024)
        click.echo(f"{f.name}  ({size_mb:.1f} MB)")


@cli.command()
@click.option("--yes", "-y", is_flag=True, help="Skip all confirmation prompts.")
def uninstall(yes: bool) -> None:
    """Remove Vice cleanly — config, service, and optionally clips."""
    click.echo("Vice uninstaller\n")

    if _installed_via_aur():
        click.echo("Vice was installed via AUR.")
        click.echo("Run: yay -Rns vice-clipper")
        return

    # 1. Stop daemon
    if SOCKET_FILE.exists():
        click.echo("Stopping daemon…")
        asyncio.run(_ipc("stop"))

    # 2. Disable systemd user service
    service = actual_home_dir() / ".config" / "systemd" / "user" / SERVICE_NAME
    if service.exists():
        if yes or click.confirm("Disable and remove the systemd user service?", default=True):
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", "vice"],
                capture_output=True,
            )
            service.unlink()
            click.echo("  Removed systemd service.")

    # 3. Remove config
    if CONFIG_DIR.exists():
        if yes or click.confirm(f"Remove config directory {CONFIG_DIR}?", default=False):
            shutil.rmtree(CONFIG_DIR)
            click.echo(f"  Removed {CONFIG_DIR}.")

    # 4. Offer to remove clips
    try:
        cfg = load_config() if CONFIG_PATH.exists() else None
        clips_dir = resolve_path(cfg.output.directory) if cfg else actual_home_dir() / "Videos" / "Vice"
    except Exception:
        clips_dir = actual_home_dir() / "Videos" / "Vice"

    if clips_dir.exists():
        n = len(list(clips_dir.glob("*.mp4")))
        if n > 0 and (yes or click.confirm(
            f"Delete {n} saved clip(s) in {clips_dir}?", default=False
        )):
            shutil.rmtree(clips_dir)
            click.echo(f"  Deleted {n} clip(s).")

    using_venv = _using_install_script_venv()

    # 5. Remove the Python package or the dedicated install.sh virtualenv
    if using_venv:
        click.echo("\nRemoving Vice virtual environment…")
        shutil.rmtree(INSTALL_VENV_DIR, ignore_errors=True)
        click.echo(f"  Removed {INSTALL_VENV_DIR}.")
    else:
        click.echo("\nUninstalling Python package…")
        subprocess.run([sys.executable, "-m", "pip", "uninstall", "vice", "-y"])

    removed = _remove_local_install_artifacts()
    removed.extend(_remove_legacy_user_site_artifacts())
    if using_venv and INSTALL_VENV_DIR not in removed:
        removed.append(INSTALL_VENV_DIR)
    if removed:
        click.echo("\nRemoved local Vice install files:")
        for path in removed:
            click.echo(f"  {path}")
        _refresh_desktop_caches()

    click.echo("\nVice has been removed. Goodbye!")


if __name__ == "__main__":
    cli()
