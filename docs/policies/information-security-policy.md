# Information Security Policy

**Version 1.0 - Effective 2026-05-18**
**Owner:** Security contact, {{COMPANY_NAME}}

## 1. About this document

This Information Security Policy sets out {{COMPANY_NAME}}'s approach to protecting the confidentiality, integrity, and availability of information assets, including customer data processed through the CS Agent product.

It applies to all personnel (currently the founder; expanded to employees and contractors as the company grows), all systems operated by {{COMPANY_NAME}}, and all data processed in the course of business.

## 2. Roles and responsibilities

| Role | Holder | Responsibilities |
| :---- | :---- | :---- |
| Security Officer | Security contact | Overall accountability for information security, incident response, vendor risk, policy maintenance |
| Incident Response Lead | Security contact | Coordinates response to security incidents per the Incident Response Policy |
| Data Protection Contact | Security contact | GDPR Article 27 / privacy inquiries |

As {{COMPANY_NAME}} grows, these roles will be reassigned and additional roles (legal, communications, dedicated security personnel) will be added.

## 3. Data classification

All data processed by {{COMPANY_NAME}} is classified into one of the following levels:

**Restricted** - OAuth refresh tokens, OAuth access tokens, API keys, encryption keys, customer authentication credentials, payment card data (handled exclusively by Stripe), Personally Identifiable Information (PII) of customers' contacts retrieved from integrated services (Gmail, Calendar, Drive, HubSpot, Gainsight).

**Confidential** - Customer business data including account names, deal information, internal notes, generated AI summaries, edit history, customer billing data, employee/contractor records.

**Internal** - Operational logs, error tracking data, aggregated usage metrics, internal documentation, business records.

**Public** - Marketing content, public documentation, blog posts, this policy.

## 4. Protection requirements by classification

### 4.1 Restricted data

- **Encryption at rest:** All OAuth tokens are encrypted at the application layer using Fernet (AES-128-CBC with HMAC-SHA256) before storage in PostgreSQL. The encryption key is stored as an environment secret managed via Render's Secret Files, with a paper-printed backup held offline.
- **Encryption in transit:** All data is transmitted over TLS 1.2 or higher. HTTP connections to cs-agent.app are redirected to HTTPS at the edge.
- **Access:** Restricted data is not displayed in application logs, error tracking systems, or user-facing interfaces. Database access is limited to the founder and the running application.
- **Retention:** OAuth tokens are revoked when a user disconnects an integration or closes their account. Token records are removed from the database within 30 days of revocation.
- **No raw email storage:** CS Agent never persists raw email content. Only derived relationship intelligence is retained.

### 4.2 Confidential data

- **Encryption at rest:** All customer data is stored in PostgreSQL on Render. PostgreSQL volume encryption is provided by the hosting provider.
- **Encryption in transit:** TLS 1.2 or higher.
- **Access:** Customer data is scoped per user via authenticated sessions. No cross-customer access is permitted at the application layer. Database queries enforce user isolation.
- **Retention:** Customer data is retained for the duration of the active subscription plus a wind-down period as described in the Privacy Policy. Data is deleted on customer request, subject to legal retention obligations.

### 4.3 Internal and Public data

Standard hygiene applies. No special handling required beyond preventing accidental disclosure of internal operational data.

## 5. Access control

- **Authentication:** All user access to CS Agent is authenticated through Clerk, which enforces secure password handling, optional multi-factor authentication, and session management.
- **Authorization:** Access controls are enforced at the trusted server layer (FastAPI routes), not on the client. Authorization decisions are made at both the route level and the resource level.
- **Principle of least privilege:** Users access only their own data. Administrative access to production systems is limited to the founder.
- **Multi-factor authentication:** Required for administrative access to {{COMPANY_NAME}}'s cloud accounts (Render, GitHub, Clerk, Stripe, Anthropic, Google Workspace, registrar).
- **Session management:** Sessions are managed by Clerk with appropriate timeout and invalidation. Logout invalidates the session token.

## 6. Password and credential management

User-facing authentication is delegated to Clerk, which enforces:

- Password complexity and length requirements (12+ characters recommended).
- Secure password storage using one-way hashing with salting.
- Account lockout and rate limiting on failed authentication attempts.
- Optional multi-factor authentication.

For internal credentials used by {{COMPANY_NAME}} personnel:

- All accounts holding production access (Render, GitHub, Clerk, Stripe, Anthropic, Google Workspace, registrar) must use strong unique passwords managed in a password manager.
- Multi-factor authentication is required where supported.
- Credentials are not stored in plaintext, in code, in commit history, or in shared documents.
- Shared accounts are not used. Each person with access has their own account.

## 7. Application development security

{{COMPANY_NAME}}'s product development follows these security practices:

