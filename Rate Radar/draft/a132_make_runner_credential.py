#!/usr/bin/env python3
"""a132 DRAFT: make the password for the rate_radar_runner login, without ever showing it.

    python a132_make_runner_credential.py --out C:\\secrets\\rate_radar_runner.pw

Generates a random password, writes it to --out (a file OUTSIDE any git repository; refuses to overwrite), and prints
ONLY a statement that sets the role's password to a SCRAM-SHA-256 verifier. Francisco runs that statement once in the
Supabase SQL editor after the migration is applied. The plaintext password never reaches the terminal, the chat or the
repository; it is read from the file when the Railway variable is set.

The verifier follows PostgreSQL's format (RFC 5802 / 7677, 4096 iterations): SCRAM-SHA-256$4096:<salt>$<StoredKey>:<ServerKey>.
Run with --self-test to check the derivation against the RFC 7677 test vector.
"""
import argparse
import base64
import hashlib
import hmac
import os
import secrets
import subprocess
import sys
from pathlib import Path

ROLE = "rate_radar_runner"
ITERATIONS = 4096


def scram_verifier(password, salt=None, iterations=ITERATIONS):
    """PostgreSQL SCRAM-SHA-256 verifier for an ASCII password (SASLprep is the identity on ASCII)."""
    salt = salt if salt is not None else secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def self_test():
    # RFC 7677 section 3: user "user", password "pencil", salt W22ZaJ0SNY7soEsUEjb6gQ==, 4096 iterations
    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
    salted = hashlib.pbkdf2_hmac("sha256", b"pencil", salt, 4096)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    # StoredKey the RFC 7677 exchange is built on: the client proof XOR ClientKey must equal ClientKey
    proof = base64.b64decode("dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=")
    auth_message = ("n=user,r=rOprNGfwEbeRWgbNEkqO,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,"
                    "s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0").encode()
    client_sig = hmac.new(stored_key, auth_message, hashlib.sha256).digest()
    recovered = bytes(a ^ b for a, b in zip(proof, client_sig))
    ok = hashlib.sha256(recovered).digest() == stored_key
    print("self-test:", "PASS" if ok else "FAIL")
    return ok


def inside_git_repo(path):
    try:
        out = subprocess.run(["git", "-C", str(path.parent), "rev-parse", "--is-inside-work-tree"],
                             capture_output=True, text=True)
        return out.returncode == 0 and out.stdout.strip() == "true"
    except FileNotFoundError:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="file to write the password to (outside any git repository; must not exist)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return 0 if self_test() else 1
    if not args.out:
        ap.error("--out is required")
    out = Path(args.out).expanduser().resolve()
    if inside_git_repo(out):
        print(f"REFUSED: {out.parent} is inside a git repository. Choose a folder outside every repository.", file=sys.stderr)
        return 2
    if out.exists():
        print(f"REFUSED: {out} already exists. Delete it deliberately if you mean to rotate the password.", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(36)
    fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(password + "\n")
    verifier = scram_verifier(password)
    print(f"Password written to {out} (not shown here; keep it out of chat, email and git).")
    print("Run this once in the Supabase SQL editor, as postgres, AFTER the a132 migration is applied:\n")
    print(f"ALTER ROLE {ROLE} PASSWORD '{verifier}';\n")
    print("Then set RATE_RADAR_DATABASE_URL on the Railway service from that file (see RAILWAY_CRON_WEEKLY.md). "
          "To rotate: delete the file, run this script again, run the new ALTER ROLE and update the variable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
