"""Local credential intake: synthetic values, no website or real secret reads."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "deploy/employmenthero-cred-setup"
loader = importlib.machinery.SourceFileLoader("employmenthero_setup", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
setup = importlib.util.module_from_spec(spec)
loader.exec_module(setup)

CIPHER = b"EH_EMAIL=ENC[AES256_GCM,data:fake]\nEH_PASSWORD=ENC[AES256_GCM,data:fake]\n"


def test_intake_uses_hidden_prompts_and_preserves_password():
    prompts = []
    values = iter([" test@example.invalid ", 'punctuation $`\\\"= and Unicode \u00e9'])
    def hidden(prompt):
        prompts.append(prompt)
        return next(values)
    got = setup.collect(hidden)
    assert got["EH_EMAIL"] == "test@example.invalid"
    assert got["EH_PASSWORD"] == 'punctuation $`\\\"= and Unicode \u00e9'
    assert all("hidden" in p for p in prompts)


@pytest.mark.parametrize("email,password", [("", "pw"), ("not-email", "pw"), ("a@b", "")])
def test_incomplete_input_is_rejected(email, password):
    values = iter([email, password])
    with pytest.raises(ValueError):
        setup.collect(lambda prompt: next(values))


def test_encrypt_pipes_json_to_sops_without_plaintext_files(tmp_path, monkeypatch):
    (tmp_path / ".sops.yaml").write_text("test policy")
    before = set(tmp_path.iterdir())
    monkeypatch.setattr(setup.shutil, "which", lambda name: "/sops")
    calls = []
    def run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=0, stdout=CIPHER, stderr=b"")
    monkeypatch.setattr(setup.subprocess, "run", run)
    secret = {"EH_EMAIL": "test@example.invalid", "EH_PASSWORD": "fake-secret"}
    assert setup.encrypt(secret, tmp_path) == CIPHER
    argv, kw = calls[0]
    assert "fake-secret" not in " ".join(argv)
    assert json.loads(kw["input"]) == secret
    assert "env" not in kw
    assert set(tmp_path.iterdir()) == before


def test_encrypt_error_does_not_echo_sensitive_tool_output(tmp_path, monkeypatch):
    (tmp_path / ".sops.yaml").write_text("test policy")
    monkeypatch.setattr(setup.shutil, "which", lambda name: "/sops")
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=1, stdout=b"", stderr=b"fake-secret"))
    with pytest.raises(ValueError) as err:
        setup.encrypt({"EH_PASSWORD": "fake-secret"}, tmp_path)
    assert "fake-secret" not in str(err.value)


def test_storage_is_private_atomic_and_checked_without_decryption(tmp_path):
    dest = tmp_path / "private/employee.env"
    setup.save(CIPHER, dest)
    assert dest.read_bytes() == CIPHER
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert stat.S_IMODE(dest.parent.stat().st_mode) == 0o700
    assert list(dest.parent.iterdir()) == [dest]
    setup.check(dest)
    with pytest.raises(ValueError):
        setup.save(CIPHER + b"changed", dest)
    assert dest.read_bytes() == CIPHER
    setup.save(CIPHER + b"changed", dest, replace=True)
    assert dest.read_bytes() == CIPHER + b"changed"


def test_symlink_destination_is_never_followed_or_replaced(tmp_path):
    target = tmp_path / "unrelated"
    target.write_text("leave alone")
    dest = tmp_path / "private/employee.env"
    dest.parent.mkdir()
    dest.symlink_to(target)
    with pytest.raises(ValueError):
        setup.save(CIPHER, dest, replace=True)
    assert target.read_text() == "leave alone" and dest.is_symlink()


def test_storage_check_rejects_public_permissions_and_plaintext(tmp_path):
    dest = tmp_path / "employee.env"
    setup.save(CIPHER, dest)
    os.chmod(dest, 0o644)
    with pytest.raises(ValueError):
        setup.check(dest)
    os.chmod(dest, 0o600)
    dest.write_text("EH_PASSWORD=not-encrypted\n")
    with pytest.raises(ValueError):
        setup.check(dest)
