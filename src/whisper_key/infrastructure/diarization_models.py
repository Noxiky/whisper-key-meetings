from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


class DiarizationModelInstallError(RuntimeError):
    pass


class DiarizationModelManager:
    SEGMENTATION_URL = (
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/"
        "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
    )
    SEGMENTATION_SHA256 = "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488"
    SEGMENTATION_FILE_SHA256 = "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079"
    EMBEDDING_URL = (
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
        "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
    )
    EMBEDDING_SHA256 = "1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b"
    EMBEDDING_LICENSE_URL = (
        "https://raw.githubusercontent.com/modelscope/3D-Speaker/065629c313eaf1a01c65c640c46d77e61e9607b4/LICENSE"
    )
    EMBEDDING_LICENSE_SHA256 = "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"
    SEGMENTATION_NAME = "sherpa-onnx-pyannote-segmentation-3-0.onnx"
    EMBEDDING_NAME = "3dspeaker-eres2net-base-16k.onnx"
    MANIFEST_NAME = "manifest.json"
    SEGMENTATION_LICENSE_NAME = "PYANNOTE-SEGMENTATION-LICENSE.txt"
    EMBEDDING_LICENSE_NAME = "3D-SPEAKER-LICENSE.txt"
    MAX_SEGMENTATION_ARCHIVE_BYTES = 16 * 1024 * 1024
    MAX_EMBEDDING_BYTES = 80 * 1024 * 1024
    MAX_LICENSE_BYTES = 128 * 1024
    MIN_FREE_BYTES = 160 * 1024 * 1024

    def __init__(self, model_root: Path):
        self.model_root = Path(model_root)

    @property
    def installed(self) -> bool:
        valid, _ = self.verify_installed()
        return valid

    def verify_installed(self) -> tuple[bool, str]:
        required = {
            self.SEGMENTATION_NAME: self.SEGMENTATION_FILE_SHA256,
            self.EMBEDDING_NAME: self.EMBEDDING_SHA256,
            self.EMBEDDING_LICENSE_NAME: self.EMBEDDING_LICENSE_SHA256,
        }
        expected_names = (*required, self.SEGMENTATION_LICENSE_NAME, self.MANIFEST_NAME)
        missing = [name for name in expected_names if not (self.model_root / name).is_file()]
        if missing:
            return False, f"Faltan archivos: {', '.join(missing)}"
        try:
            manifest = json.loads((self.model_root / self.MANIFEST_NAME).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return False, f"El manifiesto local no es válido: {exc}"
        if manifest.get("schema_version") != 2:
            return False, "El manifiesto local necesita reinstalación"
        for name, expected in required.items():
            try:
                actual = self._sha256(self.model_root / name)
            except OSError as exc:
                return False, f"No se pudo leer {name}: {exc}"
            if actual != expected:
                return False, f"La integridad de {name} no coincide"
        return True, "Instalada y verificada"

    def install(self, progress=None) -> dict:
        self.model_root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.model_root).free < self.MIN_FREE_BYTES:
            raise DiarizationModelInstallError(
                "Se necesitan al menos 160 MB libres para descargar y verificar la diarización."
            )
        archive = self.model_root / "segmentation.tar.bz2.download"
        embedding_download = self.model_root / f"{self.EMBEDDING_NAME}.download"
        embedding_license_download = self.model_root / f"{self.EMBEDDING_LICENSE_NAME}.download"
        segmentation_temp = self.model_root / f"{self.SEGMENTATION_NAME}.tmp"
        license_temp = self.model_root / f"{self.SEGMENTATION_LICENSE_NAME}.tmp"
        try:
            self._download(
                self.SEGMENTATION_URL,
                archive,
                0,
                3,
                progress,
                self.MAX_SEGMENTATION_ARCHIVE_BYTES,
            )
            self._verify(archive, self.SEGMENTATION_SHA256)
            self._extract_member(archive, "/model.onnx", segmentation_temp, self.MAX_SEGMENTATION_ARCHIVE_BYTES)
            self._verify(segmentation_temp, self.SEGMENTATION_FILE_SHA256)
            self._extract_member(archive, "/LICENSE", license_temp, self.MAX_LICENSE_BYTES)
            self._download(
                self.EMBEDDING_URL,
                embedding_download,
                1,
                3,
                progress,
                self.MAX_EMBEDDING_BYTES,
            )
            self._verify(embedding_download, self.EMBEDDING_SHA256)
            self._download(
                self.EMBEDDING_LICENSE_URL,
                embedding_license_download,
                2,
                3,
                progress,
                self.MAX_LICENSE_BYTES,
            )
            self._verify(embedding_license_download, self.EMBEDDING_LICENSE_SHA256)
            os.replace(segmentation_temp, self.model_root / self.SEGMENTATION_NAME)
            os.replace(embedding_download, self.model_root / self.EMBEDDING_NAME)
            os.replace(license_temp, self.model_root / self.SEGMENTATION_LICENSE_NAME)
            os.replace(embedding_license_download, self.model_root / self.EMBEDDING_LICENSE_NAME)
            manifest = {
                "schema_version": 2,
                "installed_at": datetime.now(UTC).isoformat(),
                "local_only": True,
                "models": [
                    {
                        "role": "speaker_segmentation",
                        "path": self.SEGMENTATION_NAME,
                        "source": self.SEGMENTATION_URL,
                        "archive_sha256": self.SEGMENTATION_SHA256,
                        "file_sha256": self.SEGMENTATION_FILE_SHA256,
                        "license_path": self.SEGMENTATION_LICENSE_NAME,
                    },
                    {
                        "role": "speaker_embedding",
                        "path": self.EMBEDDING_NAME,
                        "source": self.EMBEDDING_URL,
                        "file_sha256": self.EMBEDDING_SHA256,
                        "license_path": self.EMBEDDING_LICENSE_NAME,
                        "license_source": self.EMBEDDING_LICENSE_URL,
                        "license_sha256": self.EMBEDDING_LICENSE_SHA256,
                    },
                ],
            }
            self._atomic_text(self.MANIFEST_NAME, json.dumps(manifest, indent=2) + "\n")
            valid, detail = self.verify_installed()
            if not valid:
                raise DiarizationModelInstallError(detail)
            if progress:
                progress("Diarización instalada", 3, 3)
            return manifest
        except Exception as exc:
            if isinstance(exc, DiarizationModelInstallError):
                raise
            raise DiarizationModelInstallError(str(exc)) from exc
        finally:
            for path in (
                archive,
                embedding_download,
                embedding_license_download,
                segmentation_temp,
                license_temp,
            ):
                path.unlink(missing_ok=True)

    def _download(
        self,
        url: str,
        destination: Path,
        completed: int,
        total: int,
        progress,
        maximum_bytes: int,
    ) -> None:
        request = urllib.request.Request(url, headers={"User-Agent": "WhisperKey/0.9.0"})
        with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
            expected = int(response.headers.get("Content-Length") or 0)
            if expected > maximum_bytes:
                raise DiarizationModelInstallError(f"La descarga supera el tamaño permitido ({expected} bytes).")
            received = 0
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                received += len(block)
                if received > maximum_bytes:
                    raise DiarizationModelInstallError("La descarga excedió el límite de seguridad.")
                output.write(block)
                if progress:
                    fraction = received / expected if expected else 0
                    progress("Descargando modelos de diarización", completed + min(fraction, 0.99), total)
            output.flush()
            os.fsync(output.fileno())
        if received == 0 or (expected and received != expected):
            raise DiarizationModelInstallError(f"La descarga quedó incompleta ({received} de {expected} bytes).")

    @classmethod
    def _verify(cls, path: Path, expected: str) -> None:
        actual = cls._sha256(path)
        if actual != expected:
            raise DiarizationModelInstallError(f"La descarga de {path.name} no coincide con su SHA-256 ({actual}).")

    @staticmethod
    def _extract_member(archive: Path, suffix: str, destination: Path, maximum_bytes: int) -> None:
        with tarfile.open(archive, "r:bz2") as bundle:
            candidates = [member for member in bundle.getmembers() if member.isfile() and member.name.endswith(suffix)]
            if len(candidates) != 1:
                raise DiarizationModelInstallError(f"El archivo no contiene exactamente un {suffix}.")
            if candidates[0].size <= 0 or candidates[0].size > maximum_bytes:
                raise DiarizationModelInstallError(f"El miembro {suffix} tiene un tamaño no permitido.")
            source = bundle.extractfile(candidates[0])
            if source is None:
                raise DiarizationModelInstallError(f"No se pudo extraer {suffix}.")
            with source, destination.open("wb") as output:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())

    def _atomic_text(self, name: str, content: str) -> None:
        destination = self.model_root / name
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, destination)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
