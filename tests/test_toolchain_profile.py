"""Tests for toolchain activation — content-addressing, tamper evidence, transient resolution."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from re_agent.toolchain.activation import VerifiedCommand, activate_profile, resolve_capability, verify_command
from re_agent.toolchain.profile import ProfileError, load_profile, load_profile_from_dict

# ── helpers ───────────────────────────────────────────────────────────────


def make_profile_yaml(path: Path, *, extra: str = "") -> None:
    """Write a minimal valid profile YAML."""
    compiler_path = str(Path(__file__).resolve())
    path.write_text(
        "backend: offline-export\n"
        "target: arbitrary\n"
        "compiler:\n"
        f"  command: [{json.dumps(compiler_path)}]\n"
        f"  flags: ['-c']\n"
        f"{extra}",
        encoding="utf-8",
    )


def make_profile_with_linker_yaml(path: Path) -> None:
    """Write a profile with both compiler and linker."""
    compiler_path = str(Path(__file__).resolve())
    path.write_text(
        "backend: offline-export\n"
        "target: arbitrary\n"
        "compiler:\n"
        f"  command: [{json.dumps(compiler_path)}]\n"
        f"  flags: ['-c']\n"
        "linker:\n"
        f"  command: [{json.dumps(compiler_path)}]\n"
        "  args: ['-o', 'out']\n",
        encoding="utf-8",
    )


def _profile_hash(path: Path) -> str:
    """Compute the content hash a profile YAML would produce."""
    p = load_profile(path)
    return p.sha256


# ── profile.py additions ──────────────────────────────────────────────────


class TestLoadProfileFromDict:
    def test_valid_dict_matches_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "p.yaml"
        make_profile_yaml(path)
        from_yaml = load_profile(path)
        raw = {
            "backend": "offline-export",
            "target": "arbitrary",
            "compiler": {"command": [str(Path(__file__).resolve())], "flags": ["-c"]},
            "extensions": {},
        }
        from_dict = load_profile_from_dict(raw)
        assert from_yaml == from_dict

    def test_rejects_unknown_key(self) -> None:
        with pytest.raises(ProfileError, match="unknown key"):
            load_profile_from_dict({"backend": "x", "target": "y", "compiler": {"command": ["gcc"]}, "bogus": 1})

    def test_rejects_missing_required(self) -> None:
        with pytest.raises(ProfileError, match="profile.backend is required"):
            load_profile_from_dict({"target": "x", "compiler": {"command": ["gcc"]}})

    def test_rejects_non_string_backend(self) -> None:
        with pytest.raises(ProfileError, match="profile.backend"):
            load_profile_from_dict({"backend": 42, "target": "x", "compiler": {"command": ["gcc"]}})


# ── verify_command ────────────────────────────────────────────────────────


class TestVerifyCommand:
    def test_passes_for_unchanged_binary(self) -> None:
        path = Path(__file__).resolve()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        vc = VerifiedCommand(argv=(str(path), "-c"), executable_sha256=digest)
        verify_command(vc)  # no raise

    def test_raises_when_binary_missing(self) -> None:
        vc = VerifiedCommand(argv=("/nonexistent/binary",), executable_sha256="0" * 64)
        with pytest.raises(ProfileError, match="toolchain binary not found"):
            verify_command(vc)

    def test_raises_when_hash_mismatch(self, tmp_path: Path) -> None:
        binary = tmp_path / "tool.exe"
        binary.write_text("original content")
        digest = hashlib.sha256(b"different content").hexdigest()
        vc = VerifiedCommand(argv=(str(binary),), executable_sha256=digest)
        with pytest.raises(ProfileError, match="has changed"):
            verify_command(vc)


# ── activation ────────────────────────────────────────────────────────────


class TestActivateProfile:
    def test_publishes_profile_and_creates_active_link(self, tmp_path: Path) -> None:
        profile = tmp_path / "toolchain.yaml"
        make_profile_yaml(profile)
        project = tmp_path / "project"
        pointer = activate_profile(project_root=project, profile_path=profile)

        assert "profile_sha256" in pointer
        assert "fingerprint_sha256" in pointer
        assert (project / "toolchain" / "active.link").is_file()

        link_data = json.loads((project / "toolchain" / "active.link").read_text(encoding="utf-8"))
        assert link_data["profile_sha256"] == pointer["profile_sha256"]

    def test_uses_unique_staging(self, tmp_path: Path) -> None:
        """No .staging_* directories should remain after activation."""
        profile = tmp_path / "toolchain.yaml"
        make_profile_yaml(profile)
        project = tmp_path / "project"
        activate_profile(project_root=project, profile_path=profile)

        staging_dirs = [d for d in (project / "toolchain").iterdir() if d.name.startswith(".staging_")]
        assert len(staging_dirs) == 0

    def test_content_addressed_skip_on_repeat(self, tmp_path: Path) -> None:
        """Re-activating with the same profile should not create a second hash dir."""
        profile = tmp_path / "toolchain.yaml"
        make_profile_yaml(profile)
        project = tmp_path / "project"
        activate_profile(project_root=project, profile_path=profile)
        activate_profile(project_root=project, profile_path=profile)

        hash_dirs = [d for d in (project / "toolchain").iterdir() if d.name.startswith(".staging_")]
        assert len(hash_dirs) == 0
        # Only active.link, maybe .tmp leftover (shouldn't exist)
        items = {d.name for d in (project / "toolchain").iterdir()}
        assert "active.link" in items

    def test_two_different_profiles_create_two_hash_dirs(self, tmp_path: Path) -> None:
        p1 = tmp_path / "p1.yaml"
        p2 = tmp_path / "p2.yaml"
        make_profile_yaml(p1)
        make_profile_yaml(p2, extra="extensions:\n  variant: two\n")
        project = tmp_path / "project"
        activate_profile(project_root=project, profile_path=p1)
        activate_profile(project_root=project, profile_path=p2)
        items = {
            directory.name
            for directory in (project / "toolchain").iterdir()
            if directory.name != "active.link" and not directory.name.startswith(".")
        }
        assert len(items) == 2


# ── active resolution & hash chain ────────────────────────────────────────


class TestResolveCapabilityActive:
    def test_resolves_compile_from_active_link(self, tmp_path: Path) -> None:
        profile = tmp_path / "toolchain.yaml"
        make_profile_yaml(profile)
        project = tmp_path / "project"
        activate_profile(project_root=project, profile_path=profile)

        resolved = resolve_capability(project_root=project, capability="compile")
        assert len(resolved) == 1
        assert resolved[0].argv[0] == str(Path(__file__).resolve())

    def test_detects_tampered_profile_json(self, tmp_path: Path) -> None:
        profile = tmp_path / "toolchain.yaml"
        make_profile_yaml(profile)
        project = tmp_path / "project"
        activate_profile(project_root=project, profile_path=profile)

        # Read the current pointer to find the hash dir
        link = json.loads((project / "toolchain" / "active.link").read_text(encoding="utf-8"))
        hash_dir = project / "toolchain" / link["profile_sha256"]
        # Tamper with profile.json
        (hash_dir / "profile.json").write_text('{"tampered": true}', encoding="utf-8")

        with pytest.raises(ProfileError, match="hash mismatch"):
            resolve_capability(project_root=project, capability="compile")

    def test_detects_tampered_fingerprint_json(self, tmp_path: Path) -> None:
        profile = tmp_path / "toolchain.yaml"
        make_profile_yaml(profile)
        project = tmp_path / "project"
        activate_profile(project_root=project, profile_path=profile)

        link = json.loads((project / "toolchain" / "active.link").read_text(encoding="utf-8"))
        hash_dir = project / "toolchain" / link["profile_sha256"]
        (hash_dir / "fingerprint.json").write_text('{"tampered": true}', encoding="utf-8")

        with pytest.raises(ProfileError, match="fingerprint.json hash mismatch"):
            resolve_capability(project_root=project, capability="compile")

    def test_detects_tampered_active_link(self, tmp_path: Path) -> None:
        profile = tmp_path / "toolchain.yaml"
        make_profile_yaml(profile)
        project = tmp_path / "project"
        activate_profile(project_root=project, profile_path=profile)

        # Corrupt active.link
        (project / "toolchain" / "active.link").write_text(
            '{"profile_sha256":"0000000000000000000000000000000000000000000000000000000000000000",'
            '"fingerprint_sha256":"1111111111111111111111111111111111111111111111111111111111"}',
            encoding="utf-8",
        )

        with pytest.raises(ProfileError, match="invalid fingerprint_sha256"):
            resolve_capability(project_root=project, capability="compile")

    def test_detects_missing_active_link(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        with pytest.raises(ProfileError, match="no toolchain activation"):
            resolve_capability(project_root=project, capability="compile")

    def test_detects_fingerprint_wrong_profile_reference(self, tmp_path: Path) -> None:
        """Fingerprint's profile_sha256 must match the stored profile."""
        profile = tmp_path / "toolchain.yaml"
        make_profile_yaml(profile)
        project = tmp_path / "project"
        activate_profile(project_root=project, profile_path=profile)

        link = json.loads((project / "toolchain" / "active.link").read_text(encoding="utf-8"))
        hash_dir = project / "toolchain" / link["profile_sha256"]
        # Read fingerprint, tamper the cross-reference, fix the hash to pass first check
        fp = json.loads((hash_dir / "fingerprint.json").read_text(encoding="utf-8"))
        fp["profile_sha256"] = "f" * 64
        (hash_dir / "fingerprint.json").write_text(json.dumps(fp, sort_keys=True) + "\n", encoding="utf-8")
        # Also need to update active.link fingerprint_sha256 since the content changed
        new_fp_hash = hashlib.sha256((hash_dir / "fingerprint.json").read_bytes()).hexdigest()
        link["fingerprint_sha256"] = new_fp_hash
        (project / "toolchain" / "active.link").write_text(json.dumps(link, sort_keys=True) + "\n", encoding="utf-8")

        with pytest.raises(ProfileError, match="references wrong profile"):
            resolve_capability(project_root=project, capability="compile")


