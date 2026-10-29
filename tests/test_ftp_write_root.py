"""Read-only guarantee (build 1, #4): every remote refuses makedirs / put / rename / delete outside FTP_WRITE_ROOT.

Run against a real local FTP server (pyftpdlib) and against the SFTP class with a recording fake, so the check
is proven in the code path each protocol really uses."""
import threading

import pytest
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from app.config import Settings, get_settings
from app.services import ftp
from app.services.ftp import FtpRemote, FtpWriteRefused, SftpRemote, is_writable


@pytest.fixture(autouse=True)
def write_root(monkeypatch):
    monkeypatch.setenv("FTP_WRITE_ROOT", "/Vartamaan_DND")
    monkeypatch.setenv("FTP_BASE_DIR", "/Vartamaan_DND")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("path,ok", [
    ("/Vartamaan_DND", True), ("/Vartamaan_DND/slug", True), ("/Vartamaan_DND/a/b.mp4", True),
    ("/Ajmer/2026/x", False), ("/", False), ("/Vartamaan_DND/../Ajmer", False), ("/Vartamaan_DND_old/x", False),
    ("/vartamaan_dnd/x", False), ("Vartamaan_DND/x", False), ("", False), ("/Vartamaan_DND/./x/..", True),
])
def test_is_writable(path, ok):
    assert is_writable(path) is ok


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "ftp"
    (root / "Ajmer" / "2026").mkdir(parents=True)
    (root / "Ajmer" / "2026" / "clip.mp4").write_bytes(b"newsroom footage")
    auth = DummyAuthorizer()
    auth.add_user("vai", "vai-pass", str(root), perm="elradfmwMT")
    handler = type("Handler", (FTPHandler,), {"authorizer": auth, "encoding": "utf-8"})
    ftpd = FTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=ftpd.serve_forever, kwargs={"timeout": 0.1}, daemon=True).start()
    remote = FtpRemote(Settings(ftp_protocol="ftp", ftp_host="127.0.0.1", ftp_port=ftpd.address[1], ftp_user="vai",
                                ftp_password="vai-pass"))
    yield remote, root
    remote.close()
    ftpd.close_all()


def test_ftp_writes_outside_the_root_are_refused_and_nothing_changes(server, tmp_path):
    remote, root = server
    local = tmp_path / "x.bin"
    local.write_bytes(b"data")
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    for call in (lambda: remote.makedirs("/Ajmer/2026/NEW"),
                 lambda: remote.put(str(local), "/Ajmer/2026/x.bin"),
                 lambda: remote.rename("/Ajmer/2026/clip.mp4", "/Vartamaan_DND/clip.mp4"),
                 lambda: remote.rename("/Vartamaan_DND/a", "/Ajmer/a"),
                 lambda: remote.delete("/Ajmer/2026/clip.mp4"),
                 lambda: remote.put(str(local), "/Vartamaan_DND/../Ajmer/x.bin"),
                 lambda: ftp.upload_verified(remote, str(local), "/Ajmer/2026/y.bin", 4)):
        with pytest.raises(FtpWriteRefused):
            call()
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == before
    assert (root / "Ajmer" / "2026" / "clip.mp4").read_bytes() == b"newsroom footage"


@pytest.mark.parametrize("path,ok", [
    ("/Vartamaan_DND", False), ("/Vartamaan_DND/", False), ("/Vartamaan_DND/x/..", False), ("/Vartamaan_DND/slug", True),
    ("/Ajmer/x", False), ("/", False),
])
def test_is_writable_strict_excludes_the_root_itself(path, ok):
    """Review L1: put, rename and delete need a path strictly below the root; makedirs may still name the root."""
    assert is_writable(path, strict=True) is ok


def test_ftp_root_itself_cannot_be_renamed_or_deleted(server, tmp_path):
    remote, root = server
    remote.makedirs("/Vartamaan_DND")  # creating (or re-confirming) the root is allowed
    (root / "Vartamaan_DND" / "slug").mkdir()
    local = tmp_path / "x.bin"
    local.write_bytes(b"data")
    for call in (lambda: remote.rename("/Vartamaan_DND", "/Vartamaan_DND/slug/moved"),
                 lambda: remote.rename("/Vartamaan_DND/slug", "/Vartamaan_DND"),
                 lambda: remote.rename("/Vartamaan_DND/slug/..", "/Vartamaan_DND/other"),
                 lambda: remote.delete("/Vartamaan_DND"),
                 lambda: remote.delete("/Vartamaan_DND/"),
                 lambda: remote.put(str(local), "/Vartamaan_DND")):
        with pytest.raises(FtpWriteRefused):
            call()
    assert (root / "Vartamaan_DND" / "slug").is_dir()


