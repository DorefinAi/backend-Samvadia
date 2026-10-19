"""One interface over FTP, FTPS and SFTP; FTP_PROTOCOL picks which.

Uploads go to "<name>.part" first and are renamed only after the size matches,
so nobody on the newsroom side ever opens a half-written file.

FTP servers differ (IIS, Pure-FTPd, vsftpd, FileZilla): some refuse SIZE, some answer MKD on an
existing folder with 521 or 553 instead of 550, some need OPTS UTF8 ON before Hindi names work.
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
    def size(self, path: str) -> int | None: ...  # None: the server cannot tell us
    def rename(self, src: str, dst: str) -> None: ...
    def delete(self, path: str) -> None: ...
    def close(self) -> None: ...


class FtpRemote:
    def __init__(self, s: Settings):
        self.ftp: ftplib.FTP = ftplib.FTP_TLS() if s.ftp_protocol == "ftps" else ftplib.FTP()
        self.ftp.encoding = "utf-8"  # Hindi file names
        self.ftp.connect(s.ftp_host, s.ftp_port, timeout=60)
        self.ftp.login(s.ftp_user, s.ftp_password)
        try:
            self.ftp.sendcmd("OPTS UTF8 ON")  # Hindi file names; ftplib does not send it
        except ftplib.Error:
            pass  # servers that do not know the command already use UTF-8 or never will
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
                if str(e)[:3] not in ("550", "521", "553"):  # "already exists", by server
                    raise
                try:
                    self.ftp.cwd(current)  # the same codes also mean "not allowed": check it is a usable folder
                except ftplib.error_perm:
                    raise e from None

    def listdir(self, path: str) -> list[str]:
        try:
            return [posixpath.basename(n) for n in self.ftp.nlst(path)]
        except (ftplib.error_perm, ftplib.error_temp) as e:
            if str(e)[:3] in ("550", "450"):  # empty or missing folder (450 on vsftpd and IIS)
                return []
            raise

    def put(self, local_path: str, remote_path: str) -> None:
        with open(local_path, "rb") as f:
            self.ftp.storbinary(f"STOR {remote_path}", f, blocksize=1024 * 1024)

    def size(self, path: str) -> int | None:
        try:
            got = self.ftp.size(path)
        except ftplib.error_perm:
            return None  # SIZE refused or not implemented
        return int(got) if got is not None else None

    def rename(self, src: str, dst: str) -> None:
        self.ftp.rename(src, dst)

    def delete(self, path: str) -> None:
        self.ftp.delete(path)

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

    def size(self, path: str) -> int | None:
        return self.sftp.stat(path).st_size

    def rename(self, src: str, dst: str) -> None:
        self.sftp.posix_rename(src, dst)

    def delete(self, path: str) -> None:
        self.sftp.remove(path)

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
    """Upload as .part, compare sizes, then rename into place. Raises if the sizes differ.

    A server that refuses SIZE gets no size check; the upload's own 226 reply is all we have.
    On any failure the .part is removed, so a retry or a person never finds a half file."""
    part = remote_path + ".part"
    try:
        remote.put(local_path, part)
        got = remote.size(part)
        if got is not None and got != expected_size:
            raise IOError(f"Size check failed for {remote_path}: FTP has {got} bytes, expected {expected_size}.")
        remote.rename(part, remote_path)
    except Exception:
        try:
            remote.delete(part)
        except Exception:
            pass  # the connection may be gone; the next upload overwrites the .part
        raise


def folder_for(slug: str) -> str:
    return posixpath.join(get_settings().ftp_base_dir, slug)
