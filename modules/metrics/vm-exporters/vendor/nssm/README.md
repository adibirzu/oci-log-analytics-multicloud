# NSSM (the Non-Sucking Service Manager)

The installer downloads NSSM from public release sources at runtime instead of
committing `nssm.exe` to the repository. It tries a public GitHub release first,
then falls back to https://nssm.cc/ and an Internet Archive mirror because
`nssm.cc` has repeatedly returned HTTP 503. NSSM is public domain.
