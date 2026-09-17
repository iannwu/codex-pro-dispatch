#!/usr/bin/env python3
"""Register the existing user-global source installation with Claude Code."""
import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--remove', action='store_true', help='Remove only our Claude link')
    args = parser.parse_args()
    home = Path.home()
    source = home / '.agents/skills/codex-pro-dispatch'
    target = home / '.claude/skills/codex-pro-dispatch'
    owned = target.is_symlink() and os.readlink(target) == str(source)
    if os.path.lexists(target) and not owned:
        parser.exit(1, f'Refusing to replace unrelated path: {target}\n')
    if args.remove:
        if owned:
            target.unlink()
        print('Claude registration removed. Codex installation and request state retained.')
        return
    required = ('SKILL.md', 'scripts/pro-dispatch', 'scripts/parked-activation.mjs',
                'scripts/parked-serving.mjs')
    if not all((source / name).is_file() for name in required):
        parser.exit(1, 'Codex source installation is missing or incomplete. Paste into Codex:\n'
                    'Install codex-pro-dispatch globally using its documented source installer. '
                    'Do not open a listener or send a request. Return the installed skill path.\n'
                    'Then rerun this registration command in Claude.\n')
    target.parent.mkdir(parents=True, exist_ok=True)
    if not owned:
        # Atomic no-clobber creation also refuses a competing registration.
        try:
            target.symlink_to(source, target_is_directory=True)
        except FileExistsError:
            parser.exit(1, f'Registration target appeared concurrently; inspect {target}\n')
    print(f'Claude user-global skill: {target} -> {source}')
    print('One shared copy. No permissions changed, listener opened, or request sent.')
    print('In a fresh Claude Code task, invoke /codex-pro-dispatch to verify discovery.')


if __name__ == '__main__':
    main()
