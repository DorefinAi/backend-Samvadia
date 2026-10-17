"""One interface over FTP, FTPS and SFTP; FTP_PROTOCOL picks which.

Uploads go to "<name>.part" first and are renamed only after the size matches,
so nobody on the newsroom side ever opens a half-written file.
"""
import base64
import ftplib
import posixpath
from typing import Protocol

from app.config import Settings, get_settings


class Remote(Protocol):
    def exists(self, path: str) -> bool: ...
    def makedirs(self, path: str) -> None: ...
    def listdir(self, path: str) -> list[str]: ...
    def put(self, local_path: str, remote_path: str) -> None: ...
    def size(self, path: str) -> int: ...
    def rename(self, src: str, dst: str) -> None: ...
    def close(self) -> None: ...


class FtpRemote:
    def __init__(self, s: Settings):
        self.ftp: ftplib.FTP = ftplib.FTP_TLS() if s.ftp_protocol == "ftps" else ftplib.FTP()
        self.ftp.encoding = "utf-8"  # Hindi file names
        self.ftp.connect(s.ftp_host, s.ftp_port, timeout=60)
        self.ftp.login(s.ftp_user, s.ftp_password)
        if isinstance(self.ftp, ftplib.FTP_TLS):
            self.ftp.prot_p()  # encrypt the data channel too
        self.ftp.set_pasv(s.ftp_passive)
        self.ftp.voidcmd("TYPE I")

    def exists(self, path: str) -> bool:
        parent, name = posixpath.split(path.rstrip("/"))
        return name in self.listdir(parent or "/")

    def makedirs(self, path: str) -> None:
        current = ""
        for part in path.strip("/").split("/"):
            current += "/" + part
            try:
                self.ftp.mkd(current)
            except ftplib.error_perm as e:
                if not str(e).startswith("550"):  # 550: already exists
                    raise

    def listdir(self, path: str) -> list[str]:
        try:
            return [posixpath.basename(n) for n in self.ftp.nlst(path)]
        except ftplib.error_perm as e:
            if str(e).startswith("550"):  # empty or missing folder
                return []
            raise

    def put(self, local_path: str, remote_path: str) -> None:
        with open(local_path, "rb") as f:
            self.ftp.storbinary(f"STOR {remote_path}", f, blocksize=1024 * 1024)

    def size(self, path: str) -> int:
        return int(self.ftp.size(path) or -1)

    def rename(self, src: str, dst: str) -> None:
        self.ftp.rename(src, dst)

    def close(self) -> None:
        try:
            self.ftp.quit()
        except Exception:
            self.ftp.close()


class SftpRemote:
    def __init__(self, s: Settings):
        import paramiko

        self.ssh = paramiko.SSHClient()
        if s.sftp_host_key:
            key_type, key_b64 = s.sftp_host_key.split()[:2]
            key = paramiko.PKey.from_type_string(key_type, base64.b64decode(key_b64))
            host = s.ftp_host if s.ftp_port == 22 else f"[{s.ftp_host}]:{s.ftp_port}"
            self.ssh.get_host_keys().add(host, key_type, key)
            self.ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        elif s.sftp_allow_any_host_key:
            self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            raise RuntimeError("Set SFTP_HOST_KEY (or SFTP_ALLOW_ANY_HOST_KEY=true for testing only).")
        self.ssh.connect(s.ftp_host, port=s.ftp_port, username=s.ftp_user, password=s.ftp_password, timeout=60)
        self.sftp = self.ssh.open_sftp()

    def exists(self, path: str) -> bool:
        try:
            self.sftp.stat(path)
            return True
        except FileNotFoundError:
            return False

    def makedirs(self, path: str) -> None:
        current = ""
        for part in path.strip("/").split("/"):
            current += "/" + part
            if not self.exists(current):
                self.sftp.mkdir(current)

    def listdir(self, path: str) -> list[str]:
        try:
            return self.sftp.listdir(path)
        except FileNotFoundError:
            return []

    def put(self, local_path: str, remote_path: str) -> None:
        self.sftp.put(local_path, remote_path)

    def size(self, path: str) -> int:
        return int(self.sftp.stat(path).st_size or -1)

    def rename(self, src: str, dst: str) -> None:
        self.sftp.posix_rename(src, dst)

    def close(self) -> None:
        self.sftp.close()
        self.ssh.close()


def connect() -> Remote:
    s = get_settings()
    if s.ftp_protocol == "sftp":
        return SftpRemote(s)
    if s.ftp_protocol in ("ftp", "ftps"):
        return FtpRemote(s)
    raise RuntimeError(f"Unknown FTP_PROTOCOL {s.ftp_protocol!r}; use ftp, ftps or sftp.")


def upload_verified(remote: Remote, local_path: str, remote_path: str, expected_size: int) -> None:
    """Upload as .part, compare sizes, then rename into place. Raises if the sizes differ."""
    part = remote_path + ".part"
    remote.put(local_path, part)
    got = remote.size(part)
    if got != expected_size:
        raise IOError(f"Size check failed for {remote_path}: FTP has {got} bytes, expected {expected_size}.")
    remote.rename(part, remote_path)


def folder_for(slug: str) -> str:
    return posixpath.join(get_settings().ftp_base_dir, slug)
