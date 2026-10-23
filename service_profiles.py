#!/usr/bin/env python3
"""Create profiles that start and stop systemd services when applied."""
import argparse
import json
import os
import subprocess
import sys

DEFAULT_CONFIG = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "linux-service-profiles",
    "profiles.json",
)


def load_profiles(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_profiles(path, profiles):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(profiles, f, indent=2, sort_keys=True)


def systemctl_command(action, service, user=False):
    cmd = ["systemctl"]
    if user:
        cmd.append("--user")
    return cmd + [action, "--", service]


def apply_profile(profile, user=False, dry_run=False, runner=subprocess.run):
    """Stop then start the services; return the list of failed commands."""
    failures = []
    steps = [("stop", s) for s in profile.get("stop", [])]
    steps += [("start", s) for s in profile.get("start", [])]
    for action, service in steps:
        cmd = systemctl_command(action, service, user)
        print(" ".join(cmd))
        if dry_run:
            continue
        if runner(cmd).returncode != 0:
            failures.append(cmd)
    return failures


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=DEFAULT_CONFIG, help="profiles file")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("create", help="create or replace a profile")
    c.add_argument("name")
    c.add_argument("--start", nargs="*", default=[], metavar="SERVICE")
    c.add_argument("--stop", nargs="*", default=[], metavar="SERVICE")

    sub.add_parser("list", help="list profiles")
    s = sub.add_parser("show", help="show a profile")
    s.add_argument("name")
    d = sub.add_parser("delete", help="delete a profile")
    d.add_argument("name")

    a = sub.add_parser("apply", help="stop/start services of a profile")
    a.add_argument("name")
    a.add_argument("--user", action="store_true", help="use systemctl --user")
    a.add_argument("--dry-run", action="store_true")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    profiles = load_profiles(args.config)

    if args.command == "create":
        overlap = set(args.start) & set(args.stop)
        if overlap:
            print("Services in both start and stop: " + ", ".join(sorted(overlap)),
                  file=sys.stderr)
            return 2
        profiles[args.name] = {"start": args.start, "stop": args.stop}
        save_profiles(args.config, profiles)
        return 0
    if args.command == "list":
        for name in sorted(profiles):
            print(name)
        return 0
    if args.name not in profiles:
        print("No such profile: " + args.name, file=sys.stderr)
        return 1
    if args.command == "show":
        print(json.dumps(profiles[args.name], indent=2))
    elif args.command == "delete":
        del profiles[args.name]
        save_profiles(args.config, profiles)
    elif args.command == "apply":
        failures = apply_profile(profiles[args.name], args.user, args.dry_run)
        if failures:
            print("%d command(s) failed" % len(failures), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
