from pathlib import Path

from src.core.updater import DEFAULT_UPDATE_REPO_NAME, DEFAULT_UPDATE_REPO_OWNER


def test_installer_and_updater_target_funina_repository():
    project_root = Path(__file__).resolve().parents[1]
    installer = (project_root / "install_or_update.bat").read_text(encoding="utf-8")

    assert "https://github.com/puhbvuio-lab/funina.git" in installer
    assert "https://codeload.github.com/puhbvuio-lab/funina/zip/refs/heads/main" in installer
    assert DEFAULT_UPDATE_REPO_OWNER == "puhbvuio-lab"
    assert DEFAULT_UPDATE_REPO_NAME == "funina"