# ── transient resolution ──────────────────────────────────────────────────


class TestResolveCapabilityTransient:
    def test_resolves_compile_without_active_link(self, tmp_path: Path) -> None:
        profile = tmp_path / "toolchain.yaml"
        make_profile_yaml(profile)
        project = tmp_path / "project"
        # No activate — test directly passes profile_path
        resolved = resolve_capability(project_root=project, capability="compile", profile_path=profile)
        assert len(resolved) == 1
        assert resolved[0].argv[0] == str(Path(__file__).resolve())

    def test_resolves_link_with_required_only(self, tmp_path: Path) -> None:
        """Transient resolution with linker-capable profile should not fail on compiler if only linker is needed."""
        profile = tmp_path / "toolchain.yaml"
        make_profile_with_linker_yaml(profile)
        project = tmp_path / "project"
        resolved = resolve_capability(project_root=project, capability="link", profile_path=profile)
        assert len(resolved) == 1
        assert "-o" in resolved[0].argv

    def test_leaves_no_temp_files(self, tmp_path: Path) -> None:
        profile = tmp_path / "toolchain.yaml"
        make_profile_yaml(profile)
        project = tmp_path / "project"

        resolve_capability(project_root=project, capability="compile", profile_path=profile)

        temp_yamls = list(project.rglob(".profile.yaml"))
        assert len(temp_yamls) == 0

    def test_unknown_capability(self, tmp_path: Path) -> None:
        profile = tmp_path / "toolchain.yaml"
        make_profile_yaml(profile)
        project = tmp_path / "project"

        with pytest.raises(ProfileError, match="unknown capability"):
            resolve_capability(project_root=project, capability="fly_to_moon", profile_path=profile)

    def test_missing_command_on_required(self, tmp_path: Path) -> None:
        """A profile with only a compiler should fail if we request link."""
        profile = tmp_path / "toolchain.yaml"
        make_profile_yaml(profile)
        project = tmp_path / "project"

        with pytest.raises(ProfileError, match="toolchain capability unavailable"):
            resolve_capability(project_root=project, capability="link", profile_path=profile)

    def test_missing_binary_detected(self, tmp_path: Path) -> None:
        p = tmp_path / "toolchain.yaml"
        p.write_text(
            "backend: offline-export\ntarget: arbitrary\ncompiler:\n  command: [/nonexistent/gcc]\n  flags: [-c]\n",
            encoding="utf-8",
        )
        project = tmp_path / "project"
        with pytest.raises(ProfileError, match="unavailable"):
            resolve_capability(project_root=project, capability="compile", profile_path=p)


