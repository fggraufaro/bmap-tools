"""Throwaway local PostgreSQL for the a132 tests. Nothing here touches Supabase or any real database.

ScratchCluster starts its own cluster in a temp directory on a high port (trust auth, localhost only) and removes it
on stop(). build_runner_db() loads the local shim, the replica of the production Rate Radar tables and the a132
migration draft, which is the state the runner role sees once the migration is applied.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
RR = HERE.parent.parent
MIGRATION = RR / "draft" / "a132_runner_watchlist.sql"
SHIM = HERE / "local_000_shim.sql"
BASELINE = HERE / "local_001_baseline.sql"


def find_pgbin(arg=None):
    cands = [arg, os.environ.get("PGBIN")]
    which = shutil.which("psql")
    if which:
        cands.append(str(Path(which).parent))
    cands += [r"C:\Program Files\PostgreSQL\18\bin", r"C:\Program Files\PostgreSQL\17\bin", r"C:\Program Files\PostgreSQL\16\bin"]
    for c in cands:
        if c and (Path(c) / ("initdb.exe" if os.name == "nt" else "initdb")).exists():
            return Path(c)
    return None


class ScratchCluster:
    def __init__(self, bindir, port=54341):
        self.bin, self.port = Path(bindir), port
        self.work = Path(tempfile.mkdtemp(prefix="a132_pg_"))
        self.data = self.work / "data"

    def _exe(self, name):
        return str(self.bin / (name + (".exe" if os.name == "nt" else "")))

    def start(self):
        r = subprocess.run([self._exe("initdb"), "-D", str(self.data), "-U", "postgres", "-A", "trust", "-E", "UTF8", "--no-locale"],
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("initdb failed: " + r.stderr[:300])
        # the server inherits pg_ctl's stdio: pipes would block forever, so DEVNULL plus a log file
        r = subprocess.run([self._exe("pg_ctl"), "-D", str(self.data), "-o", f"-p {self.port} -c listen_addresses=127.0.0.1",
                            "-l", str(self.work / "pg.log"), "-w", "start"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode:
            raise RuntimeError("pg_ctl start failed; see " + str(self.work / "pg.log"))

    def stop(self):
        subprocess.run([self._exe("pg_ctl"), "-D", str(self.data), "-m", "immediate", "stop"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(self.work, ignore_errors=True)

    def psql(self, db, sql=None, file=None, single_tx=False):
        cmd = [self._exe("psql"), "-h", "127.0.0.1", "-p", str(self.port), "-U", "postgres", "-X", "-q", "-t", "-A",
               "-v", "ON_ERROR_STOP=1", "-d", db] + (["-1"] if single_tx else [])
        cmd += ["-c", sql] if sql is not None else ["-f", str(file)]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        if r.returncode:
            raise RuntimeError(f"psql failed on {db}: {r.stderr.strip()[:500]}")
        return r.stdout.strip()

    def dsn(self, db, user):
        return f"postgresql://{user}@127.0.0.1:{self.port}/{db}?sslmode=disable"


def build_runner_db(cluster, db):
    """Create db with the production-shaped Rate Radar tables and the a132 migration applied."""
    cluster.psql("postgres", sql=f"create database {db}")
    cluster.psql(db, file=SHIM)
    cluster.psql(db, file=BASELINE)
    cluster.psql(db, file=MIGRATION, single_tx=True)
