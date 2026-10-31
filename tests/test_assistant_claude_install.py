"""Offline checks of the signed native Claude installer and its trust boundary."""

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "assistant/install-claude.sh"
PUBLISHED_KEY = """-----BEGIN PGP PUBLIC KEY BLOCK-----

mQINBGnK73ABEACnbytJXkjweYrwIr0aLEFRlH+C0nF44KxFc7gQmJ6PjSPMGZAD
dxZcaixU7zZl8WxEpVO0wLmIH8cf2zGOdyuZg1Yaugk1vHb2b8WBhAGCQJdPgB8W
XquedepEYtk56uP/gCoTjJDUZluEGBHnlnuujSJ4orxEdhSykEoAUfJZGEILPpMd
bphFt/Sn+Eb/TxM5jpKPdwnv8AShNF/1mZU1fWTQq9tRKJUakZj04gdaDFElQXak
CtTij+GT6yoYCARSHwGO+PC/Pr6q4tc+D7LRjxSBvUWDoFSmlqb/PJ1hj9D/7I2O
e4XXniAPWMR56KvxHlzOzrNQdJujbJdSkCwh1ZijkSd3y8ayW5WYUTGdRab99NUw
agzlabe/VVF6kzJ0Scn5q3PihB2Y9Bwo0CKnkYk7a7KT77EWv0Kkq+VHmOtqX3a2
hhX+b6a6ve9rzJ1qZYGj+obv/C3Sx1LzUjAfqVy7RJDf2uAoP5t2g8u/TkSpUxhM
VEjZBkSxYZhMyzQM6t8IgkUfnSrIPTHixbDWARZ4beMOBjxyPZK1nP7OOrNR3TkK
JtwLMQAabURCDnL0PjS0iwBTU4jtumBD1XSULyWuoTvMljrpQr1nV1oDyOt0OLqa
KA2McWtd9PdXhC8y2EIg7TmrTlJLfHYbdmkiCYj4J49Q8HWkN/6WE+RTUwARAQAB
tD5BbnRocm9waWMgQ2xhdWRlIENvZGUgUmVsZWFzZSBTaWduaW5nIDxzZWN1cml0
eUBhbnRocm9waWMuY29tPokCUQQTAQoAOxYhBDHd3iTd+rZ59C170rqpKf8afsrO
BQJpyu9wAhsPBQsJCAcCAiICBhUKCQgLAgQWAgMBAh4HAheAAAoJELqpKf8afsrO
l5IP/2I8X1dFy5xYczWB/coIxGjuzS/V6ByZGZZEJsbr04pmuHiFUykJqPGWGQ6q
U0YF5iEwvEkaagS5m7DzhSEf3FM3Cgafax/6d70tar9Vr1D+w6uPfxetu7u/WYJp
aolIsdh5fTrBh9zSM1Njl8FM8wG8CwZQjS33Oa7d8cwRkgdUWbt6LXgz+cTQNuBn
BgW6Ks7oZFI25dfu0ojDR+aDFJg4+4wZoyDLPvJz1SIrJ5WFGs67zsx9SfS3yZnf
XKmBe+f0dUy+GJ2nFZrXFf99+c0dPEHYO8DCeAHZizjkFrdYtUHdDU0YDYEGkLJa
bE+pgcpkHf5EvsZzHsyDbl95W/eh7pcXMbwkN+W4CBYUE9X4uHhqzWaC5yAVRWUA
1BJ9V4LjZfHPLEJt0I3TxzXiEg9/BVeaTYq9RjaxIFo9Nfk158HqJY6SA5jslBlx
Gv/No8u+xVcze2UJyGVfEIUfm92+0UAIkny3+5cuVV0ICzJxXlXj0CnLM9Lt50wE
p3suVwuBEviCbZ08eAH1Ht8gbBdSsiOkIU8CX3v/scwHHx5q0+NBL6xLrQObg13a
tRXBlKObfElkPN3lTUbUnJOW4U8uSjH8VRP+AujKWMDFe7x0zCs+iYY1mTOvbrTS
9n3CmZUmbynZ+E/QWNENpW/pDNZdWFy43PASmML5FHu4m9Sn
=oqMI
-----END PGP PUBLIC KEY BLOCK-----"""


