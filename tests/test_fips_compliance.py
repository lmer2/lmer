"""Test FIPS compliance in container environment."""
import os
import subprocess
import hashlib
import ssl
import pytest


class TestFIPSCompliance:
    """Test that the container environment is FIPS-compliant."""

    def test_fips_kernel_enabled(self):
        """Verify FIPS is enabled at kernel level."""
        # Skip if not in container
        if not os.path.exists('/home/developer/.claude/container_env'):
            pytest.skip("Not in container environment")

        fips_file = '/proc/sys/crypto/fips_enabled'
        if os.path.exists(fips_file):
            with open(fips_file, 'r') as f:
                fips_status = f.read().strip()
            assert fips_status == '1', f"FIPS mode not enabled (status: {fips_status})"
        else:
            pytest.skip("FIPS status file not found (not on FIPS-capable system)")

    def test_openssl_fips_mode(self):
        """Verify OpenSSL is in FIPS mode."""
        if not os.path.exists('/home/developer/.claude/container_env'):
            pytest.skip("Not in container environment")

        result = subprocess.run(
            ['openssl', 'version', '-a'],
            capture_output=True,
            text=True
        )

        # Oracle Linux 9 should have FIPS-capable OpenSSL
        assert 'FIPS' in result.stdout or 'fips' in result.stdout.lower(), \
            "OpenSSL not showing FIPS capability"

    def test_approved_hash_algorithms(self):
        """Test FIPS-approved hash algorithms work."""
        # These should always work
        approved_algos = ['sha256', 'sha384', 'sha512', 'sha3_256', 'sha3_512']

        for algo in approved_algos:
            try:
                h = hashlib.new(algo)
                h.update(b'test data')
                h.hexdigest()
            except Exception as e:
                pytest.fail(f"FIPS-approved algorithm {algo} failed: {e}")

    def test_non_approved_algorithms(self):
        """Test non-FIPS algorithms behavior."""
        # In strict FIPS mode, these should fail
        non_approved = ['md5', 'sha1']  # MD5 and SHA1 are not FIPS-approved for signatures

        if not os.getenv('OPENSSL_FIPS'):
            pytest.skip("OPENSSL_FIPS not set, skipping strict mode test")

        # Note: Behavior depends on FIPS enforcement level
        # Some systems allow MD5/SHA1 for non-crypto purposes
        for algo in non_approved:
            try:
                h = hashlib.new(algo)
                h.update(b'test')
                result = h.hexdigest()
                # If it works, issue warning (some FIPS modes allow legacy algorithms)
                print(f"Warning: {algo} is available (may be allowed for non-crypto use)")
            except Exception:
                # This is expected in strict FIPS mode
                print(f"✓ {algo} correctly blocked in FIPS mode")

    def test_ssl_fips_ciphers(self):
        """Verify only FIPS-approved ciphers are available."""
        context = ssl.create_default_context()
        ciphers = context.get_ciphers()

        # Check that we have ciphers
        assert len(ciphers) > 0, "No SSL ciphers available"

        # In FIPS mode, weak ciphers should be excluded
        weak_ciphers = ['RC4', 'DES', 'MD5']
        cipher_names = [c['name'] for c in ciphers]

        for weak in weak_ciphers:
            for cipher in cipher_names:
                assert weak not in cipher, \
                    f"Weak cipher containing {weak} found: {cipher}"

    def test_python_random_fips(self):
        """Test Python's random number generation in FIPS mode."""
        import random
        import secrets

        # These should work in FIPS mode
        random.randint(1, 100)
        secrets.token_bytes(32)
        secrets.token_hex(16)

        # Verify we can generate cryptographically strong random numbers
        token1 = secrets.token_bytes(32)
        token2 = secrets.token_bytes(32)
        assert token1 != token2, "Random number generation not working properly"

    def test_container_security_settings(self):
        """Verify container security hardening."""
        if not os.path.exists('/home/developer/.claude/container_env'):
            pytest.skip("Not in container environment")

        # Check we're running as non-root
        import pwd
        user = pwd.getpwuid(os.getuid()).pw_name
        assert user == 'developer', f"Not running as developer user (found: {user})"

        # Check home directory ownership
        home_stat = os.stat('/home/developer')
        assert home_stat.st_uid == os.getuid(), "Home directory not owned by current user"

    def test_fips_requirements_installable(self):
        """Verify FIPS-compliant requirements can be installed."""
        if os.path.exists('fips-requirements.txt'):
            with open('fips-requirements.txt', 'r') as f:
                lines = f.readlines()

            # Check for known FIPS-incompatible packages
            incompatible = ['pycrypto', 'cryptography<3.0']  # Old crypto libraries

            for line in lines:
                line = line.strip()
                if line and not line.startswith('#'):
                    for bad_pkg in incompatible:
                        assert bad_pkg not in line.lower(), \
                            f"Non-FIPS compatible package found: {line}"
