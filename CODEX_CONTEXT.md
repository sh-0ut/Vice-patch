# Codex handoff: Vice Patch

Останнє оновлення контексту: 2026-08-03, Europe/Kyiv.

## Репозиторій і Git

- Checkout: `/home/sh0ut/Projects/Vice`
- Upstream: `https://github.com/eklonofficial/Vice.git`
- Fork: `https://github.com/sh-0ut/Vice-patch`
- Гілка: `vice-patch`
- `upstream/main`: `e4a92b3` (`v2.6.0`)
- Опублікований commit: `004cc13 Add isolated Vice Patch with multi-stream audio support`
- Локальний commit, який ще треба push-нути: `a634b4c Add safe trim copies and structured GSR controls`
- Перед commit цього handoff-файла гілка була `ahead 1` від `origin/main`.
- Команда публікації:

  ```bash
  git push origin vice-patch:main
  ```

Handoff-файл має бути збережений окремим локальним commit; перевірити актуальний стан через `git log`.

## Критичні правила безпеки

Production Vice не можна змінювати, перевстановлювати або використовувати як місце встановлення.

Production paths:

- CLI: `~/.local/bin/vice`
- GUI: `~/.local/bin/vice-app`
- venv: `~/.local/share/vice/venv`
- config: `~/.config/vice/config.toml`
- service: `~/.config/systemd/user/vice.service`
- desktop entry: `~/.local/share/applications/vice.desktop`
- ports: 8765/8766
- clips: `/mnt/games/Clips`, `~/Videos/Vice`

Ніколи без прямої команди користувача:

- не запускати upstream `./install.sh`;
- не stop/restart/enable/disable `vice.service`;
- не змінювати production config/venv/wrappers;
- не змінювати або видаляти існуючі clips;
- не запускати одночасно два replay recorder;
- не перевстановлювати системний `gpu-screen-recorder`.

Перед змінами завжди перевіряти `git status`. Worktree може містити зміни користувача.

## Ізольований Vice Patch

Створено:

- installer: `install-patch.sh`
- uninstaller: `uninstall-patch.sh`
- docs: `VICE-PATCH.md`
- instance abstraction: `vice/instance.py`

Patch paths:

- venv: `~/.local/share/vice-patch/venv`
- data/logs: `~/.local/share/vice-patch`
- config: `~/.config/vice-patch/config.toml`
- cache: `~/.cache/vice-patch`
- runtime: `/tmp/vice-patch`
- output: `~/Videos/Vice-Patch`
- CLI: `~/.local/bin/vice-patch`
- GUI: `~/.local/bin/vice-patch-app`
- unit: `~/.config/systemd/user/vice-patch.service`
- desktop: `~/.local/share/applications/vice-patch.desktop`
- icon: `~/.local/share/icons/hicolor/scalable/apps/vice-patch.svg`
- ports: 8875/8876

Instance environment variables:

- `VICE_INSTANCE=vice-patch`
- `VICE_APP_NAME=Vice Patch`
- `VICE_CONFIG_DIR=~/.config/vice-patch`
- `VICE_DATA_DIR=~/.local/share/vice-patch`
- `VICE_CACHE_DIR=~/.cache/vice-patch`
- `VICE_RUNTIME_DIR=/tmp/vice-patch`
- `VICE_RUNTIME_NAME=vice-patch`
- `VICE_CLI_NAME=vice-patch`
- `VICE_APP_CLI_NAME=vice-patch-app`
- `VICE_SERVICE_NAME=vice-patch.service`

Без env production defaults мають залишатися незмінними.

Installer встановлює unit, але не запускає і не enable-ить його. Після оновлення встановленого коду запущений service підхоплює backend-код тільки після ручного:

```bash
systemctl --user restart vice-patch.service
```

Не виконувати restart автоматично.

## Multi-stream editor

Реалізовано:

- ffprobe повертає список audio streams із relative stream index, codec, channels, channel layout, title/handler_name і language;
- `Detach audio` створює item на кожен stream;
- item поля: `audioStreamIndex`, `volume`, `muted`;
- старі projects: `audioStreamIndex=0`, `volume=1.0`;
- split/duplicate/copy/paste зберігають stream index і volume;
- exporter використовує `[input:a:audioStreamIndex]` та окремий `volume=` до mix;
- muted items не експортуються;
- preview endpoint: `/api/editor/audio/{slug}/{stream_index}`;
- endpoint використовує тільки clip registry, не приймає filesystem path;
- preview cache: `~/.cache/vice-patch/editor-audio`, key включає canonical path, size, mtime_ns і stream index;
- preview transcoding: Opus/WebM, atomic temp + per-key asyncio lock;
- frontend використовує Web Audio `MediaElementAudioSourceNode -> GainNode` для 0–200%;
- Full Mix item muted за замовчуванням при detach, raw items активні, щоб уникнути double-play.

Основні файли: `vice/media.py`, `vice/editor.py`, `vice/share.py`, `vice/ui/scripts/editor-core.js`, `vice/ui/scripts/editor-preview.js`.

## Поточна Full Mix схема

Після обговорення обрано hybrid fallback design.

GSR записує чотири input streams:

1. `default_output|device:alsa_input.usb-3142_FIFINE_Microphone-00.mono-fallback` — working GSR fallback Full Mix.
2. `app-inverse:Zen` — Game + Discord.
3. `app:Zen` — Browser / Zen.
4. `device:alsa_input.usb-3142_FIFINE_Microphone-00.mono-fallback` — raw microphone.

Config має `audio_tracks_mix_first=true`. У цьому fork це НЕ додає ще один GSR pipe. Post-save FFmpeg:

- ігнорує input stream 0 при побудові нового mix;
- змішує raw input streams 1–3;
- застосовує `audio_track_mix_gains` тільки до generated Full Mix;
- mic gain зараз 0.35;
- output знову має рівно 4 streams: rebuilt Full Mix + три raw;
- raw tracks stream-copy, AAC fallback за потреби;
- якщо post-processing не вдається, valid GSR combined stream 0 зберігається як fallback і clip не втрачається;
- processing validation + atomic replacement + idempotency.

Friendly names:

- `Full Mix (GSR fallback)`
- `Game + Discord`
- `Browser — Zen`
- `Microphone — FIFINE`

Основний код: `vice/recorder.py` (`_gsr_audio_args`, `build_full_mix_cmd`, `_apply_full_mix`).

## Safe trim і data-loss incident

Стався серйозний інцидент із `/mnt/games/Clips/hunt-03.08.mp4`:

- оригінал мав 731,444,701 bytes, але MP4 container уже не читався ffprobe;
- старий `_api_trim()` створив результат 291,349 bytes / 0.008333 s / один frame;
- код без validation виконав `tmp.replace(path)` і втратив оригінальний directory entry;
- `debugfs lsdel`, `extundelete` і `ext4magic` не змогли повернути стару версію;
- inode `10223618` був повторно використаний/current;
- файл вважається практично втраченим, raw carving через PhotoRec лишається низькоімовірним варіантом.

Після інциденту trim виправлено:

- endpoint ніколи не replace/unlink source;
- output завжди sibling copy: `name-trimmed.ext`, `name-trimmed-2.ext`, ...;
- unreadable source відхиляється до FFmpeg;
- range перевіряється проти source duration;
- temp result перевіряється ffprobe;
- валідуються duration, video, audio stream count і size;
- invalid temp видаляється, source залишається byte-identical;
- UI показує `Trimmed copy saved as ...`.

Є synthetic FFmpeg integration test, який порівнює source bytes до/після trim і перевіряє окрему copy.

Основний код: `vice/share.py`; tests: `tests/test_patch_features.py`.

## Advanced GSR controls

У Settings -> Advanced додано toggle `Advanced encoding controls` і structured fields:

- bitrate mode: auto / qp / vbr / cbr;
- quality: medium / high / very_high / ultra;
- video bitrate kbps для CBR;
- frame rate mode: cfr / vfr / content;
- keyframe interval;
- tune: performance / quality;
- audio bitrate kbps.

Config fields у `RecordingConfig` мають prefix `gsr_...`.

`recording.gsr_args` залишено. Manual Extra args мають найвищий пріоритет per flag: autogenerated flag не додається, якщо manual args уже містять той самий flag.

Не рекомендувати manual `-a`, `-o`, `-ro`, `-r`, бо Vice керує audio/output/replay.

Structured controls default disabled, тому існуюча command line не змінюється до ввімкнення toggle.

## Тести і перевірки

Останній релевантний запуск після Advanced controls:

- 121 tests — OK;
- `python3 -m compileall -q vice` — OK;
- `git diff --check` — OK.

Раніше також проходили media/editor/installer/UI/isolation suites та synthetic FFmpeg tests для:

- 3 raw -> 4 Full Mix streams;
- hybrid fallback 4 input -> 4 output;
- stream ordering/titles;
- mic gain only in Full Mix;
- idempotency;
- safe trim copy;
- preview endpoint traversal/missing stream;
- production/patch path defaults.

Повний upstream unittest discovery іноді зависав у довгих runtime/share tests; релевантні suites запускалися окремо.

## Production integrity

Під час першої інсталяції до/після звіряли hashes/timestamps:

- `~/.local/bin/vice`
- `~/.local/bin/vice-app`
- `~/.config/vice/config.toml`
- `~/.config/systemd/user/vice.service`
- `~/.local/share/applications/vice.desktop`

Вони не змінилися. Production clips не повинні змінюватися.

Стан сервісів міг змінювати сам користувач у процесі роботи. Завжди перевіряти актуальний стан read-only перед висновками:

```bash
systemctl --user is-active vice.service
systemctl --user is-active vice-patch.service
```

## Наступному агенту

1. Прочитати цей файл і `VICE-PATCH.md`.
2. Перевірити `git status`, `git log -2`, `git remote -v`.
3. Не припускати, що локальні зміни вже push-нуті.
4. Не запускати upstream installer.
5. Не змінювати production service/config/clips.
6. Будь-який media rewrite спочатку робити на copy та валідовувати до publish/replace.
7. Для оновлення patch використовувати `./install-patch.sh`; він не має стартувати service.
8. Restart service — тільки за прямою командою користувача.
