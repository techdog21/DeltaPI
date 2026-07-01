#!/usr/bin/env python3
"""
DeltaPI Pi deployer — installs/refreshes the systemd units and logrotate config
so a (re)deploy is a single command.

Typical use on the Pi, after `git pull`:

    sudo python3 pi/deploy.py                 # install/refresh everything + restart
    sudo python3 pi/deploy.py --dry-run       # show exactly what it would do
    sudo python3 pi/deploy.py renogy_ble      # just one service
    sudo python3 pi/deploy.py --no-restart    # install but don't (re)start

It reads the unit templates in pi/deploy/*.service (and the logrotate template),
substitutes the __TOKENS__ with values detected for THIS machine — the invoking
user, this repo's path, the venv python — and writes them to /etc/systemd/system
(and /etc/logrotate.d), then daemon-reloads and restarts. Nothing about the setup
is hard-coded, so the same script works for anyone who clones the repo.

Secrets are never stored in the repo: on first install it writes a template
EnvironmentFile (default /etc/deltapi.env, mode 600) for POST_SECRET / BASE_URL.

Stdlib only; Python 3. Must run as root (via sudo) to write the system files.
"""
import argparse
import getpass
import os
import subprocess
import sys

try:
    import pwd  # Linux only; absent on non-POSIX (dry-run still works there)
except ImportError:
    pwd = None

HERE = os.path.dirname(os.path.abspath(__file__))          # the pi/ directory
TPL_DIR = os.path.join(HERE, "deploy")
UNIT_DIR = "/etc/systemd/system"
LOGROTATE_DST = "/etc/logrotate.d/vedirect"

# service name -> unit template filename
SERVICES = {
    "vedirect_logger": "vedirect_logger.service",
    "renogy_ble": "renogy_ble.service",
    "starlink_poll": "starlink_poll.service",
}
ALL_TARGETS = list(SERVICES) + ["logrotate"]


def detect_user():
    """The human user the services should run as — the sudo caller, not root."""
    return os.environ.get("SUDO_USER") or getpass.getuser()


def home_of(user):
    if pwd:
        try:
            return pwd.getpwnam(user).pw_dir
        except KeyError:
            pass
    return os.path.expanduser("~" + user)


def first_existing(paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def render(template_path, tokens):
    with open(template_path) as f:
        text = f.read()
    for tok, val in tokens.items():
        text = text.replace(tok, val)
    return text


def write_file(path, content, mode, dry):
    print(f"  write {path} (mode {oct(mode)})")
    if dry:
        return
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, mode)


def run(cmd, dry):
    print("  $ " + " ".join(cmd))
    if not dry:
        subprocess.run(cmd, check=True)


def ensure_envfile(path, dry):
    """Create a template secrets file on first install; return True if created."""
    if os.path.exists(path):
        return False
    print(f"  write {path} (mode 0o600)  [template — fill in secrets]")
    if dry:
        return True
    with open(path, "w") as f:
        f.write("# DeltaPI logger secrets — fill these in, then:\n"
                "#   sudo systemctl restart vedirect_logger\n"
                "POST_SECRET=\n"
                "BASE_URL=\n")
    os.chmod(path, 0o600)
    return True