def test_ftp_rename_never_replaces_an_existing_target(server, tmp_path):
    """Review L2: renaming onto a name that exists fails clearly instead of overwriting (upload_verified's own
    .part -> final step may still replace its earlier copy)."""
    remote, root = server
    remote.makedirs("/Vartamaan_DND/A")
    remote.makedirs("/Vartamaan_DND/B")
    (root / "Vartamaan_DND" / "A" / "a.mp4").write_bytes(b"a")
    (root / "Vartamaan_DND" / "B" / "b.mp4").write_bytes(b"b")
    with pytest.raises(FileExistsError):
        remote.rename("/Vartamaan_DND/A", "/Vartamaan_DND/B")
    with pytest.raises(FileExistsError):
        remote.rename("/Vartamaan_DND/A/a.mp4", "/Vartamaan_DND/B/b.mp4")
    assert (root / "Vartamaan_DND" / "B" / "b.mp4").read_bytes() == b"b"
    assert (root / "Vartamaan_DND" / "A" / "a.mp4").read_bytes() == b"a"
    remote.rename("/Vartamaan_DND/A", "/Vartamaan_DND/C")
    assert (root / "Vartamaan_DND" / "C" / "a.mp4").read_bytes() == b"a"


def test_ftp_writes_inside_the_root_work(server, tmp_path):
    remote, root = server
    local = tmp_path / "x.bin"
    local.write_bytes(b"data")
    remote.makedirs("/Vartamaan_DND/161026_AJM_HADSA_AV_SHUBHAM")
    ftp.upload_verified(remote, str(local), "/Vartamaan_DND/161026_AJM_HADSA_AV_SHUBHAM/x.bin", 4)
    assert (root / "Vartamaan_DND" / "161026_AJM_HADSA_AV_SHUBHAM" / "x.bin").read_bytes() == b"data"
    # reading anywhere is fine
    assert [e.name for e in remote.scan("/Ajmer/2026")] == ["clip.mp4"]


def test_makedirs_never_creates_a_parent_of_the_root(server, monkeypatch):
    remote, root = server
    monkeypatch.setenv("FTP_WRITE_ROOT", "/Vartamaan_DND/samvadia")
    get_settings.cache_clear()
    (root / "Vartamaan_DND").mkdir()
    remote.makedirs("/Vartamaan_DND/samvadia/slug")
    assert (root / "Vartamaan_DND" / "samvadia" / "slug").is_dir()
    with pytest.raises(FtpWriteRefused):
        remote.makedirs("/Vartamaan_DND/other")


def test_folder_for_refuses_a_base_dir_outside_the_root(monkeypatch):
    monkeypatch.setenv("FTP_BASE_DIR", "/Ajmer")
    get_settings.cache_clear()
    with pytest.raises(FtpWriteRefused):
        ftp.folder_for("161026_AJM_HADSA_AV_SHUBHAM")


class FakeSftp:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, *args))
            if name == "stat":
                raise FileNotFoundError
        return record


def test_sftp_writes_outside_the_root_are_refused(tmp_path):
    remote = SftpRemote.__new__(SftpRemote)  # no network: only the guard and the calls it lets through
    remote.sftp = FakeSftp()
    local = tmp_path / "x.bin"
    local.write_bytes(b"data")
    for call in (lambda: remote.makedirs("/Ajmer/NEW"), lambda: remote.put(str(local), "/Ajmer/x.bin"),
                 lambda: remote.rename("/Ajmer/a", "/Vartamaan_DND/a"), lambda: remote.delete("/Ajmer/a")):
        with pytest.raises(FtpWriteRefused):
            call()
    assert remote.sftp.calls == []
    remote.makedirs("/Vartamaan_DND/slug")
    remote.put(str(local), "/Vartamaan_DND/slug/x.bin")
    remote.rename("/Vartamaan_DND/slug/x.bin.part", "/Vartamaan_DND/slug/x.bin")
    remote.delete("/Vartamaan_DND/slug/x.bin.part")
    assert [c[0] for c in remote.sftp.calls if c[0] != "stat"] == ["mkdir", "mkdir", "put", "posix_rename", "remove"]


def test_sftp_refuses_the_root_and_an_existing_target(tmp_path):
    """Review L1 and L2 on the SFTP class: no call reaches the server for the root, and posix_rename (which would
    silently replace) is not called when the target exists."""
    remote = SftpRemote.__new__(SftpRemote)
    remote.sftp = FakeSftp()
    for call in (lambda: remote.rename("/Vartamaan_DND", "/Vartamaan_DND/x"), lambda: remote.delete("/Vartamaan_DND"),
                 lambda: remote.rename("/Vartamaan_DND/x", "/Vartamaan_DND")):
        with pytest.raises(FtpWriteRefused):
            call()
    assert remote.sftp.calls == []
    existing = SftpRemote.__new__(SftpRemote)
    existing.sftp = type("Exists", (FakeSftp,), {"__getattr__": lambda self, name: (
        lambda *args: self.calls.append((name, *args)))})()
    with pytest.raises(FileExistsError):
        existing.rename("/Vartamaan_DND/a", "/Vartamaan_DND/b")
    assert [c[0] for c in existing.sftp.calls] == ["stat"]
    existing.rename("/Vartamaan_DND/a.part", "/Vartamaan_DND/a", replace=True)
    assert [c[0] for c in existing.sftp.calls] == ["stat", "posix_rename"]
