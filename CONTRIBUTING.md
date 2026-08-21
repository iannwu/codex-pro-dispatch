# Contributing

Contributions are welcome, especially for:

- reliable targeting across ChatGPT desktop app versions
- lossless capture of long code blocks and unified diffs
- Accessibility diagnostics
- localization of Send and Stop controls
- deterministic tests that do not require a logged-in account
- daemon lifecycle and crash recovery
- macOS permission diagnostics

## Development checks

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile bin/pro-dispatch lib/codex_pro_dispatch/*.py
bash -n install.sh uninstall.sh
```

On macOS:

```bash
for file in bin/cgpt-*; do swiftc -typecheck "$file"; done
```

Keep pull requests narrow. Do not include private prompts, receipts, screenshots, account data, absolute personal paths, or proprietary repository contents.
