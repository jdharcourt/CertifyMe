import os

import pytest

from certifyme import config


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point the global config at a temp dir and clear DigiKey env vars."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for key in config.CRED_KEYS:
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_save_and_resolve_global(isolated):
    path = config.save_credentials("ID1", "SECRET123456")
    assert path.exists()
    info = config.resolve()
    assert info["client_id"] == "ID1"
    assert info["client_secret"] == "SECRET123456"
    assert info["id_source"] == "global config"
    assert info["configured"] is True


def test_project_overrides_global(isolated):
    config.save_credentials("GLOBALID", "GLOBALSECRET")
    proj = isolated / "proj"
    proj.mkdir()
    config.save_credentials("PROJID", "PROJSECRET", scope="project", project_dir=proj)
    info = config.resolve(proj)
    assert info["client_id"] == "PROJID"
    assert info["id_source"] == "project .env"


def test_env_overrides_files(isolated, monkeypatch):
    config.save_credentials("GLOBALID", "GLOBALSECRET")
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "ENVID")
    info = config.resolve()
    assert info["client_id"] == "ENVID"
    assert info["id_source"] == "environment"


def test_load_into_env_does_not_clobber(isolated, monkeypatch):
    config.save_credentials("GLOBALID", "GLOBALSECRET")
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "ENVID")
    config.load_into_env()
    assert os.environ["DIGIKEY_CLIENT_ID"] == "ENVID"          # not overwritten
    assert os.environ["DIGIKEY_CLIENT_SECRET"] == "GLOBALSECRET"  # filled in


def test_sandbox_roundtrip(isolated):
    config.save_credentials("ID", "SECRET", sandbox=True)
    assert config.resolve()["sandbox"] is True
    config.save_credentials("ID", "SECRET", sandbox=False)
    assert config.resolve()["sandbox"] is False


def test_mask():
    assert config.mask("") == "(not set)"
    assert config.mask("abc") == "***"
    assert config.mask("SECRETXYZ987").startswith("SE")
    assert config.mask("SECRETXYZ987").endswith("Z987")
