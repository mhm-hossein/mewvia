"""Deploy Mewvia project to remote Ubuntu server via Paramiko SFTP/SSH."""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import paramiko

HOST = os.environ.get("MEWVIA_REMOTE_HOST", "")
USER = os.environ.get("MEWVIA_REMOTE_USER", "root")
PASSWORD = os.environ.get("MEWVIA_REMOTE_PASSWORD", "")
REMOTE_DIR = os.environ.get("MEWVIA_REMOTE_DIR", "/root/Mewvia")
LOCAL_DIR = Path(__file__).parent

# Ensure stdout uses UTF-8 encoding on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def connect_ssh() -> paramiko.SSHClient:
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.connect(
        HOST,
        username=USER,
        password=PASSWORD,
        timeout=60,
        banner_timeout=60,
        auth_timeout=60,
        look_for_keys=False,
        allow_agent=False,
    )
    transport = ssh.get_transport()
    if transport:
        transport.set_keepalive(10)
    return ssh


def exec_ssh(ssh: paramiko.SSHClient, cmd: str, timeout: float = 300) -> tuple[str, str]:
    with ssh.get_transport().open_session(timeout=timeout) as channel:
        channel.settimeout(timeout)
        channel.set_combine_stderr(True)
        channel.exec_command(cmd)
        with channel.makefile("rb") as stream:
            out = stream.read().decode("utf-8", errors="replace")
        status = channel.recv_exit_status()
        if status != 0:
            raise RuntimeError(f"remote command failed ({status}): {out}")
        return out, ""


def main():
    if not HOST:
        sys.exit("Set MEWVIA_REMOTE_HOST and verify the server's key in known_hosts first.")
    remote_dir = shlex.quote(REMOTE_DIR)
    compose = f"cd {remote_dir} && docker compose -f docker-compose.yml"
    print(f"Connecting via SSH to {USER}@{HOST}...")
    ssh = connect_ssh()
    print("Connected to SSH successfully!")

    out, err = exec_ssh(ssh, "docker compose version || docker-compose version")
    print(f"Docker compose version: {out.strip()}")

    # Setup SFTP transfer
    print("Transferring project files via SFTP...")
    sftp = ssh.open_sftp()

    def sftp_mkdir_p(remote_directory):
        if not remote_directory or remote_directory == "/":
            return
        parts = remote_directory.split("/")
        path = ""
        for part in parts:
            if not part:
                continue
            path += "/" + part
            try:
                sftp.stat(path)
            except OSError:
                try:
                    sftp.mkdir(path)
                except OSError:
                    pass

    ignored_dirs = {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".claude",
    }
    ignored_files = {".DS_Store", "deploy_remote.py"}

    transferred = 0
    for root, dirs, files in os.walk(LOCAL_DIR):
        dirs[:] = [d for d in dirs if d not in ignored_dirs and not d.startswith(".")]
        rel_path = Path(root).relative_to(LOCAL_DIR)

        if str(rel_path) == ".":
            remote_root = REMOTE_DIR
        else:
            remote_root = f"{REMOTE_DIR}/{rel_path.as_posix()}"

        sftp_mkdir_p(remote_root)

        for file_name in files:
            if (
                file_name in ignored_files
                or (file_name.startswith(".env") and file_name != ".env.example")
                or Path(root, file_name).is_symlink()
                or file_name.endswith(".pyc")
                or file_name.endswith(".dump")
            ):
                continue

            local_file = os.path.join(root, file_name)
            remote_file = f"{remote_root}/{file_name}"

            sftp.put(local_file, remote_file)
            transferred += 1

    sftp.close()
    print(f"Transferred {transferred} files to {REMOTE_DIR}")

    # Launching containers on remote server
    print("Launching Docker Compose containers on remote server...")
    out, err = exec_ssh(ssh, f"{compose} build", timeout=900)
    print(out)
    if err:
        print("Up stderr:", err)

    print("Running database migrations on remote server...")
    out, err = exec_ssh(
        ssh, f"{compose} run --rm -T bot alembic upgrade head", timeout=300
    )
    print(out)
    if err:
        print("Migration output:", err)

    print("Inspecting remote container status...")
    out, err = exec_ssh(ssh, f"{compose} up -d --wait", timeout=300)
    print(out)

    print("Inspecting remote bot logs...")
    out, err = exec_ssh(ssh, f"{compose} logs --tail=30 bot")
    print(out)

    ssh.close()
    print(f"\n🎉 Deployment to Ubuntu server {HOST} completed successfully!")


if __name__ == "__main__":
    main()
