"""Command line: ``studia`` (no arguments) opens the app; subcommands script it."""

from __future__ import annotations

import argparse
import getpass
import sys
import threading
import time
from pathlib import Path


def _services():
    from .services import Services

    return Services()


def cmd_login(args: argparse.Namespace) -> int:
    s = _services()
    user = args.username or input("EkoID username: ").strip()
    pw = getpass.getpass("Password: ")
    p = s.sync.sign_in(user, pw)
    print(f"Signed in as {p.name} ({p.student_number}). Credentials are stored encrypted (Windows DPAPI).")
    return 0


def cmd_logout(_: argparse.Namespace) -> int:
    _services().sync.sign_out()
    print("Signed out; stored credentials removed.")
    return 0


def cmd_sync(_: argparse.Namespace) -> int:
    s = _services()
    res = s.sync.sync(lambda m: print("·", m))
    print(f"Synced {len(res.courses)} courses in {res.seconds:.1f}s — {len(res.new_ids)} new recording(s).")
    for did in res.new_ids:
        r = s.lib.get_recording(did)
        if r:
            print(f"  NEW  {r.start:%a %Y-%m-%d %H:%M}  {r.duration_s / 60:4.0f} min  {r.name}")
    for e in res.errors:
        print("  !", e)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    s = _services()
    done = s.lib.transcribed_ids()
    for c in s.lib.courses():
        if args.course and args.course.lower().replace(" ", "") not in c.code.lower().replace(" ", ""):
            continue
        print(f"\n{c.code} — {c.name} ({c.instructor})")
        for r in sorted(s.lib.recordings(c.id), key=lambda r: r.start):
            if r.duplicate_of and not args.all:
                continue
            mark = "✓" if r.delivery_id in done else ("N" if r.is_new else " ")
            print(f"  {mark} {r.start:%a %Y-%m-%d %H:%M} {r.duration_s / 60:4.0f} min  {r.delivery_id}  {r.name[:60]}")
    return 0


def cmd_transcribe(args: argparse.Namespace) -> int:
    s = _services()
    ids, local = [], []
    for item in args.items:
        if Path(item).exists():
            local.append(Path(item))
        elif "|" in item:
            _, recs = s.day(item)
            ids += [r.delivery_id for r in recs if not r.duplicate_of]
        else:
            ids.append(item)
    done = threading.Event()
    pending = set()

    def on_event(evt):
        if evt.get("type") == "job":
            if evt.get("final"):
                print(f"\n{evt['title']}: {evt['stage']}" + (f" — {evt['error']}" if evt.get("error") else ""))
                pending.discard(evt["id"])
                if not pending:
                    done.set()
            else:
                sys.stdout.write(f"\r{evt['title'][:40]:40s} {evt['stage']:12s} {evt['progress'] * 100:5.1f}%  "
                                 f"{(evt.get('detail') or '')[:50]:50s}")
                sys.stdout.flush()

    s.subscribe(on_event)
    s.jobs.enqueue(ids)
    pending |= set(ids)
    for p in local:
        pending.add(s.jobs.enqueue_local(p))
    if not pending:
        print("Nothing to do.")
        return 0
    try:
        while not done.wait(0.5):
            pass
    except KeyboardInterrupt:
        s.jobs.cancel(None)
        time.sleep(1)
        return 130
    return 0


def cmd_profiles(args: argparse.Namespace) -> int:
    from . import accounts

    if args.use:
        acc = _pick_account(args.use)
        accounts.set_active(acc.id)
        print(f"IEUStudia will open {acc.name or acc.username or acc.id} next time it starts.")
        return 0
    active_id, accs = accounts.load()
    for a in accs:
        mark = "*" if a.id == active_id else " "
        print(f" {mark} {a.id:12s} {a.name or '(not signed in yet)':32s} {a.student_number:12s} {a.username}")
    return 0


def _pick_account(key: str):
    from . import accounts

    k = key.strip().casefold()
    for a in accounts.load()[1]:
        if k in (a.id.casefold(), a.username.casefold(), a.name.casefold(), a.student_number):
            return a
    raise SystemExit(f"No profile matches {key!r} (see: studia profiles)")


def cmd_doctor(_: argparse.Namespace) -> int:
    from . import __version__, paths
    from .ai import claude_info
    from .engine.models import MODEL_PRESETS, detect_gpu, local_model_path
    from .export import find_browser

    s = _services()
    print(f"IEUStudia {__version__}")
    print("App data:     ", paths.app_dir())
    print("Profile data: ", paths.profile_dir())
    print("Output folder:", s.settings.output_dir)
    print("GPU:          ", detect_gpu())
    for m in MODEL_PRESETS:
        print(f"Model {m:15s}", local_model_path(m) or "not downloaded")
    print("Signed in:    ", s.sync.profile, "| credentials stored:", s.sync.has_credentials())
    print("Claude CLI:   ", claude_info())
    print("PDF renderer: ", find_browser() or "none (AI pack without PDF)")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):   # Turkish names on a cp1252 console must not crash the CLI
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    if not argv or argv[0] in ("--debug", "gui"):
        from .app import main as app_main

        app_main(argv)
        return 0
    from .accounts import activate
    from .paths import migrate_legacy_data

    migrate_legacy_data()            # the data folder of the app's earlier name (LectureScribe)
    ap = argparse.ArgumentParser(prog="studia", description="Sync and transcribe IEULearn/Panopto lectures.")
    ap.add_argument("--profile", help="run the command for this profile (id, username or name; see: studia profiles)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("profiles", help="list the accounts on this PC, or choose the one the app opens")
    p.add_argument("--use", metavar="PROFILE", help="open this profile next time")
    p.set_defaults(fn=cmd_profiles)
    p = sub.add_parser("login", help="store your EkoID credentials")
    p.add_argument("username", nargs="?")
    p.set_defaults(fn=cmd_login)
    sub.add_parser("logout", help="forget stored credentials").set_defaults(fn=cmd_logout)
    sub.add_parser("sync", help="look for new recordings").set_defaults(fn=cmd_sync)
    p = sub.add_parser("list", help="list recordings")
    p.add_argument("--course")
    p.add_argument("--all", action="store_true", help="include duplicates")
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("transcribe", help="transcribe recordings (delivery ids, day keys or local files)")
    p.add_argument("items", nargs="+")
    p.set_defaults(fn=cmd_transcribe)
    sub.add_parser("doctor", help="check GPU, models, login and tools").set_defaults(fn=cmd_doctor)
    args = ap.parse_args(argv)
    try:
        activate(_pick_account(args.profile).id if args.profile else None)
        return args.fn(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
