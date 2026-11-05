# Incident Response Policy

**Version 1.0 - Effective 2026-05-18**
**Owner:** Security contact, {{COMPANY_NAME}}

## 1. Purpose

This policy defines roles, responsibilities, and procedures for identifying, investigating, and responding to security incidents and data breaches affecting {{COMPANY_NAME}} systems, customer data, or company information assets.

## 2. Scope

This policy applies to:

- All {{COMPANY_NAME}} personnel, including the founder, employees, contractors, and any third parties authorized to access {{COMPANY_NAME}} information resources.
- All systems used to store, process, transmit, or access customer data, regardless of location or ownership.
- All categories of incident: confirmed security breaches, suspected breaches, vulnerability disclosures, and operational events with security implications.

## 3. Definitions

**Security incident:** Any event that compromises or may compromise the confidentiality, integrity, or availability of {{COMPANY_NAME}} systems or data, including unauthorized access, data exposure, malware infection, denial of service, or misuse of credentials.

**Data breach:** A security incident in which customer personal data, authentication credentials, or other sensitive data is confirmed to have been accessed, exfiltrated, modified, or destroyed by an unauthorized party.

**Sensitive data:** OAuth tokens, customer email addresses, customer organization data, billing information, and any data identified as Personal Data under GDPR or Personal Information under CCPA.

## 4. Roles and responsibilities

**The Security contact** is the primary responsible party for:

- Receiving and triaging incident reports.
- Coordinating investigation and resolution.
- All internal and external communication on security incidents.
- Notifying affected customers and authorities as required by applicable law.
- Post-incident review and policy updates.

**All {{COMPANY_NAME}} personnel and contractors** are responsible for:

- Promptly reporting any suspected or confirmed security incident, even if they may have contributed to the event.
- Cooperating with investigation activities.
- Preserving evidence and not taking independent remediation action that could destroy investigative artifacts.

## 5. Reporting an incident

Reports of suspected or confirmed security incidents should be sent to:

- **Email:** {{SUPPORT_EMAIL}}
- **Direct:** to the Security contact via any available channel (phone, message, chat)

Reports should include, where known: what happened, when it was discovered, which systems or data are affected, and what evidence is available.

## 6. Response procedure

When a security incident is reported, the following steps are taken:

### 6.1 Triage (within one business day of report acknowledged; within two business days triage decision)

- Acknowledge receipt of the report within one business day.
- Determine whether the report describes a credible incident within two business days of acknowledgment.
- If credible, classify the severity: low (no customer data impact), medium (limited internal impact), high (potential customer data exposure), critical (confirmed customer data exposure or active exploitation).

### 6.2 Containment (within 24 hours of triage decision for high or critical severity incidents)

- Identify the scope of affected systems and accounts.
- Rotate or revoke any potentially compromised credentials, OAuth tokens, or API keys.
- Block any identified attacker access vectors.
- Preserve logs and forensic evidence before remediation begins.

### 6.3 Investigation

- Determine the root cause, attack vector, and full scope of impact.
- Identify the categories of data affected and the customers impacted.
- Document the timeline and indicators of compromise.

### 6.4 Communication plan

For incidents involving customer data, develop a communication plan covering:

- Notification to affected customers, including what data was affected, when, and what they should do.
- Notification to regulatory authorities within the timelines specified in Section 7 (GDPR Article 33 supervisory authority within 72 hours of confirmation; data subject and US state law notifications per applicable jurisdiction).
- Public disclosure timing and content.
- Internal stakeholder communication.

### 6.5 Resolution

- Deploy the fix to all affected environments.
- Verify the vulnerability is closed and the attack vector is no longer exploitable.
- Restore any data or services degraded by the incident.

### 6.6 Post-incident review

Within four weeks of resolution:

- Document the incident timeline, root cause, and response actions.
- Identify ways to detect similar incidents earlier (logging, alerting, automated checks).
- Update relevant policies, runbooks, and architectural decisions.
- Communicate lessons learned internally.

## 7. Notification thresholds

### Investigation trigger

Investigation is initiated when {{COMPANY_NAME}} becomes aware of facts that, if confirmed, would constitute unauthorized access to customer Personal Data, unauthorized access to authentication credentials, or a material service degradation affecting customer access to their data.

### Customer notification trigger

Customer notification is initiated when:

- Investigation has confirmed unauthorized access to customer Personal Data or authentication credentials, or
- Investigation has determined unauthorized access is more likely than not, or
- A service degradation has affected customer access to their data for more than four hours, or
- Applicable law requires notification.

Notification on suspicion alone (without investigation supporting "more likely than not") is not required and is generally not advisable, as it can cause unnecessary alarm and undermine the credibility of future notifications.

### Regulatory notification under GDPR

- **Article 33 (supervisory authority):** Notify the relevant supervisory authority within 72 hours of becoming aware of a personal data breach, unless the breach is unlikely to result in risk to data subjects.
- **Article 34 (data subjects):** Notify affected individuals without undue delay when a breach is likely to result in high risk to their rights and freedoms. Notification to data subjects may be omitted if {{COMPANY_NAME}} has applied protections (such as encryption) that render the data unintelligible to any unauthorized party, or if subsequent measures ensure the high risk is no longer likely to materialize.

### US state law

Comply with the breach notification timeline and content requirements of each affected user's jurisdiction.

## 8. Documentation and records

For each incident classified as medium severity or higher, the following records are maintained:

- Initial report and triage decision.
- Timeline of investigation and containment actions.
- Scope of impact, including affected customers and data categories.
- Communications sent (internal, customer, regulatory).
- Root cause analysis and remediation actions.
- Post-incident review findings.

Records are retained for a minimum of two years, with the following exceptions extending the retention period: an active or anticipated litigation hold, an ongoing regulatory investigation, or a related incident that requires the historical record for context.

## 9. Policy review

This policy is reviewed annually and updated as {{COMPANY_NAME}}'s systems, team, and threat environment evolve.
