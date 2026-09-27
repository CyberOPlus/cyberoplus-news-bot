# Security Policy

Cybero Plus treats API credentials and publishing tokens as secrets.

## Secrets

Do not commit API keys, access tokens, passwords, private keys, service-account files, or `.env` files to this repository.

Production credentials are expected to live only in GitHub Actions Secrets. Workflow files may reference secret **names**, but secret values must never be stored in code, data files, commit messages, issues, pull requests, Actions artifacts, or logs.

If a credential is ever exposed, revoke/rotate it at the provider immediately. Removing it from the latest commit is not sufficient because Git history may still contain the old value.

## Reporting

Do not publish a suspected credential or sensitive exploit detail in a public issue. Contact the repository owner privately and include only the minimum information needed to reproduce the problem.