- **No untrusted code execution:** The application does not use `eval()` or equivalent dynamic code execution on untrusted input.
- **Input validation:** All user input is validated and sanitized. Output is encoded appropriate to the rendering context.
- **SQL injection prevention:** All database queries use parameterized statements via the ORM. No string-concatenated SQL.
- **SSRF protection:** External requests from the application are limited to known integrated services. URL inputs from users are validated against allowed protocols and domains.
- **Dependency management:** Third-party dependencies are pinned to specific versions. Security advisories are monitored via GitHub Dependabot.
- **Build and deployment:** Production deployments occur via GitHub Actions CI after tests pass. Deployments are automated and repeatable. Manual production changes are documented.
- **Debug modes disabled in production:** FastAPI runs without debug mode in production. Detailed error pages and developer consoles are not exposed.
- **Secrets management:** Secrets are stored in Render's environment configuration. No secrets in source code, in commit history, or in client-side code.

## 8. Logging and monitoring

- **Application logs** are aggregated by Render and retained per the hosting provider's log policy. Logs are reviewed when alerts trigger or when customer reports indicate issues.
- **Customer-impact alerting** is implemented at the application layer: when a customer's daily digest generation fails, an alert is sent to the founder via email. This ensures customer-affecting failures are surfaced even when application logs are not actively reviewed.
- **Uptime monitoring** is provided by UptimeRobot, with monitors on the landing page, login flow, and backend health endpoint. Alerts are sent on downtime.
- **Audit events** for sensitive operations (token revocation, account deletion, billing events) are recorded in the application database.
- **Credentials are never logged.** Authentication tokens, OAuth tokens, and passwords are excluded from all log streams at the application layer.

As the customer base grows, observability tooling will be expanded to include error aggregation, novel-exception alerting, and richer context capture. Defined triggers for adding these capabilities include sustained incident-triage difficulty, customer count crossing a defined threshold, and preparation for open self-service signup.

## 9. Vendor and third-party management

{{COMPANY_NAME}} uses third-party services to deliver CS Agent. Each is selected based on its security posture and contractually committed to appropriate data handling:

| Vendor | Purpose | Data shared |
| :---- | :---- | :---- |
| Render | Hosting (web, worker, database) | All application data |
| Clerk | Authentication | User identity, email |
| Stripe | Billing | Customer billing data ({{COMPANY_NAME}} does not store card numbers) |
| Anthropic | AI inference | Prompts and customer-context data sent for model completion |
| Google | Customer OAuth (Gmail, Calendar, Drive) | OAuth tokens issued by customer |
| HubSpot | Customer OAuth (CRM via MCP) | OAuth tokens issued by customer |
| Gainsight | Customer OAuth (CRM via MCP) | OAuth tokens issued by customer |
| UptimeRobot | Uptime monitoring | None (external probes only) |

Vendor security posture is reviewed at onboarding. Vendors handling customer Personal Data are bound by data processing agreements where required by GDPR or other applicable law.

## 10. Business continuity and disaster recovery

{{COMPANY_NAME}} maintains the following continuity arrangements:

- **Data backups:** PostgreSQL on Render is backed up daily by the hosting provider. Backups are retained for 7 days.
- **Recovery time objective:** Production restoration within 24 hours of a major incident.
- **Recovery point objective:** Data loss limited to no more than 24 hours.
- **Infrastructure as code:** Application infrastructure can be redeployed from documented configuration. The application repository contains all code and configuration required to rebuild the service.
- **Key recovery:** The Fernet encryption key for OAuth tokens has a paper-printed offline backup. Loss of the key would render encrypted tokens unrecoverable, but the application can recover by requiring customers to reconnect their integrations.
- **Plan review:** Continuity arrangements are reviewed annually. As {{COMPANY_NAME}} adds personnel, succession arrangements will be documented.

## 11. Data breach response

Data breaches are handled per the Incident Response Policy, which is incorporated by reference. Key points:

- Breaches are reported to {{SUPPORT_EMAIL}} or directly to the Security contact.
- The Founder serves as Incident Response Lead.
- GDPR Article 33 supervisory authority notification within 72 hours where applicable.
- Affected customer notification per the Incident Response Policy and applicable law.

## 12. Acceptable use

All {{COMPANY_NAME}} personnel and contractors are expected to:

- Use {{COMPANY_NAME}} systems and customer data only for legitimate business purposes.
- Not share access credentials.
- Report suspected security issues promptly.
- Not retain customer data on personal devices except as required for active work, and to delete such data when work is complete.

## 13. Policy compliance and review

- This policy is reviewed annually and updated as the company, product, and regulatory environment evolve.
- Material changes are version-controlled, with the effective date updated.
- Exceptions to this policy require written approval from the Security Officer.

## 14. Related policies

- Incident Response Policy
- Vulnerability Disclosure Policy
- Privacy Policy (published at cs-agent.app)
