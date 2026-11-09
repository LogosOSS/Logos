# Security Policy

## Supported Versions

Only the latest release and the current `main` branch receive security fixes.
Older versions do not receive security patches.

| Version        | Supported |
|----------------|-----------|
| latest release | ✅        |
| `main`         | ✅        |
| older          | ❌        |

## Reporting a Vulnerability

Please use [Github's security advisory feature](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) to report any security vulnerabilities.

**Please do not report vulnerabilities through public issues, discussions or pull requests.**

Report privately through one of these channels:

1. **GitHub Private Vulnerability Reporting** (preferred):
   <https://github.com/LogosOSS/Logos/security/advisories/new>
2. **E-mail**: <TODO: security team alias>

Please include:

- the affected component and version or commit,
- a description of the issue and its impact,
- steps to reproduce or a proof of concept,
- a suggested fix, if you have one.

## What to Expect

| Step                        | Target                                                          |
|-----------------------------|-----------------------------------------------------------------|
| Acknowledgement             | within 5 business days                                          |
| Initial assessment (triage) | within 10 business days                                         |
| Fix or mitigation           | critical/high: within 30 days; medium/low: within 90 days       |
| Public disclosure           | after a fix is released, at the latest 90 days after the report |

Reports are reviewed, prioritized by severity and impact, and tracked until resolution.
Confirmed vulnerabilities are fixed through security updates and published as GitHub Security
Advisories (with a CVE where applicable). We credit reporters unless they prefer to stay
anonymous. We do not run a bug bounty.

## Scope

In scope: code in this repository (orchestrator, webservice, web application, worker node,
agent, node manager, gateways) and the images published to `ghcr.io/logososs`.

Out of scope:

- vulnerabilities in third-party dependencies without a Logos-specific impact (report them upstream),
- the development defaults in `docker-compose.dev.yaml` and `keycloak/tum-realm.json`,
- denial of service by traffic volume and social engineering,
- findings on deployments we do not operate.

## Safe Harbor

We will not pursue legal action against good-faith research that follows this policy: no access
to or modification of data that is not your own, no service degradation, and no public
disclosure before coordinated release.

## Security Scanning

We aim to identify security vulnerabilities as early as possible through automated security checks.

The security process includes:

- **Dependabot**: detection of known vulnerabilities in project dependencies.
- **SonarQube Cloud**: static analysis with a quality gate on pull requests.
<!-- TODO: CodeQL and secret scanning are not enabled yet (Settings > Advanced Security). Enable both before publishing this file. -->
- **CodeQL**: static analysis to identify potential security vulnerabilities in source code.
- **Secret Scanning**: detection of accidentally exposed credentials, where enabled.

## Weekly Security Audits

Security findings are reviewed on a weekly basis.

The review includes:

- newly identified vulnerabilities,
- unresolved security alerts,
- severity and potential impact,
- status of previously identified issues,
- recommended remediation actions.

## Security Reporting

A weekly security report summarizes the results of the automated security checks.

Reports are accessible to the responsible maintainers and must not publicly expose sensitive
vulnerability details.

## Vulnerability Management

Reported security vulnerabilities are reviewed, prioritized based on their severity and impact,
and tracked until resolution.

Confirmed vulnerabilities are addressed through appropriate fixes and security updates.