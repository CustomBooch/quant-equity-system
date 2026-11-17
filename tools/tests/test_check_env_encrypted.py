"""tools/check_env_encrypted.py: a planted plaintext secret and an un-ignored key file are caught; the public key and a fresh clone without .env are not findings."""
from __future__ import annotations

import subprocess

import tools_loader

CE = tools_loader.load("check_env_encrypted")


def repo(tmp_path, gitignore=".env\n.env.keys\n", env=None):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(gitignore)
    if env is not None:
        (tmp_path / ".env").write_text(env)
    return tmp_path


def test_a_plaintext_value_is_named_but_the_public_key_and_ciphertexts_are_not():
    text = 'DOTENV_PUBLIC_KEY="02ab"\n# a comment\nA="encrypted:BOx"\nB=hunter2\nexport C=\nD=\'encrypted:x\'\n'
    assert CE.plaintext(text) == ["B", "C"]


def test_a_repository_whose_key_file_is_not_ignored_fails(tmp_path):
    r = repo(tmp_path, gitignore=".env\n", env='A="encrypted:x"\n')
    assert CE.findings(r) == [".env.keys is not git-ignored"]


def test_a_planted_plaintext_secret_fails(tmp_path):
    r = repo(tmp_path, env='A="encrypted:x"\nBROKER_SECRET_KEY=abc\n')
    assert CE.findings(r) == [".env: BROKER_SECRET_KEY is not encrypted (use `dotenvx set BROKER_SECRET_KEY <value>` or `dotenvx encrypt`)"]


def test_a_fresh_clone_without_an_env_file_passes(tmp_path):
    assert CE.findings(repo(tmp_path)) == []


def test_this_repository_passes():
    assert CE.findings() == []