def main():
    ap = argparse.ArgumentParser(description="Install/refresh DeltaPI Pi services.")
    ap.add_argument("targets", nargs="*",
                    help=f"services to (re)deploy (default: all). Choices: {', '.join(ALL_TARGETS)}, all")
    ap.add_argument("--user", help="service user (default: the sudo caller)")
    ap.add_argument("--venv", help="path to the BLE/Starlink venv python "
                                   "(default: ~USER/deltapi-venv/bin/python)")
    ap.add_argument("--logger-python", default="/usr/bin/python3",
                    help="python for the core logger (default: /usr/bin/python3)")
    ap.add_argument("--starlink-tools", help="path to the starlink-grpc-tools clone "
                                             "(default: ~USER/starlink-grpc-tools)")
    ap.add_argument("--envfile", default="/etc/deltapi.env",
                    help="EnvironmentFile for logger secrets (default: /etc/deltapi.env)")
    ap.add_argument("--dry-run", action="store_true", help="print actions without doing them")
    ap.add_argument("--no-restart", action="store_true", help="install/enable but don't (re)start")
    args = ap.parse_args()

    targets = args.targets or ALL_TARGETS
    if "all" in targets:
        targets = ALL_TARGETS
    unknown = [t for t in targets if t not in ALL_TARGETS]
    if unknown:
        ap.error(f"unknown target(s): {', '.join(unknown)}. Choose from: {', '.join(ALL_TARGETS)}, all")

    user = args.user or detect_user()
    if user == "root":
        sys.exit("Refusing to run services as root. Invoke with sudo from your normal "
                 "user (so SUDO_USER is set), or pass --user <name>.")
    home = home_of(user)
    workdir = HERE
    venv_py = (args.venv
               or first_existing([os.path.join(home, "deltapi-venv/bin/python"),
                                  os.path.join(os.path.dirname(HERE), ".venv/bin/python")])
               or os.path.join(home, "deltapi-venv/bin/python"))
    starlink_tools = args.starlink_tools or os.path.join(home, "starlink-grpc-tools")

    tokens = {
        "__USER__": user,
        "__WORKDIR__": workdir,
        "__VENV_PY__": venv_py,
        "__LOGGER_PY__": args.logger_python,
        "__STARLINK_TOOLS__": starlink_tools,
        "__ENVFILE__": args.envfile,
    }

    dry = args.dry_run
    euid = getattr(os, "geteuid", lambda: 0)()
    if not dry and euid != 0:
        sys.exit("Must run as root to write system files. Try:\n"
                 f"  sudo python3 {os.path.relpath(__file__)} " + " ".join(sys.argv[1:]))

    print(f"DeltaPI deploy{'  [DRY RUN]' if dry else ''}")
    print(f"  user        : {user}")
    print(f"  repo (pi/)  : {workdir}")
    print(f"  logger py   : {args.logger_python}")
    print(f"  venv py     : {venv_py}" + ("" if os.path.exists(venv_py) else "   (not found yet — create the venv per the README)"))
    if "starlink_poll" in targets:
        print(f"  starlink    : {starlink_tools}" + ("" if os.path.exists(starlink_tools) else "   (not cloned yet)"))
    print(f"  targets     : {', '.join(targets)}\n")

    warnings = []
    if "vedirect_logger" in targets:
        if ensure_envfile(args.envfile, dry):
            warnings.append(f"Fill in POST_SECRET and BASE_URL in {args.envfile}, then: "
                            "sudo systemctl restart vedirect_logger")

    # Write units / logrotate
    wrote_unit = False
    for t in targets:
        if t == "logrotate":
            write_file(LOGROTATE_DST, render(os.path.join(TPL_DIR, "vedirect-logrotate.conf"), tokens),
                       0o644, dry)
        else:
            write_file(os.path.join(UNIT_DIR, f"{t}.service"),
                       render(os.path.join(TPL_DIR, SERVICES[t]), tokens), 0o644, dry)
            wrote_unit = True

    if wrote_unit:
        run(["systemctl", "daemon-reload"], dry)
        for t in targets:
            if t == "logrotate":
                continue
            run(["systemctl", "enable", t], dry)
            if not args.no_restart:
                run(["systemctl", "restart", t], dry)

    print("\nDone." if not dry else "\nDry run complete — no changes made.")
    for w in warnings:
        print("  ! " + w)
    if not dry and wrote_unit and not args.no_restart:
        names = " ".join(t for t in targets if t != "logrotate")
        print(f"  check: systemctl status {names}")


if __name__ == "__main__":
    main()
