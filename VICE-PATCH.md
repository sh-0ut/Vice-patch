# Vice Patch

Vice Patch is an isolated experimental instance. It uses ports 8875/8876,
`~/.config/vice-patch`, `~/.local/share/vice-patch`, `~/.cache/vice-patch`,
`/tmp/vice-patch`, and `~/Videos/Vice-Patch`.

Install (does not start or enable the service):

```sh
./install-patch.sh
```

Switch to the test instance manually:

```sh
systemctl --user stop vice.service
systemctl --user start vice-patch.service
vice-patch-app
```

Return to production manually:

```sh
systemctl --user stop vice-patch.service
systemctl --user start vice.service
vice-app
```

Remove only the patch instance:

```sh
./uninstall-patch.sh
```

Never run both replay recorders simultaneously. The patch daemon refuses to
start capture if the production socket or another gpu-screen-recorder process
is present. Its UI can be exercised in tests without starting capture.