# ── idempotent activation + resolve ───────────────────────────────────────


class TestActivateThenResolve:
    """End-to-end: activate, resolve, tamper, verify failure, re-activate, resolve."""

    def test_rollback_reattivation(self, tmp_path: Path) -> None:
        profile = tmp_path / "toolchain.yaml"
        make_profile_yaml(profile)
        project = tmp_path / "project"

        pointer1 = activate_profile(project_root=project, profile_path=profile)

        # Tamper with the hash dir
        profile_hash = pointer1["profile_sha256"]
        assert isinstance(profile_hash, str)
        hash_dir = project / "toolchain" / profile_hash
        (hash_dir / "profile.json").write_text('{"tampered": true}', encoding="utf-8")

        # Resolve should fail
        with pytest.raises(ProfileError, match="hash mismatch"):
            resolve_capability(project_root=project, capability="compile")

        # A content-addressed directory is immutable: reactivation must not
        # overwrite tampered evidence or silently bless it.
        with pytest.raises(ProfileError, match="tampered"):
            activate_profile(project_root=project, profile_path=profile)

    def test_rollback_different_profile(self, tmp_path: Path) -> None:
        """Activate, tamper, activate a different profile, activate original — works."""
        p1 = tmp_path / "p1.yaml"
        p2 = tmp_path / "p2.yaml"
        make_profile_yaml(p1)
        make_profile_yaml(p2, extra="extensions:\n  v: 2\n")
        project = tmp_path / "project"

        activate_profile(project_root=project, profile_path=p1)
        # Switch to p2
        activate_profile(project_root=project, profile_path=p2)
        # Switch back to p1
        activate_profile(project_root=project, profile_path=p1)

        resolved = resolve_capability(project_root=project, capability="compile")
        assert resolved[0].argv[0] == str(Path(__file__).resolve())
