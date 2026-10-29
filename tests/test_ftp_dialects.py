"""FTP servers that behave differently from pyftpdlib's defaults: no SIZE, MKD 521/553, OPTS UTF8 ON,
450 on an empty NLST. Each test runs a real local FTP server with one quirk switched on."""
import ftplib
import os
import threading

import pytest
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from app.config import Settings
from app.services.ftp import FtpRemote, upload_verified


class QuirkHandler(FTPHandler):
    no_size = False
    mkd_exists_code = None  # "521" or "553": reply for MKD on a folder that already exists
    opts_seen: list[str] = []
    empty_nlst_450 = False

    def ftp_SIZE(self, path):
        if self.no_size:
            return self.respond("502 Command not implemented.")
        return super().ftp_SIZE(path)

    def ftp_MKD(self, path):
        if self.mkd_exists_code and os.path.exists(path):
            return self.respond(f"{self.mkd_exists_code} Already exists.")
        return super().ftp_MKD(path)

    def ftp_OPTS(self, line):
        self.opts_seen.append(line)
        if line.upper() == "UTF8 ON":
            return self.respond("200 UTF8 mode enabled.")
        return super().ftp_OPTS(line)

    def ftp_NLST(self, path):
        if self.empty_nlst_450 and os.path.isdir(path) and not os.listdir(path):
            return self.respond("450 No files found.")
        return super().ftp_NLST(path)


@pytest.fixture
def server(tmp_path):
    """Yields (make_remote, root, handler_class). Set quirks on the handler class before connecting."""
    root = tmp_path / "ftp"
    root.mkdir()
    auth = DummyAuthorizer()
    auth.add_user("vai", "vai-pass", str(root), perm="elradfmwMT")
    handler = type("Handler", (QuirkHandler,), {"authorizer": auth, "encoding": "utf-8", "opts_seen": []})
    ftpd = FTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=ftpd.serve_forever, kwargs={"timeout": 0.1}, daemon=True)
    thread.start()
    remotes: list[FtpRemote] = []

    def make_remote() -> FtpRemote:
        s = Settings(ftp_protocol="ftp", ftp_host="127.0.0.1", ftp_port=ftpd.address[1], ftp_user="vai",
                     ftp_password="vai-pass")
        remotes.append(FtpRemote(s))
        return remotes[-1]

    yield make_remote, root, handler
    for r in remotes:
        r.close()
    ftpd.close_all()


def _local(tmp_path, data=b"VIDEO-BYTES"):
    p = tmp_path / "local.bin"
    p.write_bytes(data)
    return str(p), len(data)


def test_server_without_size_still_delivers(server, tmp_path):
    make_remote, root, handler = server
    handler.no_size = True
    remote = make_remote()
    remote.makedirs("/Vartamaan_DND/SLUG")
    path, size = _local(tmp_path)
    upload_verified(remote, path, "/Vartamaan_DND/SLUG/clip.mp4", size)
    assert (root / "Vartamaan_DND/SLUG/clip.mp4").read_bytes() == b"VIDEO-BYTES"
    assert not (root / "Vartamaan_DND/SLUG/clip.mp4.part").exists()


@pytest.mark.parametrize("code", ["521", "553"])
def test_mkd_on_existing_folder(server, code):
    make_remote, root, handler = server
    handler.mkd_exists_code = code
    (root / "Vartamaan_DND" / "SLUG").mkdir(parents=True)
    make_remote().makedirs("/Vartamaan_DND/SLUG")  # no error


def test_mkd_refusal_that_is_not_a_folder_still_raises(server):
    make_remote, root, handler = server
    handler.mkd_exists_code = "553"
    (root / "Vartamaan_DND").mkdir()
    (root / "Vartamaan_DND" / "SLUG").write_bytes(b"a file, not a folder")
    with pytest.raises(ftplib.error_perm):
        make_remote().makedirs("/Vartamaan_DND/SLUG")


def test_opts_utf8_sent_and_hindi_name_uploads(server, tmp_path):
    make_remote, root, handler = server
    remote = make_remote()
    assert "UTF8 ON" in [o.upper() for o in handler.opts_seen]
    remote.makedirs("/Vartamaan_DND/SLUG")
    path, size = _local(tmp_path)
    upload_verified(remote, path, "/Vartamaan_DND/SLUG/प्रेस नोट.pdf", size)
    assert (root / "Vartamaan_DND/SLUG/प्रेस नोट.pdf").exists()


def test_empty_folder_answering_450_lists_as_empty(server):
    make_remote, root, handler = server
    handler.empty_nlst_450 = True
    (root / "Vartamaan_DND").mkdir()
    assert make_remote().listdir("/Vartamaan_DND") == []


class TornRemote:
    """The server keeps fewer bytes than were sent."""

    def __init__(self):
        self.files: dict[str, int] = {}

    def put(self, local_path, remote_path):
        self.files[remote_path] = os.path.getsize(local_path) - 3

    def size(self, path):
        return self.files[path]

    def rename(self, src, dst):
        self.files[dst] = self.files.pop(src)

    def delete(self, path):
        del self.files[path]


def test_torn_upload_raises_and_removes_part(tmp_path):
    remote = TornRemote()
    path, size = _local(tmp_path)
    with pytest.raises(IOError, match="Size check failed"):
        upload_verified(remote, path, "/Vartamaan_DND/SLUG/clip.mp4", size)
    assert remote.files == {}