def gpg(home, *args, check=True):
    return subprocess.run(
        [
            "gpg",
            "--homedir",
            str(home),
            "--batch",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            *args,
        ],
        capture_output=True,
        check=check,
        timeout=15,
    )


@pytest.fixture(scope="session")
def signing_keys():
    homes = []
    keys = []
    try:
        for index in range(2):
            home = Path(tempfile.mkdtemp(prefix="dj216-", dir="/tmp"))
            homes.append(home)
            home.chmod(0o700)
            gpg(
                home,
                "--quick-generate-key",
                f"Installer Test {index} <test@example.com>",
                "ed25519",
                "sign",
                "0",
            )
            listing = gpg(home, "--with-colons", "--list-keys").stdout.decode()
            fingerprint = next(
                line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")
            )
            public = gpg(home, "--armor", "--export").stdout
            keys.append(SimpleNamespace(home=home, fingerprint=fingerprint, public=public))
        yield keys
    finally:
        for home in homes:
            subprocess.run(
                ["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"],
                capture_output=True,
                check=True,
                timeout=10,
            )
            shutil.rmtree(home)


@pytest.fixture
def installer(tmp_path, signing_keys):
    key, foreign = signing_keys
    script = tmp_path / "installer.sh"
    original = SCRIPT.read_text()
    assert original.count("31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE") == 1
    script.write_text(original.replace("31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE", key.fingerprint))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    mapping = tmp_path / "mapping.json"
    curl = bindir / "curl"
    curl.write_text(
        f"#!{sys.executable}\n"
        "import json, os, shlex, shutil, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['calls'], 'a') as f: f.write(json.dumps(args) + '\\n')\n"
        "url = next(a for a in args if a.startswith('https://'))\n"
        "source = json.load(open(os.environ['mapping'])).get(url)\n"
        "if source is None:\n"
        "    print('curl: (22) The requested URL returned error: 404', file=sys.stderr)\n"
        "    sys.exit(22)\n"
        "shutil.copyfile(source, args[args.index('-o') + 1])\n"
    )
    curl.chmod(0o755)
    payloads = {
        "linux-x64": b"amd64 fixture, never executed\n",
        "linux-arm64": b"arm64 fixture, never executed\n",
    }
    manifest = tmp_path / "manifest.json"
    signature = tmp_path / "manifest.json.sig"
    public = tmp_path / "key.asc"
    destination = tmp_path / "installed/claude"
    inherited = tmp_path / "inherited-gnupg"
    inherited.mkdir(mode=0o700)
    (inherited / "sentinel").write_text("untouched")
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "calls": str(calls),
        "mapping": str(mapping),
        "GNUPGHOME": str(inherited),
    }

    def run(
        *,
        arch="amd64",
        version="2.1.999",
        manifest_version="2.1.999",
        checksum=None,
        corrupt=False,
        wrong_key=False,
        bad_signature=False,
        status=None,
        gpg_exit=0,
        import_exit=0,
        missing=None,
        arguments=None,
        inherit_home=True,
    ):
        signer = foreign if wrong_key else key
        public.write_bytes(signer.public)
        data = {
            "version": manifest_version,
            "platforms": {
                platform: {"checksum": hashlib.sha256(payload).hexdigest()}
                for platform, payload in payloads.items()
            },
        }
        if checksum is not None:
            data["platforms"]["linux-x64"]["checksum"] = checksum
        manifest.write_text(json.dumps(data))
        gpg(signer.home, "--yes", "--detach-sign", "--output", str(signature), str(manifest))
        if bad_signature:
            manifest.write_text(manifest.read_text() + " ")
        urls = {"https://downloads.claude.ai/keys/claude-code.asc": str(public)}
        base = f"https://downloads.claude.ai/claude-code-releases/{version}"
        urls.update(
            {f"{base}/manifest.json": str(manifest), f"{base}/manifest.json.sig": str(signature)}
        )
        for platform, payload in payloads.items():
            file = tmp_path / platform
            file.write_bytes(payload + (b"corrupted" if corrupt else b""))
            urls[f"{base}/{platform}/claude"] = str(file)
        if missing:
            urls.pop(f"{base}/{missing}")
        mapping.write_text(json.dumps(urls))
        if status is not None:
            fake = bindir / "gpg"
            fake.write_text(
                "#!/bin/sh\n"
                'case " $* " in *" --batch --no-autostart "*) ;; *) exit 9;; esac\n'
                '[ "$(stat -c %a "$GNUPGHOME")" = 700 ] || exit 8\n'
                f'case " $* " in *" --import "*) exit {import_exit};; esac\n'
                f"printf '%s' {shlex.quote(status)}\nexit {gpg_exit}\n"
            )
            fake.chmod(0o755)
        result = subprocess.run(
            [
                "/bin/sh",
                str(script),
                *(arguments if arguments is not None else [arch, version, str(destination)]),
            ],
            env=env if inherit_home else {k: v for k, v in env.items() if k != "GNUPGHOME"},
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        fetched = (
            [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        )
        assert sorted(p.name for p in inherited.iterdir()) == ["sentinel"]
        for call in fetched:
            assert not Path(call[call.index("-o") + 1]).parent.exists()
            assert call[call.index("--proto") + 1] == "=https"
            assert call[call.index("--proto-redir") + 1] == "=https"
        return result, fetched

    return SimpleNamespace(
        run=run,
        destination=destination,
        payloads=payloads,
        key=key,
        script=script,
        tmp_path=tmp_path,
    )


def assert_refused(installer, result):
    assert result.returncode != 0, result.stdout
    assert not installer.destination.exists()


def status_lines(fingerprint):
    return (
        f"[GNUPG:] GOODSIG {fingerprint[-16:]} Installer Test <test@example.com>\n"
        f"[GNUPG:] VALIDSIG {fingerprint} 2026-10-31 1793440000 0 4 0 22 8 00 {fingerprint}\n"
    )


@pytest.mark.parametrize("arch,platform", [("amd64", "linux-x64"), ("arm64", "linux-arm64")])
def test_signed_install(installer, arch, platform):
    result, calls = installer.run(arch=arch)
    assert result.returncode == 0, result.stderr
    assert installer.destination.read_bytes() == installer.payloads[platform]
    assert os.access(installer.destination, os.X_OK)
    assert [next(a for a in call if a.startswith("https://")) for call in calls] == [
        "https://downloads.claude.ai/keys/claude-code.asc",
        "https://downloads.claude.ai/claude-code-releases/2.1.999/manifest.json",
        "https://downloads.claude.ai/claude-code-releases/2.1.999/manifest.json.sig",
        f"https://downloads.claude.ai/claude-code-releases/2.1.999/{platform}/claude",
    ]


@pytest.mark.parametrize("failure", ["wrong_key", "bad_signature", "manifest_version", "corrupt"])
def test_refuses_invalid_release(installer, failure):
    kwargs = {failure: "2.1.998" if failure == "manifest_version" else True}
    result, calls = installer.run(**kwargs)
    assert_refused(installer, result)
    if failure != "corrupt":
        assert len(calls) == 3  # Authentication and version precede the binary download.
    if failure == "wrong_key":
        assert "Pinned release key" in result.stderr
        assert "documented value" in result.stderr


@pytest.mark.parametrize("arguments", [[], ["amd64"], ["amd64", "1.2.3", "dest", "extra"]])
def test_argument_count(installer, arguments):
    result, calls = installer.run(arguments=arguments)
    assert_refused(installer, result)
    assert not calls


@pytest.mark.parametrize(
    "arch,version",
    [
        ("riscv64", "1.2.3"),
        ("amd64", "latest"),
        ("amd64", "1.2.3\n4.5.6"),
        ("amd64", "1.2.3-beta"),
        ("amd64", "1.2"),
    ],
)
def test_bad_inputs_before_network(installer, arch, version):
    result, calls = installer.run(arch=arch, version=version)
    assert_refused(installer, result)
    assert not calls


@pytest.mark.parametrize(
    "checksum",
    [
        "",
        "f" * 63,
        "g" * 64,
        "A" * 64,
        "f" * 65,
        "f" * 64 + "\n" + "f" * 64,
        "f" * 64 + "\n",
        "f" * 64 + "\x00",
    ],
)
def test_checksum_format_before_binary_download(installer, checksum):
    result, calls = installer.run(checksum=checksum)
    assert_refused(installer, result)
    assert len(calls) == 3


@pytest.mark.parametrize("missing", ["manifest.json", "manifest.json.sig", "linux-x64/claude"])
def test_download_failure(installer, missing):
    result, _ = installer.run(missing=missing)
    assert_refused(installer, result)
    assert "curl: (22) The requested URL returned error: 404" in result.stderr


@pytest.mark.parametrize("bad", ["EXPSIG", "EXPKEYSIG", "REVKEYSIG", "BADSIG", "ERRSIG"])
def test_rejects_bad_status_even_with_valid_lines(installer, bad):
    fingerprint = installer.key.fingerprint
    detail = (
        f"22 8 00 1793440000 9 {fingerprint}"
        if bad == "ERRSIG"
        else "Installer Test <test@example.com>"
    )
    status = status_lines(fingerprint) + f"[GNUPG:] {bad} {fingerprint[-16:]} {detail}\n"
    result, calls = installer.run(status=status)
    assert_refused(installer, result)
    assert len(calls) == 3


@pytest.mark.parametrize("missing", ["GOODSIG", "VALIDSIG"])
def test_requires_both_signature_statuses(installer, missing):
    status = (
        "\n".join(
            line
            for line in status_lines(installer.key.fingerprint).splitlines()
            if missing not in line
        )
        + "\n"
    )
    result, calls = installer.run(status=status)
    assert_refused(installer, result)
    assert len(calls) == 3


@pytest.mark.parametrize("prefix", ["", "diagnostic: "])
def test_requires_machine_status_prefix(installer, prefix):
    status = status_lines(installer.key.fingerprint).replace("[GNUPG:] ", prefix)
    result, _ = installer.run(status=status)
    assert_refused(installer, result)


def test_accepts_subkey_with_pinned_primary(installer):
    fingerprint = installer.key.fingerprint
    status = status_lines(fingerprint).replace(f"VALIDSIG {fingerprint}", "VALIDSIG " + "A" * 40)
    result, _ = installer.run(status=status, inherit_home=False)
    assert result.returncode == 0, result.stderr
    assert installer.destination.exists()


@pytest.mark.parametrize("operation", ["import", "verify"])
def test_gpg_exit_is_checked_independently(installer, operation):
    result, calls = installer.run(
        status=status_lines(installer.key.fingerprint),
        **{f"{operation if operation == 'import' else 'gpg'}_exit": 1},
    )
    assert_refused(installer, result)
    assert len(calls) == 3


def test_published_key_matches_unmodified_script_pin(tmp_path):
    with tempfile.TemporaryDirectory(prefix="dj216-", dir="/tmp") as directory:
        home = Path(directory)
        home.chmod(0o700)
        key = tmp_path / "key.asc"
        key.write_text(PUBLISHED_KEY)
        try:
            listing = gpg(home, "--with-colons", "--show-keys", str(key)).stdout.decode()
            fingerprint = next(
                line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")
            )
            assert re.findall(r"^fingerprint=([A-F0-9]{40})$", SCRIPT.read_text(), re.M) == [
                fingerprint
            ]
        finally:
            subprocess.run(
                ["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"],
                capture_output=True,
                check=True,
                timeout=10,
            )


def test_signature_precedes_manifest_version_check(installer):
    result, calls = installer.run(wrong_key=True, manifest_version="2.1.998")
    assert_refused(installer, result)
    assert "Pinned release key" in result.stderr
    assert len(calls) == 3
