# Contributing

Thanks for helping make Codex Pro Dispatch safer and easier to understand.

## Before opening a change

- Read [README.md](README.md), [SECURITY.md](SECURITY.md), and [docs/compatibility.md](docs/compatibility.md).
- Use a public issue for bugs and proposals that contain no private data.
- Use GitHub private vulnerability reporting for security-sensitive findings.
- Keep transport changes inside the official combined ChatGPT/Codex app boundary. Browser automation, Accessibility, AppleScript, CDP, clipboard injection, and title-based thread selection are out of scope unless a separate proposal changes the product contract first.

## Development setup

The project has no third-party runtime dependencies. Python 3.9 or newer is required.

```bash
git clone https://github.com/iannwu/codex-pro-dispatch.git
cd codex-pro-dispatch
python3 -m unittest discover -s tests -v
```

## Pull requests

Keep changes focused and explain the user-visible contract they affect. Add or update tests for behavior changes. Before opening a pull request, run:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile bin/pro-dispatch skills/codex-pro-dispatch/scripts/pro-dispatch src/codex_pro_dispatch/*.py
bash -n install.sh uninstall.sh
./bin/pro-dispatch --help
```

For native workflow changes, update [docs/acceptance.md](docs/acceptance.md) and attach a redacted receipt from the exact candidate commit. Never include conversation IDs, private repository names, prompts, responses, credentials, or assignment receipts.

## Versioning

This project uses semantic versions:

- `MAJOR`: incompatible public contract changes
- `MINOR`: new user-visible features or capabilities
- `PATCH`: backward-compatible bug fixes

Update `VERSION`, package metadata, the skill version, plugin manifest, and `CHANGELOG.md` together.

By contributing, you agree that your contribution is licensed under the project's [MIT License](LICENSE).
