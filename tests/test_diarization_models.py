import hashlib
import io
import tarfile

from whisper_key.infrastructure import DiarizationModelManager


def test_model_installer_verifies_and_atomically_promotes_files(tmp_path, monkeypatch):
    segmentation = b"fake segmentation onnx"
    embedding = b"fake embedding onnx"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:bz2") as bundle:
        for name, content in {
            "sherpa-onnx-pyannote-segmentation-3-0/model.onnx": segmentation,
            "sherpa-onnx-pyannote-segmentation-3-0/LICENSE": b"test license",
        }.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            bundle.addfile(member, io.BytesIO(content))
    archive_bytes = archive.getvalue()
    manager = DiarizationModelManager(tmp_path)
    monkeypatch.setattr(manager, "SEGMENTATION_SHA256", hashlib.sha256(archive_bytes).hexdigest())
    monkeypatch.setattr(manager, "SEGMENTATION_FILE_SHA256", hashlib.sha256(segmentation).hexdigest())
    monkeypatch.setattr(manager, "EMBEDDING_SHA256", hashlib.sha256(embedding).hexdigest())
    monkeypatch.setattr(manager, "EMBEDDING_LICENSE_SHA256", hashlib.sha256(b"embedding license").hexdigest())

    def fake_download(url, destination, completed, total, progress, maximum_bytes):
        if "segmentation" in url:
            content = archive_bytes
        elif "LICENSE" in url:
            content = b"embedding license"
        else:
            content = embedding
        assert len(content) <= maximum_bytes
        destination.write_bytes(content)
        if progress:
            progress("test", completed + 1, total)

    monkeypatch.setattr(manager, "_download", fake_download)
    updates = []

    manifest = manager.install(lambda message, done, total: updates.append((message, done, total)))

    assert manager.installed
    assert (tmp_path / manager.SEGMENTATION_NAME).read_bytes() == segmentation
    assert (tmp_path / manager.EMBEDDING_NAME).read_bytes() == embedding
    assert (tmp_path / "PYANNOTE-SEGMENTATION-LICENSE.txt").read_text() == "test license"
    assert (tmp_path / "3D-SPEAKER-LICENSE.txt").read_text() == "embedding license"
    assert manifest["models"][0]["file_sha256"] == hashlib.sha256(segmentation).hexdigest()
    assert manifest["schema_version"] == 2
    assert not list(tmp_path.glob("*.download"))
    assert updates[-1] == ("Diarización instalada", 3, 3)


def test_installed_rejects_tampered_model_after_install(tmp_path, monkeypatch):
    segmentation = b"segmentation"
    embedding = b"embedding"
    license_text = b"license"
    manager = DiarizationModelManager(tmp_path)
    monkeypatch.setattr(manager, "SEGMENTATION_FILE_SHA256", hashlib.sha256(segmentation).hexdigest())
    monkeypatch.setattr(manager, "EMBEDDING_SHA256", hashlib.sha256(embedding).hexdigest())
    monkeypatch.setattr(manager, "EMBEDDING_LICENSE_SHA256", hashlib.sha256(license_text).hexdigest())
    (tmp_path / manager.SEGMENTATION_NAME).write_bytes(segmentation)
    (tmp_path / manager.EMBEDDING_NAME).write_bytes(embedding)
    (tmp_path / manager.SEGMENTATION_LICENSE_NAME).write_text("license")
    (tmp_path / manager.EMBEDDING_LICENSE_NAME).write_bytes(license_text)
    (tmp_path / manager.MANIFEST_NAME).write_text('{"schema_version": 2}')

    assert manager.installed

    (tmp_path / manager.EMBEDDING_NAME).write_bytes(b"tampered")

    valid, detail = manager.verify_installed()
    assert not valid
    assert "integridad" in detail
    assert not manager.installed
