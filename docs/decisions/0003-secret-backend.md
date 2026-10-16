# ADR-0003: Secret references with a replaceable backend; Azure Key Vault deferred

Date: 2026-10-16. Status: accepted (Key Vault implementation pending).

Credentials are stored as AES-256-GCM ciphertext in `credential_ref` with `tenant|connection` as associated data,
behind a `SecretBackend` protocol. `LocalSecretBackend` (key from `WAYAM_LOCAL_SECRET_KEY`) is for development and
tests; production refuses to start with it (`get_settings`). `AzureKeyVaultBackend` raises `NotConfiguredError`
rather than pretending: wrapping the data key with a Key Vault key and managed identity needs the
`azure-keyvault-keys`/`azure-identity` dependencies and an environment to test against, neither of which is
available in this session.
